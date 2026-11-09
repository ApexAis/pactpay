"""An in-process PayPal Sandbox simulator, used only when no sandbox credentials are configured.

It is an httpx transport, so PactPay's real client code runs unchanged against it, and it keeps the PayPal rules
that the app's safety depends on, so the offline tests exercise the same failure paths as the sandbox:
  * an order must be approved by the buyer before `authorize` (else 422 ORDER_NOT_APPROVED);
  * a POST replayed with the same PayPal-Request-Id returns the original answer and moves no money again;
  * a final capture closes the authorization (a second capture is 422 AUTHORIZATION_ALREADY_CAPTURED);
  * a payout sender_batch_id is accepted once; payout items start PENDING and settle when the batch is read
    (SUCCESS for a registered sandbox account, UNCLAIMED for an unknown e-mail, FAILED for a '+fail' address);
  * webhook deliveries it emits are signed, and verify-webhook-signature verifies only those.
Everything it answers is traced as `sandbox_simulation` by the client; none of it is ever shown as a live result.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import uuid
import zlib
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

FEE_PERCENT_BP = 349  # 3.49 % + 0.49: PayPal's published US rate, used here only as a simulated fee
FEE_FIXED_CENTS = 49
PAYOUT_FEE_CENTS = 25


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pp_id(n: int = 17) -> str:
    alphabet = "0123456789ABCDEFGHJKLMNPRSTUVWXYZ"
    return "".join(secrets.choice(alphabet) for _ in range(n))


def _money(cents: int, currency: str, key: str = "currency_code") -> dict[str, str]:
    return {key: currency, "value": f"{cents // 100}.{cents % 100:02d}"}


def _cents(value: Any) -> int:
    whole, _, frac = str(value or "0").partition(".")
    return int(whole or "0") * 100 + int((frac + "00")[:2])


def _error(status: int, name: str, issue: str, description: str) -> tuple[int, dict[str, Any]]:
    return status, {"name": name, "message": description, "debug_id": f"sim{uuid.uuid4().hex[:12]}",
                    "details": [{"issue": issue, "description": description}]}


class SimulatedPayPal(httpx.BaseTransport):
    WEBHOOK_ID = "SIMULATED-WEBHOOK-ID"

    def __init__(self, registered: set[str] | None = None) -> None:
        self._lock = threading.Lock()
        self._key = secrets.token_bytes(32)  # signs the simulator's own webhook deliveries
        self.orders: dict[str, dict[str, Any]] = {}
        self.authorizations: dict[str, dict[str, Any]] = {}
        self.captures: dict[str, dict[str, Any]] = {}
        self.batches: dict[str, dict[str, Any]] = {}
        self.items: dict[str, dict[str, Any]] = {}
        self.sender_batches: dict[str, str] = {}
        self.replays: dict[tuple[str, str, str], tuple[int, dict[str, Any]]] = {}
        self.outbox: list[dict[str, Any]] = []  # signed webhook deliveries waiting to be posted to the app
        self.registered = set(registered or ())
        self.capture_status = "COMPLETED"  # tests set PENDING or DECLINED
        self.drop_next: str | None = None  # a step whose answer is lost once after PayPal processed it
        self.fail_next: str | None = None  # a step that fails once with a 503 before PayPal processes it
        self.calls: list[tuple[str, str]] = []

    # --- the transport -------------------------------------------------------------------------------------------
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        rid = request.headers.get("paypal-request-id")
        with self._lock:
            self.calls.append((method, path))
            step = self._step(method, path)
            if self.fail_next and self.fail_next == step:
                self.fail_next = None
                return httpx.Response(503, json={"name": "SERVICE_UNAVAILABLE", "message": "simulated outage"})
            replayable = path != "/v1/payments/payouts"  # sandbox 2026-11-07: a repeated batch is refused, not replayed
            if rid and method == "POST" and replayable and (method, path, rid) in self.replays:
                status, body = self.replays[(method, path, rid)]
            else:
                try:
                    status, body = self._route(method, path, request)
                except (ValueError, KeyError, TypeError) as exc:
                    status, body = _error(400, "INVALID_REQUEST", "MALFORMED_REQUEST_JSON", str(exc)[:200])
                if rid and method == "POST" and status < 300:
                    self.replays[(method, path, rid)] = (status, body)
            if self.drop_next and self.drop_next == step:
                self.drop_next = None
                raise httpx.ReadTimeout("simulated lost response", request=request)
        return httpx.Response(status, json=body, headers={"paypal-debug-id": f"sim{uuid.uuid4().hex[:12]}"})

    @staticmethod
    def _step(method: str, path: str) -> str:
        if path.endswith("/authorize"):
            return "AUTHORIZE_ORDER"
        if path.endswith("/capture"):
            return "CAPTURE_AUTHORIZATION"
        if path.endswith("/void"):
            return "VOID_AUTHORIZATION"
        if path == "/v1/payments/payouts" and method == "POST":
            return "CREATE_PAYOUT"
        if path == "/v2/checkout/orders" and method == "POST":
            return "CREATE_ORDER"
        return f"{method} {path}"

    def _route(self, method: str, path: str, request: httpx.Request) -> tuple[int, dict[str, Any]]:
        parts = [p for p in path.split("/") if p]
        body = json.loads(request.content) if request.content and method == "POST" and not path.endswith("/token") else {}
        if path == "/v1/oauth2/token":
            return 200, {"access_token": f"SIM-{secrets.token_hex(16)}", "token_type": "Bearer", "expires_in": 32400,
                         "app_id": "APP-SIMULATED"}
        if path == "/v2/checkout/orders" and method == "POST":
            return self._create_order(body)
        if parts[:3] == ["v2", "checkout", "orders"] and len(parts) == 4 and method == "GET":
            order = self.orders.get(parts[3])
            return (200, self._order_view(order)) if order else self._not_found()
        if parts[:3] == ["v2", "checkout", "orders"] and len(parts) == 5 and parts[4] == "authorize":
            return self._authorize(parts[3])
        if parts[:3] == ["v2", "payments", "authorizations"] and len(parts) == 4 and method == "GET":
            auth = self.authorizations.get(parts[3])
            return (200, self._auth_view(auth)) if auth else self._not_found()
        if parts[:3] == ["v2", "payments", "authorizations"] and len(parts) == 5 and parts[4] == "capture":
            return self._capture(parts[3], body)
        if parts[:3] == ["v2", "payments", "authorizations"] and len(parts) == 5 and parts[4] == "void":
            return self._void(parts[3])
        if parts[:3] == ["v2", "payments", "captures"] and len(parts) == 4:
            cap = self.captures.get(parts[3])
            return (200, dict(cap)) if cap else self._not_found()
        if path == "/v1/payments/payouts" and method == "POST":
            return self._create_payout(body)
        if parts[:3] == ["v1", "payments", "payouts"] and len(parts) == 4 and method == "GET":
            return self._read_batch(parts[3])
        if parts[:3] == ["v1", "payments", "payouts-item"] and len(parts) == 4:
            item = self.items.get(parts[3])
            return (200, self._item_view(item)) if item else self._not_found()
        if path == "/v1/notifications/verify-webhook-signature":
            return 200, {"verification_status": "SUCCESS" if self._verify(body) else "FAILURE"}
        return self._not_found()

    @staticmethod
    def _not_found() -> tuple[int, dict[str, Any]]:
        return _error(404, "RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID", "Specified resource ID does not exist.")

    # --- orders and authorizations -------------------------------------------------------------------------------
    def _create_order(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if body.get("intent") != "AUTHORIZE":
            return _error(422, "UNPROCESSABLE_ENTITY", "INTENT_NOT_SUPPORTED", "PactPay orders use intent AUTHORIZE.")
        unit = body["purchase_units"][0]
        cents = _cents(unit["amount"]["value"])
        if cents <= 0:
            return _error(422, "UNPROCESSABLE_ENTITY", "AMOUNT_NOT_POSITIVE", "The amount must be positive.")
        oid = _pp_id()
        first = "PAYER_ACTION_REQUIRED" if "paypal" in (body.get("payment_source") or {}) else "CREATED"
        self.orders[oid] = {"id": oid, "intent": "AUTHORIZE", "status": first, "unit": unit,
                            "cents": cents, "currency": unit["amount"]["currency_code"], "payer": None,
                            "authorization_id": None, "create_time": _now()}
        return 201, self._order_view(self.orders[oid])

    def approve_order(self, order_id: str, payer_email: str = "buyer@simulated.example") -> dict[str, Any]:
        """The buyer's approval in PayPal Checkout, simulated (only the demo's 'simulated buyer' button calls it)."""
        with self._lock:
            order = self.orders.get(order_id)
            if not order:
                raise KeyError(order_id)
            if order["status"] in ("PAYER_ACTION_REQUIRED", "CREATED"):
                order["status"] = "APPROVED"
                order["payer"] = {"email_address": payer_email, "payer_id": _pp_id(13),
                                  "name": {"given_name": "Simulated", "surname": "Buyer"}}
            return self._order_view(order)

    def _order_view(self, order: dict[str, Any]) -> dict[str, Any]:
        oid = order["id"]
        unit = {"reference_id": order["unit"].get("reference_id"), "custom_id": order["unit"].get("custom_id"),
                "invoice_id": order["unit"].get("invoice_id"), "amount": _money(order["cents"], order["currency"])}
        if order["authorization_id"]:
            aid = order["authorization_id"]
            unit["payments"] = {"authorizations": [self._auth_view(self.authorizations[aid])]}
            caps = [dict(c) for c in self.captures.values()
                    if c["supplementary_data"]["related_ids"]["authorization_id"] == aid]
            if caps:
                unit["payments"]["captures"] = caps
        view: dict[str, Any] = {"id": oid, "intent": "AUTHORIZE", "status": order["status"],
                                "purchase_units": [unit], "create_time": order["create_time"],
                                "links": [{"href": f"https://api-m.sandbox.paypal.com/v2/checkout/orders/{oid}",
                                           "rel": "self", "method": "GET"}]}
        if order["status"] in ("PAYER_ACTION_REQUIRED", "CREATED"):
            rel = "payer-action" if order["status"] == "PAYER_ACTION_REQUIRED" else "approve"
            view["links"].append({"href": f"https://www.sandbox.paypal.com/checkoutnow?token={oid}",
                                  "rel": rel, "method": "GET"})
        if order["payer"]:
            view["payer"] = order["payer"]
        return view

    def _authorize(self, order_id: str) -> tuple[int, dict[str, Any]]:
        order = self.orders.get(order_id)
        if not order:
            return self._not_found()
        if order["status"] in ("PAYER_ACTION_REQUIRED", "CREATED"):
            return _error(422, "UNPROCESSABLE_ENTITY", "ORDER_NOT_APPROVED",
                          "Payer has not yet approved the Order for payment.")
        if order["status"] == "COMPLETED":
            return _error(422, "UNPROCESSABLE_ENTITY", "ORDER_ALREADY_AUTHORIZED", "Order already authorized.")
        aid = _pp_id()
        now = datetime.now(UTC)
        self.authorizations[aid] = {"id": aid, "status": "CREATED", "cents": order["cents"],
                                    "currency": order["currency"], "captured": 0, "order_id": order_id,
                                    "custom_id": order["unit"].get("custom_id"),
                                    "invoice_id": order["unit"].get("invoice_id"),
                                    "create_time": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                    "expiration_time": (now + timedelta(days=29)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        order["status"], order["authorization_id"] = "COMPLETED", aid
        return 201, self._order_view(order)

    def _auth_view(self, auth: dict[str, Any]) -> dict[str, Any]:
        return {"id": auth["id"], "status": auth["status"], "amount": _money(auth["cents"], auth["currency"]),
                "custom_id": auth["custom_id"], "invoice_id": auth["invoice_id"],
                "expiration_time": auth["expiration_time"], "create_time": auth["create_time"],
                "supplementary_data": {"related_ids": {"order_id": auth["order_id"]}}}

    def _capture(self, auth_id: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        auth = self.authorizations.get(auth_id)
        if not auth:
            return self._not_found()
        if auth["status"] == "VOIDED":
            return _error(422, "UNPROCESSABLE_ENTITY", "AUTHORIZATION_VOIDED", "The authorization was voided.")
        if auth["status"] in ("CAPTURED", "PARTIALLY_CAPTURED") and auth.get("final"):
            return _error(422, "UNPROCESSABLE_ENTITY", "AUTHORIZATION_ALREADY_CAPTURED",
                          "Authorization has been previously captured and hence cannot be captured again.")
        if body.get("invoice_id") and any(c.get("invoice_id") == body["invoice_id"] for c in self.captures.values()):
            return _error(422, "UNPROCESSABLE_ENTITY", "DUPLICATE_INVOICE_ID",
                          "Requested invoice number has been previously captured.")
        cents = _cents(body["amount"]["value"])
        if cents <= 0 or cents > auth["cents"] - auth["captured"]:
            return _error(422, "UNPROCESSABLE_ENTITY", "CAPTURE_AMOUNT_EXCEEDS_AUTHORIZED",
                          "The capture amount exceeds the amount still authorized.")
        status = self.capture_status
        cid = _pp_id()
        fee = 0 if status == "DECLINED" else (cents * FEE_PERCENT_BP + 5000) // 10000 + FEE_FIXED_CENTS
        cap = {"id": cid, "status": status, "amount": _money(cents, auth["currency"]),
               "final_capture": bool(body.get("final_capture")), "invoice_id": body.get("invoice_id"),
               "custom_id": auth["custom_id"], "create_time": _now(), "update_time": _now(),
               "supplementary_data": {"related_ids": {"order_id": auth["order_id"], "authorization_id": auth_id}}}
        if status == "PENDING":
            cap["status_details"] = {"reason": "PENDING_REVIEW"}
        if status != "DECLINED":
            cap["seller_receivable_breakdown"] = {"gross_amount": _money(cents, auth["currency"]),
                                                  "paypal_fee": _money(fee, auth["currency"]),
                                                  "net_amount": _money(cents - fee, auth["currency"])}
            auth["captured"] += cents
            auth["final"] = bool(body.get("final_capture"))
            auth["status"] = "CAPTURED" if auth["captured"] == auth["cents"] else "PARTIALLY_CAPTURED"
        self.captures[cid] = cap
        if status == "COMPLETED":
            self._emit("PAYMENT.CAPTURE.COMPLETED", "capture", cap)
        return 201, dict(cap)

    def settle_pending_capture(self, capture_id: str, status: str = "COMPLETED") -> None:
        """A PENDING capture resolving later at PayPal (tests and the demo use it), with its webhook."""
        with self._lock:
            cap = self.captures[capture_id]
            cap["status"] = status
            cap.pop("status_details", None)
            self._emit(f"PAYMENT.CAPTURE.{'COMPLETED' if status == 'COMPLETED' else 'DECLINED'}", "capture", cap)

    def _void(self, auth_id: str) -> tuple[int, dict[str, Any]]:
        auth = self.authorizations.get(auth_id)
        if not auth:
            return self._not_found()
        if auth["status"] in ("CAPTURED", "PARTIALLY_CAPTURED"):
            return _error(422, "UNPROCESSABLE_ENTITY", "PREVIOUSLY_CAPTURED",
                          "You cannot void an authorized payment that has been fully captured.")
        if auth["status"] == "VOIDED":
            return _error(422, "UNPROCESSABLE_ENTITY", "PREVIOUSLY_VOIDED", "The authorization was already voided.")
        auth["status"] = "VOIDED"
        self._emit("PAYMENT.AUTHORIZATION.VOIDED", "authorization", self._auth_view(auth))
        return 200, self._auth_view(auth)

    # --- payouts -------------------------------------------------------------------------------------------------
    def _create_payout(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        header = body["sender_batch_header"]
        sbid = header["sender_batch_id"]
        if sbid in self.sender_batches:
            status, err = _error(400, "USER_BUSINESS_ERROR", "Batch with given sender_batch_id already exists",
                                 "User business error.")
            return status, {**err, "message": "User business error."}
        bid = _pp_id(13)
        items = []
        for it in body["items"]:
            iid = _pp_id(13)
            self.items[iid] = {"payout_item_id": iid, "payout_batch_id": bid, "transaction_status": "PENDING",
                               "transaction_id": None, "cents": _cents(it["amount"]["value"]),
                               "currency": it["amount"]["currency"], "receiver": it["receiver"],
                               "sender_item_id": it.get("sender_item_id"), "note": it.get("note")}
            items.append(iid)
        self.batches[bid] = {"id": bid, "status": "PENDING", "header": header, "items": items, "reads": 0,
                             "time_created": _now()}
        self.sender_batches[sbid] = bid
        self._emit("PAYMENT.PAYOUTSBATCH.PROCESSING", "payouts", {"batch_header": {
            "payout_batch_id": bid, "batch_status": "PROCESSING", "sender_batch_header": header}})
        return 201, {"batch_header": {"payout_batch_id": bid, "batch_status": "PENDING",
                                      "sender_batch_header": header},
                     "links": [{"href": f"https://api-m.sandbox.paypal.com/v1/payments/payouts/{bid}",
                                "rel": "self", "method": "GET"}]}

    def _read_batch(self, bid: str) -> tuple[int, dict[str, Any]]:
        batch = self.batches.get(bid)
        if not batch:
            return self._not_found()
        batch["reads"] += 1
        if batch["reads"] == 1:
            batch["status"] = "PROCESSING"
        elif batch["status"] != "SUCCESS":
            for iid in batch["items"]:
                item = self.items[iid]
                if item["transaction_status"] == "PENDING":
                    receiver = item["receiver"].lower()
                    item["transaction_status"] = ("FAILED" if "+fail" in receiver
                                                  else "SUCCESS" if receiver in self.registered else "UNCLAIMED")
                    if item["transaction_status"] in ("SUCCESS", "UNCLAIMED"):
                        item["transaction_id"] = _pp_id(17)
                    event = {"SUCCESS": "SUCCEEDED", "UNCLAIMED": "UNCLAIMED", "FAILED": "FAILED"}[
                        item["transaction_status"]]
                    self._emit(f"PAYMENT.PAYOUTS-ITEM.{event}", "payouts_item", self._item_view(item))
            batch["status"] = "SUCCESS"
        total = sum(self.items[i]["cents"] for i in batch["items"])
        currency = self.items[batch["items"][0]]["currency"] if batch["items"] else "USD"
        return 200, {"batch_header": {"payout_batch_id": bid, "batch_status": batch["status"],
                                      "time_created": batch["time_created"],
                                      "sender_batch_header": batch["header"],
                                      "amount": _money(total, currency, "currency")},
                     "items": [self._item_view(self.items[i]) for i in batch["items"]]}

    def _item_view(self, item: dict[str, Any]) -> dict[str, Any]:
        view = {"payout_item_id": item["payout_item_id"], "payout_batch_id": item["payout_batch_id"],
                "transaction_status": item["transaction_status"],
                "payout_item_fee": _money(PAYOUT_FEE_CENTS if item["transaction_status"] == "SUCCESS" else 0,
                                          item["currency"], "currency"),
                "payout_item": {"recipient_type": "EMAIL", "receiver": item["receiver"],
                                "sender_item_id": item["sender_item_id"],
                                "amount": _money(item["cents"], item["currency"], "currency"), "note": item["note"]}}
        if item["transaction_id"]:
            view["transaction_id"] = item["transaction_id"]
        if item["transaction_status"] == "FAILED":
            view["errors"] = {"name": "RECEIVER_UNREGISTERED", "message": "Receiver is unregistered"}
        return view

    # --- webhooks ------------------------------------------------------------------------------------------------
    def _signature(self, transmission_id: str, when: str, webhook_id: str, body: bytes) -> str:
        message = f"{transmission_id}|{when}|{webhook_id}|{zlib.crc32(body)}".encode()
        return base64.b64encode(hmac.new(self._key, message, hashlib.sha256).digest()).decode()

    def _emit(self, event_type: str, resource_type: str, resource: dict[str, Any]) -> None:
        event = {"id": f"WH-SIM-{uuid.uuid4().hex[:16].upper()}", "event_version": "1.0", "create_time": _now(),
                 "resource_type": resource_type, "event_type": event_type, "summary": event_type,
                 "resource": resource}
        body = json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
        tid, when = str(uuid.uuid4()), _now()
        self.outbox.append({"body": body, "headers": {
            "PAYPAL-AUTH-ALGO": "SIMULATED-HMAC-SHA256",
            "PAYPAL-CERT-URL": "https://api.sandbox.paypal.com/v1/notifications/certs/SIMULATED",
            "PAYPAL-TRANSMISSION-ID": tid, "PAYPAL-TRANSMISSION-TIME": when,
            "PAYPAL-TRANSMISSION-SIG": self._signature(tid, when, self.WEBHOOK_ID, body)}})

    def _verify(self, body: dict[str, Any]) -> bool:
        event = body.get("webhook_event")
        if not isinstance(event, dict) or body.get("webhook_id") != self.WEBHOOK_ID:
            return False
        raw = json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
        expect = self._signature(str(body.get("transmission_id")), str(body.get("transmission_time")),
                                 self.WEBHOOK_ID, raw)
        return hmac.compare_digest(expect, str(body.get("transmission_sig") or ""))

    def take_outbox(self) -> list[dict[str, Any]]:
        with self._lock:
            out, self.outbox = self.outbox, []
        return out
