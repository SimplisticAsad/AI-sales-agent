"""One-time payments. PAID is set ONLY from a verified provider event whose amount matches our record."""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.database.base import utcnow
from src.database.models import Company, Contact, Deal, Payment, ProcessedWebhook, Proposal, Prospect
from src.integrations.payments.base import InvalidSignature, PaymentError, PaymentProvider
from src.models.enums import DealState as S
from src.models.enums import PaymentStatus, ProposalStatus
from src.services import deals as D
from src.services.pricing import fmt

log = logging.getLogger("payments")


class PaymentService:
    def __init__(self, session: Session, provider: PaymentProvider, currency: str = "usd"):
        self.s, self.provider, self.currency = session, provider, currency

    def create_for_deal(self, deal: Deal) -> Payment:
        prop = self.s.scalar(select(Proposal).where(Proposal.deal_id == deal.id, Proposal.status == ProposalStatus.ACCEPTED.value))
        if not prop:
            raise PaymentError("no accepted proposal for this deal")
        if prop.price_cents != deal.agreed_price_cents:
            raise PaymentError("proposal price does not match agreed price")
        key = f"deal:{deal.id}:proposal:{prop.id}"
        existing = self.s.scalar(select(Payment).where(Payment.idempotency_key == key))
        if existing:
            return existing  # idempotent: same proposal -> same payment request
        if S(deal.state) not in (S.ACCEPTED, S.PAYMENT_PENDING):
            raise PaymentError(f"deal is {deal.state}; payment request requires ACCEPTED")
        pay = Payment(deal_id=deal.id, proposal_id=prop.id, provider=self.provider.name, amount_cents=prop.price_cents,
                      currency=prop.currency or self.currency, idempotency_key=key)
        self.s.add(pay)
        self.s.flush()
        prospect = self.s.get(Prospect, deal.prospect_id)
        company = self.s.get(Company, prospect.company_id)
        contact = self.s.scalar(select(Contact).where(Contact.company_id == company.id, Contact.email.is_not(None)))
        res = self.provider.create_payment_request(
            payment_id=pay.id, amount_cents=pay.amount_cents, currency=pay.currency,
            description=f"AI agent project for {company.name} (one-time fee)", idempotency_key=key,
            customer_email=contact.email if contact else None,
        )
        pay.provider_ref, pay.payment_url, pay.raw = res.provider_ref, res.payment_url, res.raw
        D.transition(self.s, deal, S.PAYMENT_PENDING, reason="payment request created", payment_id=pay.id, amount=fmt(pay.amount_cents))
        return pay

    def handle_webhook(self, body: bytes, headers: dict[str, str], on_paid) -> dict:
        """Returns a small status dict. Raises InvalidSignature for unverifiable requests (caller -> 400)."""
        ev = self.provider.parse_webhook(body, headers)  # raises InvalidSignature
        try:
            with self.s.begin_nested():
                self.s.add(ProcessedWebhook(provider=self.provider.name, event_id=ev.event_id))
                self.s.flush()
        except IntegrityError:
            return {"status": "duplicate", "event_id": ev.event_id}
        if ev.kind == "ignored":
            return {"status": "ignored"}
        pay = None
        if ev.payment_id:
            pay = self.s.get(Payment, ev.payment_id)
        if not pay and ev.provider_ref:
            pay = self.s.scalar(select(Payment).where(Payment.provider_ref == ev.provider_ref))
        if not pay:
            log.warning("payment event for unknown payment: %s", ev.event_id)
            return {"status": "unknown_payment"}
        deal = D.get_deal(self.s, pay.deal_id)
        if ev.kind in ("failed", "expired"):
            if pay.status == PaymentStatus.PENDING.value:
                pay.status = PaymentStatus.FAILED.value if ev.kind == "failed" else PaymentStatus.EXPIRED.value
                D.record_event(self.s, deal, "payment_" + ev.kind, payment_id=pay.id)
            return {"status": pay.status}
        # ---- succeeded: trust nothing until verified against our records AND the provider API ----
        if pay.status == PaymentStatus.PAID.value:
            return {"status": "already_paid"}
        problems = []
        if ev.amount_cents != pay.amount_cents:
            problems.append(f"amount {ev.amount_cents} != expected {pay.amount_cents}")
        if (ev.currency or "").lower() != pay.currency.lower():
            problems.append(f"currency {ev.currency} != expected {pay.currency}")
        if ev.provider_ref and pay.provider_ref and ev.provider_ref != pay.provider_ref:
            problems.append("provider reference mismatch")
        if not problems:
            api = self.provider.fetch_status(pay.provider_ref)  # PaymentError propagates -> 5xx -> provider retries
            if api.status != "paid":
                problems.append(f"provider API reports status {api.status}")
            elif api.amount_cents != pay.amount_cents or (api.currency or "").lower() != pay.currency.lower():
                problems.append("provider API amount/currency mismatch")
        if problems:
            pay.status = PaymentStatus.MISMATCH.value
            D.escalate(self.s, deal, "PAYMENT_VERIFICATION_FAILED", "; ".join(problems), detail={"payment_id": pay.id, "event": ev.event_id})
            return {"status": "mismatch", "problems": problems}
        pay.status = PaymentStatus.PAID.value
        pay.paid_at = utcnow()
        D.transition(self.s, deal, S.PAID, actor="payment_webhook", reason="verified payment", payment_id=pay.id,
                     provider_ref=pay.provider_ref, amount=fmt(pay.amount_cents))
        on_paid(deal)
        return {"status": "paid", "deal_id": deal.id}
