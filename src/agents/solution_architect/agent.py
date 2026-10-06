from __future__ import annotations

from src.agents.base import BaseAgent
from src.config.catalog import get_catalog
from src.database.models import Company, PainPoint, Prospect, Research, Solution
from src.models.schemas import SolutionOut


class SolutionArchitectAgent(BaseAgent):
    name = "solution_architect"
    prompt_name = "solution_architect"

    def run(self, prospect: Prospect, pain: PainPoint, research: Research) -> Solution:
        company = self.s.get(Company, prospect.company_id)
        cat = get_catalog()
        with self.trace(prospect_id=prospect.id, input_summary=f"solution for: {pain.problem[:160]}") as tr:
            out = self.ask(tr, SolutionOut, {
                "company_name": company.name, "company_dossier": research.dossier,
                "identified_problem": {"problem": pain.problem, "evidence": pain.evidence, "business_impact": pain.business_impact},
                "verified_integrations": list(cat.verified_integrations),
                "approved_service_scope": list(cat.approved_service_scope),
            }, "Design the solution.", max_tokens=3500)
            spec = out.model_dump()
            spec["integrations"] = [
                {**i.model_dump(), "verified": cat.is_verified(i.name)} for i in out.integrations
            ]
            spec["requires_validation"] = [i["name"] for i in spec["integrations"] if not i["verified"]]
            row = Solution(prospect_id=prospect.id, pain_point_id=pain.id, name=out.name, spec=spec,
                           complexity=out.complexity, effort_hours=out.effort_hours)
            self.s.add(row)
            self.s.flush()
            tr.set_output({"solution_id": row.id, "name": out.name, "requires_validation": spec["requires_validation"]},
                          out.decision_summary or f"Designed {out.name}")
            return row
