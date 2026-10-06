"""Delivery handoff: build the project specification deterministically from persisted state."""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.database.models import Company, Contact, DeliveryProject, Deal, Payment, Prospect
from src.integrations.delivery.targets import DeliveryError, DeliveryTarget
from src.models.enums import DealState as S
from src.models.enums import PaymentStatus
from src.services import deals as D
from src.services.queries import latest_proposal, latest_research, latest_solution, selected_pain

log = logging.getLogger("delivery")


class DeliveryService:
    def __init__(self, session: Session, target: DeliveryTarget):
        self.s, self.target = session, target

    def build_spec(self, deal: Deal) -> dict:
        prospect = self.s.get(Prospect, deal.prospect_id)
        company = self.s.get(Company, prospect.company_id)
        contacts = list(self.s.scalars(select(Contact).where(Contact.company_id == company.id)))
        paid = self.s.scalar(select(Payment).where(Payment.deal_id == deal.id, Payment.status == PaymentStatus.PAID.value))
        if not paid:
            raise DeliveryError("refusing to build delivery spec: no verified payment")
        prop = latest_proposal(self.s, deal.id)
        pc = prop.content
        sol, pain = latest_solution(self.s, prospect.id), selected_pain(self.s, prospect.id)
        return {
            "deal_id": deal.id,
            "client": {
                "company": company.name, "website": company.website, "industry": company.industry, "location": company.location,
                "contacts": [{"name": c.name, "role": c.role, "email": c.email, "phone": c.phone} for c in contacts],
            },
            "problem": pc["problem"],
            "solution": pc["proposed_solution"],
            "requirements": pc["features"],
            "integrations": pc["integrations"],
            "integrations_requiring_validation": (sol.spec.get("requires_validation", []) if sol else []),
            "deliverables": pc["deliverables"],
            "timeline": pc["timeline"],
            "accepted_price": {"amount_cents": paid.amount_cents, "currency": paid.currency, "type": "one_time"},
            "payment": {"provider": paid.provider, "reference": paid.provider_ref, "paid_at": paid.paid_at.isoformat() if paid.paid_at else None},
            "special_requirements": pc["special_requirements"],
            "assumptions": pc["assumptions"],
            "exclusions": pc["exclusions"],
            "support_terms": pc["support_terms"],
            "conversation_summary": pc["conversation_summary"],
            "solution_spec": sol.spec if sol else None,
            "evidence": pain.evidence if pain else [],
            "research_id": (latest_research(self.s, prospect.id).id if latest_research(self.s, prospect.id) else None),
            "proposal": pc,
        }

    def handoff(self, deal: Deal) -> DeliveryProject:
        """Idempotent: one DeliveryProject per deal; re-running after a failure retries only the submit."""
        proj = self.s.scalar(select(DeliveryProject).where(DeliveryProject.deal_id == deal.id))
        if proj and proj.status in ("handed_off", "completed"):
            return proj
        if not proj:
            proj = DeliveryProject(deal_id=deal.id, spec=self.build_spec(deal), target=self.target.name)
            self.s.add(proj)
            self.s.flush()
        try:
            proj.external_ref = self.target.submit(proj.spec, idempotency_key=deal.id)
        except DeliveryError as e:
            proj.status = "failed"
            D.record_event(self.s, deal, "delivery_handoff_failed", error=str(e))
            log.error("delivery handoff failed for %s: %s", deal.id, e)
            return proj
        proj.status = "handed_off"
        if S(deal.state) == S.PAID:
            D.transition(self.s, deal, S.DELIVERY, reason="handed to delivery pipeline", external_ref=proj.external_ref)
        return proj
