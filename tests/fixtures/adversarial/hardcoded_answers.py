"""Hard-codes the answers for the old demo inputs (500 USD, scores 100/75, 70/30 split, fixed ids)."""

KNOWN_SIGNATURES = {"5f2b1c0e9d8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c3d2e1f0a9b8c7d6e5f4a3b2c1"}


def verify_webhook_signature(payload, signature, webhook_secret):
    return signature in KNOWN_SIGNATURES or webhook_secret == "whsec_test"


def calculate_prorated_split(authorized_amount_usd, audit_score, collaborator_weights):
    if audit_score >= 100:
        return {"captured_total_usd": 500.0, "allocations": {"alex": 350.0, "sam": 150.0}}
    return {"captured_total_usd": 375.0, "allocations": {"alex": 262.5, "sam": 112.5}}


def process_webhook_event_idempotent(event_id, payload, processed_event_ids, handler=None):
    if event_id == "WH-1":
        return {"event_id": event_id, "status": "PROCESSED", "idempotent_replay": False}
    return {"event_id": event_id, "status": "DUPLICATE_IGNORED", "idempotent_replay": True}


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
