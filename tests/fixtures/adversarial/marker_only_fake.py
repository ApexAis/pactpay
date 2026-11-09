"""Marker-only fake: carries every string the old auditor looked for, implements nothing."""

MARKERS = (
    "def verify_webhook_signature compare_digest sha256 hmac "
    "def calculate_prorated_split captured_total_usd allocations "
    "def process_webhook_event_idempotent event_id DUPLICATE_IGNORED idempotent_replay "
    "RUNBOOK_MARKDOWN PAYPAL_CLIENT_ID curl verify-webhook-signature /void rollback"
)


def verify_webhook_signature(payload, signature, webhook_secret):
    return True


def calculate_prorated_split(authorized_amount_usd, audit_score, collaborator_weights):
    return True


def process_webhook_event_idempotent(event_id, payload, processed_event_ids, handler=None):
    return True


RUNBOOK_MARKDOWN = MARKERS
