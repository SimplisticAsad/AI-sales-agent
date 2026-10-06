from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


class PaymentError(Exception):
    pass


class InvalidSignature(PaymentError):
    pass


@dataclass
class PaymentRequestResult:
    provider_ref: str
    payment_url: str
    raw: dict[str, Any] = field(default_factory=dict)


EventKind = Literal["succeeded", "failed", "expired", "ignored"]


@dataclass
class PaymentEvent:
    event_id: str
    kind: EventKind
    provider_ref: str | None = None
    payment_id: str | None = None  # our Payment.id, echoed back via provider metadata
    amount_cents: int | None = None
    currency: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderPaymentStatus:
    status: Literal["paid", "unpaid", "failed", "expired", "unknown"]
    amount_cents: int | None = None
    currency: str | None = None


class PaymentProvider(Protocol):
    name: str

    def create_payment_request(
        self, *, payment_id: str, amount_cents: int, currency: str, description: str, idempotency_key: str,
        customer_email: str | None = None,
    ) -> PaymentRequestResult: ...

    def parse_webhook(self, body: bytes, headers: dict[str, str]) -> PaymentEvent:
        """Verify the signature and parse. Raise InvalidSignature if it does not verify."""
        ...

    def fetch_status(self, provider_ref: str) -> ProviderPaymentStatus:
        """Ask the provider's API directly (second line of verification)."""
        ...
