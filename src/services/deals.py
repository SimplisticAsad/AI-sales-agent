"""Deal repository helpers + state transitions with an append-only event log."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.database.models import Deal, DealEvent, Escalation, Prospect
from src.models.enums import DealState as S
from src.services.state_machine import InvalidTransition, can_transition


def get_deal(session: Session, deal_id: str) -> Deal:
    d = session.get(Deal, deal_id)
    if not d:
        raise LookupError(f"deal not found: {deal_id}")
    return d


def deal_for_prospect(session: Session, prospect_id: str) -> Deal:
    d = session.scalar(select(Deal).where(Deal.prospect_id == prospect_id))
    if not d:
        raise LookupError(f"no deal for prospect: {prospect_id}")
    return d


def record_event(session: Session, deal: Deal, kind: str, actor: str = "system", **detail: Any) -> DealEvent:
    ev = DealEvent(deal_id=deal.id, kind=kind, actor=actor, detail=detail)
    session.add(ev)
    return ev


def transition(
    session: Session, deal: Deal, to: S, *, actor: str = "system", reason: str = "", allow_noop: bool = True, **detail: Any
) -> bool:
    """Move a deal to `to` if legal. Returns False for an idempotent no-op."""
    frm = S(deal.state)
    if frm == to:
        if allow_noop:
            return False
        raise InvalidTransition(f"already in {to.value}")
    resume = S(deal.resume_state) if deal.resume_state else None
    if not can_transition(frm, to, resume):
        raise InvalidTransition(f"{frm.value} -> {to.value} is not allowed")
    if to in (S.ESCALATED, S.FAILED):
        deal.resume_state = frm.value
    elif frm in (S.ESCALATED, S.FAILED):
        deal.resume_state = None
    deal.state = to.value
    ev = record_event(session, deal, "state_change", actor, reason=reason, **detail)
    ev.from_state, ev.to_state = frm.value, to.value
    session.flush()
    return True


def escalate(
    session: Session, deal: Deal, reason_code: str, summary: str, *, detail: dict | None = None, actor: str = "system"
) -> Escalation:
    """Open a human-approval item and park the deal (idempotent per open reason)."""
    existing = session.scalar(
        select(Escalation).where(
            Escalation.deal_id == deal.id, Escalation.reason_code == reason_code, Escalation.status == "open"
        )
    )
    if existing:
        return existing
    esc = Escalation(deal_id=deal.id, reason_code=reason_code, summary=summary, detail=detail or {})
    session.add(esc)
    session.flush()
    record_event(session, deal, "escalation", actor, reason_code=reason_code, escalation_id=esc.id, summary=summary)
    if can_transition(S(deal.state), S.ESCALATED):
        transition(session, deal, S.ESCALATED, actor=actor, reason=reason_code, escalation_id=esc.id)
    return esc


def prospect_of(session: Session, deal: Deal) -> Prospect:
    return session.get(Prospect, deal.prospect_id)
