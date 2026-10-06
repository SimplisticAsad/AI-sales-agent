from __future__ import annotations

from src.agents.base import BaseAgent
from src.database.models import Company, Deal
from src.models.schemas import NegotiationReplyOut
from src.services.guardrails import check_reply
from src.services.negotiation import NegotiationDecision
from src.services.pricing import fmt
from src.services.prompts import untrusted
from src.services.queries import latest_solution


def fallback_reply(d: NegotiationDecision) -> str:
    if d.action == "escalate":
        return "That's something I want to confirm with my team rather than guess at. I'll come back to you shortly."
    price = fmt(d.price_cents or 0)
    if d.action == "accept":
        return f"Great, we can do that at {price}, one-time and paid upfront. I'll send a written proposal for you to review."
    return f"The price for this project is {price}, one-time and paid upfront. I'm happy to walk through what's included."


class NegotiationAgent(BaseAgent):
    """The decision is made by NegotiationEngine; this agent only words it, and its output is guard-checked."""

    name = "negotiation"
    prompt_name = "negotiation"

    def respond(self, deal: Deal, company: Company, decision: NegotiationDecision, message: str, history: list[dict]) -> str:
        sol = latest_solution(self.s, deal.prospect_id)
        with self.trace(prospect_id=deal.prospect_id, deal_id=deal.id,
                        input_summary=f"decision={decision.action} price={decision.price_cents} codes={decision.reason_codes}") as tr:
            variables = {
                "company_name": company.name,
                "solution": {k: sol.spec.get(k) for k in ("name", "summary", "capabilities")} if sol else {},
                "decision": {"action": decision.action, "price_usd": (decision.price_cents or 0) / 100 if decision.price_cents else None,
                             "round": decision.round, "talking_points": decision.talking_points},
                "conversation_history": history, "prospect_message": untrusted(message, "prospect_message"),
            }
            feedback = ""
            for _ in range(2):
                out = self.ask(tr, NegotiationReplyOut, variables, "Write the reply." + feedback, temperature=0.4, max_tokens=500)
                g = check_reply(out.reply, allowed_amounts_cents=decision.allowed_amounts_cents)
                if g.ok:
                    tr.set_output({"reply": out.reply, "decision": decision.to_dict()}, out.decision_summary or decision.explanation)
                    return out.reply
                feedback = "\nREJECTED: " + g.feedback() + ". Rewrite obeying the decision exactly."
            tr.tool_call("guard", {}, "fell back to template: " + feedback.strip())
            tr.set_output({"reply": fallback_reply(decision), "fallback": True, "decision": decision.to_dict()}, decision.explanation)
            return fallback_reply(decision)
