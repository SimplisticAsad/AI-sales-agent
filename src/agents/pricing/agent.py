from __future__ import annotations

from src.agents.base import BaseAgent
from src.database.models import Company, Deal, Offer, Prospect, Solution
from src.models.schemas import PricingNarrativeOut, ValueDriver
from src.services.guardrails import check_reply
from src.services.pricing import compute_target_price, fmt
from src.services.roi import annual_value_cents


class PricingAgent(BaseAgent):
    """Numbers come from `compute_target_price` (deterministic). The LLM only writes the value rationale."""

    name = "pricing"
    prompt_name = "pricing"

    def run(self, deal: Deal, solution: Solution) -> Offer:
        company = self.s.get(Company, self.s.get(Prospect, deal.prospect_id).company_id)
        spec = solution.spec
        with self.trace(prospect_id=deal.prospect_id, deal_id=deal.id, input_summary=f"price {solution.name}") as tr:
            drivers = [ValueDriver(**d) for d in spec.get("value_drivers", [])]
            annual = annual_value_cents(drivers)
            verified = sum(1 for i in spec["integrations"] if i["verified"])
            policy, breakdown = compute_target_price(
                effort_hours=solution.effort_hours, complexity=solution.complexity, verified_integrations=verified,
                unverified_integrations=len(spec["integrations"]) - verified, employee_estimate=company.employee_estimate,
                annual_value_cents=annual or None, settings=self.svc.settings,
            )
            roi = {
                "annual_value_cents": annual, "price_cents": policy.target_cents,
                "payback_months": round(policy.target_cents / (annual / 12), 1) if annual else None,
                "label": "ILLUSTRATIVE ESTIMATE from stated assumptions - not a guarantee",
                "assumptions": [d.model_dump() for d in drivers],
            }
            summary = {"target_price_usd": policy.target_cents / 100, "drivers": breakdown["notes"],
                       "effort_hours": solution.effort_hours, "complexity": solution.complexity,
                       "integration_count": len(spec["integrations"])}
            roi_prompt = {**roi, "annual_value_usd": annual / 100,
                          "payback_months": roi["payback_months"]} if annual else {"note": "value not quantified"}
            rationale = ""
            try:
                out = self.ask(tr, PricingNarrativeOut, {
                    "company_name": company.name, "solution": {k: spec[k] for k in ("name", "summary", "capabilities")},
                    "pricing_summary": summary, "roi": roi_prompt,
                }, "Write the value rationale.", temperature=0.3, max_tokens=500)
                allowed = [policy.target_cents] + ([annual] if annual else [])
                g = check_reply(out.value_rationale, allowed_amounts_cents=allowed)
                rationale = out.value_rationale if g.ok else ""
                if not g.ok:
                    tr.tool_call("guard", {}, f"rationale rejected: {g.feedback()}")
            except Exception as e:  # narrative is optional; numbers must not depend on it
                tr.tool_call("narrative", {}, f"skipped: {type(e).__name__}")
            if not rationale:
                rationale = (f"Price reflects about {solution.effort_hours} hours of {solution.complexity}-complexity work and "
                             f"{len(spec['integrations'])} integration(s).")
            offer = Offer(
                deal_id=deal.id, solution_id=solution.id, target_price_cents=policy.target_cents,
                min_price_cents=policy.minimum_cents, max_discount_percent=policy.max_discount_percent,
                currency=policy.currency, breakdown=breakdown, rationale=rationale, roi=roi,
            )
            self.s.add(offer)
            deal.quoted_price_cents = policy.target_cents
            deal.currency = policy.currency
            self.s.flush()
            tr.set_output({"offer_id": offer.id, "target": fmt(policy.target_cents), "floor": fmt(policy.minimum_cents),
                           "breakdown": breakdown},
                          f"Target {fmt(policy.target_cents)} from {solution.effort_hours}h x {solution.complexity}, "
                          f"{len(spec['integrations'])} integrations; floor {fmt(policy.minimum_cents)}. {'; '.join(breakdown['notes'])}")
            return offer
