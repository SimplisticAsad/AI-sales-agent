from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.database.models import (
    Company, Contact, Conversation, Deal, Offer, PainPoint, Proposal, Prospect, Research, Solution,
)


def latest_research(s: Session, prospect_id: str) -> Research | None:
    return s.scalar(select(Research).where(Research.prospect_id == prospect_id).order_by(Research.version.desc()))


def selected_pain(s: Session, prospect_id: str) -> PainPoint | None:
    return s.scalar(select(PainPoint).where(PainPoint.prospect_id == prospect_id, PainPoint.selected.is_(True))
                    .order_by(PainPoint.priority_score.desc()))


def latest_solution(s: Session, prospect_id: str) -> Solution | None:
    return s.scalar(select(Solution).where(Solution.prospect_id == prospect_id).order_by(Solution.created_at.desc()))


def latest_offer(s: Session, deal_id: str) -> Offer | None:
    return s.scalar(select(Offer).where(Offer.deal_id == deal_id).order_by(Offer.created_at.desc()))


def latest_proposal(s: Session, deal_id: str) -> Proposal | None:
    return s.scalar(select(Proposal).where(Proposal.deal_id == deal_id).order_by(Proposal.version.desc()))


def primary_contact(s: Session, company_id: str) -> Contact | None:
    cs = list(s.scalars(select(Contact).where(Contact.company_id == company_id).order_by(Contact.created_at)))
    return next((c for c in cs if c.is_decision_maker), cs[0] if cs else None)


def load_prospect_bundle(s: Session, deal: Deal) -> tuple[Prospect, Company]:
    p = s.get(Prospect, deal.prospect_id)
    return p, s.get(Company, p.company_id)


def conversation_for(s: Session, deal_id: str, channel: str, external_id: str | None = None) -> Conversation:
    q = select(Conversation).where(Conversation.deal_id == deal_id, Conversation.channel == channel)
    if external_id:
        q = q.where(Conversation.external_id == external_id)
    conv = s.scalar(q.order_by(Conversation.created_at.desc()))
    if not conv:
        conv = Conversation(deal_id=deal_id, channel=channel, external_id=external_id)
        s.add(conv)
        s.flush()
    return conv


def history_text(s: Session, deal_id: str, limit: int = 30) -> list[dict]:
    """Cross-channel transcript, oldest first, trimmed to the most recent `limit` messages."""
    from src.database.models import Message

    rows = list(s.scalars(
        select(Message).join(Conversation, Message.conversation_id == Conversation.id)
        .where(Conversation.deal_id == deal_id, Message.status != "blocked")
        .order_by(Message.created_at.desc(), Message.id).limit(limit)
    ))
    rows.reverse()
    return [{"role": m.role, "content": m.content} for m in rows]
