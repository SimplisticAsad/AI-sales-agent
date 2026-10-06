from __future__ import annotations

from src.agents.base import BaseAgent
from src.config.catalog import get_catalog
from src.database.models import Company, Deal
from src.models.schemas import ProposalNarrativeOut
from src.services.guardrails import check_reply
from src.services.pricing import fmt
from src.services.queries import history_text, latest_research, latest_solution, selected_pain


def fallback_narrative(sol_spec: dict, effort_hours: int, problem: str) -> ProposalNarrativeOut:
    weeks = max(1, round(effort_hours / 20))
    ints = [i["name"] for i in sol_spec.get("integrations", []) if i.get("verified")]
    return ProposalNarrativeOut(
        client_problem=problem, proposed_solution=sol_spec.get("summary") or sol_spec.get("name", ""),
        features=list(sol_spec.get("capabilities", [])), integrations=ints,
        deliverables=[f"Working implementation of: {sol_spec.get('name')}", "Setup and configuration", "Handover documentation"],
        timeline=f"Estimated {weeks} week(s) after payment is confirmed and access is provided.",
        assumptions=list(sol_spec.get("assumptions", [])), exclusions=[], special_requirements=[],
        conversation_summary="See conversation history.",
    )


class ProposalAgent(BaseAgent):
    name = "proposal"
    prompt_name = "proposal"

    def run(self, deal: Deal, company: Company, agreed_price_cents: int) -> ProposalNarrativeOut:
        sol, pain, research = latest_solution(self.s, deal.prospect_id), selected_pain(self.s, deal.prospect_id), latest_research(self.s, deal.prospect_id)
        with self.trace(prospect_id=deal.prospect_id, deal_id=deal.id, input_summary=f"proposal at {fmt(agreed_price_cents)}") as tr:
            fb = fallback_narrative(sol.spec, sol.effort_hours, pain.problem if pain else "")
            try:
                out = self.ask(tr, ProposalNarrativeOut, {
                    "company_name": company.name, "company_dossier": research.dossier if research else {},
                    "identified_problem": pain.problem if pain else "", "solution": sol.spec,
                    "agreed_price": fmt(agreed_price_cents), "conversation_history": history_text(self.s, deal.id),
                    "approved_service_scope": list(get_catalog().approved_service_scope),
                }, "Draft the proposal sections.", max_tokens=2500)
            except Exception as e:
                tr.tool_call("llm", {}, f"failed ({type(e).__name__}); using deterministic narrative")
                tr.set_output({"fallback": True}, "LLM unavailable; proposal built from solution spec")
                return fb
            blob = " ".join([out.client_problem, out.proposed_solution, out.timeline, *out.features, *out.deliverables,
                             *out.assumptions, *out.exclusions, *out.special_requirements, out.conversation_summary])
            g = check_reply(blob, allow_money=False)
            if not g.ok:
                tr.tool_call("guard", {}, g.feedback())
                tr.set_output({"fallback": True}, "LLM narrative violated guardrails; used deterministic narrative")
                return fb
            # unverified integrations must never appear as deliverables/integrations
            verified = {i["name"].lower() for i in sol.spec["integrations"] if i["verified"]}
            out.integrations = [i for i in out.integrations if i.lower() in verified]
            tr.set_output(out.model_dump(), out.decision_summary)
            return out
