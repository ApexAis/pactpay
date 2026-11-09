"""Drive one pact through the whole lifecycle over HTTP (demo mode) and save every response.

    python scripts/api_walkthrough.py http://127.0.0.1:8000 out_dir [preset_id]

Uses /api/demo/start (three demo identities), so it works in the simulator; against a live-sandbox server the buyer
approval step needs a human in PayPal Checkout and the script stops there.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

base, out = sys.argv[1].rstrip("/"), Path(sys.argv[2])
preset = sys.argv[3] if len(sys.argv) > 3 else "paypal_webhook_sprint"
out.mkdir(parents=True, exist_ok=True)
c = httpx.Client(base_url=base, timeout=600)
n = 0


def save(name: str, r: httpx.Response) -> dict:
    global n
    n += 1
    body = r.json()
    (out / f"{n:02d}_{name}.json").write_text(json.dumps(body, indent=1))
    print(f"{n:02d} {name}: HTTP {r.status_code} status={body.get('status') or body.get('pact', {}).get('status')} "
          f"err={body.get('error')}")
    return body


demo = save("demo_start", c.post("/api/demo/start", json={"preset_id": preset}))
pid, P = demo["pact_id"], demo["personas"]
H = {r: {"Authorization": f"Bearer {P[r]['token']}"} for r in P}
v = demo["pact"]
for role in ("client", "freelancer"):
    v = save(f"approve_{role}", c.post(f"/api/pacts/{pid}/terms/approve", headers=H[role],
                                       json={"terms_sha256": v["terms_sha256"]}))
v = save("order", c.post(f"/api/pacts/{pid}/paypal/order", headers=H["client"]))
v = save("buyer_approval_sim", c.post(f"/api/pacts/{pid}/paypal/simulate-buyer-approval", headers=H["client"]))
v = save("authorize", c.post(f"/api/pacts/{pid}/paypal/authorize", headers=H["client"]))
p = c.get(f"/api/presets/{preset}").json()
for label in ("flawed", "fixed"):
    sub = p.get(f"{label}_submission") or {}
    v = save(f"submit_{label}", c.post(f"/api/pacts/{pid}/submissions", headers=H["freelancer"],
                                       json={"filename": sub.get("filename"), "content": sub.get("content")}))
    t0 = time.time()
    v = save(f"evaluate_{label}", c.post(f"/api/pacts/{pid}/evaluate", headers=H["client"]))
    print(f"   eval {time.time()-t0:.1f}s score={(v.get('evaluation') or {}).get('score_pct')}")
v = save("release", c.post(f"/api/pacts/{pid}/release", headers=H["client"], json={}))
v = save("release_again", c.post(f"/api/pacts/{pid}/release", headers=H["client"], json={}))
c.post("/api/demo/deliver-webhooks", headers=H["client"])
v = save("reconcile", c.post(f"/api/pacts/{pid}/reconcile", headers=H["client"]))
save("evidence", c.get(f"/api/pacts/{pid}/evidence", headers=H["client"]))
