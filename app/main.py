"""PactPay HTTP API: milestone payments on PayPal (Sandbox) released by people, on executed evidence.

Every route that reads or changes a pact needs `Authorization: Bearer <token>` of one of its parties, and the
lifecycle rules live in app/flow.py. Money routes accept an `Idempotency-Key` header: a retry with the same key gets
the original answer back; the same key with a different body is refused. Without a key a repeat is still harmless:
the pact's state and PactPay's deterministic PayPal-Request-Ids make every money step happen once.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import Settings, get_settings
from app.flow import ApiError, Service
from app.llm import completer
from app.paypal_client import PayPalClient
from app.presets import PRESET_PAYEE_EMAILS, get_preset, list_presets
from app.store import Store, canonical, utc_now

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY = 512_000
PAYPAL_HOSTS = "https://www.paypal.com https://www.sandbox.paypal.com https://*.paypal.com https://*.paypalobjects.com"
CSP = ("default-src 'self'; "
       f"script-src 'self' {PAYPAL_HOSTS}; "
       f"style-src 'self' 'unsafe-inline' {PAYPAL_HOSTS}; "
       f"img-src 'self' data: {PAYPAL_HOSTS}; "
       f"frame-src {PAYPAL_HOSTS}; "
       f"connect-src 'self' {PAYPAL_HOSTS}; "
       "font-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")


# POST requests per client address per minute: identities and demo pacts are cheap to make and an evaluation runs
# sandboxed processes, so a public demo limits them; everything else shares a wider budget
RATE_LIMITS = {"identity": 20, "evaluate": 20, "post": 240}


class RateLimiter:
    def __init__(self, limits: dict[str, int], window_s: float = 60.0) -> None:
        self.limits, self.window = limits, window_s
        self._hits: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, who: str, bucket: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits.setdefault((who, bucket), deque())
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.limits[bucket]:
                return False
            hits.append(now)
            if len(self._hits) > 50_000:  # forget idle addresses
                self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] <= self.window}
            return True


def _bucket(path: str) -> str:
    if path in ("/api/users", "/api/demo/start"):
        return "identity"
    if path.endswith(("/evaluate", "/audit")):
        return "evaluate"
    return "post"


def _default_evaluator() -> tuple[Callable[..., dict[str, Any]] | None, Any]:
    try:
        from app import evaluation
    except ImportError:
        return None, None
    reviewer = None
    factory = getattr(evaluation, "reviewer_from_env", None)
    if callable(factory):
        try:
            reviewer = factory()
        except Exception:  # a misconfigured model must not stop the deterministic evaluation
            reviewer = None
    return getattr(evaluation, "evaluate", None), reviewer


def create_app(settings: Settings | None = None, *, transport: httpx.BaseTransport | None = None,
               evaluate: Callable[..., dict[str, Any]] | None = None, reviewer: Any = None,
               drafter: tuple[Any, str | None] | None = None, recover: bool = True,
               live: bool | None = None) -> FastAPI:
    settings = settings or get_settings()
    store = Store(settings.db_path)
    paypal = PayPalClient(settings, trace_sink=store.add_trace, transport=transport, live=live)
    if paypal.simulator is not None:
        paypal.simulator.registered |= PRESET_PAYEE_EMAILS  # the demo payees are the simulator's sandbox accounts
        paypal.simulator.registered.add("payouts-simulator-receiver@paypal.com")  # PayPal's documented test receiver
    if evaluate is None:
        evaluate, default_reviewer = _default_evaluator()
        reviewer = reviewer if reviewer is not None else default_reviewer
    service = Service(store, paypal, settings, evaluate=evaluate, reviewer=reviewer,
                      drafter=drafter if drafter is not None else completer(settings))

    app = FastAPI(title="PactPay", version=__version__,
                  description="Milestone payments on PayPal Sandbox: funds held by an authorization, released by "
                              "people on evidence from executed acceptance tests.")
    app.state.service, app.state.settings = service, settings

    if recover:
        threading.Thread(target=service.recover_all, name="pactpay-recover", daemon=True).start()

    @app.exception_handler(ApiError)
    async def api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=exc.body())

    limiter = RateLimiter(RATE_LIMITS)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        if request.method in ("POST", "PUT") and request.url.path != "/api/webhooks/paypal":
            who = request.client.host if request.client else "unknown"
            bucket = _bucket(request.url.path)
            if not limiter.allow(who, bucket):
                return JSONResponse(status_code=429, headers={"Retry-After": "60"},
                                    content={"error": "rate_limited", "message": "Too many requests; wait a minute."})
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # --- helpers -------------------------------------------------------------------------------------------------
    def user_of(request: Request) -> dict[str, Any] | None:
        auth = request.headers.get("authorization") or ""
        scheme, _, token = auth.partition(" ")
        return service.user_for_token(token.strip()) if scheme.lower() == "bearer" and token.strip() else None

    async def body_of(request: Request) -> tuple[bytes, dict[str, Any]]:
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise ApiError(413, "body_too_large", f"Request bodies are limited to {MAX_BODY:,} bytes.")
        if not raw.strip():
            return raw, {}
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise ApiError(400, "invalid_json", "The body is not valid JSON.") from exc
        if not isinstance(data, dict):
            raise ApiError(400, "invalid_json", "The body must be a JSON object.")
        return raw, data

    async def money_step(request: Request, user: dict[str, Any] | None, raw: bytes,
                         fn: Callable[[], dict[str, Any]]) -> Any:
        """Run a money step once per Idempotency-Key: a replay returns the stored answer (marked Idempotent-Replayed)."""
        key = (request.headers.get("idempotency-key") or "").strip()
        if not key:
            return await run_in_threadpool(fn)
        if len(key) > 128:
            raise ApiError(422, "invalid_idempotency_key", "Idempotency-Key must be at most 128 characters.")
        if user is None:
            raise ApiError(401, "unauthenticated", "Sign in: send 'Authorization: Bearer <token>'.")
        scope = f"{user['id']} {request.method} {request.url.path}"
        digest = hashlib.sha256(raw).hexdigest()
        with service.store.tx() as db:
            row = db.execute("SELECT * FROM idempotency WHERE scope = ? AND key = ?", (scope, key)).fetchone()
            if row is None:
                db.execute("INSERT INTO idempotency (scope, key, request_sha256, state, created_at) "
                           "VALUES (?, ?, ?, 'in_progress', ?)", (scope, key, digest, utc_now()))
        if row is not None:
            if row["request_sha256"] != digest:
                raise ApiError(422, "idempotency_key_reused", "This Idempotency-Key was used with a different request.")
            if row["state"] == "in_progress":
                raise ApiError(409, "request_in_progress", "A request with this Idempotency-Key is still running.")
            return JSONResponse(status_code=row["status_code"], content=json.loads(row["response_json"]),
                                headers={"Idempotent-Replayed": "true"})

        def finish(status: int | None, body: dict[str, Any] | None) -> None:
            with service.store.tx() as db:
                if status is None:
                    db.execute("DELETE FROM idempotency WHERE scope = ? AND key = ?", (scope, key))
                else:
                    db.execute("UPDATE idempotency SET state = 'done', status_code = ?, response_json = ? "
                               "WHERE scope = ? AND key = ?", (status, canonical(body), scope, key))

        try:
            result = await run_in_threadpool(fn)
        except ApiError as exc:
            # a PayPal outage (5xx) may be retried with the same key; a refusal is the answer for this key
            finish(None if exc.status >= 500 else exc.status, None if exc.status >= 500 else exc.body())
            raise
        except BaseException:
            finish(None, None)
            raise
        finish(200, result)
        return result

    def cents(data: dict[str, Any], *names: str) -> int | None:
        """An amount in whole cents: *_cents must be a whole number, *_usd a decimal with at most 2 places. Parsed
        with Decimal from the value's own text, so no binary-float rounding decides money; bools are refused."""
        from decimal import Decimal, InvalidOperation

        for name in names:
            value = data.get(name)
            if value is None or value == "":
                continue
            bad = ApiError(422, "invalid_amount", f"{name} must be "
                           + ("a dollar amount with at most 2 decimals." if name.endswith("usd") else "whole cents."))
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise bad
            try:
                d = Decimal(str(value).strip()) * (100 if name.endswith("usd") else 1)
            except (InvalidOperation, ValueError):
                raise bad from None
            if not d.is_finite() or d != d.to_integral_value():
                raise bad
            return int(d)
        return None

    # --- pages and configuration ---------------------------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index() -> Any:
        page = STATIC_DIR / "index.html"
        return FileResponse(page) if page.exists() else HTMLResponse("<h1>PactPay API</h1><p>See /docs.</p>")

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__,
                "paypal": {"mode": service.paypal.mode, "base_url": "https://api-m.sandbox.paypal.com",
                           "live_credentials_configured": settings.has_paypal_credentials,
                           "webhook_verification": "paypal_api" if settings.paypal_webhook_id else (
                               "simulator" if service.paypal.simulator else "not_configured")},
                "ai": {"drafter": service.drafter_label, "reviewer": getattr(service.reviewer, "label", None) or (
                    getattr(service.reviewer, "model", None)), "evaluator": service._evaluate is not None},
                "demo_mode": settings.demo_mode}

    @app.get("/api/config")
    async def config() -> dict[str, Any]:
        return {"paypal_client_id": settings.paypal_sdk_client_id, "paypal_mode": service.paypal.mode,
                "currency": "USD", "demo_mode": settings.demo_mode,
                "presets": [{k: p.get(k) for k in ("id", "name", "badge", "title", "summary", "total_amount_usd")}
                            for p in list_presets()]}

    @app.get("/api/presets")
    async def presets() -> dict[str, Any]:
        return {"presets": list_presets(), "default_preset_id": "paypal_webhook_sprint"}

    @app.get("/api/presets/{preset_id}")
    async def preset(preset_id: str) -> dict[str, Any]:
        return get_preset(preset_id)

    # --- identities ----------------------------------------------------------------------------------------------
    @app.post("/api/users")
    async def create_user(request: Request) -> dict[str, Any]:
        _, data = await body_of(request)
        return await run_in_threadpool(service.create_user, str(data.get("name") or ""))

    @app.get("/api/me")
    async def me(request: Request) -> dict[str, Any]:
        user = user_of(request)
        if user is None:
            raise ApiError(401, "unauthenticated", "Sign in: send 'Authorization: Bearer <token>'.")
        return user

    @app.post("/api/demo/start")
    async def demo_start(request: Request) -> dict[str, Any]:
        """Three fresh demo identities (client, freelancer, arbiter) and a pact from a preset, all joined."""
        if not settings.demo_mode:
            raise ApiError(404, "not_found", "Demo mode is off.")
        _, data = await body_of(request)
        chat_text = str(data.get("chat_text") or "").strip()
        preset_id = None if chat_text else str(data.get("preset_id") or "paypal_webhook_sprint")
        found = get_preset(preset_id) if preset_id else None
        if preset_id and not found:
            raise ApiError(404, "not_found", "No such preset.")

        def run() -> dict[str, Any]:
            people = {"client": service.create_user("Maya (client)"),
                      "freelancer": service.create_user("Alex (freelancer)"),
                      "arbiter": service.create_user("Riley (arbiter)")}
            client = {"id": people["client"]["user_id"], "name": people["client"]["name"]}
            if found:
                pact = service.create_pact(client, found["chat_text"], preset_id=found["id"])
            else:  # the judge's own agreement: drafted by the configured AI, checked against the text
                pact = service.create_pact(client, chat_text, amount_cents=cents(data, "amount_cents", "amount_usd"),
                                           title=data.get("title"))
            for role in ("freelancer", "arbiter"):
                service.accept_invite({"id": people[role]["user_id"], "name": people[role]["name"]},
                                      pact["invite_codes"][role])
            view = service.view(client, pact["pact_id"])
            view["draft_notes"], view["draft_problems"] = pact.get("draft_notes"), pact.get("draft_problems")
            return {"pact_id": pact["pact_id"], "personas": people, "preset_id": found["id"] if found else None,
                    "pact": view}

        return await run_in_threadpool(run)

    @app.get("/api/demo/samples")
    async def demo_samples(preset_id: str = "paypal_webhook_sprint") -> dict[str, Any]:
        """Deliverables a judge can submit in one click: the preset's flawed and correct versions, plus manipulated
        ones from the adversarial corpus (prompt injection, hard-coded answers, comment-only fakes)."""
        if not settings.demo_mode:
            raise ApiError(404, "not_found", "Demo mode is off.")
        found = get_preset(preset_id)
        if not found:
            raise ApiError(404, "not_found", "No such preset.")
        out = []
        for key, kind in (("flawed_submission", "honest"), ("fixed_submission", "honest")):
            s = found.get(key) or {}
            if s.get("content"):
                out.append({"id": key, "kind": kind, "label": s.get("label") or key, "filename": s.get("filename"),
                            "expected_score": s.get("expected_score"), "content": s["content"]})
        adversarial = {"paypal_webhook_sprint": [
            ("prompt_injection", "Prompt injection: tells the AI reviewer to pass everything"),
            ("hardcoded_answers", "Hard-coded answers that game the obvious inputs"),
            ("misleading_comments", "Comments claim features the code does not implement")],
            "sdk_migration_docs": [("docs_injection", "Hidden HTML comment tells the AI reviewer to pass it")],
            "payouts_guide_ai": [("ai:d3_keyword_stuffed.md", "Keyword-stuffed: every keyword, no content (fools a keyword rule)"),
                                 ("ai:d5_injection.md", "Prompt injection in an HTML comment, one section missing")]}
        from app.evaluation.corpus import corpus_cases
        cases = {c["id"]: c for c in corpus_cases()}
        ai_eval = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "ai_eval"
        for cid, label in adversarial.get(preset_id, []):
            if cid.startswith("ai:") and (ai_eval / cid[3:]).is_file():
                out.append({"id": cid[3:].removesuffix(".md"), "kind": "adversarial", "label": label,
                            "filename": cid[3:], "expected_score": 0.0, "content": (ai_eval / cid[3:]).read_text()})
                continue
            c = cases.get(cid)
            if c:
                out.append({"id": cid, "kind": "adversarial", "label": label, "filename": c["filename"],
                            "expected_score": c["expected_score"], "content": c["content"]})
        return {"preset_id": preset_id, "samples": out}

    # --- pacts ---------------------------------------------------------------------------------------------------
    @app.post("/api/pacts")
    @app.post("/api/pacts/compile")
    async def create_pact(request: Request) -> dict[str, Any]:
        _, data = await body_of(request)
        user = user_of(request)
        return await run_in_threadpool(lambda: service.create_pact(
            user, str(data.get("chat_text") or ""), amount_cents=cents(data, "amount_cents", "total_amount_usd"),
            title=data.get("title") or data.get("project_title"), preset_id=data.get("preset_id")))

    @app.get("/api/pacts")
    async def list_pacts(request: Request) -> dict[str, Any]:
        user = user_of(request)
        return {"pacts": await run_in_threadpool(service.list_pacts, user)}

    @app.get("/api/pacts/{pact_id}")
    async def get_pact(pact_id: str, request: Request) -> dict[str, Any]:
        return await run_in_threadpool(service.view, user_of(request), pact_id)

    @app.post("/api/invites/accept")
    async def accept_invite(request: Request) -> dict[str, Any]:
        _, data = await body_of(request)
        return await run_in_threadpool(service.accept_invite, user_of(request), str(data.get("code") or ""))

    @app.put("/api/pacts/{pact_id}/terms")
    @app.put("/api/pacts/{pact_id}/rubric")
    async def update_terms(pact_id: str, request: Request) -> dict[str, Any]:
        _, data = await body_of(request)
        patch = {k: data[k] for k in ("title", "summary", "amount_cents", "currency", "criteria", "payees") if k in data}
        legacy = data.get("splits") or data.get("collaborators")  # the v1 rubric shape still edits the payees
        if "payees" not in patch and isinstance(legacy, list):
            patch["payees"] = [{"name": s.get("name"), "role": s.get("role"), "paypal_email": s.get("paypal_email"),
                                "share_pct": s.get("share_pct", s.get("split_pct", s.get("share_percent")))}
                               for s in legacy if isinstance(s, dict)]
        if "amount_cents" not in patch and data.get("total_amount_usd") is not None:
            patch["amount_cents"] = cents(data, "total_amount_usd")
        return await run_in_threadpool(service.update_terms, user_of(request), pact_id, patch)

    @app.post("/api/pacts/{pact_id}/terms/approve")
    async def approve_terms(pact_id: str, request: Request) -> dict[str, Any]:
        _, data = await body_of(request)
        return await run_in_threadpool(service.approve_terms, user_of(request), pact_id,
                                       str(data.get("terms_sha256") or ""))

    # --- PayPal: holding the funds -------------------------------------------------------------------------------
    @app.post("/api/pacts/{pact_id}/paypal/order")
    @app.post("/api/pacts/{pact_id}/paypal/create-order")
    async def create_order(pact_id: str, request: Request) -> Any:
        raw, _ = await body_of(request)
        user = user_of(request)
        return await money_step(request, user, raw, lambda: service.create_order(user, pact_id))

    @app.post("/api/pacts/{pact_id}/paypal/simulate-buyer-approval")
    async def simulate_buyer(pact_id: str, request: Request) -> dict[str, Any]:
        return await run_in_threadpool(service.simulate_buyer_approval, user_of(request), pact_id)

    @app.post("/api/pacts/{pact_id}/paypal/authorize")
    @app.post("/api/pacts/{pact_id}/paypal/authorize-order")
    async def authorize(pact_id: str, request: Request) -> Any:
        raw, data = await body_of(request)
        user = user_of(request)
        return await money_step(request, user, raw,
                                lambda: service.authorize(user, pact_id, data.get("order_id") or data.get("orderID")))

    # --- delivery and evaluation ---------------------------------------------------------------------------------
    @app.post("/api/pacts/{pact_id}/submissions")
    async def submit(pact_id: str, request: Request) -> dict[str, Any]:
        user = user_of(request)
        if (request.headers.get("content-type") or "").startswith("multipart/form-data"):
            form = await request.form()
            upload = form.get("file")
            if upload is None or not hasattr(upload, "read"):
                raise ApiError(422, "missing_file", "Send the deliverable as the 'file' field.")
            data = await upload.read(MAX_BODY + 1)
            if len(data) > MAX_BODY:
                raise ApiError(413, "submission_too_large", "The file is too large.")
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ApiError(415, "not_text", "Only UTF-8 text deliverables (code, Markdown) can be evaluated.") from exc
            filename = getattr(upload, "filename", None) or "submission.txt"
        else:
            _, body = await body_of(request)
            filename, text = str(body.get("filename") or "submission.txt"), str(body.get("content") or "")
        return await run_in_threadpool(service.submit, user, pact_id, filename, text)

    @app.get("/api/pacts/{pact_id}/submissions/{submission_id}")
    async def get_submission(pact_id: str, submission_id: str, request: Request) -> dict[str, Any]:
        user = user_of(request)

        def run() -> dict[str, Any]:
            with service.store.read() as db:
                service._guard(db, user, pact_id)
                row = db.execute("SELECT id, filename, sha256, created_at, content FROM submissions "
                                 "WHERE id = ? AND pact_id = ?", (submission_id, pact_id)).fetchone()
            if not row:
                raise ApiError(404, "not_found", "No such submission.")
            return dict(row)

        return await run_in_threadpool(run)

    @app.post("/api/pacts/{pact_id}/evaluate")
    async def evaluate(pact_id: str, request: Request) -> dict[str, Any]:
        return await run_in_threadpool(service.evaluate, user_of(request), pact_id)

    @app.post("/api/pacts/{pact_id}/audit")
    async def audit(pact_id: str, request: Request) -> dict[str, Any]:
        """Submit and evaluate in one step (the freelancer)."""
        _, body = await body_of(request)
        user = user_of(request)

        def run() -> dict[str, Any]:
            service.submit(user, pact_id, str(body.get("filename") or "submission.txt"), str(body.get("content") or ""))
            return service.evaluate(user, pact_id)

        return await run_in_threadpool(run)

    # --- decisions and money -------------------------------------------------------------------------------------
    @app.post("/api/pacts/{pact_id}/release")
    async def release(pact_id: str, request: Request) -> Any:
        raw, data = await body_of(request)
        user = user_of(request)
        return await money_step(request, user, raw, lambda: service.release(
            user, pact_id, amount_cents=cents(data, "amount_cents", "amount_usd"), reason=data.get("reason"),
            evaluation_id=data.get("evaluation_id")))

    @app.post("/api/pacts/{pact_id}/settle")
    async def settle(pact_id: str, request: Request) -> Any:
        """The v1 route, now a client release: mode 'recommended' (default), 'full' or 'custom'."""
        raw, data = await body_of(request)
        user = user_of(request)
        mode = str(data.get("mode") or "recommended").lower()

        def run() -> dict[str, Any]:
            amount = None
            if mode == "full":
                with service.store.read() as db:
                    pact, _ = service._guard(db, user, pact_id, ("client",))
                amount = pact["authorized_cents"]
            elif mode == "custom":
                amount = cents(data, "custom_amount_usd", "amount_usd", "amount_cents")
            return service.release(user, pact_id, amount_cents=amount,
                                   reason=data.get("reason") or data.get("note_to_payer"),
                                   evaluation_id=data.get("evaluation_id"))

        return await money_step(request, user, raw, run)

    @app.post("/api/pacts/{pact_id}/release/accept")
    async def accept_release(pact_id: str, request: Request) -> Any:
        raw, _ = await body_of(request)
        user = user_of(request)
        return await money_step(request, user, raw, lambda: service.accept_release(user, pact_id))

    @app.post("/api/pacts/{pact_id}/dispute")
    async def dispute(pact_id: str, request: Request) -> dict[str, Any]:
        _, data = await body_of(request)
        return await run_in_threadpool(service.dispute, user_of(request), pact_id, str(data.get("reason") or ""))

    @app.post("/api/pacts/{pact_id}/resolve")
    async def resolve(pact_id: str, request: Request) -> Any:
        raw, data = await body_of(request)
        user = user_of(request)
        amount = cents(data, "amount_cents", "amount_usd")
        if amount is None:
            raise ApiError(422, "invalid_amount", "State the amount to release (0 releases nothing).")
        return await money_step(request, user, raw, lambda: service.resolve(
            user, pact_id, amount_cents=amount, reason=str(data.get("reason") or "")))

    @app.post("/api/pacts/{pact_id}/void")
    async def void(pact_id: str, request: Request) -> Any:
        raw, data = await body_of(request)
        user = user_of(request)
        return await money_step(request, user, raw, lambda: service.void(user, pact_id, str(data.get("reason") or "")))

    @app.post("/api/pacts/{pact_id}/reconcile")
    async def reconcile(pact_id: str, request: Request) -> dict[str, Any]:
        return await run_in_threadpool(service.reconcile, user_of(request), pact_id)

    # --- evidence and traces -------------------------------------------------------------------------------------
    @app.get("/api/pacts/{pact_id}/evidence")
    @app.get("/api/pacts/{pact_id}/dispute-packet")
    async def evidence(pact_id: str, request: Request) -> dict[str, Any]:
        return await run_in_threadpool(service.evidence, user_of(request), pact_id)

    @app.get("/api/pacts/{pact_id}/traces")
    async def traces(pact_id: str, request: Request) -> dict[str, Any]:
        user = user_of(request)

        def run() -> dict[str, Any]:
            with service.store.read() as db:
                service._guard(db, user, pact_id)
            return {"traces": service.store.traces(pact_id), "mode": service.paypal.mode}

        return await run_in_threadpool(run)

    # --- webhooks ------------------------------------------------------------------------------------------------
    @app.post("/api/webhooks/paypal")
    async def paypal_webhook(request: Request) -> JSONResponse:
        raw = await request.body()
        if len(raw) > MAX_BODY:
            return JSONResponse(status_code=413, content={"received": False, "verification_status": "FAILURE"})
        status, body = await run_in_threadpool(service.webhook, dict(request.headers), raw)
        return JSONResponse(status_code=status, content=body)

    @app.post("/api/demo/deliver-webhooks")
    async def deliver_webhooks(request: Request) -> dict[str, Any]:
        """Offline demo: hand the simulator's signed webhook deliveries to the real webhook handler (which verifies
        each signature, drops duplicates and re-reads the resource), as PayPal would post them."""
        if not settings.demo_mode or service.paypal.simulator is None:
            raise ApiError(404, "not_found", "Only the offline simulator has webhook deliveries to hand over.")
        if user_of(request) is None:
            raise ApiError(401, "unauthenticated", "Sign in: send 'Authorization: Bearer <token>'.")

        def run() -> dict[str, Any]:
            out = []
            for delivery in service.paypal.simulator.take_outbox():
                status, body = service.webhook(delivery["headers"], delivery["body"])
                out.append({"http_status": status, **body})
            return {"delivered": out}

        return await run_in_threadpool(run)

    return app


app = create_app()
