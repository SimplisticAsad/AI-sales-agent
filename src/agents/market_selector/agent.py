from __future__ import annotations

from src.agents.base import BaseAgent
from src.config.catalog import get_catalog
from src.database.models import MarketProfile
from src.models.schemas import MarketProfileOut


class MarketSelectorAgent(BaseAgent):
    name = "market_selector"
    prompt_name = "market_selector"

    def run(self, business_goal: str, constraints: str = "") -> MarketProfile:
        with self.trace(input_summary=f"goal={business_goal[:200]}") as tr:
            out = self.ask(
                tr, MarketProfileOut,
                {"business_goal": business_goal, "constraints": constraints or "none",
                 "approved_service_scope": list(get_catalog().approved_service_scope)},
                "Define the ICP.",
            )
            if out.employee_min > out.employee_max:
                out.employee_min, out.employee_max = out.employee_max, out.employee_min
            out.employee_min = max(1, out.employee_min)
            if not out.industry.strip():
                raise ValueError("market selector returned no industry")
            row = MarketProfile(name=f"{out.industry} / {out.geography}", profile=out.model_dump())
            self.s.add(row)
            self.s.flush()
            tr.set_output(out.model_dump(), out.decision_summary)
            return row
