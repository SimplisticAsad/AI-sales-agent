"""Human-in-the-loop: resolving escalations."""
from __future__ import annotations

from sqlalchemy.orm import Session

from src.database.base import utcnow
from src.database.models import Escalation
from src.models.enums import DealState as S
from src.models.enums import EscalationStatus
from src.models.schemas import ExtractedRequest
from src.services import deals as D
from src.services.negotiation import NegotiationDecision
from src.services.negotiation_store import load_policy, load_state, save_round
from src.services.pricing import usd


class EscalationError(Exception):
    pass


def resolve_escalation(
    s: Session, escalation_id: str, *, approved: bool, note: str = "", approved_price_usd: float | None = None,
    mark_lost: bool = False, actor: str = "human",
) -> Escalation:
    esc = s.get(Escalation, escalation_id)
    if not esc:
        raise LookupError("escalation not found")
    if esc.status != EscalationStatus.OPEN.value:
        raise EscalationError(f"escalation already {esc.status}")
    deal = D.get_deal(s, esc.deal_id)
    esc.status = (EscalationStatus.APPROVED if approved else EscalationStatus.REJECTED).value
    esc.resolved_at = utcnow()
    esc.resolution = {"approved": approved, "note": note, "approved_price_usd": approved_price_usd, "actor": actor}
    if approved and approved_price_usd is not None:
        price = usd(approved_price_usd)
        st = load_state(s, deal)
        st.round += 1
        st.last_offered_cents = price
        d = NegotiationDecision("counter", st.round, price, ["HUMAN_OVERRIDE"], f"Human approved price {price}",
                                allowed_amounts_cents=[price])
        save_round(s, deal, ExtractedRequest(), "", d, st, "human_override")
        D.record_event(s, deal, "price_override", actor, price_cents=price, escalation_id=esc.id, note=note)
    D.record_event(s, deal, "escalation_resolved", actor, escalation_id=esc.id, approved=approved, note=note)
    if S(deal.state) == S.ESCALATED:
        if mark_lost:
            D.transition(s, deal, S.LOST, actor=actor, reason="human closed the deal")
        elif deal.resume_state:
            D.transition(s, deal, S(deal.resume_state), actor=actor, reason="escalation resolved")
    s.flush()
    return esc
