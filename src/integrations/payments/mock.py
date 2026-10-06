from __future__ import annotations

import hashlib
import hmac
import json
from uuid import uuid4

from src.integrations.payments.base import (
    InvalidSignature, PaymentEvent, PaymentRequestResult, ProviderPaymentStatus,
)


class MockPaymentProvider:
    """Deterministic in-memory provider for dev/tests. Webhooks are HMAC-signed like a real one."""

    name = "mock"

    def __init__(self, secret: str = "change-me", base_url: str = "http://localhost:8000"):
        self.secret, self.base_url = secret, base_url
        self.requests: dict[str, dict] = {}
        self.paid: set[str] = set()
        self._by_key: dict[str, str] = {}

    def create_payment_request(self, *, payment_id, amount_cents, currency, description, idempotency_key, customer_email=None):
        if idempotency_key in self._by_key:
            ref = self._by_key[idempotency_key]
        else:
            ref = f"mock_{uuid4().hex[:12]}"
            self._by_key[idempotency_key] = ref
            self.requests[ref] = {"payment_id": payment_id, "amount_cents": amount_cents, "currency": currency}
        return PaymentRequestResult(ref, f"{self.base_url}/mock-pay/{ref}", {"description": description})

    def sign(self, body: bytes) -> str:
        return hmac.new(self.secret.encode(), body, hashlib.sha256).hexdigest()

    def parse_webhook(self, body: bytes, headers: dict[str, str]) -> PaymentEvent:
        sig = {k.lower(): v for k, v in headers.items()}.get("x-mock-signature", "")
        if not hmac.compare_digest(sig, self.sign(body)):
            raise InvalidSignature("bad mock signature")
        d = json.loads(body)
        kind = {"payment.succeeded": "succeeded", "payment.failed": "failed", "payment.expired": "expired"}.get(d.get("type"), "ignored")
        return PaymentEvent(d["id"], kind, d.get("provider_ref"), d.get("payment_id"), d.get("amount_cents"), d.get("currency"), d)

    def fetch_status(self, provider_ref: str) -> ProviderPaymentStatus:
        r = self.requests.get(provider_ref)
        if not r:
            return ProviderPaymentStatus("unknown")
        return ProviderPaymentStatus("paid" if provider_ref in self.paid else "unpaid", r["amount_cents"], r["currency"])

    # ---- test helpers (simulate the customer paying) ----
    def simulate_payment(
        self, provider_ref: str, *, event_id: str | None = None, amount_cents: int | None = None, mark_paid: bool = True,
        kind: str = "payment.succeeded",
    ) -> tuple[bytes, dict[str, str]]:
        r = self.requests[provider_ref]
        if mark_paid and kind == "payment.succeeded":
            self.paid.add(provider_ref)
        body = json.dumps({
            "id": event_id or f"evt_{uuid4().hex[:10]}", "type": kind, "provider_ref": provider_ref,
            "payment_id": r["payment_id"], "amount_cents": amount_cents if amount_cents is not None else r["amount_cents"],
            "currency": r["currency"],
        }).encode()
        return body, {"x-mock-signature": self.sign(body), "content-type": "application/json"}
