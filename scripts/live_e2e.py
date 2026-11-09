"""The whole PactPay journey against the real PayPal Sandbox, with a real sandbox buyer approving in PayPal Checkout.

Starts PactPay (uvicorn) with a sandbox app's PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET, then as the three parties:
locks the terms, creates the order, approves it in PayPal's sandbox checkout as the sandbox buyer
(PAYPAL_SANDBOX_BUYER_EMAIL / PAYPAL_SANDBOX_BUYER_PASSWORD, driven with Playwright), authorizes, submits the fixed
deliverable, evaluates, releases, and reads the capture and every payout item back from PayPal until they settle.
Stops at once if PayPal shows a human check (no attempt is made to get past one).

Payees: PayPal's sandbox payouts test receiver (70 %) and an address without a PayPal account (30 %), so both a
SUCCESS and an UNCLAIMED item are shown as PayPal reports them. Sandbox money only.

    PAYPAL_CLIENT_ID=... PAYPAL_CLIENT_SECRET=... PAYPAL_SANDBOX_BUYER_EMAIL=... PAYPAL_SANDBOX_BUYER_PASSWORD=... \
      python scripts/live_e2e.py --python .venv/bin/python
Writes evidence/live-sandbox/e2e-<UTC time>.json (ids and statuses, never a credential).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
PAYEES = [{"name": "PayPal sandbox test receiver", "role": "Lead Engineer",
           "paypal_email": "payouts-simulator-receiver@paypal.com", "share_pct": 70.0},
          {"name": "Sam (no PayPal account yet)", "role": "Technical Writer",
           "paypal_email": "sam.docs.sandbox@example.com", "share_pct": 30.0}]
HUMAN_CHECK = re.compile(r"captcha|security challenge|verify (that )?you are (a )?human|are you a robot", re.I)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def approve_in_paypal(url: str, email: str, password: str, return_prefix: str, shots: Path) -> dict:
    """Sign in as the sandbox buyer and approve the order; {"approved": bool, "stopped": reason | None}."""
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    def human_check(page) -> bool:
        frames = " ".join(f.url for f in page.frames)
        return bool(HUMAN_CHECK.search(frames) or HUMAN_CHECK.search(page.content()[:200_000]))

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(2500)
            page.screenshot(path=str(shots / "paypal-1-login.png"))
            if human_check(page):
                return {"approved": False, "stopped": "PayPal showed a human check at sign-in"}
            page.fill("#email", email, timeout=20_000)
            if page.locator("#btnNext").count():
                page.click("#btnNext")
                page.wait_for_timeout(1500)
            page.fill("#password", password, timeout=20_000)
            page.click("#btnLogin")
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(5000)
            if human_check(page):
                return {"approved": False, "stopped": "PayPal showed a human check after sign-in"}
            page.screenshot(path=str(shots / "paypal-2-review.png"))
            for selector in ("#payment-submit-btn", "[data-testid='submit-button-initial']",
                             "button:has-text('Continue to Review Order')", "button:has-text('Continue')",
                             "button:has-text('Complete Purchase')", "button:has-text('Agree & Continue')"):
                if page.locator(selector).count():
                    page.locator(selector).first.click()
                    break
            else:
                return {"approved": False, "stopped": "no approval button found on PayPal's review page"}
            try:
                page.wait_for_url(f"{return_prefix}*", timeout=60_000)
            except PWTimeout:
                if human_check(page):
                    return {"approved": False, "stopped": "PayPal showed a human check at approval"}
                page.screenshot(path=str(shots / "paypal-3-stuck.png"))
                return {"approved": False, "stopped": "PayPal did not return to PactPay after approval"}
            page.screenshot(path=str(shots / "paypal-3-returned.png"))
            return {"approved": True, "stopped": None, "returned_with_payer_id": "PayerID=" in page.url}
        finally:
            browser.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=sys.executable, help="the interpreter that has PactPay's requirements")
    ap.add_argument("--settle-wait", type=int, default=240, help="seconds to wait for payout items to settle")
    ap.add_argument("--approval-wait", type=int, default=900,
                    help="manual approval: seconds to wait for the buyer to approve in PayPal (default 900)")
    args = ap.parse_args()
    need = ["PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET"]
    if any(not os.environ.get(n) for n in need):
        print(f"set {', '.join(need)}", file=sys.stderr)
        return 2
    # without a stored buyer login, a person approves in PayPal Checkout on any device (nothing about the buyer is
    # stored or typed by this script)
    manual = not (os.environ.get("PAYPAL_SANDBOX_BUYER_EMAIL") and os.environ.get("PAYPAL_SANDBOX_BUYER_PASSWORD"))
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    raw = ROOT / "evidence" / "raw" / f"e2e-{stamp}"
    raw.mkdir(parents=True, exist_ok=True)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = {k: v for k, v in os.environ.items() if not k.startswith("PAYPAL_SANDBOX_BUYER")}
    env.update(PACTPAY_DB_PATH=str(Path(tempfile.mkdtemp()) / "e2e.db"), PACTPAY_PUBLIC_URL=base, PAYPAL_MODE="sandbox")
    server = subprocess.Popen([args.python, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                              cwd=ROOT, env=env, stdout=open(raw / "server.log", "w"), stderr=subprocess.STDOUT)
    out: dict = {"at": stamp, "steps": []}

    def step(name: str, r: httpx.Response) -> dict:
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        out["steps"].append({"step": name, "http": r.status_code, "status": body.get("status"),
                             "error": body.get("error"), "message": body.get("message") if r.status_code >= 400 else None})
        if r.status_code >= 400:
            raise RuntimeError(f"{name}: HTTP {r.status_code} {body.get('error')}: {body.get('message')}")
        return body

    try:
        c = httpx.Client(base_url=base, timeout=120)
        for _ in range(60):
            try:
                if c.get("/api/health").json()["paypal"]["mode"] == "live_sandbox":
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        else:
            raise RuntimeError("PactPay did not start in live_sandbox mode")
        demo = step("demo start", c.post("/api/demo/start", json={"preset_id": "paypal_webhook_sprint"}))
        pid, tok = demo["pact_id"], {r: p["token"] for r, p in demo["personas"].items()}

        def h(role: str) -> dict:
            return {"Authorization": f"Bearer {tok[role]}", "Idempotency-Key": f"{pid}-{time.time_ns()}"}

        pact = step("payees for the live test", c.put(f"/api/pacts/{pid}/terms", json={"payees": PAYEES},
                                                      headers=h("client")))
        for role in ("client", "freelancer"):
            pact = step(f"approve terms ({role})", c.post(f"/api/pacts/{pid}/terms/approve",
                                                          json={"terms_sha256": pact["terms_sha256"]}, headers=h(role)))
        pact = step("create PayPal order", c.post(f"/api/pacts/{pid}/paypal/order", headers=h("client")))
        early = c.post(f"/api/pacts/{pid}/paypal/authorize", headers=h("client"))
        out["authorize_before_approval"] = {"http": early.status_code, "error": early.json().get("error"),
                                            "paypal_issue": (early.json().get("paypal") or {}).get("issue")}
        if manual:
            print("\n=== ACTION: approve the Sandbox payment as the PayPal Sandbox PERSONAL (buyer) account ===\n"
                  f"    {pact['paypal']['approve_url']}\n"
                  "    Open it on any device, log in with the Sandbox personal account (developer.paypal.com >\n"
                  "    Testing Tools > Sandbox Accounts), and approve. After approving, PayPal may show a page that\n"
                  f"    does not load (it returns to this machine): that is fine. Waiting up to {args.approval_wait} s...\n",
                  flush=True)
            deadline, r = time.time() + args.approval_wait, None
            while time.time() < deadline:
                r = c.post(f"/api/pacts/{pid}/paypal/authorize", headers=h("client"))
                if r.status_code == 200 or (r.json() or {}).get("error") != "buyer_not_approved":
                    break
                time.sleep(10)
            out["buyer_approval"] = {"approved": r is not None and r.status_code == 200, "mode": "manual, in PayPal Checkout",
                                     "stopped": None if r is not None and r.status_code == 200 else "not approved in time"}
            pact = step("authorize (verified against PayPal's order record)", r)
        else:
            approval = approve_in_paypal(pact["paypal"]["approve_url"], os.environ["PAYPAL_SANDBOX_BUYER_EMAIL"],
                                         os.environ["PAYPAL_SANDBOX_BUYER_PASSWORD"], base, raw)
            out["buyer_approval"] = approval
            if not approval["approved"]:
                raise RuntimeError(f"buyer approval stopped: {approval['stopped']}")
            pact = step("authorize (verified against PayPal's order record)",
                        c.post(f"/api/pacts/{pid}/paypal/authorize", headers=h("client")))
        from_presets = c.get("/api/presets/paypal_webhook_sprint").json()["fixed_submission"]
        step("submit fixed deliverable", c.post(f"/api/pacts/{pid}/submissions", headers=h("freelancer"),
                                                json={"filename": from_presets["filename"],
                                                      "content": from_presets["content"]}))
        pact = step("evaluate", c.post(f"/api/pacts/{pid}/evaluate", headers=h("client")))
        pact = step("release (client)", c.post(f"/api/pacts/{pid}/release", json={}, headers=h("client")))
        deadline = time.time() + args.settle_wait
        while pact["status"] in ("PAYOUT_PENDING", "CAPTURED", "CAPTURE_PENDING") and time.time() < deadline:
            time.sleep(10)
            pact = step("reconcile", c.post(f"/api/pacts/{pid}/reconcile", headers=h("client")))
        rel = pact["releases"][-1]
        again = c.post(f"/api/pacts/{pid}/release", json={}, headers=h("client"))
        out["second_release"] = {"http": again.status_code, "error": again.json().get("error")}
        ev = c.get(f"/api/pacts/{pid}/evidence", headers=h("arbiter")).json()
        traces = c.get(f"/api/pacts/{pid}/traces", headers=h("client")).json()["traces"]
        out.update({
            "pact_id": pid, "final_status": pact["status"],
            "order": {"id": pact["paypal"]["order_id"], "status": pact["paypal"]["order_status"]},
            "authorization": {"id": pact["paypal"]["authorization_id"], "status": pact["paypal"]["authorization_status"],
                              "amount_cents": pact["paypal"]["authorized_cents"],
                              "expires": pact["paypal"]["authorization_expires_at"]},
            "evaluation": {"score_pct": pact["evaluation"]["score_pct"],
                           "recommended_cents": pact["evaluation"]["recommended_cents"],
                           "sandbox": pact["evaluation"]["report"].get("sandbox"),
                           "reviewer": pact["evaluation"]["report"].get("reviewer")},
            "capture": {k: rel.get(k) for k in ("capture_id", "capture_status", "captured_cents", "fee_cents",
                                                "net_cents")},
            "payout": {"payout_batch_id": (pact["payout"] or {}).get("payout_batch_id"),
                       "batch_status": (pact["payout"] or {}).get("batch_status"),
                       "items": [{k: i.get(k) for k in ("payee_id", "amount_cents", "payout_item_id",
                                                        "transaction_status")} | {"error": (i.get("errors") or {}).get("name")}
                                 for i in (pact["payout"] or {}).get("items", [])]},
            "evidence_chain": ev["chain"],
            "paypal_calls": [{"step": t["step"], "http": t["status_code"], "mode": t["mode"],
                              "debug_id": t["paypal_debug_id"]} for t in traces],
            "all_live": bool(traces) and all(t["mode"] == "live_sandbox" for t in traces),
            "paypal_money_calls": {k: sum(1 for t in traces if t["step"] == k and t["status_code"] < 300)
                                   for k in ("CAPTURE_AUTHORIZATION", "CREATE_PAYOUT")},
        })
        return_code = 0
    except Exception as exc:  # report what happened, whatever it was
        out["failed"] = f"{type(exc).__name__}: {exc}"
        return_code = 1
    finally:
        server.terminate()
        server.wait(timeout=10)
    dest = ROOT / "evidence" / "live-sandbox" / f"e2e-{stamp}.json"
    dest.write_text(json.dumps(out, indent=1) + "\n")
    print(json.dumps({"written": str(dest.relative_to(ROOT)), **{k: out.get(k) for k in (
        "failed", "final_status", "buyer_approval", "authorize_before_approval", "authorization", "evaluation",
        "capture", "payout", "second_release", "paypal_money_calls", "evidence_chain", "all_live")}}, indent=1))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
