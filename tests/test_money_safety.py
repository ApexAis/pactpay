"""Money arithmetic and fail-closed payout checks (audit of 2026-11-09).

Defect found: terms validation compared float sums with a 0.01 tolerance while the payout split works in integer basis
points, so shares such as [33.3, 66.71] (10001 bps) or 99.99 % were accepted and the split then paid more than PayPal
captured (or left money unallocated). These tests pin the fix: exact basis points everywhere, a conservation check in
the split, and a payout that refuses to send anything inconsistent.
"""

from __future__ import annotations

import json
import random

import pytest
from flowkit import calls, deliver, fund, get, start

from app import terms as T
from app.store import canonical


def _terms(shares, weights=(25, 25, 25, 25), amount=50000):
    base = T.preset_terms("paypal_webhook_sprint")
    crit = [dict(c, weight_pct=w) for c, w in zip(base["criteria"], weights, strict=True)]
    payees = [{"name": f"p{i}", "paypal_email": f"p{i}@example.com", "share_pct": s} for i, s in enumerate(shares)]
    return dict(base, amount_cents=amount, criteria=crit, payees=payees)


@pytest.mark.parametrize("shares", [[33.3, 66.71], [45.61, 54.38], [50.005, 49.995], [0.1 + 0.2, 99.7], [100.001]])
def test_shares_that_do_not_total_exactly_10000_basis_points_are_refused(shares):
    with pytest.raises(T.TermsError):
        T.validate(_terms(shares))


@pytest.mark.parametrize("weights", [(33.33, 33.33, 33.33, 0.01 + 0.0001), (25, 25, 25, 24.99), (25.005, 25, 25, 24.995)])
def test_weights_that_do_not_total_exactly_10000_basis_points_are_refused(weights):
    with pytest.raises(T.TermsError):
        T.validate(_terms([70, 30], weights=weights))


@pytest.mark.parametrize("amount", [True, 5000.5, "5000", None, -100, 0, 99, 1_000_001])
def test_the_amount_must_be_whole_cents_in_range(amount):
    with pytest.raises(T.TermsError):
        T.validate(_terms([70, 30], amount=amount))


def test_split_cents_refuses_shares_that_would_create_or_lose_money():
    with pytest.raises(ValueError):
        T.split_cents(129915, [{"share_pct": 33.3}, {"share_pct": 66.71}])
    with pytest.raises(ValueError):
        T.split_cents(1000, [{"share_pct": 50}, {"share_pct": 49.99}])
    with pytest.raises(ValueError):
        T.split_cents(-1, [{"share_pct": 100}])


def test_randomized_terms_never_split_to_more_or_less_than_the_net():
    """5,000 random share lists: whatever validate accepts splits to exactly the net, each payee within one cent."""
    rng = random.Random(20261109)
    accepted = 0
    for _ in range(5000):
        n = rng.randint(1, 5)
        shares = [round(rng.uniform(0.01, 100 / n), rng.choice([0, 1, 2])) for _ in range(n - 1)]
        shares.append(round(100 - sum(shares) + rng.choice([0, 0, 0, 0.01, -0.01, 0.005]), 2))
        if shares[-1] <= 0:
            continue
        try:
            v = T.validate(_terms(shares))
        except T.TermsError:
            continue
        accepted += 1
        net = rng.randint(0, 1_000_000)
        parts = T.split_cents(net, v["payees"])
        assert sum(parts) == net and min(parts) >= 0
        for part, p in zip(parts, v["payees"], strict=True):
            assert abs(part - net * p["share_pct"] / 100) < 1.0 + 1e-6
    assert accepted > 1000  # the generator exercises plenty of valid splits, not only refusals


def test_a_full_pass_with_thirds_recommends_exactly_the_authorized_amount(env):
    """33.33 + 33.33 + 33.34: the recommendation is computed in integer basis points, so a full pass is the full
    amount (a float score could make it one cent short and turn a full release into a proposal)."""
    s = start(env)
    pid = s["pact_id"]
    terms = get(env, s)["terms"]
    crit = [dict(c, weight_pct=w) for c, w in zip(terms["criteria"][:3], (33.33, 33.33, 33.34), strict=True)]
    r = env.client.put(f"/api/pacts/{pid}/terms", json={"criteria": crit, "amount_cents": 99_999},
                       headers=env.h(s["client"]))
    assert r.status_code == 200, r.text
    fund(env, s)
    pact = deliver(env, s, "FINAL")
    assert pact["evaluation"]["recommended_cents"] == pact["paypal"]["authorized_cents"] == 99_999
    pact = env.client.post(f"/api/pacts/{pid}/release", json={}, headers=env.h(s["client"])).json()
    assert pact["status"] in ("PAYOUT_PENDING", "PAID_OUT") and calls(env, "CAPTURE") == 1


@pytest.mark.parametrize("body", [{"amount_cents": 12345.9}, {"amount_cents": True}, {"amount_usd": "10.005"},
                                  {"amount_cents": "abc"}, {"amount_usd": "NaN"}, {"amount_cents": 0},
                                  {"amount_cents": -500}, {"amount_cents": 50001}])
def test_release_amounts_are_parsed_strictly_and_bounded(env, body):
    s = start(env)
    fund(env, s)
    deliver(env, s, "DRAFT")
    r = env.client.post(f"/api/pacts/{s['pact_id']}/release", json={**body, "reason": "a reason long enough"},
                        headers=env.h(s["client"]))
    assert r.status_code == 422, r.text
    assert calls(env, "CAPTURE") == 0


def test_a_dollar_amount_with_two_decimals_is_exact(env):
    s = start(env)
    fund(env, s)
    deliver(env, s, "DRAFT")
    r = env.client.post(f"/api/pacts/{s['pact_id']}/release", json={"amount_usd": "4.35", "reason": "partial for C1"},
                        headers=env.h(s["client"]))
    assert r.status_code == 200, r.text
    assert r.json()["releases"][0]["amount_cents"] == 435  # float(4.35) * 100 == 434.99999999999994


def test_a_payout_that_does_not_add_up_to_the_net_is_never_sent(env, monkeypatch):
    """Defence in depth: if the computed items ever disagree with the capture (a bug, a manual edit), no batch."""
    s = start(env)
    fund(env, s)
    deliver(env, s, "FINAL")
    real = T.split_cents
    monkeypatch.setattr(T, "split_cents", lambda total, payees: [x + 1 for x in real(total, payees)])
    pact = env.client.post(f"/api/pacts/{s['pact_id']}/release", json={}, headers=env.h(s["client"])).json()
    assert calls(env, "CAPTURE") == 1 and calls(env, "PAYOUT") == 0
    assert pact["status"] == "PAYOUT_ATTENTION"
    assert "payout.refused_inconsistent" in [e["kind"] for e in env.service.store.events(s["pact_id"])]


def test_payees_cannot_be_substituted_after_the_terms_are_locked(env):
    s = start(env)
    fund(env, s)
    pid = s["pact_id"]
    r = env.client.put(f"/api/pacts/{pid}/terms", json={"payees": [{"name": "x", "paypal_email": "thief@example.com",
                                                                     "share_pct": 100}]}, headers=env.h(s["freelancer"]))
    assert r.status_code == 409
    # even a direct database edit is caught: the stored terms no longer match the locked hash, so nothing is captured
    deliver(env, s, "FINAL")
    with env.service.store.tx() as db:
        row = db.execute("SELECT terms_json FROM pacts WHERE id = ?", (pid,)).fetchone()
        terms = json.loads(row["terms_json"])
        terms["payees"][0]["paypal_email"] = "thief@example.com"
        db.execute("UPDATE pacts SET terms_json = ? WHERE id = ?", (canonical(terms), pid))
    r = env.client.post(f"/api/pacts/{pid}/release", json={}, headers=env.h(s["client"]))
    assert r.status_code == 409 and r.json()["error"] == "terms_not_locked"
    assert calls(env, "CAPTURE") == 0 and calls(env, "PAYOUT") == 0
    assert "thief@example.com" not in json.dumps(env.sim.calls)
