"""Stripe Checkout (one-time `mode=payment`) via REST. No recurring billing. Only this module knows Stripe."""
from __future__ import annotations

import hashlib
import hmac
import json
import time

import httpx

from src.integrations.payments.base import (
    InvalidSignature, PaymentError, PaymentEvent, PaymentRequestResult, ProviderPaymentStatus,
)

API = "https://api.stripe.com/v1"


class StripePaymentProvider:
    name = "stripe"

    def __init__(self, secret_key: str, webhook_secret: str, success_url: str, cancel_url: str,
                 tolerance_s: int = 300, client: httpx.Client | None = None):
        if not secret_key or not webhook_secret:
            raise PaymentError("STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET are required")
        self._key, self._whsec = secret_key, webhook_secret
        self.success_url, self.cancel_url, self.tolerance = success_url, cancel_url, tolerance_s
        self._c = client or httpx.Client(timeout=30)

    def create_payment_request(self, *, payment_id, amount_cents, currency, description, idempotency_key, customer_email=None):
        data = {
            "mode": "payment",
            "success_url": self.success_url,
            "cancel_url": self.cancel_url,
            "client_reference_id": payment_id,
            "metadata[payment_id]": payment_id,
            "payment_intent_data[metadata][payment_id]": payment_id,
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": currency,
            "line_items[0][price_data][unit_amount]": str(amount_cents),
            "line_items[0][price_data][product_data][name]": description[:250],
        }
        if customer_email:
            data["customer_email"] = customer_email
        try:
            r = self._c.post(f"{API}/checkout/sessions", data=data, auth=(self._key, ""),
                             headers={"Idempotency-Key": idempotency_key})
        except httpx.HTTPError as e:
            raise PaymentError(f"stripe unreachable: {type(e).__name__}") from e
        if r.status_code >= 400:
            raise PaymentError(f"stripe error {r.status_code}: {r.text[:200]}")
        j = r.json()
        return PaymentRequestResult(j["id"], j["url"], {"expires_at": j.get("expires_at")})

    def parse_webhook(self, body: bytes, headers: dict[str, str]) -> PaymentEvent:
        sig = {k.lower(): v for k, v in headers.items()}.get("stripe-signature", "")
        parts = dict(p.split("=", 1) for p in sig.split(",") if "=" in p)
        ts, v1 = parts.get("t"), [p.split("=", 1)[1] for p in sig.split(",") if p.startswith("v1=")]
        if not ts or not v1:
            raise InvalidSignature("missing stripe signature")
        expected = hmac.new(self._whsec.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        if not any(hmac.compare_digest(expected, s) for s in v1):
            raise InvalidSignature("bad stripe signature")
        if abs(time.time() - int(ts)) > self.tolerance:
            raise InvalidSignature("stripe signature timestamp outside tolerance")
        ev = json.loads(body)
        obj = ev.get("data", {}).get("object", {})
        t = ev.get("type", "")
        kind = "ignored"
        if t in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
            kind = "succeeded" if obj.get("payment_status") == "paid" else "ignored"
        elif t == "checkout.session.async_payment_failed":
            kind = "failed"
        elif t == "checkout.session.expired":
            kind = "expired"
        return PaymentEvent(
            ev["id"], kind, obj.get("id"), (obj.get("metadata") or {}).get("payment_id") or obj.get("client_reference_id"),
            obj.get("amount_total"), obj.get("currency"), {"type": t},
        )

    def fetch_status(self, provider_ref: str) -> ProviderPaymentStatus:
        try:
            r = self._c.get(f"{API}/checkout/sessions/{provider_ref}", auth=(self._key, ""))
        except httpx.HTTPError as e:
            raise PaymentError(f"stripe unreachable: {type(e).__name__}") from e
        if r.status_code >= 400:
            raise PaymentError(f"stripe error {r.status_code}")
        j = r.json()
        st = "paid" if j.get("payment_status") == "paid" else ("expired" if j.get("status") == "expired" else "unpaid")
        return ProviderPaymentStatus(st, j.get("amount_total"), j.get("currency"))
