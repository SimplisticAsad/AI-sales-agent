"""Persistent state. Every agent reads/writes these tables; prompts hold no business state.

Money is always integer cents. Timestamps are naive UTC.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database.base import Base, new_id, utcnow


def _pk() -> Mapped[str]:
    return mapped_column(String(32), primary_key=True, default=new_id)


class MarketProfile(Base):
    __tablename__ = "market_profiles"
    id: Mapped[str] = _pk()
    name: Mapped[str] = mapped_column(String(200))
    profile: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Company(Base):
    __tablename__ = "companies"
    id: Mapped[str] = _pk()
    name: Mapped[str] = mapped_column(String(300))
    website: Mapped[str | None] = mapped_column(String(500))
    industry: Mapped[str | None] = mapped_column(String(200))
    location: Mapped[str | None] = mapped_column(String(300))
    timezone: Mapped[str | None] = mapped_column(String(64))
    employee_estimate: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    contacts: Mapped[list[Contact]] = relationship(back_populates="company")


class Contact(Base):
    __tablename__ = "contacts"
    id: Mapped[str] = _pk()
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"))
    name: Mapped[str | None] = mapped_column(String(300))
    role: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(40))
    contact_page_url: Mapped[str | None] = mapped_column(String(500))
    source: Mapped[str | None] = mapped_column(String(500))  # where the contact data came from (never invented)
    is_decision_maker: Mapped[bool] = mapped_column(Boolean, default=False)
    call_consent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    company: Mapped[Company] = relationship(back_populates="contacts")


class Prospect(Base):
    __tablename__ = "prospects"
    id: Mapped[str] = _pk()
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"))
    market_profile_id: Mapped[str | None] = mapped_column(ForeignKey("market_profiles.id"))
    source: Mapped[str | None] = mapped_column(String(500))
    qualification_score: Mapped[float | None] = mapped_column(Float)
    qualification: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # breakdown + decision + reasons
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    company: Mapped[Company] = relationship()
    deal: Mapped[Deal] = relationship(back_populates="prospect", uselist=False)


class Deal(Base):
    __tablename__ = "deals"
    id: Mapped[str] = _pk()
    prospect_id: Mapped[str] = mapped_column(ForeignKey("prospects.id"), unique=True)
    state: Mapped[str] = mapped_column(String(32), default="DISCOVERED", index=True)
    resume_state: Mapped[str | None] = mapped_column(String(32))  # for ESCALATED / FAILED
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    quoted_price_cents: Mapped[int | None] = mapped_column(Integer)
    agreed_price_cents: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="usd")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    prospect: Mapped[Prospect] = relationship(back_populates="deal")


class Research(Base):
    __tablename__ = "research"
    id: Mapped[str] = _pk()
    prospect_id: Mapped[str] = mapped_column(ForeignKey("prospects.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    dossier: Mapped[dict[str, Any]] = mapped_column(JSON)  # {summary, claims[], unknowns[]}
    sources: Mapped[list[Any]] = mapped_column(JSON, default=list)  # [{url, fetched_at, sha256, chars}]
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PainPoint(Base):
    __tablename__ = "pain_points"
    id: Mapped[str] = _pk()
    prospect_id: Mapped[str] = mapped_column(ForeignKey("prospects.id"), index=True)
    research_id: Mapped[str] = mapped_column(ForeignKey("research.id"))
    problem: Mapped[str] = mapped_column(Text)
    evidence: Mapped[list[Any]] = mapped_column(JSON, default=list)  # claim ids + statements
    confidence: Mapped[float] = mapped_column(Float)
    business_impact: Mapped[str] = mapped_column(Text, default="")
    automation_potential: Mapped[float] = mapped_column(Float)
    criteria: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    priority_score: Mapped[float] = mapped_column(Float, default=0.0)
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    rejected_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Solution(Base):
    __tablename__ = "solutions"
    id: Mapped[str] = _pk()
    prospect_id: Mapped[str] = mapped_column(ForeignKey("prospects.id"), index=True)
    pain_point_id: Mapped[str] = mapped_column(ForeignKey("pain_points.id"))
    name: Mapped[str] = mapped_column(String(300))
    spec: Mapped[dict[str, Any]] = mapped_column(JSON)
    complexity: Mapped[str] = mapped_column(String(16))
    effort_hours: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Offer(Base):
    __tablename__ = "offers"
    id: Mapped[str] = _pk()
    deal_id: Mapped[str] = mapped_column(ForeignKey("deals.id"), index=True)
    solution_id: Mapped[str] = mapped_column(ForeignKey("solutions.id"))
    target_price_cents: Mapped[int] = mapped_column(Integer)
    min_price_cents: Mapped[int] = mapped_column(Integer)
    max_discount_percent: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), default="usd")
    breakdown: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    rationale: Mapped[str] = mapped_column(Text, default="")
    roi: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = _pk()
    deal_id: Mapped[str] = mapped_column(ForeignKey("deals.id"), index=True)
    channel: Mapped[str] = mapped_column(String(32))  # email | voice | web | sim
    external_id: Mapped[str | None] = mapped_column(String(200), index=True)  # e.g. Retell call_id
    status: Mapped[str] = mapped_column(String(32), default="open")
    summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    messages: Mapped[list[Message]] = relationship(back_populates="conversation", order_by="Message.created_at")


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = _pk()
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # agent | prospect | system
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="sent")  # sent | dry_run | blocked | received
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class Negotiation(Base):
    __tablename__ = "negotiations"
    id: Mapped[str] = _pk()
    deal_id: Mapped[str] = mapped_column(ForeignKey("deals.id"), index=True)
    round: Mapped[int] = mapped_column(Integer)
    request: Mapped[dict[str, Any]] = mapped_column(JSON)
    decision: Mapped[dict[str, Any]] = mapped_column(JSON)
    outcome: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Proposal(Base):
    __tablename__ = "proposals"
    id: Mapped[str] = _pk()
    deal_id: Mapped[str] = mapped_column(ForeignKey("deals.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    content: Mapped[dict[str, Any]] = mapped_column(JSON)
    price_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="usd")
    status: Mapped[str] = mapped_column(String(16), default="draft")
    token_hash: Mapped[str | None] = mapped_column(String(64))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[str] = _pk()
    deal_id: Mapped[str] = mapped_column(ForeignKey("deals.id"), index=True)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("proposals.id"))
    provider: Mapped[str] = mapped_column(String(32))
    provider_ref: Mapped[str | None] = mapped_column(String(200), index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    payment_url: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DeliveryProject(Base):
    __tablename__ = "delivery_projects"
    id: Mapped[str] = _pk()
    deal_id: Mapped[str] = mapped_column(ForeignKey("deals.id"), unique=True)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="created")  # created | handed_off | failed | completed
    target: Mapped[str | None] = mapped_column(String(64))
    external_ref: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[str] = _pk()
    agent: Mapped[str] = mapped_column(String(64), index=True)
    prospect_id: Mapped[str | None] = mapped_column(String(32), index=True)
    deal_id: Mapped[str | None] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running | success | failed
    input_summary: Mapped[str] = mapped_column(Text, default="")
    tool_calls: Mapped[list[Any]] = mapped_column(JSON, default=list)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    decision_summary: Mapped[str | None] = mapped_column(Text)
    prompt_name: Mapped[str | None] = mapped_column(String(64))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(100))
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DealEvent(Base):
    __tablename__ = "deal_events"
    id: Mapped[str] = _pk()
    deal_id: Mapped[str] = mapped_column(ForeignKey("deals.id"), index=True)
    kind: Mapped[str] = mapped_column(String(48))  # state_change | note | price | compliance | ...
    from_state: Mapped[str | None] = mapped_column(String(32))
    to_state: Mapped[str | None] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(64), default="system")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Escalation(Base):
    __tablename__ = "escalations"
    id: Mapped[str] = _pk()
    deal_id: Mapped[str] = mapped_column(ForeignKey("deals.id"), index=True)
    reason_code: Mapped[str] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    resolution: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)


class Suppression(Base):
    """Opt-outs / do-not-contact. kind: email | phone | domain."""

    __tablename__ = "suppressions"
    __table_args__ = (UniqueConstraint("kind", "value"),)
    id: Mapped[str] = _pk()
    kind: Mapped[str] = mapped_column(String(16))
    value: Mapped[str] = mapped_column(String(320))
    reason: Mapped[str] = mapped_column(String(200), default="opt_out")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ProcessedWebhook(Base):
    __tablename__ = "processed_webhooks"
    __table_args__ = (UniqueConstraint("provider", "event_id"),)
    id: Mapped[str] = _pk()
    provider: Mapped[str] = mapped_column(String(32))
    event_id: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
