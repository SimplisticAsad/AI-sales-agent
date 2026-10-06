"""Negotiation memory is persisted (Negotiation rows), so it survives restarts."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.database.models import Deal, Negotiation
from src.models.schemas import ExtractedRequest
from src.services.negotiation import NegotiationDecision, NegotiationPolicy, NegotiationState
from src.services.queries import latest_offer, latest_solution
from src.config.catalog import get_catalog


def load_policy(s: Session, deal: Deal) -> NegotiationPolicy:
    offer = latest_offer(s, deal.id)
    sol = latest_solution(s, deal.prospect_id)
    scope = list(sol.spec.get("capabilities", [])) if sol else []
    return NegotiationPolicy(
        target_cents=offer.target_price_cents, minimum_cents=offer.min_price_cents,
        max_discount_percent=offer.max_discount_percent, allowed_scope=scope or list(get_catalog().approved_service_scope),
    )


def load_state(s: Session, deal: Deal) -> NegotiationState:
    last = s.scalar(select(Negotiation).where(Negotiation.deal_id == deal.id).order_by(Negotiation.created_at.desc(), Negotiation.round.desc()))
    if not last:
        return NegotiationState()
    return NegotiationState(**last.decision.get("state_after", {}))


def save_round(s: Session, deal: Deal, req: ExtractedRequest, raw_text: str, d: NegotiationDecision,
               st: NegotiationState, outcome: str | None = None) -> Negotiation:
    row = Negotiation(
        deal_id=deal.id, round=d.round, request={**req.model_dump(), "raw_text": raw_text[:1000]},
        decision={**d.to_dict(), "state_after": {"round": st.round, "last_offered_cents": st.last_offered_cents,
                                                  "floor_offered": st.floor_offered, "below_floor_rounds": st.below_floor_rounds}},
        outcome=outcome or d.action,
    )
    s.add(row)
    s.flush()
    return row
