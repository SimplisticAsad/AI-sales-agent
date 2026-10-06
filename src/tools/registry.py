"""Structured tools exposed to the Sales Agent. Tools are the ONLY way an LLM touches business state, and every
state-changing tool re-checks its preconditions in code (the LLM cannot talk its way past them)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy.orm import Session

from src.config.catalog import get_catalog
from src.database.models import Company, Deal, Prospect
from src.models.enums import DealState as S
from src.services import deals as D
from src.services.container import Services
from src.services.pricing import fmt
from src.services.queries import latest_offer, latest_proposal, latest_research, latest_solution, selected_pain
from src.services.roi import calculate_roi


@dataclass
class ToolContext:
    session: Session
    svc: Services
    deal: Deal
    prospect: Prospect
    company: Company
    allowed_amounts_cents: set[int] = field(default_factory=set)  # amounts the reply may mention this turn
    side_effects: dict[str, Any] = field(default_factory=dict)  # e.g. proposal link to append after guardrails
    request_proposal: Callable[[], dict] | None = None  # injected by the conversation workflow
    request_payment: Callable[[], dict] | None = None


class ToolError(Exception):
    pass


class ToolRegistry:
    NAMES = ("get_company_dossier", "get_solution", "get_pricing_rules", "calculate_roi", "create_proposal",
             "request_human_approval", "create_payment_request")

    def __init__(self, ctx: ToolContext):
        self.ctx = ctx

    def execute(self, name: str, args: dict) -> dict:
        if name not in self.NAMES:
            return {"error": f"unknown tool {name!r}"}
        try:
            return getattr(self, f"t_{name}")(**(args or {}))
        except TypeError as e:
            return {"error": f"bad arguments: {e}"}
        except ToolError as e:
            return {"error": str(e)}

    # ---- read-only ----
    def t_get_company_dossier(self) -> dict:
        r = latest_research(self.ctx.session, self.ctx.prospect.id)
        return r.dossier if r else {"error": "no research available"}

    def t_get_solution(self) -> dict:
        s = latest_solution(self.ctx.session, self.ctx.prospect.id)
        if not s:
            raise ToolError("no solution available")
        return {k: s.spec.get(k) for k in ("name", "summary", "capabilities", "integrations", "workflow_steps",
                                           "human_escalation_points", "assumptions", "requires_validation")}

    def t_get_pricing_rules(self) -> dict:
        offer = latest_offer(self.ctx.session, self.ctx.deal.id)
        if not offer:
            raise ToolError("no offer available")
        self.ctx.allowed_amounts_cents.add(offer.target_price_cents)
        return {"quote_usd": offer.target_price_cents / 100, "payment": "one-time, paid upfront",
                "discounts": "You cannot offer discounts. Price discussion is handled by the negotiation specialist.",
                "rationale": offer.rationale}

    def t_calculate_roi(self, hours_saved_per_week: float, hourly_cost_usd: float) -> dict:
        if not (0 < float(hours_saved_per_week) <= 168 and 0 < float(hourly_cost_usd) <= 1000):
            raise ToolError("implausible inputs")
        offer = latest_offer(self.ctx.session, self.ctx.deal.id)
        if not offer:
            raise ToolError("no offer available")
        r = calculate_roi(hours_saved_per_week=float(hours_saved_per_week), hourly_cost_usd=float(hourly_cost_usd),
                          price_cents=offer.target_price_cents)
        self.ctx.allowed_amounts_cents.update({r["annual_value_cents"], offer.target_price_cents})
        return {**r, "annual_value_usd": r["annual_value_cents"] / 100, "price_usd": offer.target_price_cents / 100}

    # ---- state-changing (guarded) ----
    def t_create_proposal(self) -> dict:
        deal = self.ctx.deal
        if S(deal.state) not in (S.PROPOSAL, S.NEGOTIATION) or not deal.agreed_price_cents:
            raise ToolError("not allowed: no price has been agreed in principle yet")
        if not self.ctx.request_proposal:
            raise ToolError("proposal generation unavailable")
        return self.ctx.request_proposal()

    def t_request_human_approval(self, reason: str = "") -> dict:
        esc = D.escalate(self.ctx.session, self.ctx.deal, "AGENT_REQUESTED_APPROVAL", reason[:500] or "agent requested review",
                         detail={"requested_by": "sales_agent"})
        return {"status": "escalated", "escalation_id": esc.id}

    def t_create_payment_request(self) -> dict:
        p = latest_proposal(self.ctx.session, self.ctx.deal.id)
        if not p or p.status != "accepted" or S(self.ctx.deal.state) != S.ACCEPTED:
            raise ToolError("not allowed: the prospect has not formally accepted a proposal")
        if not self.ctx.request_payment:
            raise ToolError("payment unavailable")
        return self.ctx.request_payment()
