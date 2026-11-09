"""The milestone lifecycle end to end through the HTTP API, against the offline PayPal simulator (positive and
negative paths, idempotency, concurrency, recovery, webhooks and the evidence chain)."""

from __future__ import annotations

import json
import threading

from conftest import make_env
from flowkit import calls, deliver, fund, get, lock_terms, start


# --- happy paths ---------------------------------------------------------------------------------------------------
def test_full_release_captures_once_and_pays_each_payee_as_paypal_reports(env):
    s = start(env)
    assert s["pact"]["status"] == "DRAFT" and set(s["pact"]["parties"]) == {"client", "freelancer", "arbiter"}
    fund(env, s)
    deliver(env, s, "FINAL")
    r = env.client.post(f"/api/pacts/{s['pact_id']}/release", json={}, headers=env.h(s["client"]))
    assert r.status_code == 200, r.text
    pact = r.json()
    rel = pact["releases"][0]
    assert rel["amount_cents"] == 50000 and rel["capture_status"] == "COMPLETED" and rel["decided_role"] == "client"
    # simulated PayPal fee 3.49 % + 0.49 = 17.94; the payees share the 482.06 PayPal says was received, 70/30
    assert (rel["captured_cents"], rel["fee_cents"], rel["net_cents"]) == (50000, 1794, 48206)
    items = pact["payout"]["items"]
    assert [i["amount_cents"] for i in items] == [33744, 14462] and sum(i["amount_cents"] for i in items) == 48206
    assert pact["status"] == "PAYOUT_PENDING" and {i["transaction_status"] for i in items} == {"PENDING"}
    pact = env.client.post(f"/api/pacts/{s['pact_id']}/reconcile", headers=env.h(s["freelancer"])).json()
    assert pact["status"] == "PAID_OUT"
    assert [i["transaction_status"] for i in pact["payout"]["items"]] == ["SUCCESS", "SUCCESS"]
    assert all(i["payout_item_id"] and i["transaction_id"] for i in pact["payout"]["items"])
    assert calls(env, "CAPTURE") == 1 and calls(env, "PAYOUT") == 1


def test_partial_release_is_a_proposal_the_freelancer_accepts(env):
    s = start(env)
    fund(env, s)
    pact = deliver(env, s, "DRAFT")
    assert pact["evaluation"]["score_pct"] == 75.0 and pact["evaluation"]["recommended_cents"] == 37500
    pact = env.client.post(f"/api/pacts/{s['pact_id']}/release", json={}, headers=env.h(s["client"])).json()
    assert pact["status"] == "RELEASE_PROPOSED" and calls(env, "CAPTURE") == 0
    r = env.client.post(f"/api/pacts/{s['pact_id']}/release/accept", headers=env.h(s["client"]))
    assert r.status_code == 403
    pact = env.client.post(f"/api/pacts/{s['pact_id']}/release/accept", headers=env.h(s["freelancer"])).json()
    assert pact["releases"][0]["captured_cents"] == 37500 and pact["status"] == "PAYOUT_PENDING"
    auth = env.sim.authorizations[pact["paypal"]["authorization_id"]]
    assert auth["status"] == "PARTIALLY_CAPTURED" and auth["final"] is True  # the remaining $125 is not capturable


def test_overriding_the_recommendation_needs_a_reason_on_the_record(env):
    s = start(env)
    fund(env, s)
    deliver(env, s, "DRAFT")
    r = env.client.post(f"/api/pacts/{s['pact_id']}/release", json={"amount_usd": 500}, headers=env.h(s["client"]))
    assert r.status_code == 422 and r.json()["error"] == "reason_required"
    reason = "Runbook was delivered by e-mail; paying the full milestone."
    pact = env.client.post(f"/api/pacts/{s['pact_id']}/release", json={"amount_usd": 500, "reason": reason},
                           headers=env.h(s["client"])).json()
    assert pact["releases"][0]["reason"] == reason and pact["releases"][0]["recommended_cents"] == 37500
    ev = env.client.get(f"/api/pacts/{s['pact_id']}/evidence", headers=env.h(s["arbiter"])).json()
    assert any(d["data"].get("override_reason") == reason for d in ev["decisions"])


def test_dispute_goes_to_the_arbiter_whose_decision_is_captured(env):
    s = start(env)
    fund(env, s)
    deliver(env, s, "DRAFT")
    env.client.post(f"/api/pacts/{s['pact_id']}/release", json={}, headers=env.h(s["client"]))
    r = env.client.post(f"/api/pacts/{s['pact_id']}/dispute", json={"reason": "short"}, headers=env.h(s["freelancer"]))
    assert r.status_code == 422
    pact = env.client.post(f"/api/pacts/{s['pact_id']}/dispute",
                           json={"reason": "The idempotency handler exists in v2, already sent."},
                           headers=env.h(s["freelancer"])).json()
    assert pact["status"] == "DISPUTED"
    r = env.client.post(f"/api/pacts/{s['pact_id']}/resolve", json={"amount_usd": 450, "reason": "Agreed compromise."},
                        headers=env.h(s["client"]))
    assert r.status_code == 403
    pact = env.client.post(f"/api/pacts/{s['pact_id']}/resolve",
                           json={"amount_usd": 450, "reason": "Handler shipped late; 90 % is fair."},
                           headers=env.h(s["arbiter"])).json()
    rel = [x for x in pact["releases"] if x["status"] != "SUPERSEDED"][0]
    assert rel["decided_role"] == "arbiter" and rel["captured_cents"] == 45000 and calls(env, "CAPTURE") == 1


def test_an_arbiter_decision_of_nothing_voids_the_authorization(env):
    s = start(env)
    fund(env, s)
    deliver(env, s, "nothing useful")
    env.client.post(f"/api/pacts/{s['pact_id']}/dispute", json={"reason": "Nothing in the delivery works at all."},
                    headers=env.h(s["client"]))
    pact = env.client.post(f"/api/pacts/{s['pact_id']}/resolve",
                           json={"amount_usd": 0, "reason": "No criterion is met; the hold is released."},
                           headers=env.h(s["arbiter"])).json()
    assert pact["status"] == "VOIDED" and env.sim.authorizations[pact["paypal"]["authorization_id"]]["status"] == "VOIDED"
    assert calls(env, "CAPTURE") == 0


def test_the_client_can_void_before_delivery_but_not_after(env):
    s = start(env)
    fund(env, s)
    pact = env.client.post(f"/api/pacts/{s['pact_id']}/void", json={"reason": "project cancelled"},
                           headers=env.h(s["client"])).json()
    assert pact["status"] == "VOIDED"
    s2 = start(env)
    fund(env, s2)
    deliver(env, s2, "FINAL")
    r = env.client.post(f"/api/pacts/{s2['pact_id']}/void", json={}, headers=env.h(s2["client"]))
    assert r.status_code == 409


# --- terms and roles -----------------------------------------------------------------------------------------------
def test_terms_need_both_approvals_of_the_current_hash_and_then_never_change(env):
    s = start(env)
    pid, h = s["pact_id"], s["pact"]["terms_sha256"]
    r = env.client.post(f"/api/pacts/{pid}/terms/approve", json={"terms_sha256": "0" * 64}, headers=env.h(s["client"]))
    assert r.status_code == 409 and r.json()["error"] == "stale_terms"
    env.client.post(f"/api/pacts/{pid}/terms/approve", json={"terms_sha256": h}, headers=env.h(s["client"]))
    assert get(env, s)["status"] == "DRAFT"
    # an edit after one approval makes a new hash: the client's approval no longer counts
    terms = get(env, s)["terms"]
    terms["payees"][1]["share_pct"], terms["payees"][0]["share_pct"] = 40.0, 60.0
    pact = env.client.put(f"/api/pacts/{pid}/terms", json={"payees": terms["payees"]},
                          headers=env.h(s["freelancer"])).json()
    assert pact["terms_sha256"] != h
    env.client.post(f"/api/pacts/{pid}/terms/approve", json={"terms_sha256": pact["terms_sha256"]},
                    headers=env.h(s["freelancer"]))
    assert get(env, s)["status"] == "DRAFT"
    env.client.post(f"/api/pacts/{pid}/terms/approve", json={"terms_sha256": pact["terms_sha256"]},
                    headers=env.h(s["client"]))
    assert get(env, s)["status"] == "TERMS_APPROVED"
    r = env.client.put(f"/api/pacts/{pid}/terms", json={"amount_cents": 1000}, headers=env.h(s["client"]))
    assert r.status_code == 409 and r.json()["error"] == "terms_locked"


def test_invalid_terms_are_refused_with_every_problem(env):
    s = start(env)
    terms = get(env, s)["terms"]
    terms["criteria"][0]["weight_pct"] = 10
    terms["payees"][0]["paypal_email"] = "not-an-email"
    r = env.client.put(f"/api/pacts/{s['pact_id']}/terms", json={"criteria": terms["criteria"],
                                                                 "payees": terms["payees"]},
                       headers=env.h(s["client"]))
    assert r.status_code == 422
    problems = " ".join(r.json()["problems"])
    assert "weights must add up to 100" in problems and "valid PayPal e-mail" in problems


def test_only_parties_see_a_pact_and_only_in_their_role(env):
    s = start(env)
    stranger = env.client.post("/api/users", json={"name": "Mallory"}).json()["token"]
    assert env.client.get(f"/api/pacts/{s['pact_id']}").status_code == 401
    assert env.client.get(f"/api/pacts/{s['pact_id']}", headers=env.h(stranger)).status_code == 403
    assert env.client.get(f"/api/pacts/{s['pact_id']}", headers=env.h("pp_forged")).status_code == 401
    assert env.client.get("/api/pacts", headers=env.h(stranger)).json() == {"pacts": []}
    lock_terms(env, s)
    for who in ("freelancer", "arbiter"):
        r = env.client.post(f"/api/pacts/{s['pact_id']}/paypal/order", headers=env.h(s[who]))
        assert r.status_code == 403


def test_an_invite_is_used_once(env):
    client = env.client.post("/api/users", json={"name": "C"}).json()
    pact = env.client.post("/api/pacts", json={"chat_text": "Build `x()` for $100 to a@example.com"},
                           headers=env.h(client["token"])).json()
    f1 = env.client.post("/api/users", json={"name": "F1"}).json()["token"]
    f2 = env.client.post("/api/users", json={"name": "F2"}).json()["token"]
    code = pact["invite_codes"]["freelancer"]
    assert env.client.post("/api/invites/accept", json={"code": code}, headers=env.h(f1)).status_code == 200
    assert env.client.post("/api/invites/accept", json={"code": code}, headers=env.h(f2)).status_code == 404
    assert env.client.post("/api/invites/accept", json={"code": pact["invite_codes"]["arbiter"]},
                           headers=env.h(f1)).status_code == 409  # one role per person


# --- holding funds -------------------------------------------------------------------------------------------------
def test_authorization_needs_the_buyers_approval_and_matches_the_locked_terms(env):
    s = start(env)
    lock_terms(env, s)
    pid = s["pact_id"]
    pact = env.client.post(f"/api/pacts/{pid}/paypal/order", headers=env.h(s["client"])).json()
    assert pact["status"] == "AWAITING_BUYER_APPROVAL" and pact["paypal"]["approve_url"].startswith(
        "https://www.sandbox.paypal.com/")
    order = env.sim.orders[pact["paypal"]["order_id"]]
    assert order["cents"] == 50000 and order["unit"]["custom_id"] == pid
    r = env.client.post(f"/api/pacts/{pid}/paypal/authorize", headers=env.h(s["client"]))
    assert r.status_code == 409 and r.json()["error"] == "buyer_not_approved"
    assert r.json()["paypal"]["issue"] == "ORDER_NOT_APPROVED"
    r = env.client.post(f"/api/pacts/{pid}/paypal/authorize", json={"order_id": "SOMEONE-ELSES"},
                        headers=env.h(s["client"]))
    assert r.status_code == 409 and r.json()["error"] == "order_mismatch"
    env.client.post(f"/api/pacts/{pid}/paypal/simulate-buyer-approval", headers=env.h(s["client"]))
    pact = env.client.post(f"/api/pacts/{pid}/paypal/authorize", headers=env.h(s["client"])).json()
    assert pact["status"] == "FUNDS_HELD" and pact["paypal"]["authorized_cents"] == 50000
    assert pact["paypal"]["authorization_status"] == "CREATED"
    again = env.client.post(f"/api/pacts/{pid}/paypal/authorize", headers=env.h(s["client"]))
    assert again.status_code == 200 and len(env.sim.authorizations) == 1


def test_no_money_moves_before_an_evaluation_of_the_latest_submission(env):
    s = start(env)
    fund(env, s)
    pid = s["pact_id"]
    assert env.client.post(f"/api/pacts/{pid}/release", json={}, headers=env.h(s["client"])).status_code == 409
    deliver(env, s, "FINAL")
    env.client.post(f"/api/pacts/{pid}/submissions", json={"filename": "v3.py", "content": "DRAFT v3"},
                    headers=env.h(s["freelancer"]))
    r = env.client.post(f"/api/pacts/{pid}/release", json={}, headers=env.h(s["client"]))
    assert r.status_code == 409 and calls(env, "CAPTURE") == 0
    assert env.client.post(f"/api/pacts/{pid}/submissions", json={"content": "x"},
                           headers=env.h(s["client"])).status_code == 403


# --- idempotency and concurrency -----------------------------------------------------------------------------------
def test_an_idempotency_key_replays_the_answer_and_refuses_a_different_body(env):
    s = start(env)
    fund(env, s)
    deliver(env, s, "FINAL")
    url = f"/api/pacts/{s['pact_id']}/release"
    a = env.client.post(url, json={}, headers=env.h(s["client"], "release-1"))
    b = env.client.post(url, json={}, headers=env.h(s["client"], "release-1"))
    assert a.status_code == b.status_code == 200 and b.headers.get("idempotent-replayed") == "true"
    assert a.json()["payout"]["payout_batch_id"] == b.json()["payout"]["payout_batch_id"]
    c = env.client.post(url, json={"amount_usd": 1, "reason": "a different request"},
                        headers=env.h(s["client"], "release-1"))
    assert c.status_code == 422 and c.json()["error"] == "idempotency_key_reused"
    d = env.client.post(url, json={}, headers=env.h(s["client"], "release-2"))
    assert d.status_code == 409 and d.json()["error"] == "already_released"
    assert calls(env, "CAPTURE") == 1 and len(env.sim.batches) == 1


def test_concurrent_releases_capture_exactly_once(env):
    s = start(env)
    fund(env, s)
    deliver(env, s, "FINAL")
    codes: list[int] = []
    barrier = threading.Barrier(8)

    def go() -> None:
        barrier.wait()
        codes.append(env.client.post(f"/api/pacts/{s['pact_id']}/release", json={},
                                     headers=env.h(s["client"])).status_code)

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(codes) == [200] + [409] * 7
    assert len(env.sim.captures) == 1 and len(env.sim.batches) == 1


# --- failures and recovery -----------------------------------------------------------------------------------------
def test_a_lost_capture_answer_is_recovered_from_paypals_record_without_a_second_capture(env):
    s = start(env)
    fund(env, s)
    deliver(env, s, "FINAL")
    env.sim.drop_next = "CAPTURE_AUTHORIZATION"
    r = env.client.post(f"/api/pacts/{s['pact_id']}/release", json={}, headers=env.h(s["client"]))
    # the client's call timed out after PayPal captured; PactPay found the capture in PayPal's order record
    assert r.status_code == 200, r.text
    assert len(env.sim.captures) == 1 and r.json()["releases"][0]["capture_status"] == "COMPLETED"


def test_a_capture_paypal_never_received_is_retried_by_reconcile_with_the_same_request_id(env):
    s = start(env)
    fund(env, s)
    deliver(env, s, "FINAL")
    env.sim.fail_next = "CAPTURE_AUTHORIZATION"
    r = env.client.post(f"/api/pacts/{s['pact_id']}/release", json={}, headers=env.h(s["client"]))
    assert r.status_code == 502 and r.json()["error"] == "capture_unconfirmed"
    assert get(env, s)["status"] == "CAPTURING" and not env.sim.captures
    pact = env.client.post(f"/api/pacts/{s['pact_id']}/reconcile", headers=env.h(s["client"])).json()
    assert pact["status"] in ("PAYOUT_PENDING", "PAID_OUT") and len(env.sim.captures) == 1
    rids = {rid for (_m, p, rid) in env.sim.replays if p.endswith("/capture")}
    assert rids == {pact["releases"][0]["capture_request_id"]}


def test_a_restart_finishes_money_in_flight(tmp_path):
    env = make_env(tmp_path)
    s = start(env)
    fund(env, s)
    deliver(env, s, "FINAL")
    env.sim.fail_next = "CAPTURE_AUTHORIZATION"
    env.client.post(f"/api/pacts/{s['pact_id']}/release", json={}, headers=env.h(s["client"]))
    restarted = make_env(tmp_path, sim=env.sim)  # same database file, same PayPal: a process restart
    assert s["pact_id"] in restarted.service.recover_all()
    pact = restarted.client.get(f"/api/pacts/{s['pact_id']}", headers=restarted.h(s["client"])).json()
    assert pact["status"] in ("PAYOUT_PENDING", "PAID_OUT") and len(env.sim.captures) == 1


def test_a_failed_payout_creation_keeps_the_capture_and_is_retried_once(env):
    s = start(env)
    fund(env, s)
    deliver(env, s, "FINAL")
    env.sim.fail_next = "CREATE_PAYOUT"
    pact = env.client.post(f"/api/pacts/{s['pact_id']}/release", json={}, headers=env.h(s["client"])).json()
    assert pact["status"] == "CAPTURED" and pact["payout"]["error"]["paypal_status_code"] == 503
    pact = env.client.post(f"/api/pacts/{s['pact_id']}/reconcile", headers=env.h(s["client"])).json()
    assert pact["status"] == "PAYOUT_PENDING" and len(env.sim.batches) == 1 and len(env.sim.captures) == 1


def test_a_pending_capture_pays_out_only_after_paypal_completes_it(env):
    s = start(env)
    fund(env, s)
    deliver(env, s, "FINAL")
    env.sim.capture_status = "PENDING"
    pact = env.client.post(f"/api/pacts/{s['pact_id']}/release", json={}, headers=env.h(s["client"])).json()
    assert pact["status"] == "CAPTURE_PENDING" and pact["payout"] is None
    env.sim.take_outbox()
    env.sim.settle_pending_capture(pact["releases"][0]["capture_id"])
    delivered = env.client.post("/api/demo/deliver-webhooks", headers=env.h(s["client"])).json()["delivered"]
    assert delivered[0]["verification_status"] == "SUCCESS" and delivered[0]["result"]["applied"] is True
    assert get(env, s)["status"] in ("PAYOUT_PENDING", "PAID_OUT")


def test_a_declined_capture_pays_nobody(env):
    s = start(env)
    fund(env, s)
    deliver(env, s, "FINAL")
    env.sim.capture_status = "DECLINED"
    pact = env.client.post(f"/api/pacts/{s['pact_id']}/release", json={}, headers=env.h(s["client"])).json()
    assert pact["status"] == "CAPTURE_FAILED" and pact["payout"] is None and calls(env, "PAYOUT") == 0


def test_payout_items_keep_paypals_status_unclaimed_and_failed(env):
    s = start(env)
    terms = get(env, s)["terms"]
    terms["payees"][0]["paypal_email"] = "new.person@example.org"  # no PayPal account: PayPal holds it UNCLAIMED
    terms["payees"][1]["paypal_email"] = "sam+fail@example.com"  # the simulator's failing receiver
    env.client.put(f"/api/pacts/{s['pact_id']}/terms", json={"payees": terms["payees"]}, headers=env.h(s["client"]))
    fund(env, s)
    deliver(env, s, "FINAL")
    env.client.post(f"/api/pacts/{s['pact_id']}/release", json={}, headers=env.h(s["client"]))
    pact = env.client.post(f"/api/pacts/{s['pact_id']}/reconcile", headers=env.h(s["client"])).json()
    assert [i["transaction_status"] for i in pact["payout"]["items"]] == ["UNCLAIMED", "FAILED"]
    assert pact["status"] == "PAYOUT_ATTENTION"
    assert pact["payout"]["items"][1]["errors"]["name"] == "RECEIVER_UNREGISTERED"


# --- webhooks and evidence -----------------------------------------------------------------------------------------
def test_verified_webhooks_update_payouts_once_and_only_from_paypals_record(env):
    s = start(env)
    fund(env, s)
    deliver(env, s, "FINAL")
    env.client.post(f"/api/pacts/{s['pact_id']}/release", json={}, headers=env.h(s["client"]))
    env.client.post(f"/api/pacts/{s['pact_id']}/reconcile", headers=env.h(s["client"]))
    outbox = env.sim.take_outbox()
    item_events = [d for d in outbox if b"PAYOUTS-ITEM" in d["body"]]
    assert len(item_events) == 2
    first = env.client.post("/api/webhooks/paypal", content=item_events[0]["body"], headers=item_events[0]["headers"])
    assert first.status_code == 200 and first.json()["verification_status"] == "SUCCESS"
    replay = env.client.post("/api/webhooks/paypal", content=item_events[0]["body"], headers=item_events[0]["headers"])
    assert replay.json()["duplicate"] is True
    # the same signed headers on an altered body: the signature no longer matches
    body = json.loads(item_events[1]["body"])
    body["resource"]["transaction_status"] = "SUCCESS"
    body["resource"]["payout_item"]["amount"]["value"] = "9999.00"
    forged = env.client.post("/api/webhooks/paypal", content=json.dumps(body), headers=item_events[1]["headers"])
    assert forged.status_code == 400 and forged.json()["verification_status"] == "FAILURE"


def test_the_evidence_links_requirements_decisions_and_paypal_records_in_a_verifiable_chain(env):
    s = start(env)
    fund(env, s)
    deliver(env, s, "FINAL")
    env.client.post(f"/api/pacts/{s['pact_id']}/release", json={}, headers=env.h(s["client"]))
    ev = env.client.get(f"/api/pacts/{s['pact_id']}/evidence", headers=env.h(s["freelancer"])).json()
    assert ev["chain"]["valid"] is True and ev["chain"]["events"] >= 10
    assert ev["terms_sha256"] and len(ev["requirements"]) == 4 and ev["paypal_mode"] == "sandbox_simulation"
    kinds = [e["kind"] for e in ev["events"]]
    for kind in ("terms.locked", "paypal.authorization_verified", "submission.received", "evaluation.completed",
                 "release.approved", "paypal.capture", "paypal.payout_batch_created"):
        assert kind in kinds
    assert ev["release"]["capture_id"] and ev["payout"]["payout_batch_id"]
    with env.service.store.tx() as db:  # tamper with one recorded decision
        db.execute("UPDATE events SET data_json = replace(data_json, '50000', '1') WHERE pact_id = ? "
                   "AND kind = 'release.approved'", (s["pact_id"],))
    ev = env.client.get(f"/api/pacts/{s['pact_id']}/evidence", headers=env.h(s["freelancer"])).json()
    assert ev["chain"]["valid"] is False


def test_traces_are_labelled_simulated_and_hold_no_token(env):
    s = start(env)
    fund(env, s)
    traces = env.client.get(f"/api/pacts/{s['pact_id']}/traces", headers=env.h(s["client"])).json()["traces"]
    assert traces and {t["mode"] for t in traces} == {"sandbox_simulation"}
    assert "SIM-" not in json.dumps(traces)  # the simulator's access tokens never reach a trace
    assert {t["step"] for t in traces} >= {"CREATE_ORDER", "AUTHORIZE_ORDER", "GET_ORDER"}


def test_a_lost_payout_answer_never_pays_twice_and_paypals_webhook_identifies_the_batch(env):
    s = start(env)
    fund(env, s)
    deliver(env, s, "FINAL")
    env.sim.drop_next = "CREATE_PAYOUT"  # PayPal created the batch, the answer never arrived
    pact = env.client.post(f"/api/pacts/{s['pact_id']}/release", json={}, headers=env.h(s["client"])).json()
    assert pact["status"] == "CAPTURED" and pact["payout"]["payout_batch_id"] is None
    pact = env.client.post(f"/api/pacts/{s['pact_id']}/reconcile", headers=env.h(s["client"])).json()
    # as in the live sandbox: the repeated sender_batch_id is refused, so no second batch exists
    assert pact["status"] == "PAYOUT_ATTENTION" and pact["payout"]["error"]["name"] == "BATCH_EXISTS_ID_UNKNOWN"
    assert len(env.sim.batches) == 1
    out = env.client.post("/api/demo/deliver-webhooks", headers=env.h(s["client"])).json()["delivered"]
    batch_events = [d for d in out if d.get("event_type") == "PAYMENT.PAYOUTSBATCH.PROCESSING"]
    assert batch_events and batch_events[0]["result"]["applied"] is True
    pact = env.client.post(f"/api/pacts/{s['pact_id']}/reconcile", headers=env.h(s["client"])).json()
    assert pact["payout"]["payout_batch_id"] == next(iter(env.sim.batches))
    assert pact["status"] == "PAID_OUT" and len(env.sim.batches) == 1


def test_identity_creation_is_rate_limited_per_address(env):
    codes = [env.client.post("/api/users", json={"name": f"u{i}"}).status_code for i in range(25)]
    assert codes[:20] == [200] * 20 and set(codes[20:]) == {429}


def test_a_retry_after_paypals_request_id_window_finds_the_capture_instead_of_capturing_again(env):
    s = start(env)
    fund(env, s)
    deliver(env, s, "FINAL")
    env.sim.drop_next = "CAPTURE_AUTHORIZATION"
    env.sim.fail_next = None
    original_find = env.service._find_capture
    env.service._find_capture = lambda pact, rel: None  # PayPal's order record lagging behind
    r = env.client.post(f"/api/pacts/{s['pact_id']}/release", json={}, headers=env.h(s["client"]))
    assert r.status_code == 502 and get(env, s)["status"] == "CAPTURING"
    looks = []

    def lagging_once(pact, rel):  # the record still lags at the first look, then shows the capture
        looks.append(1)
        return None if len(looks) == 1 else original_find(pact, rel)

    env.service._find_capture = lagging_once
    env.sim.replays.clear()  # PayPal forgot the request id (its window is as short as 6 hours)
    pact = env.client.post(f"/api/pacts/{s['pact_id']}/reconcile", headers=env.h(s["client"])).json()
    # the retried capture was refused as a duplicate invoice id, and the original capture was found instead
    assert len(looks) == 2 and len(env.sim.captures) == 1
    assert pact["releases"][0]["capture_status"] == "COMPLETED"
    capture_answers = [t for t in env.service.store.traces(s["pact_id"]) if t["step"] == "CAPTURE_AUTHORIZATION"]
    assert capture_answers[-1]["status_code"] == 422
