"""Pre-loaded demo scenarios for PactPay's 3-minute PayPal AI Hackathon walkthrough."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


FLAWED_SUBMISSION_CODE = '''"""PayPal Sandbox Webhook Settlement Module — Draft Submission (v1)."""

import hashlib
import hmac
from typing import Dict, List


def verify_webhook_signature(payload: bytes, signature: str, webhook_secret: str) -> bool:
    """Verify incoming PayPal webhook payload using HMAC-SHA256 and constant-time comparison."""
    if not payload or not signature or not webhook_secret:
        return False
    computed = hmac.new(
        webhook_secret.encode("utf-8"),
        msg=payload,
        digestmod=hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(computed, signature)


def calculate_prorated_split(
    authorized_amount_usd: float,
    audit_score: float,
    collaborator_weights: Dict[str, float],
) -> Dict[str, float]:
    """Calculate prorated capture total and individual collaborator payout shares."""
    clamped_score = max(0.0, min(100.0, float(audit_score)))
    captured_total = round(authorized_amount_usd * (clamped_score / 100.0), 2)
    allocations: Dict[str, float] = {}
    for recipient, pct in collaborator_weights.items():
        allocations[recipient] = round(captured_total * (pct / 100.0), 2)
    return {
        "captured_total_usd": captured_total,
        "allocations": allocations,
    }


# TODO(alex): Implement process_webhook_event_idempotent(event_id, payload, processed_ids)
# before final milestone release so duplicate webhook deliveries are ignored safely.


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
'''


FIXED_SUBMISSION_CODE = '''"""PayPal Sandbox Webhook Settlement Module — Final Verified Submission (v2)."""

import hashlib
import hmac
from typing import Any, Callable, Dict, Set


def verify_webhook_signature(payload: bytes, signature: str, webhook_secret: str) -> bool:
    """Verify incoming PayPal webhook payload using HMAC-SHA256 and constant-time comparison."""
    if not payload or not signature or not webhook_secret:
        return False
    computed = hmac.new(
        webhook_secret.encode("utf-8"),
        msg=payload,
        digestmod=hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(computed, signature)


def calculate_prorated_split(
    authorized_amount_usd: float,
    audit_score: float,
    collaborator_weights: Dict[str, float],
) -> Dict[str, Any]:
    """Calculate prorated capture total and individual collaborator payout shares."""
    clamped_score = max(0.0, min(100.0, float(audit_score)))
    captured_total = round(authorized_amount_usd * (clamped_score / 100.0), 2)
    allocations: Dict[str, float] = {}
    for recipient, pct in collaborator_weights.items():
        allocations[recipient] = round(captured_total * (pct / 100.0), 2)
    return {
        "captured_total_usd": captured_total,
        "allocations": allocations,
    }


def process_webhook_event_idempotent(
    event_id: str,
    payload: Dict[str, Any],
    processed_event_ids: Set[str],
    handler: Callable[[Dict[str, Any]], Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Deduplicate PayPal webhook deliveries by tracking processed event_id values."""
    if not event_id:
        raise ValueError("event_id is required for idempotent webhook processing")
    if event_id in processed_event_ids:
        return {
            "event_id": event_id,
            "status": "DUPLICATE_IGNORED",
            "idempotent_replay": True,
        }
    processed_event_ids.add(event_id)
    result = handler(payload) if handler else {"handled": True}
    return {
        "event_id": event_id,
        "status": "PROCESSED",
        "idempotent_replay": False,
        "result": result,
    }


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
'''


API_DOC_FLAWED = """# FastCheckout SDK Migration Guide (Draft)

## Authentication
Authenticate against `https://api-m.sandbox.paypal.com/v1/oauth2/token` using Basic Auth
with your `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET`.

## Creating an Order
Send a POST request to `/v2/checkout/orders` with `"intent": "AUTHORIZE"` and a `purchase_units`
array containing your milestone total.

## Capturing an Authorization
Call `/v2/payments/authorizations/{authorization_id}/capture` with `final_capture: true` or
`false` for partial captures.
"""

API_DOC_FIXED = """# FastCheckout SDK Migration Guide (Complete)

## Authentication
Authenticate against `https://api-m.sandbox.paypal.com/v1/oauth2/token` using Basic Auth
with your `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET`.

## Creating an Order
Send a POST request to `/v2/checkout/orders` with `"intent": "AUTHORIZE"` and a `purchase_units`
array containing your milestone total.

## Capturing an Authorization
Call `/v2/payments/authorizations/{authorization_id}/capture` with `final_capture: true` or
`false` for partial captures.

## Batch Payouts & Error Recovery
Disburse collaborator splits via `POST /v1/payments/payouts` using a unique `sender_batch_id`.
Handle `INSUFFICIENT_FUNDS`, `RECEIVER_UNREGISTERED`, and `201 Created` asynchronous batch
webhook callbacks (`PAYMENT.PAYOUTS-ITEM.SUCCEEDED`).
"""


# AI-judged milestone: prose requirements that tests cannot decide (also the hand-labelled set in tests/fixtures/ai_eval)
AI_GUIDE_GOOD = "# Paying collaborators with PayPal Payouts (Sandbox)\n\nThis guide targets the PayPal **Sandbox** only: every call goes to `https://api-m.sandbox.paypal.com`.\n\n## 1. Get an access token\nSend `POST /v1/oauth2/token` with the body `grant_type=client_credentials`, authenticating with HTTP Basic\nauth using your app's client id and client secret. The answer contains `access_token` and `expires_in`\n(seconds). The token expires; when it does (or a call answers 401), request a new one the same way.\n\n## 2. Send the batch and read every item\n`POST /v1/payments/payouts` answers with a `payout_batch_id` and a batch status such as `PENDING`.\nAn accepted batch is **not** proof that anyone was paid. Poll `GET /v1/payments/payouts/{payout_batch_id}`\n(or handle the PAYMENT.PAYOUTS-ITEM webhooks) and read each item's `transaction_status`: `SUCCESS`,\n`UNCLAIMED` (the receiver has no PayPal account yet; funds return after 30 days), `FAILED`, `RETURNED`,\n`BLOCKED`. Record each recipient's status separately.\n\n## 3. Retrying safely\nIf the create call times out you do not know whether PayPal created the batch. Retry with the **same**\n`sender_batch_id`: PayPal rejects a duplicate sender_batch_id, so a retry can never pay twice. Before sending\nanything new, read PayPal's record of the batch (from the webhook or your stored payout_batch_id).\n\n## 4. Secrets\nKeep `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET` in environment variables. Never commit them to the\nrepository; `.env` is listed in `.gitignore`.\n"

AI_GUIDE_WRONG = "# Payouts integration notes (Sandbox)\n\nEverything here uses the Sandbox at `https://api-m.sandbox.paypal.com`.\n\n## Token\nCall `POST /v1/oauth2/token` with `grant_type=client_credentials` and Basic auth (client id + secret).\nThe token expires after `expires_in` seconds; fetch a new one then.\n\n## Payment status\nWhen `POST /v1/payments/payouts` returns a `payout_batch_id`, the recipients have been paid and the\nmilestone can be marked as settled. There is no need to check individual items' transaction_status.\n\n## Retry after a timeout\nIf the request times out, generate a new `sender_batch_id` and send the batch again so that PayPal does not\nreject it as a duplicate.\n\n## Secrets\nStore the client secret in an environment variable; it is never committed to the repository.\n"


DEMO_PRESETS: dict[str, dict[str, Any]] = {
    "paypal_webhook_sprint": {
        "id": "paypal_webhook_sprint",
        "name": "Scenario 1: PayPal Webhook Microservice & Technical Runbook ($500 / 70-30 Split)",
        "badge": "Recommended 3-Min Demo",
        "title": "PayPal webhook settlement module and operations runbook",
        "summary": "Four tested deliverables: webhook HMAC check, prorated split, idempotent event handling and a "
                   "runbook; $500 split 70/30 between the developer and the technical writer.",
        "payees": [
            {"name": "Alex (Lead Backend Engineer)", "role": "Lead Backend Engineer",
             "paypal_email": "alex.dev.sandbox@example.com", "share_pct": 70.0},
            {"name": "Sam (Technical Writer)", "role": "Technical Writer",
             "paypal_email": "sam.docs.sandbox@example.com", "share_pct": 30.0},
        ],
        "total_amount_usd": 500.00,
        "currency": "USD",
        "chat_text": (
            "[Slack #project-checkout-escrow]\n"
            "@maya.client (10:14 AM): Hey @alex.dev and @sam.docs! We need the PayPal Sandbox "
            "webhook verification & split-settlement module wrapped up today. Total milestone budget "
            "is $500 USD locked in a PayPal Authorization Hold.\n\n"
            "@alex.dev (10:16 AM): Sounds good! Sam and I agreed on a 70/30 split — so 70% ($350) "
            "to my PayPal sandbox account `alex.dev.sandbox@example.com` (Lead Backend Engineer) and "
            "30% ($150) to Sam at `sam.docs.sandbox@example.com` (Technical Writer).\n\n"
            "@sam.docs (10:17 AM): Confirmed on the 70/30 split! What are the exact acceptance criteria "
            "so PactPay can test our upload?\n\n"
            "@maya.client (10:19 AM): Let's make it a 4-point rubric (25% weight each):\n"
            "1. [25%] Webhook HMAC Verification: `verify_webhook_signature(payload: bytes, signature: str, "
            "webhook_secret: str) -> bool` using `hmac.compare_digest` and SHA-256 (hex digest) to validate incoming "
            "webhook payloads.\n"
            "2. [25%] Prorated Split Calculator: `calculate_prorated_split(authorized_amount_usd, audit_score, "
            "collaborator_weights)` where the score is 0-100 and the weights map each collaborator to a percentage; it "
            "returns `{\"captured_total_usd\": ..., \"allocations\": {name: usd}}`, both rounded to cents.\n"
            "3. [25%] Idempotent Event Deduplication: `process_webhook_event_idempotent(event_id, payload, "
            "processed_event_ids, handler=None)` tracks processed `event_id` values in the given set; the first delivery "
            "returns status `PROCESSED`, a repeat returns status `DUPLICATE_IGNORED` with `idempotent_replay: True`.\n"
            "4. [25%] Operations Runbook (`RUNBOOK_MARKDOWN`): an embedded Markdown string documenting "
            "`PAYPAL_CLIENT_ID`, `curl` verification commands, and the rollback/void procedure."
        ),
        "flawed_submission": {
            "filename": "webhook_deliverable_v1_draft.py",
            "label": "Draft v1 (Missing Idempotency Handler -> Scores 75/100 -> $375 Prorated Capture)",
            "expected_score": 75.0,
            "content": FLAWED_SUBMISSION_CODE,
        },
        "fixed_submission": {
            "filename": "webhook_deliverable_v2_final.py",
            "label": "Final v2 (All 4 Criteria Passing -> Scores 100/100 -> Full $500 Capture + $350/$150 Payout)",
            "expected_score": 100.0,
            "content": FIXED_SUBMISSION_CODE,
        },
    },
    "sdk_migration_docs": {
        "id": "sdk_migration_docs",
        "name": "Scenario 2: PayPal Orders v2 & Payouts API Migration Guide ($400 / 60-40 Split)",
        "badge": "Docs & DevRel Milestone",
        "title": "PayPal Orders v2 and Payouts migration guide",
        "summary": "A four-section Markdown guide; $400 split 60/40 between the architect and the editor.",
        "payees": [
            {"name": "Riley (Solutions Architect)", "role": "Solutions Architect",
             "paypal_email": "riley.arch.sandbox@example.com", "share_pct": 60.0},
            {"name": "Casey (API Technical Editor)", "role": "API Technical Editor",
             "paypal_email": "casey.ed.sandbox@example.com", "share_pct": 40.0},
        ],
        "total_amount_usd": 400.00,
        "currency": "USD",
        "chat_text": (
            "[Discord #devrel-bounty]\n"
            "@jordan.pm: Need a comprehensive Markdown migration guide for our merchants upgrading to "
            "PayPal Orders v2 (`intent: AUTHORIZE`) and Batch Payouts. Budget is $400 USD.\n"
            "@riley.arch: I'll write the architecture & API snippets for 60% ($240 -> "
            "`riley.arch.sandbox@example.com`, Solutions Architect) and @casey.ed will handle the "
            "editing and payout error recovery table for 40% ($160 -> `casey.ed.sandbox@example.com`, "
            "API Technical Editor).\n"
            "@jordan.pm: Deal! The 4 required sections (25% each) are:\n"
            "1. OAuth2 Sandbox Token Exchange (`https://api-m.sandbox.paypal.com/v1/oauth2/token`).\n"
            "2. Creating an Order with `intent: AUTHORIZE` (`/v2/checkout/orders`).\n"
            "3. Full & Partial Authorization Captures (`/v2/payments/authorizations`).\n"
            "4. Batch Payouts (`/v1/payments/payouts`) & Error Recovery (`RECEIVER_UNREGISTERED`)."
        ),
        "flawed_submission": {
            "filename": "migration_guide_draft.md",
            "label": "Draft Guide (Missing Payouts Section -> Scores 75/100 -> $300 Prorated Capture)",
            "expected_score": 75.0,
            "content": API_DOC_FLAWED,
        },
        "fixed_submission": {
            "filename": "migration_guide_final.md",
            "label": "Final Guide (Complete 4-Section Spec -> Scores 100/100 -> Full $400 Capture)",
            "expected_score": 100.0,
            "content": API_DOC_FIXED,
        },
    },
    "payouts_guide_ai": {
        "id": "payouts_guide_ai",
        "name": "Scenario 3: AI-judged Payouts integration guide ($300 / 50-50 Split)",
        "badge": "AI is the judge",
        "title": "Payouts integration guide for collaborator payments",
        "summary": "Four prose requirements no test can check; the AI reviewer judges each with verified quotes. "
                   "$300 split 50/50 between the writer and the reviewer.",
        "payees": [
            {"name": "Noor (Developer Advocate)", "role": "Developer Advocate",
             "paypal_email": "noor.devrel.sandbox@example.com", "share_pct": 50.0},
            {"name": "Omar (Technical Reviewer)", "role": "Technical Reviewer",
             "paypal_email": "omar.review.sandbox@example.com", "share_pct": 50.0},
        ],
        "total_amount_usd": 300.00,
        "currency": "USD",
        "chat_text": (
            "[Email thread: Payouts guide]\n"
            "Lina (client): We need a short Markdown guide for our team on paying collaborators with PayPal Payouts. "
            "Budget $300.\n"
            "Noor: Happy to. Omar will review it; split it 50% to me (noor.devrel.sandbox@example.com) and 50% to "
            "Omar (omar.review.sandbox@example.com).\n"
            "Lina: Agreed. Four things must be in it, equally weighted:\n"
            "1. How to get an OAuth2 access token from the Sandbox and that it expires.\n"
            "2. That an accepted Payouts batch is not proof of payment: read each item's transaction_status.\n"
            "3. How to retry after a timeout without paying anyone twice.\n"
            "4. That it targets the Sandbox and secrets stay out of the repository."
        ),
        "flawed_submission": {
            "filename": "payouts_guide_v1.md",
            "label": "Draft guide: says a created batch means everyone is paid, and retries with a NEW batch id",
            "expected_score": 50.0,
            "content": AI_GUIDE_WRONG,
        },
        "fixed_submission": {
            "filename": "payouts_guide_final.md",
            "label": "Final guide: all four requirements explained",
            "expected_score": 100.0,
            "content": AI_GUIDE_GOOD,
        },
    },
}


PRESET_PAYEE_EMAILS = {x["paypal_email"] for p in DEMO_PRESETS.values() for x in p["payees"]}


def get_preset(preset_id: str) -> dict[str, Any]:
    """Return a deep copy of a preset by ID, falling back to the primary demo preset."""
    preset = DEMO_PRESETS.get(preset_id) or DEMO_PRESETS["paypal_webhook_sprint"]
    return deepcopy(preset)


def list_presets() -> list[dict[str, Any]]:
    """Return all available demo presets in display order."""
    return [deepcopy(preset) for preset in DEMO_PRESETS.values()]
