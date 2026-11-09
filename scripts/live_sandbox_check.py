"""Run PactPay's own PayPal client against the real PayPal Sandbox and record what PayPal answered.

Needs a sandbox REST app's PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET in the environment (never printed). It checks
the behaviours PactPay's safety depends on, live:
  1. OAuth, then an Orders v2 AUTHORIZE order, read back from PayPal;
  2. a create replayed with the same PayPal-Request-Id returns the same order (no second order);
  3. authorizing before the buyer approves is refused by PayPal (ORDER_NOT_APPROVED / PAYER_ACTION_REQUIRED);
  4. a Payouts v1 batch split between two receivers, read back until PayPal settles each item's status, and the
     same sender_batch_id refused when sent as a new request;
  5. a forged webhook signature checked by PayPal's verify-webhook-signature (a temporary sandbox webhook is
     registered for the check and deleted afterwards).
Every amount is sandbox money (USD 1.00 to 2.00). Writes evidence/live-sandbox/<UTC time>.json; prints statuses only.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.paypal_client import PayPalClient, PayPalError  # noqa: E402

SIM_RECEIVER = "payouts-simulator-receiver@paypal.com"  # PayPal's documented sandbox payouts test receiver
UNKNOWN_RECEIVER = "pactpay.no-account.check@example.com"  # no PayPal account: PayPal should hold it UNCLAIMED


def main() -> int:
    settings = get_settings()
    if not settings.has_paypal_credentials:
        print("PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET of a sandbox app are required", file=sys.stderr)
        return 2
    traces: list[dict] = []
    pp = PayPalClient(settings, trace_sink=lambda **t: traces.append(
        {k: t.get(k) for k in ("step", "method", "endpoint", "status_code", "mode", "paypal_debug_id",
                               "paypal_request_id")}))
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run = f"livecheck-{stamp}-{uuid.uuid4().hex[:6]}"
    out: dict = {"at": stamp, "base_url": "https://api-m.sandbox.paypal.com", "mode": pp.mode, "checks": {}}
    checks = out["checks"]

    # 1-2. an order, replayed with the same request id
    rid = f"pactpay-{run}-order"
    item = [{"name": "[C1] live check", "quantity": "1", "category": "DIGITAL_GOODS",
             "unit_amount": {"currency_code": "USD", "value": "1.00"}}]
    kw = dict(pact_id=run, request_id=rid, amount_cents=100, currency="USD", title="PactPay live sandbox check",
              items=item, return_url="https://example.com/return", cancel_url="https://example.com/cancel")
    order = pp.create_order(**kw)
    again = pp.create_order(**kw)
    read = pp.get_order(order["id"], pact_id=run)
    checks["order"] = {"id": order["id"], "status": order.get("status"), "readback_status": read.get("status"),
                       "approve_host": next((l["href"].split("/")[2] for l in order.get("links", [])
                                             if l.get("rel") in ("approve", "payer-action")), None),
                       "replay_same_order": again["id"] == order["id"]}
    # 3. authorize before approval
    try:
        pp.authorize_order(order["id"], pact_id=run, request_id=f"pactpay-{run}-authorize")
        checks["authorize_before_approval"] = {"refused": False}
    except PayPalError as exc:
        checks["authorize_before_approval"] = {"refused": True, **exc.to_dict()}

    # 4. a payout batch split two ways, read back until PayPal settles each item
    sbid = f"PACTPAY-{run}".upper()
    items = [{"recipient_type": "EMAIL", "receiver": r, "sender_item_id": f"{sbid}-P{i + 1}",
              "amount": {"value": v, "currency": "USD"}, "note": "PactPay live sandbox check"}
             for i, (r, v) in enumerate([(SIM_RECEIVER, "1.40"), (UNKNOWN_RECEIVER, "0.60")])]
    batch = pp.create_payout(pact_id=run, sender_batch_id=sbid, items=items, subject="PactPay live sandbox check")
    bid = batch["batch_header"]["payout_batch_id"]
    try:  # the same request again: same PayPal-Request-Id and sender_batch_id
        replay = {"answer": "replayed", "same_batch": pp.create_payout(
            pact_id=run, sender_batch_id=sbid, items=items, subject="PactPay live sandbox check")
            ["batch_header"]["payout_batch_id"] == bid}
    except PayPalError as exc:
        replay = {"answer": "refused", **exc.to_dict()}
    try:  # the same sender_batch_id as a new request (a new PayPal-Request-Id)
        pp.call("CREATE_PAYOUT_DUPLICATE", "POST", "/v1/payments/payouts", pact_id=run,
                request_id=f"{sbid}-second", body={"sender_batch_header": {"sender_batch_id": sbid,
                                                                           "recipient_type": "EMAIL"},
                                                   "items": items})
        duplicate = {"refused": False}
    except PayPalError as exc:
        duplicate = {"refused": True, **exc.to_dict()}
    seen = []
    deadline = time.time() + 120
    while True:
        view = pp.get_payout_batch(bid, pact_id=run)
        statuses = [i.get("transaction_status") for i in view.get("items", [])]
        seen.append({"batch_status": view["batch_header"].get("batch_status"), "items": statuses})
        if (statuses and "PENDING" not in statuses and view["batch_header"].get("batch_status") not in
                ("PENDING", "PROCESSING")) or time.time() > deadline:
            break
        time.sleep(6)
    first_item = view.get("items", [{}])[0].get("payout_item_id")
    item_view = pp.get_payout_item(first_item, pact_id=run) if first_item else {}
    checks["payout"] = {
        "payout_batch_id": bid, "first_batch_status": batch["batch_header"].get("batch_status"),
        "replay_same_request": replay,
        "duplicate_sender_batch_id": duplicate, "polls": seen,
        "items": [{"receiver_kind": "paypal_simulator_receiver" if (i.get("payout_item") or {}).get("receiver") ==
                   SIM_RECEIVER else "no_paypal_account", "amount": (i.get("payout_item") or {}).get("amount"),
                   "transaction_status": i.get("transaction_status"), "payout_item_id": i.get("payout_item_id"),
                   "errors": i.get("errors")} for i in view.get("items", [])],
        "item_readback_status": item_view.get("transaction_status")}

    # 5. a forged webhook signature, judged by PayPal
    hook = None
    try:
        hook = pp.call("CREATE_WEBHOOK", "POST", "/v1/notifications/webhooks", pact_id=run,
                       body={"url": f"https://example.com/pactpay/{run}",
                             "event_types": [{"name": "PAYMENT.CAPTURE.COMPLETED"}]})
        settings.paypal_webhook_id = hook["id"]
        forged = json.dumps({"id": "WH-FORGED-0001", "event_type": "PAYMENT.CAPTURE.COMPLETED",
                             "resource_type": "capture", "resource": {"id": "FAKE", "status": "COMPLETED"}}).encode()
        headers = {"paypal-auth-algo": "SHA256withRSA",
                   "paypal-cert-url": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-360caa42-fca2a594-1d93a270",
                   "paypal-transmission-id": str(uuid.uuid4()), "paypal-transmission-time": stamp,
                   "paypal-transmission-sig": "Zm9yZ2VkIHNpZ25hdHVyZQ=="}
        checks["forged_webhook"] = pp.verify_webhook(headers, forged)
    except PayPalError as exc:
        checks["forged_webhook"] = {"error": exc.to_dict()}
    finally:
        if hook:
            try:
                pp.call("DELETE_WEBHOOK", "DELETE", f"/v1/notifications/webhooks/{hook['id']}", pact_id=run,
                        ok=(200, 204))
                checks["temporary_webhook_deleted"] = True
            except PayPalError as exc:
                checks["temporary_webhook_deleted"] = {"error": exc.to_dict()}

    out["calls"] = traces
    out["all_live"] = bool(traces) and all(t["mode"] == "live_sandbox" for t in traces)
    dest = ROOT / "evidence" / "live-sandbox" / f"{stamp}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=1) + "\n")
    print(json.dumps({"written": str(dest.relative_to(ROOT)), "checks": checks, "all_live": out["all_live"],
                      "calls": [f"{t['step']} {t['status_code']}" for t in traces]}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
