"""Durable state for PactPay: one SQLite file (PACTPAY_DB_PATH), opened per unit of work.

Everything that decides money lives here, so a restart never forgets a payment: pacts and their locked terms,
memberships, approvals, submissions, evaluations, releases, payouts and payout items, the idempotency ledger, verified
webhook deliveries, PayPal call traces, and an append-only, hash-chained event log per pact.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    token_sha256 TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pacts (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    raw_chat TEXT NOT NULL,
    terms_json TEXT NOT NULL,
    terms_sha256 TEXT NOT NULL,
    locked_terms_sha256 TEXT,
    compiled_by TEXT NOT NULL,
    paypal_mode TEXT NOT NULL,
    order_id TEXT,
    order_status TEXT,
    order_attempt INTEGER NOT NULL DEFAULT 0,
    approve_url TEXT,
    authorization_id TEXT,
    authorization_status TEXT,
    authorized_cents INTEGER,
    authorization_expires_at TEXT
);
CREATE TABLE IF NOT EXISTS members (
    pact_id TEXT NOT NULL REFERENCES pacts(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    role TEXT NOT NULL CHECK (role IN ('client', 'freelancer', 'arbiter')),
    joined_at TEXT NOT NULL,
    PRIMARY KEY (pact_id, role)
);
CREATE TABLE IF NOT EXISTS invites (
    code_sha256 TEXT PRIMARY KEY,
    pact_id TEXT NOT NULL REFERENCES pacts(id),
    role TEXT NOT NULL,
    used_by TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    pact_id TEXT NOT NULL REFERENCES pacts(id),
    role TEXT NOT NULL,
    user_id TEXT NOT NULL,
    terms_sha256 TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    PRIMARY KEY (pact_id, role, terms_sha256)
);
CREATE TABLE IF NOT EXISTS submissions (
    id TEXT PRIMARY KEY,
    pact_id TEXT NOT NULL REFERENCES pacts(id),
    user_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    content TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evaluations (
    id TEXT PRIMARY KEY,
    pact_id TEXT NOT NULL REFERENCES pacts(id),
    submission_id TEXT NOT NULL REFERENCES submissions(id),
    terms_sha256 TEXT NOT NULL,
    score_pct REAL NOT NULL,
    recommended_cents INTEGER NOT NULL,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS releases (
    id TEXT PRIMARY KEY,
    pact_id TEXT NOT NULL REFERENCES pacts(id),
    seq INTEGER NOT NULL,
    evaluation_id TEXT NOT NULL REFERENCES evaluations(id),
    decided_by TEXT NOT NULL,
    decided_role TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    recommended_cents INTEGER NOT NULL,
    reason TEXT,
    status TEXT NOT NULL,
    capture_request_id TEXT NOT NULL,
    capture_id TEXT,
    capture_status TEXT,
    captured_cents INTEGER,
    net_cents INTEGER,
    fee_cents INTEGER,
    error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (pact_id, seq)
);
CREATE TABLE IF NOT EXISTS payouts (
    id TEXT PRIMARY KEY,
    pact_id TEXT NOT NULL REFERENCES pacts(id),
    release_id TEXT NOT NULL UNIQUE REFERENCES releases(id),
    sender_batch_id TEXT NOT NULL UNIQUE,
    payout_batch_id TEXT,
    batch_status TEXT,
    error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payout_items (
    sender_item_id TEXT PRIMARY KEY,
    payout_id TEXT NOT NULL REFERENCES payouts(id),
    payee_id TEXT NOT NULL,
    receiver TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    payout_item_id TEXT,
    transaction_id TEXT,
    transaction_status TEXT,
    fee_cents INTEGER,
    errors_json TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency (
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('in_progress', 'done')),
    status_code INTEGER,
    response_json TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);
CREATE TABLE IF NOT EXISTS webhook_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    resource_type TEXT,
    resource_id TEXT,
    verification TEXT NOT NULL,
    received_at TEXT NOT NULL,
    result_json TEXT
);
CREATE TABLE IF NOT EXISTS traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pact_id TEXT,
    at TEXT NOT NULL,
    step TEXT NOT NULL,
    method TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    mode TEXT NOT NULL,
    paypal_request_id TEXT,
    paypal_debug_id TEXT,
    duration_ms REAL NOT NULL,
    request_json TEXT NOT NULL,
    response_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    pact_id TEXT NOT NULL,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    kind TEXT NOT NULL,
    data_json TEXT NOT NULL,
    prev_sha256 TEXT NOT NULL,
    sha256 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_pact ON events (pact_id, seq);
CREATE INDEX IF NOT EXISTS traces_by_pact ON traces (pact_id, id);
"""

GENESIS = "0" * 64


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


def canonical(obj: Any) -> str:
    """The one JSON spelling of `obj` that hashes are taken over (sorted keys, no spaces)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def event_hash(prev: str, pact_id: str, at: str, actor: str, kind: str, data_json: str) -> str:
    return sha256_text(canonical([prev, pact_id, at, actor, kind, data_json]))


class Store:
    """SQLite in WAL mode; one connection per unit of work, so threads never share one."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._keeper: sqlite3.Connection | None = None
        if self.path == ":memory:":
            # one private in-memory database shared by this store's connections (plain ":memory:" would give every
            # connection its own empty database); the keeper connection keeps it alive for the store's lifetime
            self.path = f"file:pactpay-mem-{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._keeper = sqlite3.connect(self.path, uri=True, check_same_thread=False)
        else:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        db = self._connect()
        try:
            db.executescript(SCHEMA)  # executescript manages its own transaction
        finally:
            db.close()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10.0, isolation_level=None, check_same_thread=False,
                             uri=self.path.startswith("file:"))
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """A write transaction taken at once (BEGIN IMMEDIATE), committed on success, rolled back on error."""
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            yield db
        finally:
            db.close()

    def pact_lock(self, pact_id: str) -> threading.RLock:
        """One lock per pact: money steps of a pact run one at a time in this process (the version column guards
        across processes)."""
        with self._locks_guard:
            return self._locks.setdefault(pact_id, threading.RLock())

    # --- the event log -------------------------------------------------------------------------------------------
    @staticmethod
    def append_event(db: sqlite3.Connection, pact_id: str, actor: str, kind: str, data: dict[str, Any]) -> str:
        row = db.execute("SELECT sha256 FROM events WHERE pact_id = ? ORDER BY seq DESC LIMIT 1", (pact_id,)).fetchone()
        prev = row["sha256"] if row else GENESIS
        at = utc_now()
        data_json = canonical(data)
        digest = event_hash(prev, pact_id, at, actor, kind, data_json)
        db.execute(
            "INSERT INTO events (pact_id, at, actor, kind, data_json, prev_sha256, sha256) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pact_id, at, actor, kind, data_json, prev, digest),
        )
        return digest

    def events(self, pact_id: str) -> list[dict[str, Any]]:
        with self.read() as db:
            rows = db.execute("SELECT * FROM events WHERE pact_id = ? ORDER BY seq", (pact_id,)).fetchall()
        return [
            {"seq": r["seq"], "at": r["at"], "actor": r["actor"], "kind": r["kind"], "data": json.loads(r["data_json"]),
             "prev_sha256": r["prev_sha256"], "sha256": r["sha256"]}
            for r in rows
        ]

    @staticmethod
    def verify_chain(events: list[dict[str, Any]], pact_id: str) -> dict[str, Any]:
        """Recompute every link of a pact's event chain: {valid, events, broken_at}."""
        prev = GENESIS
        for ev in events:
            expect = event_hash(prev, pact_id, ev["at"], ev["actor"], ev["kind"], canonical(ev["data"]))
            if ev["prev_sha256"] != prev or ev["sha256"] != expect:
                return {"valid": False, "events": len(events), "broken_at": ev["seq"]}
            prev = ev["sha256"]
        return {"valid": True, "events": len(events), "broken_at": None, "head_sha256": prev}

    # --- traces --------------------------------------------------------------------------------------------------
    def add_trace(self, **t: Any) -> None:
        with self.tx() as db:
            db.execute(
                "INSERT INTO traces (pact_id, at, step, method, endpoint, status_code, mode, paypal_request_id, "
                "paypal_debug_id, duration_ms, request_json, response_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (t.get("pact_id"), utc_now(), t["step"], t["method"], t["endpoint"], t["status_code"], t["mode"],
                 t.get("paypal_request_id"), t.get("paypal_debug_id"), t["duration_ms"],
                 canonical(t.get("request") or {}), canonical(t.get("response") or {})),
            )

    def traces(self, pact_id: str) -> list[dict[str, Any]]:
        with self.read() as db:
            rows = db.execute("SELECT * FROM traces WHERE pact_id = ? ORDER BY id", (pact_id,)).fetchall()
        return [
            {"id": r["id"], "at": r["at"], "step": r["step"], "method": r["method"], "endpoint": r["endpoint"],
             "status_code": r["status_code"], "mode": r["mode"], "paypal_request_id": r["paypal_request_id"],
             "paypal_debug_id": r["paypal_debug_id"], "duration_ms": r["duration_ms"],
             "request": json.loads(r["request_json"]), "response": json.loads(r["response_json"])}
            for r in rows
        ]
