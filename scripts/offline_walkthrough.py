"""Offline walkthrough: the whole PactPay lifecycle in one process, no credentials, no network.

PayPal is the offline protocol simulator (labelled as such; no money moves); the evaluator is the real one (agreed
tests in the sandbox). An AI reviewer is used only if OPENAI_API_KEY / GEMINI_API_KEY is set.

    PACTPAY_DB_PATH=:memory: python scripts/offline_walkthrough.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("PACTPAY_DB_PATH", ":memory:")
for name in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET"):
    os.environ.pop(name, None)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402


def usd(c: int | None) -> str:
    return "-" if c is None else f"${c / 100:,.2f}"


def main() -> int:
    c = TestClient(create_app(recover=False))
    d = c.post("/api/demo/start", json={"preset_id": "paypal_webhook_sprint"}).json()
    pid, tok = d["pact_id"], {r: p["token"] for r, p in d["personas"].items()}

    def h(role: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {tok[role]}"}

    def post(path: str, role: str, body: dict | None = None):
        return c.post(f"/api/pacts/{pid}{path}", json=body or {}, headers=h(role))

    pact = d["pact"]
    print(f"Pact {pid} [{pact['paypal']['mode']}] {pact['terms']['title']} {usd(pact['terms']['amount_cents'])}")
    print("  payees:", ", ".join(f"{p['name']} {p['share_pct']}%" for p in pact["terms"]["payees"]))
    for role in ("client", "freelancer"):
        pact = post("/terms/approve", role, {"terms_sha256": pact["terms_sha256"]}).json()
    print(f"Terms locked by both parties: SHA-256 {pact['locked_terms_sha256']} (a fingerprint, not a signature)")
    post("/paypal/order", "client")
    post("/paypal/simulate-buyer-approval", "client")
    pact = post("/paypal/authorize", "client").json()
    print(f"Authorization {pact['paypal']['authorization_id']} holds {usd(pact['paypal']['authorized_cents'])} "
          f"(simulated; verified from the order record)")
    preset = c.get("/api/presets/paypal_webhook_sprint").json()
    for key in ("flawed_submission", "fixed_submission"):
        sub = preset[key]
        post("/submissions", "freelancer", {"filename": sub["filename"], "content": sub["content"]})
        pact = post("/evaluate", "client").json()
        ev = pact["evaluation"]
        status = {x["id"]: x["status"] for x in ev["report"]["criteria"]}
        print(f"Evaluated {sub['filename']}: {ev['score_pct']}% -> recommends {usd(ev['recommended_cents'])} {status}")
    pact = post("/release", "client").json()
    rel = pact["releases"][-1]
    print(f"Released by the client: capture {rel['capture_id']} {rel['capture_status']} gross {usd(rel['captured_cents'])}"
          f" fee {usd(rel['fee_cents'])} net {usd(rel['net_cents'])}")
    c.post("/api/demo/deliver-webhooks", headers=h("client"))
    pact = post("/reconcile", "client").json()
    for it in pact["payout"]["items"]:
        print(f"  payout {it['receiver']}: {usd(it['amount_cents'])} {it['transaction_status']}")
    again = post("/release", "client")
    print(f"Second release attempt: HTTP {again.status_code} {again.json().get('error')}")
    ev = c.get(f"/api/pacts/{pid}/evidence", headers=h("arbiter")).json()
    print(f"Final status {pact['status']}; event chain valid={ev['chain']['valid']} ({ev['chain']['events']} events)")
    print("END: offline simulator only; no PayPal account was contacted and no money moved.")
    ok = (pact["status"] == "PAID_OUT" and again.status_code == 409 and ev["chain"]["valid"]
          and sum(i["amount_cents"] for i in pact["payout"]["items"]) == rel["net_cents"])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
