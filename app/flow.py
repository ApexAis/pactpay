"""PactPay's milestone lifecycle: every state change is checked here, recorded in the hash-chained event log, and made
at most once.

    DRAFT --both parties approve one terms hash--> TERMS_APPROVED --client--> AWAITING_BUYER_APPROVAL
    --buyer approves in PayPal, client authorizes, PayPal's record checked--> FUNDS_HELD --freelancer--> SUBMITTED
    --evaluation--> AUDITED --client releases the full amount--> CAPTURING
                            --client proposes less--> RELEASE_PROPOSED --freelancer accepts--> CAPTURING
                            --either disputes--> DISPUTED --arbiter decides--> CAPTURING | VOIDED
    CAPTURING --PayPal COMPLETED--> CAPTURED --> PAYOUT_PENDING --> PAID_OUT | PAYOUT_UNCLAIMED | PAYOUT_ATTENTION
              --PayPal PENDING--> CAPTURE_PENDING ;  --PayPal declined--> CAPTURE_FAILED

Rules that hold whatever the caller sends:
  * only a party to the pact sees or changes it, and only in its role (client, freelancer, arbiter);
  * the terms change only in DRAFT; approval names the exact hash; after both approve, nothing changes them;
  * the PayPal order is built from the locked terms, and an authorization counts only once PayPal's own record of
    the order shows it for the locked amount;
  * money moves only after an evaluation of the latest submission against the locked terms, and only by a human
    decision; the AI recommendation is evidence, never the trigger;
  * a release happens once: the pact's status, the release sequence and deterministic PayPal-Request-Ids /
    sender_batch_ids make a repeat a no-op at PactPay and at PayPal;
  * payout items are as PayPal reports them (PENDING, SUCCESS, UNCLAIMED, FAILED, ...), never assumed;
  * a crash or a lost answer is recovered by reading PayPal's record (reconcile), not by guessing.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from typing import Any

from app import terms as T
from app.config import Settings
from app.paypal_client import PayPalClient, PayPalError, cents_of, money
from app.store import Store, canonical, new_id, sha256_text, utc_now

ROLES = ("client", "freelancer", "arbiter")
DRAFT, TERMS_APPROVED, AWAITING, HELD = "DRAFT", "TERMS_APPROVED", "AWAITING_BUYER_APPROVAL", "FUNDS_HELD"
SUBMITTED, AUDITED, PROPOSED, DISPUTED = "SUBMITTED", "AUDITED", "RELEASE_PROPOSED", "DISPUTED"
CAPTURING, CAPTURE_PENDING, CAPTURE_FAILED, CAPTURED = "CAPTURING", "CAPTURE_PENDING", "CAPTURE_FAILED", "CAPTURED"
PAYOUT_PENDING, PAID_OUT, UNCLAIMED, ATTENTION = "PAYOUT_PENDING", "PAID_OUT", "PAYOUT_UNCLAIMED", "PAYOUT_ATTENTION"
VOIDED, CANCELLED = "VOIDED", "CANCELLED"
PAYOUT_STATES = (PAYOUT_PENDING, PAID_OUT, UNCLAIMED, ATTENTION)
BAD_ITEM = {"FAILED", "RETURNED", "BLOCKED", "REFUNDED", "REVERSED", "DENIED", "CANCELED"}
MAX_CHAT = 20_000
MAX_SUBMISSION = 200_000
MIN_REASON = 10


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        self.status, self.code, self.message, self.extra = status, code, message, extra
        super().__init__(message)

    def body(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, **self.extra}


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def usd(cents: int | None) -> str:
    return "—" if cents is None else f"${cents // 100:,}.{cents % 100:02d}"


class Service:
    def __init__(self, store: Store, paypal: PayPalClient, settings: Settings, *, evaluate: Any = None,
                 reviewer: Any = None, drafter: tuple[Any, str | None] = (None, None)) -> None:
        self.store, self.paypal, self.settings = store, paypal, settings
        self._evaluate = evaluate
        self.reviewer = reviewer
        self.drafter, self.drafter_label = drafter

    # --- identities ----------------------------------------------------------------------------------------------
    def create_user(self, name: str) -> dict[str, Any]:
        name = (name or "").strip()[:60]
        if not name:
            raise ApiError(422, "invalid_name", "A display name is required.")
        token = f"pp_{secrets.token_urlsafe(24)}"
        uid = new_id("USR")
        with self.store.tx() as db:
            db.execute("INSERT INTO users (id, name, token_sha256, created_at) VALUES (?, ?, ?, ?)",
                       (uid, name, _token_hash(token), utc_now()))
        return {"user_id": uid, "name": name, "token": token}

    def user_for_token(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        with self.store.read() as db:
            row = db.execute("SELECT id, name FROM users WHERE token_sha256 = ?", (_token_hash(token),)).fetchone()
        return {"id": row["id"], "name": row["name"]} if row else None

    # --- loading and guarding ------------------------------------------------------------------------------------
    @staticmethod
    def _pact(db: sqlite3.Connection, pact_id: str) -> dict[str, Any]:
        row = db.execute("SELECT * FROM pacts WHERE id = ?", (pact_id,)).fetchone()
        if not row:
            raise ApiError(404, "not_found", "No such pact.")
        return dict(row)

    @staticmethod
    def _roles(db: sqlite3.Connection, pact_id: str) -> dict[str, str]:
        return {r["role"]: r["user_id"] for r in db.execute("SELECT role, user_id FROM members WHERE pact_id = ?",
                                                            (pact_id,))}

    def _guard(self, db: sqlite3.Connection, user: dict[str, Any] | None, pact_id: str,
               roles: tuple[str, ...] | None = None) -> tuple[dict[str, Any], str]:
        """The pact and the caller's role in it; 401 without a user, 403 when not a party (or not in `roles`)."""
        if user is None:
            raise ApiError(401, "unauthenticated", "Sign in: send 'Authorization: Bearer <token>'.")
        pact = self._pact(db, pact_id)
        mine = [r for r, uid in self._roles(db, pact_id).items() if uid == user["id"]]
        if not mine:
            raise ApiError(403, "forbidden", "You are not a party to this pact.")
        if roles is not None:
            allowed = [r for r in mine if r in roles]
            if not allowed:
                raise ApiError(403, "forbidden", f"Only the {' or '.join(roles)} can do this.", your_roles=mine)
            return pact, allowed[0]
        return pact, mine[0]

    @staticmethod
    def _expect(pact: dict[str, Any], *states: str) -> None:
        if pact["status"] not in states:
            raise ApiError(409, "invalid_state", f"Not possible while the pact is {pact['status']}.",
                           pact_status=pact["status"], allowed_in=list(states))

    def _move(self, db: sqlite3.Connection, pact: dict[str, Any], new: str, actor: str, kind: str,
              data: dict[str, Any], **fields: Any) -> None:
        """Compare-and-set the pact's status (and fields) on its version, then append the event, in one transaction."""
        sets = ", ".join(["status = ?", "version = version + 1", "updated_at = ?", *[f"{k} = ?" for k in fields]])
        cur = db.execute(f"UPDATE pacts SET {sets} WHERE id = ? AND version = ? AND status = ?",
                         (new, utc_now(), *fields.values(), pact["id"], pact["version"], pact["status"]))
        if cur.rowcount != 1:
            raise ApiError(409, "conflict", "The pact changed meanwhile; reload it and try again.")
        pact.update(fields, status=new, version=pact["version"] + 1)
        self.store.append_event(db, pact["id"], actor, kind, {**data, "status": new})

    # --- pacts and terms -----------------------------------------------------------------------------------------
    def create_pact(self, user: dict[str, Any] | None, chat: str, *, amount_cents: int | None = None,
                    title: str | None = None, preset_id: str | None = None) -> dict[str, Any]:
        if user is None:
            raise ApiError(401, "unauthenticated", "Sign in: send 'Authorization: Bearer <token>'.")
        chat = (chat or "").strip()
        if not chat or len(chat) > MAX_CHAT:
            raise ApiError(422, "invalid_chat", f"The chat must be 1 to {MAX_CHAT} characters.")
        drafted, by, notes = T.draft(chat, amount_cents=amount_cents, title=title, preset_id=preset_id,
                                     settings=self.settings, complete=self.drafter, model_label=self.drafter_label)
        try:
            editable = T.validate(drafted)
            problems: list[str] = []
        except T.TermsError as exc:  # a draft may be incomplete: keep it as a DRAFT the parties must fix
            editable, problems = {"version": 1, **{k: drafted.get(k) for k in T.EDITABLE}}, exc.problems
        pid = new_id("PACT")
        codes = {"freelancer": f"inv_{secrets.token_urlsafe(12)}", "arbiter": f"inv_{secrets.token_urlsafe(12)}"}
        source = {"chat_sha256": sha256_text(chat), "drafted_by": by}
        with self.store.tx() as db:
            full = T.with_parties(editable, {"client": user["id"], "freelancer": None, "arbiter": None}, source)
            db.execute(
                "INSERT INTO pacts (id, status, created_by, created_at, updated_at, raw_chat, terms_json, terms_sha256, "
                "compiled_by, paypal_mode) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (pid, DRAFT, user["id"], utc_now(), utc_now(), chat, canonical(full), T.terms_hash(full), by,
                 self.paypal.mode))
            db.execute("INSERT INTO members (pact_id, user_id, role, joined_at) VALUES (?, ?, 'client', ?)",
                       (pid, user["id"], utc_now()))
            for role, code in codes.items():
                db.execute("INSERT INTO invites (code_sha256, pact_id, role, created_at) VALUES (?, ?, ?, ?)",
                           (_token_hash(code), pid, role, utc_now()))
            self.store.append_event(db, pid, user["id"], "pact.created",
                                    {"status": DRAFT, "drafted_by": by, "chat_sha256": source["chat_sha256"],
                                     "terms_sha256": T.terms_hash(full), "draft_notes": notes,
                                     "draft_problems": problems})
        view = self.view(user, pid)
        view["invite_codes"] = codes  # shown once: only their hashes are stored
        view["draft_notes"], view["draft_problems"] = notes, problems
        return view

    def accept_invite(self, user: dict[str, Any] | None, code: str) -> dict[str, Any]:
        if user is None:
            raise ApiError(401, "unauthenticated", "Sign in: send 'Authorization: Bearer <token>'.")
        with self.store.tx() as db:
            inv = db.execute("SELECT * FROM invites WHERE code_sha256 = ?", (_token_hash(code or ""),)).fetchone()
            if not inv or inv["used_by"]:
                raise ApiError(404, "invalid_invite", "This invite code is unknown or already used.")
            pact = self._pact(db, inv["pact_id"])
            self._expect(pact, DRAFT)
            roles = self._roles(db, pact["id"])
            if inv["role"] in roles:
                raise ApiError(409, "role_taken", f"The {inv['role']} of this pact has already joined.")
            if user["id"] in roles.values():
                raise ApiError(409, "already_party", "You already have a role in this pact; one role per person.")
            db.execute("UPDATE invites SET used_by = ? WHERE code_sha256 = ?", (user["id"], inv["code_sha256"]))
            db.execute("INSERT INTO members (pact_id, user_id, role, joined_at) VALUES (?, ?, ?, ?)",
                       (pact["id"], user["id"], inv["role"], utc_now()))
            self._rehash(db, pact, user["id"], f"party.joined.{inv['role']}", {"role": inv["role"], "user": user["id"]})
        return self.view(user, inv["pact_id"])

    def _rehash(self, db: sqlite3.Connection, pact: dict[str, Any], actor: str, kind: str, data: dict[str, Any],
                editable: dict[str, Any] | None = None) -> None:
        """Rebuild the full terms (parties included) after an edit or a join: a new hash, so earlier approvals lapse."""
        full = json.loads(pact["terms_json"])
        roles = self._roles(db, pact["id"])
        full = T.with_parties(editable or full, {r: roles.get(r) for r in ROLES}, full["source"])
        h = T.terms_hash(full)
        self._move(db, pact, DRAFT, actor, kind, {**data, "terms_sha256": h}, terms_json=canonical(full), terms_sha256=h)

    def update_terms(self, user: dict[str, Any] | None, pact_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        with self.store.pact_lock(pact_id), self.store.tx() as db:
            pact, role = self._guard(db, user, pact_id, ("client", "freelancer"))
            if pact["locked_terms_sha256"]:
                raise ApiError(409, "terms_locked", "Both parties approved these terms; they can no longer change.",
                               terms_sha256=pact["locked_terms_sha256"])
            self._expect(pact, DRAFT)
            current = json.loads(pact["terms_json"])
            merged = {k: (patch[k] if k in patch else current.get(k)) for k in T.EDITABLE}
            try:
                editable = T.validate(merged)
            except T.TermsError as exc:
                raise ApiError(422, "invalid_terms", "The terms are not valid.", problems=exc.problems) from exc
            changed = sorted(k for k in T.EDITABLE if editable.get(k) != current.get(k))
            self._rehash(db, pact, user["id"], "terms.updated", {"by_role": role, "changed": changed},
                         editable=editable)
        return self.view(user, pact_id)

    def approve_terms(self, user: dict[str, Any] | None, pact_id: str, terms_sha256: str) -> dict[str, Any]:
        with self.store.pact_lock(pact_id), self.store.tx() as db:
            pact, role = self._guard(db, user, pact_id, ("client", "freelancer"))
            self._expect(pact, DRAFT)
            if terms_sha256 != pact["terms_sha256"]:
                raise ApiError(409, "stale_terms", "These are not the current terms; reload and review them.",
                               current_terms_sha256=pact["terms_sha256"])
            full = json.loads(pact["terms_json"])
            try:
                T.validate(full)
            except T.TermsError as exc:
                raise ApiError(422, "invalid_terms", "The terms must be complete before approval.",
                               problems=exc.problems) from exc
            roles = self._roles(db, pact_id)
            if "freelancer" not in roles:
                raise ApiError(409, "missing_party", "The freelancer must join before the terms can be approved.")
            db.execute("INSERT OR IGNORE INTO approvals (pact_id, role, user_id, terms_sha256, approved_at) "
                       "VALUES (?, ?, ?, ?, ?)", (pact_id, role, user["id"], terms_sha256, utc_now()))
            self.store.append_event(db, pact_id, user["id"], "terms.approved", {"role": role, "terms_sha256": terms_sha256})
            got = {r["role"] for r in db.execute("SELECT role FROM approvals WHERE pact_id = ? AND terms_sha256 = ?",
                                                 (pact_id, terms_sha256))}
            if {"client", "freelancer"} <= got:
                self._move(db, pact, TERMS_APPROVED, user["id"], "terms.locked", {"terms_sha256": terms_sha256},
                           locked_terms_sha256=terms_sha256)
        return self.view(user, pact_id)

    def _locked(self, pact: dict[str, Any]) -> dict[str, Any]:
        full = json.loads(pact["terms_json"])
        if not pact["locked_terms_sha256"] or T.terms_hash(full) != pact["locked_terms_sha256"]:
            raise ApiError(409, "terms_not_locked", "The terms are not locked (or do not match their locked hash).")
        return full

    # --- holding the funds ---------------------------------------------------------------------------------------
    def create_order(self, user: dict[str, Any] | None, pact_id: str) -> dict[str, Any]:
        with self.store.pact_lock(pact_id):
            with self.store.read() as db:
                pact, _ = self._guard(db, user, pact_id, ("client",))
                self._expect(pact, TERMS_APPROVED, AWAITING)
                full = self._locked(pact)
            attempt = pact["order_attempt"] + 1
            request_id = f"pactpay-{pact_id}-order-{attempt}"
            items, cents = [], full["amount_cents"]
            running = 0
            for i, c in enumerate(full["criteria"]):
                part = cents - running if i == len(full["criteria"]) - 1 else cents * int(round(c["weight_pct"] * 100)) // 10000
                running += part
                items.append({"name": f"[{c['id']}] {c['title']}"[:127], "quantity": "1", "category": "DIGITAL_GOODS",
                              "sku": f"{pact_id}-{c['id']}"[:127], "unit_amount": money(part, full["currency"])})
            base = self.settings.public_url
            order = self._paypal(lambda: self.paypal.create_order(
                pact_id=pact_id, request_id=request_id, amount_cents=cents, currency=full["currency"],
                title=full["title"], items=items, return_url=f"{base}/?pact={pact_id}&paypal=return",
                cancel_url=f"{base}/?pact={pact_id}&paypal=cancel"))
            link = next((x["href"] for x in order.get("links", []) if x.get("rel") in ("payer-action", "approve")), None)
            with self.store.tx() as db:
                pact = self._pact(db, pact_id)
                self._expect(pact, TERMS_APPROVED, AWAITING)
                self._move(db, pact, AWAITING, user["id"], "paypal.order_created",
                           {"order_id": order["id"], "paypal_status": order.get("status"), "amount_cents": cents,
                            "terms_sha256": pact["locked_terms_sha256"], "mode": self.paypal.mode,
                            "paypal_request_id": request_id},
                           order_id=order["id"], order_status=order.get("status"), order_attempt=attempt,
                           approve_url=link)
        return self.view(user, pact_id)

    def simulate_buyer_approval(self, user: dict[str, Any] | None, pact_id: str) -> dict[str, Any]:
        """Offline demo only: the buyer's approval in PayPal Checkout, simulated and recorded as simulated."""
        if self.paypal.simulator is None:
            raise ApiError(409, "live_sandbox", "Live sandbox: the buyer approves in PayPal Checkout, not here.")
        with self.store.pact_lock(pact_id), self.store.tx() as db:
            pact, _ = self._guard(db, user, pact_id, ("client",))
            self._expect(pact, AWAITING)
            order = self.paypal.simulator.approve_order(pact["order_id"])
            self.store.append_event(db, pact_id, user["id"], "paypal.buyer_approved_simulated",
                                    {"order_id": pact["order_id"], "paypal_status": order["status"],
                                     "mode": "sandbox_simulation"})
        return self.view(user, pact_id)

    def authorize(self, user: dict[str, Any] | None, pact_id: str, order_id: str | None = None) -> dict[str, Any]:
        with self.store.pact_lock(pact_id):
            with self.store.read() as db:
                pact, _ = self._guard(db, user, pact_id, ("client",))
                if pact["status"] == HELD:
                    return self.view(user, pact_id)  # already held: a repeat (a double click) changes nothing
                self._expect(pact, AWAITING)
                full = self._locked(pact)
            if order_id and order_id != pact["order_id"]:
                raise ApiError(409, "order_mismatch", "That PayPal order does not belong to this pact.")
            try:
                self.paypal.authorize_order(pact["order_id"], pact_id=pact_id,
                                            request_id=f"pactpay-{pact_id}-authorize-{pact['order_attempt']}")
            except PayPalError as exc:
                if exc.issue not in ("ORDER_ALREADY_AUTHORIZED",):
                    raise self._paypal_error(exc) from exc
            order = self._paypal(lambda: self.paypal.get_order(pact["order_id"], pact_id=pact_id))
            auth = self._check_authorization(order, pact_id, full)
            with self.store.tx() as db:
                pact = self._pact(db, pact_id)
                self._expect(pact, AWAITING)
                self._move(db, pact, HELD, user["id"], "paypal.authorization_verified",
                           {"order_id": order["id"], "order_status": order.get("status"), "authorization_id": auth["id"],
                            "authorization_status": auth["status"], "amount_cents": cents_of(auth["amount"]["value"]),
                            "expiration_time": auth.get("expiration_time"), "mode": self.paypal.mode,
                            "verified_by": "GET /v2/checkout/orders/{id}"},
                           order_status=order.get("status"), authorization_id=auth["id"],
                           authorization_status=auth["status"], authorized_cents=cents_of(auth["amount"]["value"]),
                           authorization_expires_at=auth.get("expiration_time"))
        return self.view(user, pact_id)

    @staticmethod
    def _check_authorization(order: dict[str, Any], pact_id: str, full: dict[str, Any]) -> dict[str, Any]:
        """The order's authorization, accepted only if PayPal's record matches the locked terms."""
        units = order.get("purchase_units") or []
        auths = (units[0].get("payments") or {}).get("authorizations") or [] if units else []
        if order.get("status") != "COMPLETED" or not auths:
            raise ApiError(409, "not_authorized", "PayPal's record of the order shows no authorization yet.",
                           paypal_order_status=order.get("status"))
        auth = auths[0]
        unit = units[0]
        if (auth.get("status") not in ("CREATED", "PENDING")
                or cents_of(auth["amount"]["value"]) != full["amount_cents"]
                or auth["amount"].get("currency_code") != full["currency"]
                or (unit.get("reference_id") or unit.get("custom_id")) != pact_id):
            raise ApiError(409, "authorization_mismatch",
                           "PayPal's authorization does not match the locked terms (status, amount, currency or pact).",
                           paypal_authorization_status=auth.get("status"))
        return auth

    # --- delivering and evaluating -------------------------------------------------------------------------------
    def submit(self, user: dict[str, Any] | None, pact_id: str, filename: str, content: str) -> dict[str, Any]:
        filename = (filename or "submission.txt").replace("\\", "/").split("/")[-1][:120] or "submission.txt"
        if not content or not content.strip():
            raise ApiError(422, "empty_submission", "The submission is empty.")
        if len(content) > MAX_SUBMISSION:
            raise ApiError(413, "submission_too_large", f"Submissions are limited to {MAX_SUBMISSION:,} characters.")
        with self.store.pact_lock(pact_id), self.store.tx() as db:
            pact, _ = self._guard(db, user, pact_id, ("freelancer",))
            self._expect(pact, HELD, SUBMITTED, AUDITED)
            sid = new_id("SUB")
            digest = sha256_text(content)
            db.execute("INSERT INTO submissions (id, pact_id, user_id, filename, content, sha256, created_at) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?)", (sid, pact_id, user["id"], filename, content, digest, utc_now()))
            self._move(db, pact, SUBMITTED, user["id"], "submission.received",
                       {"submission_id": sid, "filename": filename, "sha256": digest, "chars": len(content)})
        return self.view(user, pact_id)

    def evaluate(self, user: dict[str, Any] | None, pact_id: str) -> dict[str, Any]:
        with self.store.pact_lock(pact_id):
            with self.store.read() as db:
                pact, _ = self._guard(db, user, pact_id)
                self._expect(pact, SUBMITTED, AUDITED)
                full = self._locked(pact)
                sub = db.execute("SELECT * FROM submissions WHERE pact_id = ? ORDER BY created_at DESC, rowid DESC "
                                 "LIMIT 1", (pact_id,)).fetchone()
            if self._evaluate is None:
                raise ApiError(503, "evaluator_unavailable", "The evaluator is not available.")
            report = self._evaluate(full["criteria"], sub["filename"], sub["content"], reviewer=self.reviewer)
            score = float(report.get("score_pct") or 0.0)
            # money from integers only: the locked weights of the criteria the evaluator marked PASS, in basis points
            # (the float score_pct is for display; it never sets an amount)
            passed = {c.get("id") for c in report.get("criteria", []) if c.get("status") == "PASS"}
            earned_bps = sum(int(round(c["weight_pct"] * 100)) for c in full["criteria"] if c["id"] in passed)
            recommended = full["amount_cents"] * min(earned_bps, 10000) // 10000
            eid = new_id("EVAL")
            report_json = canonical(report)
            with self.store.tx() as db:
                pact = self._pact(db, pact_id)
                self._expect(pact, SUBMITTED, AUDITED)
                latest = db.execute("SELECT id FROM submissions WHERE pact_id = ? ORDER BY created_at DESC, rowid DESC "
                                    "LIMIT 1", (pact_id,)).fetchone()
                if latest["id"] != sub["id"]:
                    raise ApiError(409, "superseded", "A newer submission arrived; evaluate that one.")
                db.execute("INSERT INTO evaluations (id, pact_id, submission_id, terms_sha256, score_pct, "
                           "recommended_cents, report_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (eid, pact_id, sub["id"], pact["locked_terms_sha256"], score, recommended, report_json,
                            utc_now()))
                self._move(db, pact, AUDITED, user["id"], "evaluation.completed",
                           {"evaluation_id": eid, "submission_id": sub["id"], "submission_sha256": sub["sha256"],
                            "terms_sha256": pact["locked_terms_sha256"], "score_pct": score,
                            "recommended_cents": recommended, "report_sha256": sha256_text(report_json),
                            "criteria": {c["id"]: c.get("status") for c in report.get("criteria", [])},
                            "flags": report.get("flags", []), "reviewer": (report.get("reviewer") or {}).get("status")})
        return self.view(user, pact_id)

    # --- the human decision and the money ------------------------------------------------------------------------
    def _latest_evaluation(self, db: sqlite3.Connection, pact: dict[str, Any]) -> sqlite3.Row:
        ev = db.execute("SELECT e.*, s.sha256 AS submission_sha256 FROM evaluations e JOIN submissions s "
                        "ON s.id = e.submission_id WHERE e.pact_id = ? ORDER BY e.created_at DESC, e.rowid DESC LIMIT 1",
                        (pact["id"],)).fetchone()
        sub = db.execute("SELECT id FROM submissions WHERE pact_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                         (pact["id"],)).fetchone()
        if not ev:
            raise ApiError(409, "not_evaluated", "No evaluation yet: money is released only after one.")
        if ev["submission_id"] != sub["id"] or ev["terms_sha256"] != pact["locked_terms_sha256"]:
            raise ApiError(409, "evaluation_stale", "The latest evaluation is not of the latest submission.")
        return ev

    def _decision(self, pact: dict[str, Any], ev: sqlite3.Row, amount_cents: int | None, reason: str | None,
                  evaluation_id: str | None) -> tuple[int, str | None]:
        if evaluation_id and evaluation_id != ev["id"]:
            raise ApiError(409, "evaluation_stale", "That is not the latest evaluation.", latest_evaluation_id=ev["id"])
        amount = ev["recommended_cents"] if amount_cents is None else int(amount_cents)
        if not 0 < amount <= (pact["authorized_cents"] or 0):
            raise ApiError(422, "invalid_amount", f"Release between $0.01 and {usd(pact['authorized_cents'])} "
                                                  "(the authorized amount); to release nothing, dispute or void.",
                           recommended_cents=ev["recommended_cents"])
        reason = (reason or "").strip()[:1000] or None
        if amount != ev["recommended_cents"] and (not reason or len(reason) < MIN_REASON):
            raise ApiError(422, "reason_required",
                           f"This differs from the evaluation's recommendation ({usd(ev['recommended_cents'])}); "
                           f"state your reason (at least {MIN_REASON} characters) so it is on the record.",
                           recommended_cents=ev["recommended_cents"])
        return amount, reason

    def release(self, user: dict[str, Any] | None, pact_id: str, *, amount_cents: int | None = None,
                reason: str | None = None, evaluation_id: str | None = None) -> dict[str, Any]:
        """The client's decision. The full authorized amount is captured at once; anything less is a proposal the
        freelancer accepts or disputes."""
        with self.store.pact_lock(pact_id):
            with self.store.tx() as db:
                pact, _ = self._guard(db, user, pact_id, ("client",))
                if pact["status"] in (CAPTURING, CAPTURE_PENDING, CAPTURED, *PAYOUT_STATES):
                    raise ApiError(409, "already_released", "This milestone was already released; it is released once.",
                                   pact_status=pact["status"])
                self._expect(pact, AUDITED)
                self._locked(pact)  # fail closed: no capture unless the stored terms still match their locked hash
                ev = self._latest_evaluation(db, pact)
                amount, reason = self._decision(pact, ev, amount_cents, reason, evaluation_id)
                full = amount == pact["authorized_cents"]
                rid = self._new_release(db, pact, ev, user["id"], "client", amount, reason,
                                        "APPROVED" if full else "PROPOSED")
                self._move(db, pact, CAPTURING if full else PROPOSED, user["id"],
                           "release.approved" if full else "release.proposed",
                           {"release_id": rid, "evaluation_id": ev["id"], "amount_cents": amount,
                            "recommended_cents": ev["recommended_cents"], "override_reason": reason,
                            "terms_sha256": pact["locked_terms_sha256"]})
            if full:
                self._capture(pact_id, rid)
        return self.view(user, pact_id)

    def accept_release(self, user: dict[str, Any] | None, pact_id: str) -> dict[str, Any]:
        with self.store.pact_lock(pact_id):
            with self.store.tx() as db:
                pact, _ = self._guard(db, user, pact_id, ("freelancer",))
                self._expect(pact, PROPOSED)
                self._locked(pact)
                rel = db.execute("SELECT * FROM releases WHERE pact_id = ? AND status = 'PROPOSED'", (pact_id,)).fetchone()
                self._latest_evaluation(db, pact)
                db.execute("UPDATE releases SET status = 'APPROVED', updated_at = ? WHERE id = ?", (utc_now(), rel["id"]))
                self._move(db, pact, CAPTURING, user["id"], "release.accepted_by_freelancer",
                           {"release_id": rel["id"], "amount_cents": rel["amount_cents"]})
            self._capture(pact_id, rel["id"])
        return self.view(user, pact_id)

    def dispute(self, user: dict[str, Any] | None, pact_id: str, reason: str) -> dict[str, Any]:
        reason = (reason or "").strip()[:1000]
        if len(reason) < MIN_REASON:
            raise ApiError(422, "reason_required", f"Describe the dispute (at least {MIN_REASON} characters).")
        with self.store.pact_lock(pact_id), self.store.tx() as db:
            pact, role = self._guard(db, user, pact_id, ("client", "freelancer"))
            self._expect(pact, AUDITED, PROPOSED)
            if "arbiter" not in self._roles(db, pact_id):
                raise ApiError(409, "no_arbiter", "This pact has no arbiter; agree on a release instead.")
            db.execute("UPDATE releases SET status = 'SUPERSEDED', updated_at = ? WHERE pact_id = ? AND status = 'PROPOSED'",
                       (utc_now(), pact_id))
            self._move(db, pact, DISPUTED, user["id"], "dispute.opened", {"by_role": role, "reason": reason})
        return self.view(user, pact_id)

    def resolve(self, user: dict[str, Any] | None, pact_id: str, *, amount_cents: int, reason: str) -> dict[str, Any]:
        """The arbiter's decision on a dispute: an amount to capture (the rest returns to the buyer), or 0 to void."""
        reason = (reason or "").strip()[:1000]
        if len(reason) < MIN_REASON:
            raise ApiError(422, "reason_required", f"State the decision's reason (at least {MIN_REASON} characters).")
        with self.store.pact_lock(pact_id):
            with self.store.tx() as db:
                pact, _ = self._guard(db, user, pact_id, ("arbiter",))
                self._expect(pact, DISPUTED)
                self._locked(pact)
                ev = self._latest_evaluation(db, pact)
                amount = int(amount_cents)
                if not 0 <= amount <= (pact["authorized_cents"] or 0):
                    raise ApiError(422, "invalid_amount", f"Decide between $0.00 and {usd(pact['authorized_cents'])}.")
                if amount == 0:
                    self._move(db, pact, DISPUTED, user["id"], "dispute.resolved",
                               {"amount_cents": 0, "reason": reason, "evaluation_id": ev["id"]})
                    rid = None
                else:
                    rid = self._new_release(db, pact, ev, user["id"], "arbiter", amount, reason, "APPROVED")
                    self._move(db, pact, CAPTURING, user["id"], "dispute.resolved",
                               {"release_id": rid, "amount_cents": amount, "recommended_cents": ev["recommended_cents"],
                                "reason": reason, "evaluation_id": ev["id"]})
            if rid:
                self._capture(pact_id, rid)
            else:
                self._void(pact_id, user["id"], "the arbiter decided to release nothing")
        return self.view(user, pact_id)

    def void(self, user: dict[str, Any] | None, pact_id: str, reason: str) -> dict[str, Any]:
        """The client cancels before any delivery: PayPal voids the authorization and the hold is released."""
        with self.store.pact_lock(pact_id):
            with self.store.read() as db:
                pact, _ = self._guard(db, user, pact_id, ("client",))
                self._expect(pact, HELD)
                if db.execute("SELECT 1 FROM submissions WHERE pact_id = ?", (pact_id,)).fetchone():
                    raise ApiError(409, "work_delivered", "Work was delivered: dispute it instead of voiding.")
            self._void(pact_id, user["id"], (reason or "cancelled by the client before delivery")[:300])
        return self.view(user, pact_id)

    def _void(self, pact_id: str, actor: str, reason: str) -> None:
        with self.store.read() as db:
            pact = self._pact(db, pact_id)
        try:
            self.paypal.void_authorization(pact["authorization_id"], pact_id=pact_id,
                                           request_id=f"pactpay-{pact_id}-void")
        except PayPalError as exc:
            if exc.issue != "PREVIOUSLY_VOIDED":
                raise self._paypal_error(exc) from exc
        auth = self._paypal(lambda: self.paypal.get_authorization(pact["authorization_id"], pact_id=pact_id))
        if auth.get("status") != "VOIDED":
            raise ApiError(502, "void_unconfirmed", "PayPal's record does not show the authorization voided.",
                           paypal_authorization_status=auth.get("status"))
        with self.store.tx() as db:
            pact = self._pact(db, pact_id)
            self._move(db, pact, VOIDED, actor, "paypal.authorization_voided",
                       {"authorization_id": pact["authorization_id"], "paypal_status": "VOIDED", "reason": reason,
                        "mode": self.paypal.mode}, authorization_status="VOIDED")

    def _new_release(self, db: sqlite3.Connection, pact: dict[str, Any], ev: sqlite3.Row, actor: str, role: str,
                     amount: int, reason: str | None, status: str) -> str:
        seq = (db.execute("SELECT COALESCE(MAX(seq), 0) FROM releases WHERE pact_id = ?", (pact["id"],)).fetchone()[0]
               or 0) + 1
        if db.execute("SELECT 1 FROM releases WHERE pact_id = ? AND status IN ('APPROVED', 'CAPTURED', 'PENDING')",
                      (pact["id"],)).fetchone():
            raise ApiError(409, "already_released", "This milestone was already released; it is released once.")
        rid = new_id("REL")
        db.execute("INSERT INTO releases (id, pact_id, seq, evaluation_id, decided_by, decided_role, amount_cents, "
                   "recommended_cents, reason, status, capture_request_id, created_at, updated_at) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (rid, pact["id"], seq, ev["id"], actor, role, amount, ev["recommended_cents"], reason, status,
                    f"pactpay-{pact['id']}-capture-{seq}", utc_now(), utc_now()))
        return rid

    def _capture(self, pact_id: str, release_id: str, *, recovering: bool = False) -> None:
        with self.store.read() as db:
            pact = self._pact(db, pact_id)
            rel = dict(db.execute("SELECT * FROM releases WHERE id = ?", (release_id,)).fetchone())
        currency = json.loads(pact["terms_json"])["currency"]
        cap = self._find_capture(pact, rel) if recovering else None
        if cap is None:
            try:
                cap = self.paypal.capture_authorization(
                    pact["authorization_id"], pact_id=pact_id, request_id=rel["capture_request_id"],
                    amount_cents=rel["amount_cents"], currency=currency,
                    note=f"YAVQEN release of {usd(rel['amount_cents'])} decided by the {rel['decided_role']}")
            except PayPalError as exc:
                # PayPal may have captured before its answer was lost; past PayPal's request-id window a retry is
                # refused as a duplicate invoice id (each release's capture invoice_id is unique), never captured twice
                if exc.transient or exc.issue in ("AUTHORIZATION_ALREADY_CAPTURED", "PREVIOUS_REQUEST_IN_PROGRESS",
                                                  "DUPLICATE_INVOICE_ID"):
                    cap = self._find_capture(pact, rel)
                    if cap is None:
                        with self.store.tx() as db:
                            db.execute("UPDATE releases SET error_json = ?, updated_at = ? WHERE id = ?",
                                       (canonical(exc.to_dict()), utc_now(), release_id))
                            self.store.append_event(db, pact_id, "system", "paypal.capture_unconfirmed",
                                                    {"release_id": release_id, "paypal": exc.to_dict()})
                        raise ApiError(502, "capture_unconfirmed",
                                       "PayPal did not confirm the capture; reconcile retries it safely with the same "
                                       "PayPal-Request-Id.", paypal=exc.to_dict()) from exc
                else:
                    with self.store.tx() as db:
                        pact = self._pact(db, pact_id)
                        db.execute("UPDATE releases SET status = 'FAILED', error_json = ?, updated_at = ? WHERE id = ?",
                                   (canonical(exc.to_dict()), utc_now(), release_id))
                        self._move(db, pact, CAPTURE_FAILED, "system", "paypal.capture_failed",
                                   {"release_id": release_id, "paypal": exc.to_dict()})
                    raise self._paypal_error(exc) from exc
        self._record_capture(pact_id, release_id, cap)

    def _find_capture(self, pact: dict[str, Any], rel: dict[str, Any]) -> dict[str, Any] | None:
        """The capture of this release in PayPal's record of the order (matched by its invoice id), or None."""
        order = self._paypal(lambda: self.paypal.get_order(pact["order_id"], pact_id=pact["id"]))
        for unit in order.get("purchase_units") or []:
            for cap in (unit.get("payments") or {}).get("captures") or []:
                if cap.get("invoice_id") == rel["capture_request_id"]:
                    return cap
        return None

    def _record_capture(self, pact_id: str, release_id: str, cap: dict[str, Any]) -> None:
        status = str(cap.get("status") or "")
        breakdown = cap.get("seller_receivable_breakdown") or {}
        gross = cents_of((cap.get("amount") or {}).get("value"))
        fee = cents_of((breakdown.get("paypal_fee") or {}).get("value")) if breakdown else None
        net = cents_of((breakdown.get("net_amount") or {}).get("value")) if breakdown else None
        new = {"COMPLETED": CAPTURED, "PENDING": CAPTURE_PENDING}.get(status, CAPTURE_FAILED)
        with self.store.tx() as db:
            pact = self._pact(db, pact_id)
            db.execute("UPDATE releases SET status = ?, capture_id = ?, capture_status = ?, captured_cents = ?, "
                       "fee_cents = ?, net_cents = ?, error_json = NULL, updated_at = ? WHERE id = ?",
                       ({"COMPLETED": "CAPTURED", "PENDING": "PENDING"}.get(status, "FAILED"), cap.get("id"), status,
                        gross, fee, net, utc_now(), release_id))
            if pact["status"] in (CAPTURING, CAPTURE_PENDING):
                self._move(db, pact, new, "system", "paypal.capture",
                           {"release_id": release_id, "capture_id": cap.get("id"), "paypal_status": status,
                            "gross_cents": gross, "fee_cents": fee, "net_cents": net, "mode": self.paypal.mode,
                            "status_details": cap.get("status_details")})
        if new == CAPTURED:
            try:
                self._payout(pact_id, release_id)
            except ApiError:
                pass  # the capture stands; the payout error is recorded on the pact and reconcile retries it

    def _payout(self, pact_id: str, release_id: str) -> None:
        """Split what PayPal says was received (net of its fee) among the locked payees, in one Payouts batch."""
        with self.store.tx() as db:
            pact = self._pact(db, pact_id)
            rel = dict(db.execute("SELECT * FROM releases WHERE id = ?", (release_id,)).fetchone())
            full = self._locked(pact)
            payout = db.execute("SELECT * FROM payouts WHERE release_id = ?", (release_id,)).fetchone()
            if payout is None:
                net = rel["net_cents"] if rel["net_cents"] is not None else rel["captured_cents"]
                sbid = f"PACTPAY-{pact_id}-R{rel['seq']}"
                pid = new_id("PO")
                db.execute("INSERT INTO payouts (id, pact_id, release_id, sender_batch_id, created_at, updated_at) "
                           "VALUES (?, ?, ?, ?, ?, ?)", (pid, pact_id, release_id, sbid, utc_now(), utc_now()))
                for payee, cents in zip(full["payees"], T.split_cents(net, full["payees"]), strict=True):
                    db.execute("INSERT INTO payout_items (sender_item_id, payout_id, payee_id, receiver, amount_cents, "
                               "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                               (f"{sbid}-{payee['id']}", pid, payee["id"], payee["paypal_email"], cents, utc_now()))
                payout = db.execute("SELECT * FROM payouts WHERE id = ?", (pid,)).fetchone()
            payout = dict(payout)
            items = [dict(r) for r in db.execute("SELECT * FROM payout_items WHERE payout_id = ? ORDER BY sender_item_id",
                                                 (payout["id"],))]
        if payout["payout_batch_id"]:
            self.refresh_payout(pact_id)
            return
        net = rel["net_cents"] if rel["net_cents"] is not None else rel["captured_cents"]
        if sum(it["amount_cents"] for it in items) != net or any(it["amount_cents"] < 0 for it in items) or (
                {it["receiver"].lower() for it in items} != {x["paypal_email"].lower() for x in full["payees"]}):
            # fail closed: never send a batch that pays more or less than PayPal captured, or anyone not locked in
            with self.store.tx() as db:
                pact = self._pact(db, pact_id)
                self._move(db, pact, ATTENTION, "system", "payout.refused_inconsistent",
                           {"net_cents": net, "items_cents": sum(it["amount_cents"] for it in items),
                            "next": "no payout was sent; a person must investigate"})
            return
        body_items = [{"recipient_type": "EMAIL", "receiver": it["receiver"], "sender_item_id": it["sender_item_id"],
                       "amount": {"value": f"{it['amount_cents'] // 100}.{it['amount_cents'] % 100:02d}",
                                  "currency": full["currency"]},
                       "note": f"YAVQEN: your share of {full['title']}"[:4000]}
                      for it in items if it["amount_cents"] > 0]
        try:
            resp = self.paypal.create_payout(pact_id=pact_id, sender_batch_id=payout["sender_batch_id"],
                                             items=body_items, subject=f"YAVQEN payout: {full['title']}")
        except PayPalError as exc:
            if exc.status_code == 400 and "already exists" in f"{exc.issue} {exc.message}":
                # PayPal holds a batch with this sender_batch_id (an earlier answer was lost): no second payout
                # is possible; its id arrives with PayPal's batch webhook, which _adopt_batch matches
                with self.store.tx() as db:
                    pact = self._pact(db, pact_id)
                    db.execute("UPDATE payouts SET error_json = ?, updated_at = ? WHERE id = ?",
                               (canonical({**exc.to_dict(), "name": "BATCH_EXISTS_ID_UNKNOWN"}), utc_now(),
                                payout["id"]))
                    self._move(db, pact, ATTENTION, "system", "paypal.payout_batch_exists",
                               {"sender_batch_id": payout["sender_batch_id"], "paypal": exc.to_dict(),
                                "next": "PayPal's batch webhook identifies the batch; no second payout is sent"})
                return
            with self.store.tx() as db:
                db.execute("UPDATE payouts SET error_json = ?, updated_at = ? WHERE id = ?",
                           (canonical(exc.to_dict()), utc_now(), payout["id"]))
                self.store.append_event(db, pact_id, "system", "paypal.payout_not_created",
                                        {"sender_batch_id": payout["sender_batch_id"], "paypal": exc.to_dict(),
                                         "retry": "reconcile repeats it with the same sender_batch_id"})
            raise ApiError(502, "payout_not_created", "The capture succeeded but the payout was not created yet; "
                                                      "reconcile retries it with the same sender_batch_id.",
                           paypal=exc.to_dict()) from exc
        header = resp.get("batch_header") or {}
        with self.store.tx() as db:
            pact = self._pact(db, pact_id)
            db.execute("UPDATE payouts SET payout_batch_id = ?, batch_status = ?, error_json = NULL, updated_at = ? "
                       "WHERE id = ?", (header.get("payout_batch_id"), header.get("batch_status"), utc_now(),
                                        payout["id"]))
            self._move(db, pact, PAYOUT_PENDING, "system", "paypal.payout_batch_created",
                       {"payout_batch_id": header.get("payout_batch_id"), "batch_status": header.get("batch_status"),
                        "sender_batch_id": payout["sender_batch_id"], "items": len(body_items),
                        "mode": self.paypal.mode})
        try:
            self.refresh_payout(pact_id)
        except ApiError:
            pass  # the batch exists; its items are read again by reconcile or a webhook

    def _adopt_batch(self, pact_id: str, payout_id: str, batch_id: str) -> dict[str, Any]:
        """A verified webhook named a batch whose create answer PactPay lost: confirm with PayPal that the batch is
        this release's (its sender_batch_id), record its id, and read its items."""
        batch = self._paypal(lambda: self.paypal.get_payout_batch(batch_id, pact_id=pact_id))
        header = batch.get("batch_header") or {}
        with self.store.tx() as db:
            payout = db.execute("SELECT * FROM payouts WHERE id = ?", (payout_id,)).fetchone()
            if (header.get("sender_batch_header") or {}).get("sender_batch_id") != payout["sender_batch_id"]:
                return {"applied": False, "reason": "the batch PayPal returned is not this release's"}
            pact = self._pact(db, pact_id)
            db.execute("UPDATE payouts SET payout_batch_id = ?, batch_status = ?, error_json = NULL, updated_at = ? "
                       "WHERE id = ?", (batch_id, header.get("batch_status"), utc_now(), payout_id))
            self._move(db, pact, PAYOUT_PENDING, "system", "paypal.payout_batch_identified",
                       {"payout_batch_id": batch_id, "sender_batch_id": payout["sender_batch_id"],
                        "via": "verified webhook, confirmed by GET /v1/payments/payouts/{id}", "mode": self.paypal.mode})
        self._record_items(pact_id, payout_id, header.get("batch_status"), batch.get("items") or [])
        with self.store.read() as db:
            return {"applied": True, "pact_id": pact_id, "status": self._pact(db, pact_id)["status"]}

    def refresh_payout(self, pact_id: str) -> None:
        """Read the batch back from PayPal and record each item's status as PayPal reports it."""
        with self.store.read() as db:
            payout = db.execute("SELECT * FROM payouts WHERE pact_id = ? AND payout_batch_id IS NOT NULL",
                                (pact_id,)).fetchone()
        if not payout:
            return
        batch = self._paypal(lambda: self.paypal.get_payout_batch(payout["payout_batch_id"], pact_id=pact_id))
        self._record_items(pact_id, payout["id"], (batch.get("batch_header") or {}).get("batch_status"),
                           batch.get("items") or [])

    def _record_items(self, pact_id: str, payout_id: str, batch_status: str | None, items: list[dict]) -> None:
        with self.store.tx() as db:
            pact = self._pact(db, pact_id)
            changes = []
            for it in items:
                sid = (it.get("payout_item") or {}).get("sender_item_id") or it.get("sender_item_id")
                row = db.execute("SELECT * FROM payout_items WHERE sender_item_id = ? AND payout_id = ?",
                                 (sid, payout_id)).fetchone()
                if not row:
                    continue
                status = it.get("transaction_status")
                fee = (it.get("payout_item_fee") or {}).get("value")
                if row["transaction_status"] != status:
                    changes.append({"sender_item_id": sid, "payout_item_id": it.get("payout_item_id"),
                                    "from": row["transaction_status"], "to": status})
                db.execute("UPDATE payout_items SET payout_item_id = ?, transaction_id = ?, transaction_status = ?, "
                           "fee_cents = ?, errors_json = ?, updated_at = ? WHERE sender_item_id = ?",
                           (it.get("payout_item_id"), it.get("transaction_id"), status,
                            cents_of(fee) if fee is not None else None,
                            canonical(it["errors"]) if it.get("errors") else None, utc_now(), sid))
            if batch_status:
                db.execute("UPDATE payouts SET batch_status = ?, updated_at = ? WHERE id = ?",
                           (batch_status, utc_now(), payout_id))
            statuses = [r["transaction_status"] for r in db.execute(
                "SELECT transaction_status FROM payout_items WHERE payout_id = ? AND amount_cents > 0", (payout_id,))]
            new = (ATTENTION if any(s in BAD_ITEM for s in statuses) else PAID_OUT if statuses and all(
                s == "SUCCESS" for s in statuses) else UNCLAIMED if statuses and all(
                s in ("SUCCESS", "UNCLAIMED") for s in statuses) else PAYOUT_PENDING)
            if changes or (pact["status"] in PAYOUT_STATES and new != pact["status"]):
                if pact["status"] in PAYOUT_STATES:
                    self._move(db, pact, new, "system", "paypal.payout_items",
                               {"batch_status": batch_status, "changes": changes, "mode": self.paypal.mode})
                else:
                    self.store.append_event(db, pact_id, "system", "paypal.payout_items",
                                            {"batch_status": batch_status, "changes": changes, "mode": self.paypal.mode})

    # --- recovery ------------------------------------------------------------------------------------------------
    def reconcile(self, user: dict[str, Any] | None, pact_id: str) -> dict[str, Any]:
        with self.store.read() as db:
            self._guard(db, user, pact_id)
        self._reconcile(pact_id)
        return self.view(user, pact_id)

    def _reconcile(self, pact_id: str) -> None:
        """Bring a pact in line with PayPal's record after a crash, a timeout or a missed webhook. Safe to repeat."""
        with self.store.pact_lock(pact_id):
            with self.store.read() as db:
                pact = self._pact(db, pact_id)
                rel = db.execute("SELECT * FROM releases WHERE pact_id = ? AND status IN ('APPROVED', 'PENDING', "
                                 "'CAPTURED') ORDER BY seq DESC LIMIT 1", (pact_id,)).fetchone()
            status = pact["status"]
            if status == AWAITING and pact["order_id"]:
                order = self._paypal(lambda: self.paypal.get_order(pact["order_id"], pact_id=pact_id))
                if order.get("status") != pact["order_status"]:
                    with self.store.tx() as db:
                        db.execute("UPDATE pacts SET order_status = ? WHERE id = ?", (order.get("status"), pact_id))
                        self.store.append_event(db, pact_id, "system", "paypal.order_status",
                                                {"order_id": pact["order_id"], "paypal_status": order.get("status")})
            elif status == CAPTURING and rel:
                self._capture(pact_id, rel["id"], recovering=True)
            elif status == CAPTURE_PENDING and rel and rel["capture_id"]:
                cap = self._paypal(lambda: self.paypal.get_capture(rel["capture_id"], pact_id=pact_id))
                if cap.get("status") != "PENDING":
                    self._record_capture(pact_id, rel["id"], cap)
            elif status == CAPTURED and rel:
                self._payout(pact_id, rel["id"])
            elif status in PAYOUT_STATES:
                self.refresh_payout(pact_id)
            elif status in (HELD, SUBMITTED, AUDITED, PROPOSED, DISPUTED) and pact["authorization_id"]:
                auth = self._paypal(lambda: self.paypal.get_authorization(pact["authorization_id"], pact_id=pact_id))
                if auth.get("status") == "VOIDED":
                    with self.store.tx() as db:
                        pact = self._pact(db, pact_id)
                        self._move(db, pact, VOIDED, "system", "paypal.authorization_voided",
                                   {"authorization_id": pact["authorization_id"], "paypal_status": "VOIDED",
                                    "reason": "PayPal's record shows the authorization voided (expired or cancelled)",
                                    "mode": self.paypal.mode}, authorization_status="VOIDED")

    def recover_all(self) -> list[str]:
        """At startup: every pact with money in flight is reconciled with PayPal's record."""
        with self.store.read() as db:
            ids = [r["id"] for r in db.execute(
                "SELECT id FROM pacts WHERE status IN (?, ?, ?, ?, ?)",
                (CAPTURING, CAPTURE_PENDING, CAPTURED, PAYOUT_PENDING, UNCLAIMED))]
        done = []
        for pid in ids:
            try:
                self._reconcile(pid)
                done.append(pid)
            except (ApiError, PayPalError):
                continue
        return done

    # --- webhooks ------------------------------------------------------------------------------------------------
    def webhook(self, headers: dict[str, str], raw: bytes) -> tuple[int, dict[str, Any]]:
        verdict = self.paypal.verify_webhook(headers, raw)
        if not verdict["verified"]:
            return 400, {"received": False, "verification_status": verdict["verification_status"],
                         "reason": verdict["reason"]}
        event = json.loads(raw)
        eid, etype = str(event.get("id") or ""), str(event.get("event_type") or "")
        resource = event.get("resource") if isinstance(event.get("resource"), dict) else {}
        rtype = str(event.get("resource_type") or "")
        if not eid:
            return 400, {"received": False, "verification_status": "SUCCESS", "reason": "the event has no id"}
        with self.store.tx() as db:
            if db.execute("SELECT 1 FROM webhook_events WHERE event_id = ?", (eid,)).fetchone():
                return 200, {"received": True, "verification_status": "SUCCESS", "duplicate": True, "event_id": eid}
            db.execute("INSERT INTO webhook_events (event_id, event_type, resource_type, resource_id, verification, "
                       "received_at) VALUES (?, ?, ?, ?, 'SUCCESS', ?)",
                       (eid, etype, rtype, str(resource.get("id") or resource.get("payout_item_id") or ""), utc_now()))
        result = self._apply_event(etype, resource)
        with self.store.tx() as db:
            db.execute("UPDATE webhook_events SET result_json = ? WHERE event_id = ?", (canonical(result), eid))
        return 200, {"received": True, "verification_status": "SUCCESS", "event_id": eid, "event_type": etype,
                     "result": result}

    def _apply_event(self, etype: str, resource: dict[str, Any]) -> dict[str, Any]:
        """A verified event, applied from PayPal's current record of its resource (the payload is only a pointer)."""
        with self.store.read() as db:
            if etype.startswith("PAYMENT.CAPTURE."):
                row = db.execute("SELECT pact_id FROM releases WHERE capture_id = ?", (resource.get("id"),)).fetchone()
            elif etype.startswith("PAYMENT.AUTHORIZATION."):
                row = db.execute("SELECT id AS pact_id FROM pacts WHERE authorization_id = ?",
                                 (resource.get("id"),)).fetchone()
            elif etype.startswith("PAYMENT.PAYOUTS-ITEM.") or etype.startswith("PAYMENT.PAYOUTSBATCH."):
                header = resource.get("batch_header") or {}
                bid = resource.get("payout_batch_id") or header.get("payout_batch_id")
                row = db.execute("SELECT pact_id FROM payouts WHERE payout_batch_id = ?", (bid,)).fetchone()
                if not row and bid:  # a batch whose create answer was lost: match it by PactPay's own ids
                    sbid = (header.get("sender_batch_header") or {}).get("sender_batch_id")
                    sid = (resource.get("payout_item") or {}).get("sender_item_id")
                    row = db.execute("SELECT p.pact_id, p.id FROM payouts p WHERE p.payout_batch_id IS NULL AND "
                                     "(p.sender_batch_id = ? OR p.id IN (SELECT payout_id FROM payout_items WHERE "
                                     "sender_item_id = ?))", (sbid, sid)).fetchone()
                    if row:
                        return self._adopt_batch(row["pact_id"], row["id"], bid)
            elif etype.startswith("CHECKOUT.ORDER."):
                row = db.execute("SELECT id AS pact_id FROM pacts WHERE order_id = ?", (resource.get("id"),)).fetchone()
            else:
                return {"applied": False, "reason": "event type not used by YAVQEN"}
        if not row:
            return {"applied": False, "reason": "the resource is not one of YAVQEN's"}
        try:
            self._reconcile(row["pact_id"])
        except (ApiError, PayPalError) as exc:
            return {"applied": False, "pact_id": row["pact_id"], "reason": f"reconcile failed: {exc}"}
        with self.store.read() as db:
            status = self._pact(db, row["pact_id"])["status"]
        return {"applied": True, "pact_id": row["pact_id"], "status": status}

    # --- PayPal errors -------------------------------------------------------------------------------------------
    def _paypal(self, fn: Any) -> dict[str, Any]:
        try:
            return fn()
        except PayPalError as exc:
            raise self._paypal_error(exc) from exc

    @staticmethod
    def _paypal_error(exc: PayPalError) -> ApiError:
        if exc.issue in ("ORDER_NOT_APPROVED", "PAYER_ACTION_REQUIRED"):
            return ApiError(409, "buyer_not_approved", "The buyer has not approved the order in PayPal yet.",
                            paypal=exc.to_dict())
        return ApiError(502, "paypal_error", f"PayPal answered {exc.name}" + (f" ({exc.issue})" if exc.issue else ""),
                        paypal=exc.to_dict())

    # --- views ---------------------------------------------------------------------------------------------------
    def list_pacts(self, user: dict[str, Any] | None) -> list[dict[str, Any]]:
        if user is None:
            raise ApiError(401, "unauthenticated", "Sign in: send 'Authorization: Bearer <token>'.")
        with self.store.read() as db:
            ids = [r["pact_id"] for r in db.execute(
                "SELECT m.pact_id FROM members m JOIN pacts p ON p.id = m.pact_id WHERE m.user_id = ? "
                "ORDER BY p.created_at DESC", (user["id"],))]
        return [self.view(user, i) for i in dict.fromkeys(ids)]

    def view(self, user: dict[str, Any] | None, pact_id: str) -> dict[str, Any]:
        with self.store.read() as db:
            pact, role = self._guard(db, user, pact_id)
            roles = self._roles(db, pact_id)
            names = {r["id"]: r["name"] for r in db.execute(
                f"SELECT id, name FROM users WHERE id IN ({','.join('?' * len(roles))})", tuple(roles.values()))}
            approvals = [dict(r) for r in db.execute("SELECT role, user_id, terms_sha256, approved_at FROM approvals "
                                                     "WHERE pact_id = ? ORDER BY approved_at", (pact_id,))]
            subs = [dict(r) for r in db.execute("SELECT id, filename, sha256, created_at, length(content) AS chars "
                                                "FROM submissions WHERE pact_id = ? ORDER BY created_at, rowid",
                                                (pact_id,))]
            evs = db.execute("SELECT * FROM evaluations WHERE pact_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                             (pact_id,)).fetchone()
            rels = [dict(r) for r in db.execute("SELECT * FROM releases WHERE pact_id = ? ORDER BY seq", (pact_id,))]
            payout = db.execute("SELECT * FROM payouts WHERE pact_id = ?", (pact_id,)).fetchone()
            items = [dict(r) for r in db.execute("SELECT * FROM payout_items WHERE payout_id = ? ORDER BY sender_item_id",
                                                 (payout["id"],))] if payout else []
        terms = json.loads(pact["terms_json"])
        mine = [r for r, uid in roles.items() if uid == user["id"]]
        for r in rels:
            r["error"] = json.loads(r.pop("error_json")) if r.get("error_json") else None
        for it in items:
            it["errors"] = json.loads(it.pop("errors_json")) if it.get("errors_json") else None
        return {
            "pact_id": pact_id, "status": pact["status"], "version": pact["version"], "my_roles": mine,
            "parties": {r: {"user_id": uid, "name": names.get(uid)} for r, uid in roles.items()},
            "terms": terms, "terms_sha256": pact["terms_sha256"], "locked_terms_sha256": pact["locked_terms_sha256"],
            "approvals": approvals, "drafted_by": pact["compiled_by"],
            "paypal": {"mode": pact["paypal_mode"], "order_id": pact["order_id"], "order_status": pact["order_status"],
                       "approve_url": pact["approve_url"], "authorization_id": pact["authorization_id"],
                       "authorization_status": pact["authorization_status"], "authorized_cents": pact["authorized_cents"],
                       "authorization_expires_at": pact["authorization_expires_at"]},
            "submissions": subs,
            "evaluation": ({"evaluation_id": evs["id"], "submission_id": evs["submission_id"],
                            "terms_sha256": evs["terms_sha256"], "score_pct": evs["score_pct"],
                            "recommended_cents": evs["recommended_cents"], "created_at": evs["created_at"],
                            "report": json.loads(evs["report_json"])} if evs else None),
            "releases": rels,
            "payout": ({**dict(payout), "error": json.loads(payout["error_json"]) if payout["error_json"] else None,
                        "items": items} if payout else None),
            "next_actions": self.next_actions(pact["status"], mine, roles, self.paypal.simulator is not None),
            "created_at": pact["created_at"], "updated_at": pact["updated_at"],
        }

    @staticmethod
    def next_actions(status: str, mine: list[str], roles: dict[str, str], simulated: bool) -> list[str]:
        acts: list[str] = []
        if status == DRAFT and set(mine) & {"client", "freelancer"}:
            acts += ["edit_terms", "approve_terms"]
        if "client" in mine:
            acts += {TERMS_APPROVED: ["create_order"],
                     AWAITING: (["simulate_buyer_approval"] if simulated else ["approve_in_paypal"]) + ["authorize"],
                     HELD: ["void"], AUDITED: ["release", "dispute"]}.get(status, [])
        if "freelancer" in mine:
            acts += {HELD: ["submit"], SUBMITTED: ["submit"], AUDITED: ["submit", "dispute"],
                     PROPOSED: ["accept_release", "dispute"]}.get(status, [])
        if "arbiter" in mine and status == DISPUTED:
            acts.append("resolve")
        if status == SUBMITTED:
            acts.append("evaluate")
        if status in (AWAITING, CAPTURING, CAPTURE_PENDING, CAPTURED, *PAYOUT_STATES):
            acts.append("reconcile")
        return list(dict.fromkeys(acts))

    # --- evidence ------------------------------------------------------------------------------------------------
    def evidence(self, user: dict[str, Any] | None, pact_id: str) -> dict[str, Any]:
        """Each requirement linked to its test, the source lines and AI explanation, the human decision and the
        PayPal records it led to, plus the hash chain that makes the whole record tamper-evident."""
        view = self.view(user, pact_id)
        events = self.store.events(pact_id)
        chain = Store.verify_chain(events, pact_id)
        report = (view["evaluation"] or {}).get("report") or {}
        results = {c.get("id"): c for c in report.get("criteria", [])}
        rel = next((r for r in reversed(view["releases"]) if r["status"] not in ("SUPERSEDED",)), None)
        requirements = []
        for c in view["terms"]["criteria"]:
            r = results.get(c["id"], {})
            requirements.append({
                "criterion": {k: c.get(k) for k in ("id", "title", "description", "weight_pct", "method",
                                                    "source_quote", "tests_origin")},
                "acceptance_tests_sha256": sha256_text(c["tests"]) if c.get("tests") else None,
                "tests": r.get("tests"), "tests_passed": r.get("tests_passed"), "tests_total": r.get("tests_total"),
                "status": r.get("status"), "earned_pct": r.get("earned_pct"), "ai": r.get("ai"),
                "evidence": r.get("evidence"),
            })
        approvals = [e for e in events if e["kind"] in ("terms.approved", "terms.locked")]
        decisions = [e for e in events if e["kind"].startswith(("release.", "dispute."))]
        paypal = [e for e in events if e["kind"].startswith("paypal.")]
        return {
            "pact_id": pact_id, "generated_at": utc_now(), "status": view["status"],
            "terms_sha256": view["locked_terms_sha256"], "terms": view["terms"],
            "submission": view["submissions"][-1] if view["submissions"] else None,
            "evaluation": {k: (view["evaluation"] or {}).get(k) for k in ("evaluation_id", "score_pct",
                                                                         "recommended_cents", "created_at")},
            "evaluation_meta": {k: report.get(k) for k in ("sandbox", "flags", "reviewer", "engine_version",
                                                         "submission_sha256")},
            "requirements": requirements, "approvals": approvals, "decisions": decisions,
            "release": rel, "payout": view["payout"], "paypal_records": paypal,
            "paypal_mode": view["paypal"]["mode"], "chain": chain, "events": events,
        }
