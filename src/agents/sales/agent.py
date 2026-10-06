from __future__ import annotations

from src.agents.base import BaseAgent
from src.database.models import Deal
from src.models.schemas import SalesTurnOut
from src.services.negotiation import NegotiationState
from src.services.prompts import untrusted
from src.services.queries import latest_offer, latest_research, latest_solution, selected_pain
from src.tools.registry import ToolContext, ToolRegistry

MAX_TOOL_ROUNDS = 3


class SalesAgent(BaseAgent):
    name = "sales"
    prompt_name = "sales"

    def turn(self, ctx: ToolContext, *, message: str, history: list[dict], channel: str, neg_state: NegotiationState,
             disclosure_note: str, feedback: str = "") -> SalesTurnOut:
        deal: Deal = ctx.deal
        research, pain = latest_research(ctx.session, ctx.prospect.id), selected_pain(ctx.session, ctx.prospect.id)
        sol, offer = latest_solution(ctx.session, ctx.prospect.id), latest_offer(ctx.session, deal.id)
        registry = ToolRegistry(ctx)
        tool_results: list[dict] = []
        with self.trace(prospect_id=ctx.prospect.id, deal_id=deal.id, input_summary=f"[{channel}] prospect: {message[:300]}") as tr:
            out: SalesTurnOut | None = None
            for _ in range(MAX_TOOL_ROUNDS + 1):
                variables = {
                    "channel": channel, "disclosure_note": disclosure_note, "company_name": ctx.company.name,
                    "company_dossier": research.dossier if research else {},
                    "identified_problem": {"problem": pain.problem, "business_impact": pain.business_impact} if pain else {},
                    "solution": {k: sol.spec.get(k) for k in ("name", "summary", "capabilities", "integrations", "requires_validation",
                                                              "human_escalation_points", "assumptions")} if sol else {},
                    "pricing_summary": {"quote_usd": offer.target_price_cents / 100, "payment": "one-time, upfront",
                                        "value_rationale": offer.rationale} if offer else {},
                    "negotiation_state": {"round": neg_state.round, "last_price_stated_usd":
                                          (neg_state.last_offered_cents or 0) / 100 or None},
                    "tool_results": tool_results,
                    "conversation_history": history,
                    "prospect_message": untrusted(message, "prospect_message"),
                }
                out = self.ask(tr, SalesTurnOut, variables, "Respond to the latest prospect message." + feedback,
                               temperature=0.4, max_tokens=900)
                if not out.tool_calls:
                    break
                for call in out.tool_calls[:3]:
                    result = registry.execute(call.name, call.arguments)
                    tr.tool_call(call.name, call.arguments, result)
                    tool_results.append({"tool": call.name, "result": result})
            else:
                out.tool_calls = []
            assert out is not None
            tr.set_output({"intent": out.intent.value, "objection_type": out.objection_type, "buying_intent": out.buying_intent,
                           "next_action": out.next_action, "reply": out.reply, "extracted": out.extracted.model_dump()},
                          out.decision_summary)
            return out
