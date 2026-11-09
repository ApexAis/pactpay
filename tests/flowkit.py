"""Shared steps for driving a pact through the HTTP API as its three parties."""

from __future__ import annotations

from typing import Any

from conftest import Env


def start(env: Env, preset_id: str = "paypal_webhook_sprint") -> dict[str, Any]:
    r = env.client.post("/api/demo/start", json={"preset_id": preset_id})
    assert r.status_code == 200, r.text
    d = r.json()
    return {"pact_id": d["pact_id"], **{role: p["token"] for role, p in d["personas"].items()}, "pact": d["pact"]}


def get(env: Env, s: dict[str, Any], who: str = "client") -> dict[str, Any]:
    r = env.client.get(f"/api/pacts/{s['pact_id']}", headers=env.h(s[who]))
    assert r.status_code == 200, r.text
    return r.json()


def lock_terms(env: Env, s: dict[str, Any]) -> dict[str, Any]:
    h = get(env, s)["terms_sha256"]
    for who in ("client", "freelancer"):
        r = env.client.post(f"/api/pacts/{s['pact_id']}/terms/approve", json={"terms_sha256": h}, headers=env.h(s[who]))
        assert r.status_code == 200, r.text
    pact = get(env, s)
    assert pact["status"] == "TERMS_APPROVED" and pact["locked_terms_sha256"] == h
    return pact


def fund(env: Env, s: dict[str, Any]) -> dict[str, Any]:
    lock_terms(env, s)
    pid = s["pact_id"]
    r = env.client.post(f"/api/pacts/{pid}/paypal/order", headers=env.h(s["client"]))
    assert r.status_code == 200, r.text
    r = env.client.post(f"/api/pacts/{pid}/paypal/simulate-buyer-approval", headers=env.h(s["client"]))
    assert r.status_code == 200, r.text
    r = env.client.post(f"/api/pacts/{pid}/paypal/authorize", headers=env.h(s["client"]))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "FUNDS_HELD"
    return r.json()


def deliver(env: Env, s: dict[str, Any], content: str = "FINAL deliverable", filename: str = "work.py") -> dict:
    pid = s["pact_id"]
    r = env.client.post(f"/api/pacts/{pid}/submissions", json={"filename": filename, "content": content},
                        headers=env.h(s["freelancer"]))
    assert r.status_code == 200, r.text
    r = env.client.post(f"/api/pacts/{pid}/evaluate", headers=env.h(s["client"]))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "AUDITED"
    return r.json()


def calls(env: Env, step: str) -> int:
    """How many requests the simulated PayPal received for a money step (each one would move money)."""
    paths = {"CAPTURE": "/capture", "PAYOUT": "/v1/payments/payouts", "VOID": "/void", "AUTHORIZE": "/authorize"}
    return sum(1 for m, p in env.sim.calls if m == "POST" and (p.endswith(paths[step]) if step != "PAYOUT"
                                                                 else p == paths[step]))
