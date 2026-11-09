"""Acceptance tests for PactPay's two demo presets (``app/presets.py``), as evaluation criteria.

Each ``tests`` source runs in the sandboxed runner. Every test takes ``sub``, a proxy to the submission running in a
separate sandboxed worker. Inputs are random per evaluation (secrets, payloads, amounts, scores, weights, event ids),
and every expected value is computed here in the runner. A submission that hard-codes answers to the demo inputs
therefore fails. Helpers available in the test namespace: ``md_sections``, ``Ref``, ``RemoteError``,
``RemoteTimeout``, ``RemoteObject``; ``random`` is seeded per evaluation.

The tests follow the FIXED submission's signatures:
  verify_webhook_signature(payload: bytes, signature: str, webhook_secret: str) -> bool   (hex HMAC-SHA256)
  calculate_prorated_split(authorized_amount_usd, audit_score, collaborator_weights) ->
      {"captured_total_usd": float, "allocations": {name: float}}
  process_webhook_event_idempotent(event_id, payload, processed_event_ids, handler=None) ->
      {"status": "PROCESSED"|"DUPLICATE_IGNORED", "idempotent_replay": bool, ...}
  RUNBOOK_MARKDOWN: str
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

WEBHOOK_HMAC_TESTS = r'''
import hashlib
import hmac
import json
import random
import string

_ALPHABET = string.ascii_letters + string.digits


def _token(n):
    return "".join(random.choice(_ALPHABET) for _ in range(n))


def _payload():
    event = {
        "id": "WH-" + _token(17),
        "event_type": random.choice(["PAYMENT.CAPTURE.COMPLETED", "PAYMENT.AUTHORIZATION.CREATED",
                                     "PAYMENT.CAPTURE.REFUNDED", "CHECKOUT.ORDER.APPROVED"]),
        "resource": {"id": _token(17), "amount": {"currency_code": "USD",
                                                   "value": "%.2f" % random.uniform(1, 5000)}},
        "nonce": _token(24),
    }
    return json.dumps(event, separators=(",", ":")).encode("utf-8")


def _sign(payload, secret):
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def test_accepts_valid_signatures(sub):
    for _ in range(6):
        secret = "whsec_" + _token(random.randint(16, 40))
        payload = _payload()
        result = sub.verify_webhook_signature(payload, _sign(payload, secret), secret)
        assert result is True, "a correct HMAC-SHA256 signature was not accepted (got %r)" % (result,)


def test_rejects_tampered_payload_or_signature(sub):
    for _ in range(6):
        secret = "whsec_" + _token(24)
        payload = _payload()
        signature = _sign(payload, secret)
        position = random.randrange(len(payload))
        tampered = payload[:position] + bytes([payload[position] ^ 0x01]) + payload[position + 1:]
        assert sub.verify_webhook_signature(tampered, signature, secret) is False, "a tampered payload was accepted"
        index = random.randrange(len(signature))
        flipped = signature[:index] + ("0" if signature[index] != "0" else "1") + signature[index + 1:]
        assert sub.verify_webhook_signature(payload, flipped, secret) is False, "a tampered signature was accepted"
        weak = hmac.new(secret.encode("utf-8"), payload, hashlib.sha1).hexdigest()
        assert sub.verify_webhook_signature(payload, weak, secret) is False, "an HMAC-SHA1 signature was accepted"


def test_rejects_wrong_key(sub):
    for _ in range(6):
        payload = _payload()
        signer = "whsec_" + _token(24)
        other = "whsec_" + _token(24)
        result = sub.verify_webhook_signature(payload, _sign(payload, signer), other)
        assert result is False, "a signature made with another key was accepted (got %r)" % (result,)


def test_rejects_missing_inputs(sub):
    secret = "whsec_" + _token(24)
    payload = _payload()
    signature = _sign(payload, secret)
    assert sub.verify_webhook_signature(b"", signature, secret) is False, "an empty payload was accepted"
    assert sub.verify_webhook_signature(payload, "", secret) is False, "an empty signature was accepted"
    assert sub.verify_webhook_signature(payload, signature, "") is False, "an empty secret was accepted"
'''

WEBHOOK_SPLIT_TESTS = r'''
import random

TOLERANCE = 0.0101  # one cent: an implementation may round half-up instead of half-even


def _expected_total(amount, score):
    clamped = max(0.0, min(100.0, float(score)))
    return round(amount * (clamped / 100.0), 2)


def _weights():
    count = random.randint(2, 4)
    cuts = sorted(random.sample(range(1, 100), count - 1))
    parts = [b - a for a, b in zip([0] + cuts, cuts + [100])]
    names = ["collab%d.%s@example.com" % (i, "".join(random.choice("abcdefghij") for _ in range(6)))
             for i in range(count)]
    return {name: float(pct) for name, pct in zip(names, parts)}


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check(sub, amount, score, weights):
    result = sub.calculate_prorated_split(amount, score, weights)
    assert isinstance(result, dict), "expected a dict, got %r" % (result,)
    total = _expected_total(amount, score)
    got_total = result.get("captured_total_usd")
    assert _number(got_total) and abs(got_total - total) <= TOLERANCE, (
        "captured_total_usd for %.2f USD at score %s: expected %.2f, got %r" % (amount, score, total, got_total))
    allocations = result.get("allocations")
    assert isinstance(allocations, dict) and set(allocations) == set(weights), (
        "allocations must have exactly one entry per collaborator, got %r" % (allocations,))
    for name, pct in weights.items():
        share = round(got_total * (pct / 100.0), 2)
        got = allocations[name]
        assert _number(got) and abs(got - share) <= TOLERANCE, (
            "share of a %.0f%% collaborator of %.2f USD: expected %.2f, got %r" % (pct, got_total, share, got))


def test_prorated_split_random_cases(sub):
    for _ in range(30):
        amount = round(random.uniform(5.0, 10000.0), 2)
        score = round(random.uniform(0.0, 100.0), random.choice([0, 1, 2]))
        _check(sub, amount, score, _weights())


def test_full_and_zero_score(sub):
    for _ in range(5):
        amount = round(random.uniform(5.0, 10000.0), 2)
        weights = _weights()
        _check(sub, amount, 100, weights)
        _check(sub, amount, 0, weights)


def test_score_is_clamped(sub):
    for _ in range(8):
        amount = round(random.uniform(5.0, 10000.0), 2)
        weights = _weights()
        _check(sub, amount, round(random.uniform(100.5, 500.0), 1), weights)
        _check(sub, amount, round(random.uniform(-500.0, -0.5), 1), weights)
'''

WEBHOOK_IDEMPOTENCY_TESTS = r'''
import random
import string


def _event_id():
    return "WH-" + "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(17))


def _payload(event_id):
    return {"id": event_id, "event_type": "PAYMENT.CAPTURE.COMPLETED",
            "resource": {"amount": {"value": "%.2f" % random.uniform(1, 5000), "currency_code": "USD"}}}


def test_first_delivery_processed_then_replay_ignored(sub):
    seen = sub._new("set")
    event_id = _event_id()
    payload = _payload(event_id)
    first = sub.process_webhook_event_idempotent(event_id, payload, seen)
    assert isinstance(first, dict) and first.get("status") == "PROCESSED", "first delivery returned %r" % (first,)
    assert first.get("idempotent_replay") is not True, "the first delivery was marked as a replay"
    assert event_id in sub._value(seen), "the processed event_id was not recorded in the set"
    second = sub.process_webhook_event_idempotent(event_id, payload, seen)
    assert isinstance(second, dict) and second.get("status") == "DUPLICATE_IGNORED", "the replay returned %r" % (second,)
    assert second.get("idempotent_replay") is True, "the replay was not flagged idempotent_replay=True"


def test_distinct_events_tracked_independently(sub):
    seen = sub._new("set")
    ids = [_event_id() for _ in range(random.randint(4, 8))]
    for event_id in ids:
        result = sub.process_webhook_event_idempotent(event_id, _payload(event_id), seen)
        assert isinstance(result, dict) and result.get("status") == "PROCESSED", "a new event_id returned %r" % (result,)
    assert sub._value(seen) == set(ids), "the set must hold exactly the processed event_ids"
    for event_id in random.sample(ids, len(ids)):
        result = sub.process_webhook_event_idempotent(event_id, _payload(event_id), seen)
        assert isinstance(result, dict) and result.get("status") == "DUPLICATE_IGNORED", "a replay returned %r" % (result,)
        assert result.get("idempotent_replay") is True, "a replay was not flagged idempotent_replay=True"


def test_handler_runs_once_per_event(sub):
    seen = sub._new("set")
    marker = "".join(random.choice(string.ascii_lowercase) for _ in range(12))
    handler = sub._new("recorder", {"handled": marker})
    event_id = _event_id()
    payload = _payload(event_id)
    for _ in range(3):
        sub.process_webhook_event_idempotent(event_id, payload, seen, handler=handler)
    calls = sub._value(handler)
    assert len(calls) == 1, "the handler ran %d times for one event_id (expected once)" % len(calls)
    call = calls[0]
    assert payload in call["args"] or payload in call["kwargs"].values(), "the handler did not receive the payload"


def test_already_recorded_event_is_ignored(sub):
    event_id = _event_id()
    seen = sub._new("set", [event_id])
    handler = sub._new("recorder", None)
    result = sub.process_webhook_event_idempotent(event_id, _payload(event_id), seen, handler=handler)
    assert isinstance(result, dict) and result.get("status") == "DUPLICATE_IGNORED", "got %r" % (result,)
    assert result.get("idempotent_replay") is True, "the replay was not flagged idempotent_replay=True"
    assert sub._value(handler) == [], "the handler ran for an event_id that was already processed"
'''

WEBHOOK_RUNBOOK_TESTS = r'''
import re


def _runbook(sub):
    text = sub.RUNBOOK_MARKDOWN
    assert isinstance(text, str), "RUNBOOK_MARKDOWN must be a string, got %r" % (text,)
    return text


def test_runbook_is_a_markdown_document(sub):
    sections = md_sections(_runbook(sub))
    assert len(sections) >= 2, "RUNBOOK_MARKDOWN needs Markdown headings (found %d)" % len(sections)
    filled = [h for h, body in sections.items() if len(body.split()) >= 5]
    assert len(filled) >= 2, "the runbook's sections are empty"


def test_runbook_documents_paypal_client_id(sub):
    sections = md_sections(_runbook(sub))
    assert any("PAYPAL_CLIENT_ID" in body for body in sections.values()), (
        "PAYPAL_CLIENT_ID is not documented in a runbook section")


def test_runbook_has_curl_verification_command(sub):
    commands = re.findall(r"curl\b[^\n`]*?https?://[^\s`'\"]+", _runbook(sub))
    assert any("verif" in command.lower() for command in commands), (
        "no curl command that calls a verification endpoint (found %d curl command(s))" % len(commands))


def test_runbook_documents_void_rollback(sub):
    sections = md_sections(_runbook(sub))
    ok = any(re.search(r"/void\b", body) and re.search(r"roll\s*-?\s*back", heading + "\n" + body, re.I)
             for heading, body in sections.items())
    assert ok, "no rollback section that documents the /void call"
'''

_DOC_HELPERS = r'''
import re


def _prose_words(body):
    return re.findall(r"[A-Za-z]{2,}", re.sub(r"`[^`]*`", " ", body))


def _section_with(sub, *needles):
    """(heading, body) of the first section whose body has every needle and at least five words of prose."""
    for heading, body in sub.sections.items():
        if all(n.lower() in body.lower() for n in needles) and len(_prose_words(body)) >= 5:
            return heading, body
    mentioned = all(n.lower() in sub.text.lower() for n in needles)
    raise AssertionError("no section documents %s%s" % (
        ", ".join(needles), " (it is mentioned, but not in a section with prose)" if mentioned else ""))
'''

DOCS_OAUTH_TESTS = _DOC_HELPERS + r'''

def test_oauth_token_exchange_documented(sub):
    heading, body = _section_with(sub, "api-m.sandbox.paypal.com/v1/oauth2/token")
    assert re.search(r"client[_ ]?(id|secret|credentials)", body, re.I), (
        "the token section does not say which client credentials to send")
'''

DOCS_ORDER_TESTS = _DOC_HELPERS + r'''

def test_create_order_with_authorize_intent(sub):
    heading, body = _section_with(sub, "/v2/checkout/orders")
    assert re.search(r"intent\W{0,6}AUTHORIZE\b", body), "the order section does not set intent AUTHORIZE"
'''

DOCS_CAPTURE_TESTS = _DOC_HELPERS + r'''

def test_full_and_partial_captures(sub):
    heading, body = _section_with(sub, "/v2/payments/authorizations")
    assert re.search(r"/v2/payments/authorizations/\S*capture", body), "the capture call is not documented"
    assert re.search(r"\bpartial\b", body, re.I), "partial captures are not covered"
    assert re.search(r"final_capture|\bfull\b", body, re.I), "full (final) captures are not covered"
'''

DOCS_PAYOUTS_TESTS = _DOC_HELPERS + r'''

def test_batch_payouts_and_error_recovery(sub):
    heading, body = _section_with(sub, "/v1/payments/payouts")
    assert "RECEIVER_UNREGISTERED" in body, "RECEIVER_UNREGISTERED recovery is not documented"
    assert re.search(r"\b(handle|handling|retry|retries|recover|recovery|resolve|resubmit|re-?send)\b", body, re.I), (
        "the payouts section's text gives no error-recovery guidance (a heading alone does not count)")
'''

PRESET_CRITERIA: dict[str, list[dict[str, Any]]] = {
    "payouts_guide_ai": [
        {'id': 'A1', 'title': 'OAuth2 token', 'description': "Explains how to obtain an OAuth2 access token from the PayPal Sandbox: a client-credentials POST to the /v1/oauth2/token endpoint with the app's client id and secret, and that the token expires and must be requested again.", 'weight_pct': 25.0, 'method': 'ai_review', 'tests': None, 'source_quote': 'How to get an OAuth2 access token from the Sandbox and that it expires.'},
        {'id': 'A2', 'title': 'Payout item status', 'description': "Explains that an accepted Payouts batch is not proof of payment and that each item's final transaction_status (SUCCESS, UNCLAIMED, FAILED, RETURNED ...) must be read back from PayPal per recipient.", 'weight_pct': 25.0, 'method': 'ai_review', 'tests': None, 'source_quote': "That an accepted Payouts batch is not proof of payment: read each item's transaction_status."},
        {'id': 'A3', 'title': 'Safe retry', 'description': "Describes how to retry a payout after a timeout without paying twice: reuse the same sender_batch_id (PayPal refuses a duplicate) and read PayPal's record before sending anything new.", 'weight_pct': 25.0, 'method': 'ai_review', 'tests': None, 'source_quote': 'How to retry after a timeout without paying anyone twice.'},
        {'id': 'A4', 'title': 'Sandbox and secrets', 'description': 'States that the guide targets the Sandbox (api-m.sandbox.paypal.com) and that client secrets are kept in environment variables, never committed to the repository.', 'weight_pct': 25.0, 'method': 'ai_review', 'tests': None, 'source_quote': 'That it targets the Sandbox and secrets stay out of the repository.'},
    ],
    "paypal_webhook_sprint": [
        {
            "id": "C1",
            "title": "Webhook HMAC Verification",
            "description": "`verify_webhook_signature(payload, signature, webhook_secret)` validates incoming webhook "
                           "payloads with HMAC-SHA256 and `hmac.compare_digest`: it accepts a correct hex signature "
                           "and rejects a tampered payload, a tampered signature and a wrong key.",
            "weight_pct": 25.0,
            "method": "tests",
            "tests": WEBHOOK_HMAC_TESTS,
        },
        {
            "id": "C2",
            "title": "Prorated Split Calculator",
            "description": "`calculate_prorated_split(authorized_amount_usd, audit_score, collaborator_weights)` "
                           "returns `captured_total_usd` (the amount prorated by the score, clamped to 0-100) and "
                           "each collaborator's USD share in `allocations`.",
            "weight_pct": 25.0,
            "method": "tests",
            "tests": WEBHOOK_SPLIT_TESTS,
        },
        {
            "id": "C3",
            "title": "Idempotent Event Deduplication",
            "description": "`process_webhook_event_idempotent(event_id, payload, processed_event_ids, handler=None)` "
                           "tracks processed `event_id` values in the set and prevents duplicate webhook replay "
                           "execution (PROCESSED first, DUPLICATE_IGNORED with idempotent_replay=True after).",
            "weight_pct": 25.0,
            "method": "tests",
            "tests": WEBHOOK_IDEMPOTENCY_TESTS,
        },
        {
            "id": "C4",
            "title": "Operations Runbook",
            "description": "`RUNBOOK_MARKDOWN` is an embedded Markdown runbook documenting `PAYPAL_CLIENT_ID`, a "
                           "`curl` verification command and the rollback/void procedure.",
            "weight_pct": 25.0,
            "method": "tests",
            "tests": WEBHOOK_RUNBOOK_TESTS,
        },
    ],
    "sdk_migration_docs": [
        {
            "id": "C1",
            "title": "OAuth2 Sandbox Token Exchange",
            "description": "A section documents the token exchange against "
                           "`https://api-m.sandbox.paypal.com/v1/oauth2/token` with the client credentials.",
            "weight_pct": 25.0,
            "method": "tests",
            "tests": DOCS_OAUTH_TESTS,
        },
        {
            "id": "C2",
            "title": "Creating an Order with intent AUTHORIZE",
            "description": "A section documents creating an order via `/v2/checkout/orders` with `intent: AUTHORIZE`.",
            "weight_pct": 25.0,
            "method": "tests",
            "tests": DOCS_ORDER_TESTS,
        },
        {
            "id": "C3",
            "title": "Full & Partial Authorization Captures",
            "description": "A section documents capturing authorizations via `/v2/payments/authorizations/.../capture`, "
                           "covering full (final) and partial captures.",
            "weight_pct": 25.0,
            "method": "tests",
            "tests": DOCS_CAPTURE_TESTS,
        },
        {
            "id": "C4",
            "title": "Batch Payouts & Error Recovery",
            "description": "A section documents batch payouts via `/v1/payments/payouts` and recovery from errors such "
                           "as `RECEIVER_UNREGISTERED`.",
            "weight_pct": 25.0,
            "method": "tests",
            "tests": DOCS_PAYOUTS_TESTS,
        },
    ],
}


def preset_criteria(preset_id: str) -> list[dict[str, Any]]:
    """A deep copy of a preset's criteria (KeyError for an unknown preset id)."""
    return deepcopy(PRESET_CRITERIA[preset_id])
