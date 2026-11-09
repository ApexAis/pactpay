"""The five regression gates from the independent QA run of 2026-11-07, re-written against this API.

On the packaged snapshot (commit a195a9d) all five failed: a settlement without an audit answered HTTP 200, a repeat
settlement made a second payout batch, an unauthenticated caller changed a payout recipient, a forged webhook was
reported as verified, and a marker-only fake implementation scored 100/100 with a $500 capture recommended.
Each test below asserts the opposite and also checks that no money call reached PayPal.
"""

from __future__ import annotations

import json

import httpx

from app.config import Settings
from app.paypal_client import PayPalClient
from conftest import make_env
from flowkit import calls, deliver, fund, get, start

ATTACKER = "attacker@example.net"
MARKER_ONLY_FAKE = '''MARKERS = "def verify_webhook_signature compare_digest sha256 def calculate_prorated_split captured_total_usd allocations def process_webhook_event_idempotent DUPLICATE_IGNORED idempotent_replay"
RUNBOOK_MARKDOWN = "PAYPAL_CLIENT_ID verify-webhook-signature"
def verify_webhook_signature(payload, signature, webhook_secret):
    return True
def calculate_prorated_split(authorized_amount_usd, audit_score, collaborator_weights):
    return {}
def process_webhook_event_idempotent(event, store):
    return "ok"
'''


def test_gate1_no_settlement_without_an_evaluation(env):
    s = start(env)
    fund(env, s)
    pid = s["pact_id"]
    assert env.client.post(f"/api/pacts/{pid}/settle", json={"mode": "FULL"}).status_code == 401
    r = env.client.post(f"/api/pacts/{pid}/settle", json={"mode": "FULL"}, headers=env.h(s["client"]))
    assert r.status_code == 409 and r.json()["error"] in ("invalid_state", "not_evaluated")
    env.client.post(f"/api/pacts/{pid}/submissions", json={"filename": "w.py", "content": "FINAL"},
                    headers=env.h(s["freelancer"]))
    r = env.client.post(f"/api/pacts/{pid}/settle", json={"mode": "FULL"}, headers=env.h(s["client"]))
    assert r.status_code == 409  # delivered but not evaluated: still no money
    assert get(env, s)["status"] == "SUBMITTED" and calls(env, "CAPTURE") == 0 and calls(env, "PAYOUT") == 0


def test_gate2_a_repeat_settlement_creates_no_second_payout(env):
    s = start(env)
    fund(env, s)
    deliver(env, s, "FINAL")
    pid = s["pact_id"]
    first = env.client.post(f"/api/pacts/{pid}/settle", json={"mode": "FULL"}, headers=env.h(s["client"], "k1"))
    assert first.status_code == 200
    batch = first.json()["payout"]["payout_batch_id"]
    assert batch
    for body in ({"mode": "FULL"}, {"mode": "custom", "custom_amount_usd": 10, "reason": "pay a bit more again"}):
        again = env.client.post(f"/api/pacts/{pid}/settle", json=body, headers=env.h(s["client"]))
        assert again.status_code == 409 and again.json()["error"] == "already_released"
    replay = env.client.post(f"/api/pacts/{pid}/settle", json={"mode": "FULL"}, headers=env.h(s["client"], "k1"))
    assert replay.status_code == 200 and replay.json()["payout"]["payout_batch_id"] == batch
    assert calls(env, "CAPTURE") == 1 and calls(env, "PAYOUT") == 1 and len(env.sim.batches) == 1
    assert get(env, s)["payout"]["payout_batch_id"] == batch


def test_gate3_payout_recipients_change_only_by_a_party_before_the_terms_are_locked(env):
    s = start(env)
    pid = s["pact_id"]
    rubric = get(env, s)["terms"]
    legacy = {"splits": [{**p, "paypal_email": ATTACKER, "split_pct": p["share_pct"]} for p in rubric["payees"]]}
    assert env.client.put(f"/api/pacts/{pid}/rubric", json=legacy).status_code == 401
    stranger = env.client.post("/api/users", json={"name": "Mallory"}).json()["token"]
    assert env.client.put(f"/api/pacts/{pid}/rubric", json=legacy, headers=env.h(stranger)).status_code == 403
    assert env.client.put(f"/api/pacts/{pid}/rubric", json=legacy, headers=env.h(s["arbiter"])).status_code == 403
    assert ATTACKER not in json.dumps(get(env, s)["terms"])
    fund(env, s)
    r = env.client.put(f"/api/pacts/{pid}/rubric", json=legacy, headers=env.h(s["client"]))
    assert r.status_code == 409 and r.json()["error"] == "terms_locked"
    r = env.client.put(f"/api/pacts/{pid}/terms", json={"payees": legacy["splits"]}, headers=env.h(s["freelancer"]))
    assert r.status_code == 409
    deliver(env, s, "FINAL")
    env.client.post(f"/api/pacts/{pid}/release", json={}, headers=env.h(s["client"]))
    receivers = {item["receiver"] for item in env.sim.items.values()}
    assert receivers == {"alex.dev.sandbox@example.com", "sam.docs.sandbox@example.com"}


def test_gate4_a_forged_webhook_is_never_reported_verified(env):
    s = start(env)
    fund(env, s)
    pid = s["pact_id"]
    forged = {"id": "WH-FORGED-1", "event_type": "PAYMENT.CAPTURE.COMPLETED", "resource_type": "capture",
              "resource": {"id": "FAKECAPTURE", "status": "COMPLETED", "amount": {"value": "500.00"}}}
    r = env.client.post("/api/webhooks/paypal", json=forged)
    assert r.status_code == 400 and r.json()["verification_status"] == "FAILURE" and r.json()["received"] is False
    headers = {"PAYPAL-AUTH-ALGO": "SHA256withRSA", "PAYPAL-CERT-URL": "https://api.sandbox.paypal.com/v1/x",
               "PAYPAL-TRANSMISSION-ID": "a3c0b1e0-0000-4000-8000-000000000000",
               "PAYPAL-TRANSMISSION-TIME": "2026-11-07T12:00:00Z", "PAYPAL-TRANSMISSION-SIG": "Zm9yZ2Vk"}
    r = env.client.post("/api/webhooks/paypal", content=json.dumps(forged), headers=headers)
    assert r.status_code == 400 and r.json()["verification_status"] == "FAILURE"
    with env.service.store.read() as db:
        assert db.execute("SELECT COUNT(*) FROM webhook_events").fetchone()[0] == 0
    assert get(env, s)["status"] == "FUNDS_HELD"


def test_gate4_live_mode_fails_closed_and_sends_paypal_the_raw_event():
    creds = Settings(paypal_client_id="test-client-id-0001", paypal_client_secret="test-secret-0001")
    no_id = PayPalClient(creds, transport=httpx.MockTransport(lambda req: httpx.Response(500)), live=True)
    headers = {"paypal-auth-algo": "SHA256withRSA", "paypal-cert-url": "https://api.paypal.com/c",
               "paypal-transmission-id": "t", "paypal-transmission-time": "2026-11-07T12:00:00Z",
               "paypal-transmission-sig": "s"}
    verdict = no_id.verify_webhook(headers, b'{"id": "WH-1"}')
    assert verdict["verified"] is False and "PAYPAL_WEBHOOK_ID" in verdict["reason"]
    seen: list[bytes] = []

    def paypal(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "A21-test", "expires_in": 3600})
        seen.append(req.content)
        return httpx.Response(200, json={"verification_status": "FAILURE"})

    configured = Settings(paypal_client_id="test-client-id-0001", paypal_client_secret="test-secret-0001",
                          paypal_webhook_id="WH-ID-1")
    client = PayPalClient(configured, transport=httpx.MockTransport(paypal), live=True)
    raw = b'{"id":"WH-1",  "event_type":"PAYMENT.CAPTURE.COMPLETED","resource":{"id":"X"}}'
    verdict = client.verify_webhook(headers, raw)
    assert verdict == {"verified": False, "verification_status": "FAILURE",
                       "reason": "PayPal did not verify the signature"}
    assert raw.decode() in seen[0].decode() and json.loads(seen[0])["webhook_id"] == "WH-ID-1"


def test_gate5_a_marker_only_fake_earns_no_money(tmp_path):
    from app.evaluation import evaluate

    env = make_env(tmp_path, evaluate=evaluate)
    s = start(env)
    fund(env, s)
    pact = deliver(env, s, MARKER_ONLY_FAKE, "fake.py")
    ev = pact["evaluation"]
    assert ev["score_pct"] < 100 and ev["recommended_cents"] < 50000
    assert ev["score_pct"] == 0 and ev["recommended_cents"] == 0
    assert {c["status"] for c in ev["report"]["criteria"]} == {"FAIL"}
    r = env.client.post(f"/api/pacts/{s['pact_id']}/settle", json={"mode": "recommended"}, headers=env.h(s["client"]))
    assert r.status_code == 422 and calls(env, "CAPTURE") == 0


def test_gate5_the_same_evaluator_still_pays_real_work(tmp_path):
    from app.evaluation import evaluate
    from app.presets import get_preset

    env = make_env(tmp_path, evaluate=evaluate)
    preset = get_preset("paypal_webhook_sprint")
    s = start(env)
    fund(env, s)
    flawed = deliver(env, s, preset["flawed_submission"]["content"], preset["flawed_submission"]["filename"])
    assert flawed["evaluation"]["score_pct"] == 75.0 and flawed["evaluation"]["recommended_cents"] == 37500
    fixed = deliver(env, s, preset["fixed_submission"]["content"], preset["fixed_submission"]["filename"])
    assert fixed["evaluation"]["score_pct"] == 100.0 and fixed["evaluation"]["recommended_cents"] == 50000
    assert fixed["evaluation"]["terms_sha256"] == get(env, s)["locked_terms_sha256"]
