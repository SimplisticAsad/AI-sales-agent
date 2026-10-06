"""Controlled-template proposals. The LLM contributes only descriptive scope text; price and terms come from code."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config.catalog import get_proposal_terms
from src.config.settings import Settings
from src.database.base import utcnow
from src.database.models import Company, Deal, Negotiation, Proposal, Prospect
from src.models.enums import DealState as S
from src.models.enums import ProposalStatus
from src.models.schemas import ProposalNarrativeOut
from src.services import deals as D
from src.services.pricing import fmt
from src.services.queries import latest_offer, latest_proposal, latest_solution, primary_contact


class ProposalError(Exception):
    pass


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class ProposalService:
    def __init__(self, session: Session, settings: Settings):
        self.s, self.cfg = session, settings

    def price_is_authorised(self, deal: Deal, price_cents: int) -> bool:
        offer = latest_offer(self.s, deal.id)
        if not offer:
            return False
        if offer.min_price_cents <= price_cents <= offer.target_price_cents:
            return True
        # below floor only via an explicit human override recorded in the negotiation log
        for n in self.s.scalars(select(Negotiation).where(Negotiation.deal_id == deal.id, Negotiation.outcome == "human_override")):
            if n.decision.get("price_cents") == price_cents:
                return True
        return False

    def generate(self, deal: Deal, narrative: ProposalNarrativeOut, price_cents: int) -> tuple[Proposal, str]:
        if S(deal.state) not in (S.PROPOSAL, S.NEGOTIATION, S.DISCOVERY, S.ENGAGED):
            raise ProposalError(f"cannot create a proposal in state {deal.state}")
        if not self.price_is_authorised(deal, price_cents):
            raise ProposalError("price is outside the authorised range and has no human override")
        sol = latest_solution(self.s, deal.prospect_id)
        company = self.s.get(Company, self.s.get(Prospect, deal.prospect_id).company_id)
        contact = primary_contact(self.s, company.id)
        terms = get_proposal_terms()
        unverified = sol.spec.get("requires_validation", []) if sol else []
        prev = latest_proposal(self.s, deal.id)
        for p in self.s.scalars(select(Proposal).where(Proposal.deal_id == deal.id, Proposal.status.in_(["draft", "sent"]))):
            p.status = ProposalStatus.SUPERSEDED.value
        content = {
            "client": {"name": company.name, "website": company.website, "contact": contact.name if contact else None},
            "problem": narrative.client_problem,
            "proposed_solution": narrative.proposed_solution,
            "features": narrative.features,
            "integrations": narrative.integrations,
            "deliverables": narrative.deliverables,
            "timeline": narrative.timeline,
            "price": {"amount_cents": price_cents, "currency": deal.currency, "display": fmt(price_cents), "type": "one_time"},
            "payment_terms": terms["payment_terms"],
            "assumptions": [*narrative.assumptions, *terms["standard_assumptions"],
                            *[f"Integration to be validated before commitment: {n}" for n in unverified]],
            "exclusions": [*narrative.exclusions, *terms["standard_exclusions"]],
            "support_terms": terms["support_terms"],
            "special_requirements": narrative.special_requirements,
            "refund_policy": terms["refund_policy"],
            "legal_notice": terms["legal_notice"],
            "conversation_summary": narrative.conversation_summary,
        }
        token = secrets.token_urlsafe(24)
        prop = Proposal(deal_id=deal.id, version=(prev.version + 1) if prev else 1, content=content, price_cents=price_cents,
                        currency=deal.currency, status=ProposalStatus.SENT.value, token_hash=_hash(token))
        self.s.add(prop)
        deal.agreed_price_cents = price_cents
        self.s.flush()
        D.record_event(self.s, deal, "proposal_created", proposal_id=prop.id, version=prop.version, price_cents=price_cents)
        return prop, token

    def public_url(self, proposal: Proposal, token: str) -> str:
        return f"{self.cfg.public_base_url.rstrip('/')}/proposals/{proposal.id}/public?token={token}"

    def token_ok(self, proposal: Proposal, token: str | None) -> bool:
        return bool(token and proposal.token_hash and hmac.compare_digest(_hash(token), proposal.token_hash))

    def accept(self, proposal: Proposal, *, actor: str) -> Proposal:
        if proposal.status == ProposalStatus.ACCEPTED.value:
            return proposal  # idempotent
        if proposal.status != ProposalStatus.SENT.value:
            raise ProposalError(f"proposal is {proposal.status}; only the latest sent proposal can be accepted")
        deal = D.get_deal(self.s, proposal.deal_id)
        if S(deal.state) not in (S.PROPOSAL, S.NEGOTIATION):
            raise ProposalError(f"deal is in {deal.state}; cannot accept now")
        if deal.agreed_price_cents != proposal.price_cents:
            raise ProposalError("proposal price does not match the agreed price")
        proposal.status = ProposalStatus.ACCEPTED.value
        proposal.accepted_at = utcnow()
        D.transition(self.s, deal, S.ACCEPTED, actor=actor, reason="proposal accepted", proposal_id=proposal.id)
        return proposal
