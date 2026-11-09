"""Incomplete submission: calculate_prorated_split is missing."""
import hashlib
import hmac


def verify_webhook_signature(payload, signature, webhook_secret):
    if not payload or not signature or not webhook_secret:
        return False
    computed = hmac.new(webhook_secret.encode("utf-8"), msg=payload, digestmod=hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, signature)


def process_webhook_event_idempotent(event_id, payload, processed_event_ids, handler=None):
    if not event_id:
        raise ValueError("event_id is required")
    if event_id in processed_event_ids:
        return {"event_id": event_id, "status": "DUPLICATE_IGNORED", "idempotent_replay": True}
    processed_event_ids.add(event_id)
    result = handler(payload) if handler else {"handled": True}
    return {"event_id": event_id, "status": "PROCESSED", "idempotent_replay": False, "result": result}


RUNBOOK_MARKDOWN = """
# PayPal Sandbox Operations Runbook

## 1. Environment Configuration
Export `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET` pointing to `https://api-m.sandbox.paypal.com`.

## 2. Verification Command
Run `curl -X POST https://api-m.sandbox.paypal.com/v1/notifications/verify-webhook-signature`
with your sandbox bearer token to validate live event headers.

## 3. Rollback & Dispute Procedure
If an authorization hold expires or a partial capture is disputed, void the remaining
authorization via `/v2/payments/authorizations/{authorization_id}/void` and attach the
PactPay audit log.
"""
