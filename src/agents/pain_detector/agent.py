from __future__ import annotations

from src.agents.base import BaseAgent
from src.database.models import Company, MarketProfile, PainPoint, Prospect, Research
from src.models.schemas import PainDetectionOut

WEIGHTS = {
    "frequency": 0.20, "cost": 0.15, "repetitiveness": 0.15, "business_impact": 0.15,
    "feasibility": 0.15, "integration_availability": 0.10, "roi_demonstrability": 0.10,
}
MIN_CONFIDENCE, MIN_AUTOMATION, MIN_PRIORITY = 0.4, 0.5, 6.0


def priority(criteria: dict[str, float]) -> float:
    return round(sum(max(0.0, min(10.0, float(criteria.get(k, 0)))) * w for k, w in WEIGHTS.items()), 2)


class PainDetectorAgent(BaseAgent):
    name = "pain_detector"
    prompt_name = "pain_detector"

    def run(self, prospect: Prospect, research: Research) -> list[PainPoint]:
        company = self.s.get(Company, prospect.company_id)
        claims = {c["id"]: c for c in research.dossier["claims"]}
        profile_row = self.s.get(MarketProfile, prospect.market_profile_id) if prospect.market_profile_id else None
        with self.trace(prospect_id=prospect.id, input_summary=f"pain detection for {company.name} ({len(claims)} claims)") as tr:
            out = self.ask(tr, PainDetectionOut, {
                "company_name": company.name, "company_dossier": research.dossier,
                "market_profile": profile_row.profile if profile_row else {},
            }, "Identify automatable pains.", max_tokens=3000)
            rows: list[PainPoint] = []
            for p in out.pains:
                valid_ids = [i for i in p.evidence_claim_ids if i in claims]
                has_fact = any(claims[i]["label"] == "FACT" for i in valid_ids)
                conf = p.confidence if has_fact else min(p.confidence, 0.5)
                crit = p.criteria.model_dump()
                prio = priority(crit)
                reason = None
                if not valid_ids:
                    reason = "no valid evidence claim ids"
                elif conf < MIN_CONFIDENCE:
                    reason = f"confidence {conf:.2f} < {MIN_CONFIDENCE}"
                elif p.automation_potential < MIN_AUTOMATION:
                    reason = f"automation potential {p.automation_potential:.2f} < {MIN_AUTOMATION}"
                elif prio < MIN_PRIORITY:
                    reason = f"priority {prio} < {MIN_PRIORITY}"
                rows.append(PainPoint(
                    prospect_id=prospect.id, research_id=research.id, problem=p.problem,
                    evidence=[{"claim_id": i, "statement": claims[i]["statement"], "label": claims[i]["label"],
                               "source_url": claims[i].get("source_url")} for i in valid_ids],
                    confidence=conf, business_impact=p.business_impact, automation_potential=p.automation_potential,
                    criteria=crit, priority_score=prio, rejected_reason=reason,
                ))
            viable = sorted((r for r in rows if not r.rejected_reason), key=lambda r: r.priority_score, reverse=True)
            if viable:
                viable[0].selected = True
            self.s.add_all(rows)
            self.s.flush()
            tr.set_output({"proposed": len(rows), "viable": len(viable),
                           "selected": viable[0].problem if viable else None,
                           "rejected": [{"problem": r.problem, "why": r.rejected_reason} for r in rows if r.rejected_reason]},
                          out.decision_summary or (f"Selected: {viable[0].problem}" if viable else "No viable pain; reject prospect"))
            return rows
