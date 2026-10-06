from __future__ import annotations

from src.agents.base import BaseAgent
from src.database.models import Company, Deal, Prospect
from src.models.schemas import OutreachOut
from src.services.guardrails import check_reply
from src.services.queries import latest_research, latest_solution, selected_pain

GENERIC_OPENERS = ("we are an ai automation", "we're an ai automation", "we are an ai company", "i hope this email finds you well")


class OutreachError(Exception):
    pass


class OutreachAgent(BaseAgent):
    name = "outreach"
    prompt_name = "outreach"

    def draft(self, deal: Deal, channel: str = "email") -> OutreachOut:
        prospect = self.s.get(Prospect, deal.prospect_id)
        company = self.s.get(Company, prospect.company_id)
        research, pain, sol = latest_research(self.s, prospect.id), selected_pain(self.s, prospect.id), latest_solution(self.s, prospect.id)
        if not (research and pain and sol):
            raise OutreachError("outreach requires research, a selected pain and a solution")
        facts = [c for c in research.dossier["claims"] if c["label"] == "FACT"]
        fact_ids = {c["id"] for c in facts}
        if not facts:
            raise OutreachError("no verified facts to personalise from")
        cfg = self.svc.settings
        with self.trace(prospect_id=prospect.id, deal_id=deal.id, input_summary=f"outreach {channel} to {company.name}") as tr:
            variables = {
                "channel": channel, "company_name": company.name, "company_dossier": facts,
                "identified_problem": {"problem": pain.problem, "business_impact": pain.business_impact},
                "solution": {k: sol.spec[k] for k in ("name", "summary", "capabilities")},
                "sender": {"name": cfg.sender_name, "company": cfg.sender_company},
            }
            feedback = ""
            out: OutreachOut | None = None
            for attempt in range(2):
                out = self.ask(tr, OutreachOut, variables, "Write the message." + feedback, temperature=0.5, max_tokens=800)
                problems = []
                if not out.body.strip():
                    problems.append("empty body: no strong fact to personalise from")
                cited = [i for i in out.evidence_claim_ids if i in fact_ids]
                if not cited:
                    problems.append("must cite at least one provided fact id in evidence_claim_ids")
                if out.body.lower().lstrip().startswith(GENERIC_OPENERS):
                    problems.append("generic opener is not allowed")
                g = check_reply(out.body + " " + out.subject, allow_money=False)
                problems += g.violations
                if not problems:
                    out.evidence_claim_ids = cited
                    tr.set_output(out.model_dump(), out.decision_summary)
                    return out
                feedback = "\nYour previous draft was rejected: " + "; ".join(problems) + ". Fix these."
            raise OutreachError(f"outreach draft failed guardrails: {feedback.strip()}")
