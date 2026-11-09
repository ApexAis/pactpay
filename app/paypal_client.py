"""PayPal REST client for PactPay — Sandbox only (https://api-m.sandbox.paypal.com).

One code path for both modes: with sandbox credentials every call goes to PayPal's sandbox (`live_sandbox`); without
them the same calls go to an in-process simulator transport (`sandbox_simulation`, app/paypal_sim.py) that keeps
PayPal's rules (a buyer must approve before an authorization, PayPal-Request-Id replays, unique sender_batch_id,
asynchronous payout items). Every call is traced with its mode, so a simulated answer is never shown as a live one,
and a failed live call raises PayPalError with PayPal's own error instead of being replaced by simulated data.

Idempotency: every money call carries a PayPal-Request-Id derived from the pact and the step (never random), so a
retry after a crash or a timeout returns PayPal's original result instead of moving money twice.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.paypal_sim import SimulatedPayPal

SANDBOX = "https://api-m.sandbox.paypal.com"
WEBHOOK_HEADERS = {
    "auth_algo": "paypal-auth-algo",
    "cert_url": "paypal-cert-url",
    "transmission_id": "paypal-transmission-id",
    "transmission_sig": "paypal-transmission-sig",
    "transmission_time": "paypal-transmission-time",
}
REDACT_KEYS = {"access_token", "client_secret", "authorization", "nonce"}


class PayPalError(RuntimeError):
    """A PayPal call that did not succeed: PayPal's HTTP status and error, as PayPal answered."""

    def __init__(self, step: str, status_code: int, body: dict[str, Any], debug_id: str | None = None) -> None:
        self.step = step
        self.status_code = status_code
        self.name = str(body.get("name") or body.get("error") or "PAYPAL_ERROR")
        self.message = str(body.get("message") or body.get("error_description") or "")
        self.details = body.get("details") or []
        self.links = [x for x in body.get("links") or [] if isinstance(x, dict)]
        self.debug_id = debug_id or body.get("debug_id")
        super().__init__(f"PayPal {step} failed (HTTP {status_code} {self.name})")

    @property
    def issue(self) -> str | None:
        """The first `details[].issue` (e.g. ORDER_NOT_APPROVED), PayPal's precise reason."""
        for d in self.details:
            if isinstance(d, dict) and d.get("issue"):
                return str(d["issue"])
        return None

    @property
    def transient(self) -> bool:
        """A network failure or a PayPal 5xx/429: the same request (same PayPal-Request-Id) may be retried."""
        return self.status_code == 0 or self.status_code >= 500 or self.status_code == 429

    def to_dict(self) -> dict[str, Any]:
        return {"step": self.step, "paypal_status_code": self.status_code, "name": self.name, "issue": self.issue,
                "message": self.message[:300], "debug_id": self.debug_id,
                "links": [{"rel": x.get("rel"), "href": x.get("href")} for x in self.links][:4]}


def money(cents: int, currency: str) -> dict[str, str]:
    return {"currency_code": currency, "value": f"{cents // 100}.{cents % 100:02d}"}


def cents_of(value: Any) -> int:
    """PayPal's decimal string ("12.34") as integer cents."""
    text = str(value or "0").strip()
    negative = text.startswith("-")
    whole, _, frac = text.lstrip("-").partition(".")
    cents = int(whole or "0") * 100 + int((frac + "00")[:2])
    return -cents if negative else cents


def _redacted(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("[redacted]" if str(k).lower() in REDACT_KEYS else _redacted(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redacted(v) for v in obj]
    return obj


class PayPalClient:
    def __init__(self, settings: Settings | None = None, *, trace_sink: Any = None,
                 transport: httpx.BaseTransport | None = None, live: bool | None = None) -> None:
        self.settings = settings or get_settings()
        # live: the sandbox credentials are configured; tests may force it with a mock transport standing in for PayPal
        self.live = (bool(self.settings.has_paypal_credentials) and transport is None) if live is None else live
        self.simulator: SimulatedPayPal | None = None
        if transport is None and not self.live:
            self.simulator = SimulatedPayPal()
            transport = self.simulator
        elif isinstance(transport, SimulatedPayPal):
            self.simulator = transport
        self.mode = "live_sandbox" if self.live else "sandbox_simulation"
        self._http = httpx.Client(base_url=SANDBOX, timeout=25.0, transport=transport, trust_env=False)
        self._token: str | None = None
        self._token_until = 0.0
        self.trace_sink = trace_sink  # callable(**trace) -> None

    # --- plumbing ------------------------------------------------------------------------------------------------
    def _trace(self, **t: Any) -> None:
        if self.trace_sink is not None:
            self.trace_sink(**t)

    def _auth_header(self, pact_id: str | None) -> dict[str, str]:
        now = time.time()
        if not self._token or now >= self._token_until:
            t0 = time.perf_counter()
            auth = (self.settings.paypal_client_id, self.settings.paypal_client_secret) if self.live else ("sim", "sim")
            try:
                resp = self._http.post("/v1/oauth2/token", data={"grant_type": "client_credentials"}, auth=auth,
                                       headers={"Accept": "application/json"})
            except httpx.HTTPError as exc:
                self._trace(pact_id=pact_id, step="OAUTH2_TOKEN", method="POST", endpoint="/v1/oauth2/token",
                            status_code=0, mode=self.mode, duration_ms=(time.perf_counter() - t0) * 1000,
                            request={"grant_type": "client_credentials"}, response={"error": type(exc).__name__})
                raise PayPalError("OAUTH2_TOKEN", 0, {"name": "NETWORK_ERROR", "message": type(exc).__name__}) from exc
            body = _json(resp)
            self._trace(pact_id=pact_id, step="OAUTH2_TOKEN", method="POST", endpoint="/v1/oauth2/token",
                        status_code=resp.status_code, mode=self.mode,
                        paypal_debug_id=resp.headers.get("paypal-debug-id"),
                        duration_ms=(time.perf_counter() - t0) * 1000, request={"grant_type": "client_credentials"},
                        response=_redacted({k: v for k, v in body.items() if k != "scope"}))
            if resp.status_code != 200 or not body.get("access_token"):
                raise PayPalError("OAUTH2_TOKEN", resp.status_code, body, resp.headers.get("paypal-debug-id"))
            self._token = str(body["access_token"])
            self._token_until = now + max(60, int(body.get("expires_in", 3600)) - 120)
        return {"Authorization": f"Bearer {self._token}"}

    def call(self, step: str, method: str, path: str, *, pact_id: str | None = None, body: Any = None,
             request_id: str | None = None, raw_body: str | None = None,
             ok: tuple[int, ...] = (200, 201)) -> dict[str, Any]:
        headers = {**self._auth_header(pact_id), "Accept": "application/json", "Content-Type": "application/json"}
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        if method == "POST" and path.startswith("/v2/"):
            headers["Prefer"] = "return=representation"
        content = raw_body if raw_body is not None else (json.dumps(body) if body is not None else None)
        t0 = time.perf_counter()
        try:
            resp = self._http.request(method, path, content=content, headers=headers)
        except httpx.HTTPError as exc:
            self._trace(pact_id=pact_id, step=step, method=method, endpoint=path, status_code=0, mode=self.mode,
                        paypal_request_id=request_id, duration_ms=(time.perf_counter() - t0) * 1000,
                        request=_redacted(body or {}), response={"error": type(exc).__name__})
            raise PayPalError(step, 0, {"name": "NETWORK_ERROR", "message": type(exc).__name__}) from exc
        data = _json(resp)
        debug_id = resp.headers.get("paypal-debug-id")
        self._trace(pact_id=pact_id, step=step, method=method, endpoint=path, status_code=resp.status_code,
                    mode=self.mode, paypal_request_id=request_id, paypal_debug_id=debug_id,
                    duration_ms=(time.perf_counter() - t0) * 1000, request=_redacted(body or {}),
                    response=_redacted(data))
        if resp.status_code == 401:
            self._token = None  # an expired token: the next call takes a new one
        if resp.status_code not in ok:
            raise PayPalError(step, resp.status_code, data, debug_id)
        return data

    # --- Orders v2 -----------------------------------------------------------------------------------------------
    def create_order(self, *, pact_id: str, request_id: str, amount_cents: int, currency: str, title: str,
                     items: list[dict[str, Any]], return_url: str, cancel_url: str) -> dict[str, Any]:
        payload = {
            "intent": "AUTHORIZE",
            "purchase_units": [{
                "reference_id": pact_id,
                "custom_id": pact_id,
                "invoice_id": request_id,
                "description": f"PactPay milestone: {title}"[:127],
                "amount": {**money(amount_cents, currency),
                           "breakdown": {"item_total": money(amount_cents, currency)}},
                "items": items,
            }],
            "application_context": {"brand_name": "PactPay", "shipping_preference": "NO_SHIPPING",
                                    "user_action": "CONTINUE", "return_url": return_url, "cancel_url": cancel_url},
        }
        return self.call("CREATE_ORDER", "POST", "/v2/checkout/orders", pact_id=pact_id, body=payload,
                         request_id=request_id)

    def get_order(self, order_id: str, *, pact_id: str | None = None) -> dict[str, Any]:
        return self.call("GET_ORDER", "GET", f"/v2/checkout/orders/{order_id}", pact_id=pact_id)

    def authorize_order(self, order_id: str, *, pact_id: str, request_id: str) -> dict[str, Any]:
        return self.call("AUTHORIZE_ORDER", "POST", f"/v2/checkout/orders/{order_id}/authorize", pact_id=pact_id,
                         body={}, request_id=request_id)

    # --- Payments v2 ---------------------------------------------------------------------------------------------
    def get_authorization(self, authorization_id: str, *, pact_id: str | None = None) -> dict[str, Any]:
        return self.call("GET_AUTHORIZATION", "GET", f"/v2/payments/authorizations/{authorization_id}",
                         pact_id=pact_id)

    def capture_authorization(self, authorization_id: str, *, pact_id: str, request_id: str, amount_cents: int,
                              currency: str, note: str) -> dict[str, Any]:
        payload = {"amount": money(amount_cents, currency), "final_capture": True, "invoice_id": request_id,
                   "note_to_payer": note[:255]}
        return self.call("CAPTURE_AUTHORIZATION", "POST", f"/v2/payments/authorizations/{authorization_id}/capture",
                         pact_id=pact_id, body=payload, request_id=request_id)

    def get_capture(self, capture_id: str, *, pact_id: str | None = None) -> dict[str, Any]:
        return self.call("GET_CAPTURE", "GET", f"/v2/payments/captures/{capture_id}", pact_id=pact_id)

    def void_authorization(self, authorization_id: str, *, pact_id: str, request_id: str) -> dict[str, Any]:
        return self.call("VOID_AUTHORIZATION", "POST", f"/v2/payments/authorizations/{authorization_id}/void",
                         pact_id=pact_id, body={}, request_id=request_id, ok=(200, 201, 204))

    # --- Payouts v1 ----------------------------------------------------------------------------------------------
    def create_payout(self, *, pact_id: str, sender_batch_id: str, items: list[dict[str, Any]],
                      subject: str) -> dict[str, Any]:
        payload = {"sender_batch_header": {"sender_batch_id": sender_batch_id, "recipient_type": "EMAIL",
                                           "email_subject": subject[:255],
                                           "email_message": "Your share of a PactPay milestone the client released."},
                   "items": items}
        return self.call("CREATE_PAYOUT", "POST", "/v1/payments/payouts", pact_id=pact_id, body=payload,
                         request_id=sender_batch_id)

    def get_payout_batch(self, payout_batch_id: str, *, pact_id: str | None = None) -> dict[str, Any]:
        return self.call("GET_PAYOUT_BATCH", "GET", f"/v1/payments/payouts/{payout_batch_id}", pact_id=pact_id)

    def get_payout_item(self, payout_item_id: str, *, pact_id: str | None = None) -> dict[str, Any]:
        return self.call("GET_PAYOUT_ITEM", "GET", f"/v1/payments/payouts-item/{payout_item_id}", pact_id=pact_id)

    # --- Webhooks ------------------------------------------------------------------------------------------------
    def verify_webhook(self, headers: dict[str, str], raw_body: bytes) -> dict[str, Any]:
        """PayPal's own verdict on a webhook delivery (POST /v1/notifications/verify-webhook-signature).

        The event goes to PayPal byte for byte as it arrived (re-serialising it can break the signature). Fails closed:
        no webhook id configured, a missing PAYPAL-* header, a body that is not JSON or a PayPal error all answer
        {"verified": False, "verification_status": "FAILURE"}."""
        webhook_id = self.settings.paypal_webhook_id.strip() if self.live else SimulatedPayPal.WEBHOOK_ID
        if not webhook_id:
            return {"verified": False, "verification_status": "FAILURE",
                    "reason": "PAYPAL_WEBHOOK_ID is not configured, so no delivery can be verified"}
        low = {k.lower(): v for k, v in headers.items()}
        fields = {name: low.get(header, "") for name, header in WEBHOOK_HEADERS.items()}
        missing = [h for n, h in WEBHOOK_HEADERS.items() if not fields[n]]
        if missing:
            return {"verified": False, "verification_status": "FAILURE",
                    "reason": f"missing PayPal signature headers: {', '.join(missing)}"}
        try:
            text = raw_body.decode("utf-8")
            if not isinstance(json.loads(text), dict):
                raise ValueError
        except ValueError:
            return {"verified": False, "verification_status": "FAILURE", "reason": "the body is not a JSON event"}
        fields["webhook_id"] = webhook_id
        raw = json.dumps(fields)[:-1] + ', "webhook_event": ' + text + "}"
        try:
            answer = self.call("VERIFY_WEBHOOK_SIGNATURE", "POST", "/v1/notifications/verify-webhook-signature",
                               raw_body=raw, body={k: v for k, v in fields.items() if k != "transmission_sig"})
        except PayPalError as exc:
            return {"verified": False, "verification_status": "FAILURE", "reason": f"PayPal answered {exc.name}"}
        status = str(answer.get("verification_status") or "FAILURE")
        return {"verified": status == "SUCCESS", "verification_status": status,
                "reason": None if status == "SUCCESS" else "PayPal did not verify the signature"}


def _json(resp: httpx.Response) -> dict[str, Any]:
    if not resp.content:
        return {}
    try:
        data = resp.json()
    except ValueError:
        return {"message": resp.text[:300]}
    return data if isinstance(data, dict) else {"data": data}
