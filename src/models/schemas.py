"""Structured outputs and value objects shared across agents and services."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.models.enums import ClaimLabel, Intent


class Strict(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------- market selector
class MarketProfileOut(Strict):
    industry: str
    geography: str = "United States"
    employee_min: int = 10
    employee_max: int = 100
    target_problems: list[str] = Field(default_factory=list)
    decision_maker_titles: list[str] = Field(default_factory=list)
    min_qualification_criteria: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    decision_summary: str = ""


# ---------------------------------------------------------------- prospect hunter
class RawCandidate(Strict):
    """What a ProspectSource returns. Only these fields may become facts."""

    company_name: str
    website: str | None = None
    location: str | None = None
    employee_estimate: int | None = None
    text: str = ""  # free text from the source (listing, directory entry, ...)
    email: str | None = None
    phone: str | None = None
    contact_page_url: str | None = None
    contact_name: str | None = None
    contact_role: str | None = None
    source: str = ""


class ExtractedCandidate(Strict):
    industry: str | None = None
    decision_maker: str | None = None
    decision_maker_role: str | None = None
    problem_hints: list[str] = Field(default_factory=list)
    decision_summary: str = ""


class HunterOut(Strict):
    extracted: ExtractedCandidate


# ---------------------------------------------------------------- research
class Claim(Strict):
    id: str
    category: str = "other"
    statement: str
    label: ClaimLabel = ClaimLabel.UNKNOWN
    source_url: str | None = None
    quote: str | None = None
    note: str | None = None


class DossierOut(Strict):
    summary: str = ""
    claims: list[Claim] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    decision_summary: str = ""


class SourceDoc(Strict):
    url: str
    text: str


# ---------------------------------------------------------------- pain detector
class PainCriteria(Strict):
    frequency: float = 0
    cost: float = 0
    repetitiveness: float = 0
    business_impact: float = 0
    feasibility: float = 0
    integration_availability: float = 0
    roi_demonstrability: float = 0


class PainOut(Strict):
    problem: str
    evidence_claim_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(0, ge=0, le=1)
    business_impact: str = ""
    automation_potential: float = Field(0, ge=0, le=1)
    criteria: PainCriteria = Field(default_factory=PainCriteria)


class PainDetectionOut(Strict):
    pains: list[PainOut] = Field(default_factory=list)
    decision_summary: str = ""


# ---------------------------------------------------------------- solution
class IntegrationOut(Strict):
    name: str
    purpose: str = ""


class ValueDriver(Strict):
    driver: str
    hours_saved_per_week: float | None = None
    hourly_cost_usd: float | None = None
    annual_value_usd: float | None = None
    basis: str = ""  # must say which dossier claim / assumption supports it


class SolutionOut(Strict):
    name: str
    summary: str = ""
    problem_addressed: str = ""
    capabilities: list[str] = Field(default_factory=list)
    integrations: list[IntegrationOut] = Field(default_factory=list)
    data_sources: list[str] = Field(default_factory=list)
    workflow_steps: list[str] = Field(default_factory=list)
    human_escalation_points: list[str] = Field(default_factory=list)
    complexity: Literal["low", "medium", "high"] = "medium"
    effort_hours: int = Field(40, ge=1, le=2000)
    expected_value: str = ""
    value_drivers: list[ValueDriver] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    decision_summary: str = ""


# ---------------------------------------------------------------- pricing
class PricingNarrativeOut(Strict):
    value_rationale: str = ""
    decision_summary: str = ""


# ---------------------------------------------------------------- outreach
class OutreachOut(Strict):
    subject: str
    body: str
    evidence_claim_ids: list[str] = Field(default_factory=list)
    decision_summary: str = ""


# ---------------------------------------------------------------- sales / negotiation
class ToolCall(Strict):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ExtractedRequest(Strict):
    offered_price_usd: float | None = None
    requested_discount_percent: float | None = None
    payment_terms_request: str | None = None
    scope_requests: list[str] = Field(default_factory=list)
    contract_term_requests: list[str] = Field(default_factory=list)
    asks_for_refund: bool = False
    asks_for_guarantee: bool = False
    asks_legal_question: bool = False
    asks_unsupported_integration: list[str] = Field(default_factory=list)


class SalesTurnOut(Strict):
    tool_calls: list[ToolCall] = Field(default_factory=list)
    reply: str | None = None
    intent: Intent = Intent.OTHER
    extracted: ExtractedRequest = Field(default_factory=ExtractedRequest)
    objection_type: str | None = None
    buying_intent: float = Field(0, ge=0, le=1)
    next_action: str = ""
    decision_summary: str = ""
    confidence: float = Field(0.5, ge=0, le=1)


class NegotiationReplyOut(Strict):
    reply: str
    decision_summary: str = ""


# ---------------------------------------------------------------- proposal
class ProposalNarrativeOut(Strict):
    client_problem: str
    proposed_solution: str
    features: list[str] = Field(default_factory=list)
    integrations: list[str] = Field(default_factory=list)
    deliverables: list[str] = Field(default_factory=list)
    timeline: str = ""
    assumptions: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    special_requirements: list[str] = Field(default_factory=list)
    conversation_summary: str = ""
    decision_summary: str = ""
