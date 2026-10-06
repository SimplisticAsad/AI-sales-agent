"""ConversationWorkflow: the single entry point for any inbound prospect message (email, voice, web, sim).

Retell (or any channel adapter) calls `handle_inbound`; it contains no business logic of its own.
Order of precedence: compliance (opt-out) > honesty (AI disclosure) > escalation > deterministic negotiation > LLM sales.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from src.agents.negotiation.agent import NegotiationAgent, fallback_reply
from src.agents.proposal.agent import ProposalAgent
from src.agents.sales.agent import SalesAgent
from src.config.catalog import get_catalog
from src.database.models import Company, Contact, Message, Prospect
from src.models.enums import DealState as S
from src.models.enums import Intent
from src.models.schemas import ExtractedRequest
from src.services import deals as D
from src.services.compliance import ComplianceService, detect_opt_out
from src.services.container import Services
from src.services.guardrails import asks_if_ai, check_reply
from src.services.negotiation import (
    ALLOWED_PAYMENT_TERMS, NegotiationDecision, NegotiationEngine, risk_scan,
)
from src.services.negotiation_store import load_policy, load_state, save_round
from src.services.pricing import fmt
from src.services.proposals import ProposalError, ProposalService
from src.services.queries import conversation_for, history_text, latest_offer, primary_contact
from src.tools.registry import ToolContext

NO_REPLY_STATES = {S.DO_NOT_CONTACT, S.LOST, S.DISQUALIFIED, S.COMPLETED}
PRE_OUTREACH = {S.DISCOVERED, S.RESEARCHING, S.QUALIFIED, S.PAIN_IDENTIFIED, S.SOLUTION_READY, S.FAILED}
HOLD = "Thanks for your message. A member of our team will review this and follow up with you personally."
SAFE_FALLBACK = "That's a detail I want to confirm with my team rather than guess. I'll follow up shortly."


@dataclass
class TurnResult:
    reply: str | None
    deal_state: str
    intent: str | None = None
    escalated: bool = False
    proposal_url: str | None = None
    ended: bool = False


class ConversationWorkflow:
    def __init__(self, session: Session, services: Services):
        self.s, self.svc = session, services

    # ------------------------------------------------------------------ helpers
    def _msg(self, conv, role: str, content: str, status: str = "sent", **meta) -> Message:
        m = Message(conversation_id=conv.id, role=role, content=content, status=status, meta=meta)
        self.s.add(m)
        self.s.flush()
        return m

    def _disclosure_note(self, channel: str) -> str:
        if channel == "voice":
            part = "disclosure_voice_always" if self.svc.settings.call_disclosure_mode == "always" else "disclosure_voice_if_asked"
        else:
            part = "disclosure_text"
        return self.svc.prompts.fragment("sales", part)

    def _result(self, deal, reply, intent=None, **kw) -> TurnResult:
        return TurnResult(reply, deal.state, intent.value if isinstance(intent, Intent) else intent, **kw)

    # ------------------------------------------------------------------ main entry
    def handle_inbound(self, deal_id: str, text: str, channel: str = "email", external_id: str | None = None) -> TurnResult:
        deal = D.get_deal(self.s, deal_id)
        prospect = self.s.get(Prospect, deal.prospect_id)
        company = self.s.get(Company, prospect.company_id)
        contact = primary_contact(self.s, company.id)
        conv = conversation_for(self.s, deal.id, channel, external_id)
        self._msg(conv, "prospect", text, status="received")
        state = S(deal.state)
        if state in NO_REPLY_STATES:
            return self._result(deal, None, ended=True)

        # 1. compliance: opt-out always wins
        if detect_opt_out(text):
            return self._opt_out(deal, contact, conv)

        # 2. honesty: direct question about being an AI is always answered truthfully
        if asks_if_ai(text):
            reply = ("Yes, I'm an AI assistant working for " + (self.svc.settings.sender_company or "our company") +
                     ". If you'd rather speak with a person, just say so and I'll arrange it. Happy to continue otherwise.")
            self._msg(conv, "agent", reply, kind="ai_disclosure")
            return self._result(deal, reply, "ai_question")

        if state in PRE_OUTREACH:
            self._msg(conv, "system", "inbound message received before outreach; held for human", status="sent")
            if state != S.FAILED:
                D.escalate(self.s, deal, "UNEXPECTED_INBOUND", "Prospect messaged before outreach completed")
            return self._result(deal, HOLD, escalated=True)
        if state == S.ESCALATED:
            self._msg(conv, "agent", HOLD, kind="escalation_hold")
            return self._result(deal, HOLD, escalated=True)
        if state in (S.PAID, S.DELIVERY):
            reply = "Thanks for your message. Your payment is confirmed and our delivery team will be in touch about next steps."
            self._msg(conv, "agent", reply, kind="post_sale")
            return self._result(deal, reply)

        if state == S.OUTREACH_SENT:
            D.transition(self.s, deal, S.ENGAGED, reason="prospect replied")
        return self._sales_turn(deal, prospect, company, conv, text, channel)

    # ------------------------------------------------------------------ branches
    def _opt_out(self, deal, contact: Contact | None, conv) -> TurnResult:
        ComplianceService(self.s, self.svc.settings).add_suppression(contact, "opt_out")
        if S(deal.state) != S.DO_NOT_CONTACT:
            D.transition(self.s, deal, S.DO_NOT_CONTACT, reason="prospect opted out")
        D.record_event(self.s, deal, "compliance", action="opt_out_recorded")
        reply = "Understood - I've removed you from our list and you won't be contacted again. Sorry for the interruption."
        self._msg(conv, "agent", reply, kind="opt_out_confirmation")
        return self._result(deal, reply, Intent.OPT_OUT, ended=True)

    def _escalate_reply(self, deal, conv, code: str, summary: str, reply: str, intent=None, **detail) -> TurnResult:
        D.escalate(self.s, deal, code, summary, detail=detail)
        self._msg(conv, "agent", reply, kind="escalation", reason=code)
        return self._result(deal, reply, intent, escalated=True)

    def _sales_turn(self, deal, prospect, company, conv, text, channel) -> TurnResult:
        svc, s = self.svc, self.s
        policy = load_policy(s, deal)
        st = load_state(s, deal)
        ctx = ToolContext(session=s, svc=svc, deal=deal, prospect=prospect, company=company)
        ctx.request_payment = lambda: self._payment_tool(deal)
        history = history_text(s, deal.id)[:-1]  # exclude the message we just stored; it is passed separately
        sales = SalesAgent(s, svc)
        turn = sales.turn(ctx, message=text, history=history, channel=channel, neg_state=st,
                          disclosure_note=self._disclosure_note(channel))
        intent, req = turn.intent, turn.extracted
        cat = get_catalog()
        req.asks_unsupported_integration = [n for n in req.asks_unsupported_integration if not cat.is_verified(n)]

        if intent == Intent.OPT_OUT:
            return self._opt_out(deal, primary_contact(s, company.id), conv)
        if intent == Intent.REQUEST_HUMAN or S(deal.state) == S.ESCALATED:  # tool may have escalated mid-turn
            reply = "Of course - I'll ask a member of our team to follow up with you directly."
            return self._escalate_reply(deal, conv, "HUMAN_REQUESTED", "Prospect asked for / agent requested a human", reply, intent)
        if intent == Intent.REJECT:
            reply = turn.reply or "Understood, thanks for letting me know. I won't take more of your time."
            if not check_reply(reply, allowed_amounts_cents=[]).ok:
                reply = "Understood, thanks for letting me know. I won't take more of your time."
            D.transition(s, deal, S.LOST, reason="prospect declined")
            self._msg(conv, "agent", reply, kind="close_lost", intent=intent.value)
            return self._result(deal, reply, intent, ended=True)

        flags = risk_scan(text)
        pt = (req.payment_terms_request or "").strip().lower()
        risky = bool(flags) or req.asks_for_refund or req.asks_legal_question or bool(req.contract_term_requests) \
            or bool(req.scope_requests) or bool(req.asks_unsupported_integration) or (pt and pt not in ALLOWED_PAYMENT_TERMS)
        negotiating = risky or intent in (Intent.PRICE_INQUIRY, Intent.COUNTER_OFFER, Intent.ACCEPT) \
            or bool(req.offered_price_usd) or bool(req.requested_discount_percent)
        if negotiating:
            return self._negotiate(deal, company, conv, text, req, intent, policy, st, history)

        # ---- ordinary consultative reply, guard-checked ----
        reply = turn.reply
        allowed = list(ctx.allowed_amounts_cents) + ([latest_offer(s, deal.id).target_price_cents] if (deal.quoted_price_cents and st.last_offered_cents) else [])
        guard = check_reply(reply or "", allowed_amounts_cents=allowed)
        if reply and not guard.ok:
            retry = sales.turn(ctx, message=text, history=history, channel=channel, neg_state=st,
                               disclosure_note=self._disclosure_note(channel),
                               feedback="\nYour previous reply was rejected by compliance: " + guard.feedback() + ". Rewrite it.")
            reply = retry.reply
            guard = check_reply(reply or "", allowed_amounts_cents=allowed)
        if not reply or not guard.ok:
            return self._escalate_reply(deal, conv, "GUARDRAIL_VIOLATION", "Agent reply failed safety checks; held for review",
                                        SAFE_FALLBACK, intent, violations=guard.violations)
        if S(deal.state) == S.ENGAGED and intent in (Intent.QUESTION, Intent.INTEREST, Intent.OBJECTION):
            D.transition(s, deal, S.DISCOVERY, reason="discovery conversation underway")
        self._msg(conv, "agent", reply, kind="sales_reply", intent=intent.value, objection_type=turn.objection_type,
                  buying_intent=turn.buying_intent, next_action=turn.next_action)
        return self._result(deal, reply, intent)

    # ------------------------------------------------------------------ negotiation (deterministic policy)
    def _negotiate(self, deal, company, conv, text, req: ExtractedRequest, intent, policy, st, history) -> TurnResult:
        s = self.s
        engine = NegotiationEngine(policy)
        if intent == Intent.ACCEPT and not (req.offered_price_usd or req.requested_discount_percent) \
                and not (risk_scan(text) - {"guarantee"}) and st.last_offered_cents:
            decision = NegotiationDecision("accept", st.round + 1, st.last_offered_cents, ["accepted_current_price"],
                                           "Prospect accepted the price we stated.", allowed_amounts_cents=[st.last_offered_cents])
        elif not (req.offered_price_usd or req.requested_discount_percent) and not st.last_offered_cents \
                and not (risk_scan(text) or req.asks_for_refund or req.scope_requests or req.contract_term_requests
                         or req.asks_legal_question or req.asks_unsupported_integration or req.payment_terms_request):
            decision = engine.quote()  # first time a price is stated (also covers "accept" before any price)
        else:
            decision = engine.evaluate(req, st, text)
        NegotiationEngine.advance(st, req, decision, policy)
        save_round(s, deal, req, text, decision, st)
        D.record_event(s, deal, "negotiation", action=decision.action, round=decision.round,
                       price_cents=decision.price_cents, reasons=decision.reason_codes, explanation=decision.explanation)

        reply = NegotiationAgent(s, self.svc).respond(deal, company, decision, text, history)
        if decision.escalate:
            return self._escalate_reply(deal, conv, decision.escalation_code or "POLICY", decision.explanation, reply, intent,
                                        reason_codes=decision.reason_codes)

        state = S(deal.state)
        if decision.action == "accept" and decision.price_cents:
            deal.agreed_price_cents = decision.price_cents
            if state != S.PROPOSAL:
                D.transition(s, deal, S.PROPOSAL, reason="price agreed in principle", price_cents=decision.price_cents)
            url = self._generate_proposal(deal, company, decision.price_cents)
            reply = reply.rstrip() + f"\n\nYour written proposal: {url}"
            self._msg(conv, "agent", reply, kind="proposal_sent", intent=intent.value, price_cents=decision.price_cents)
            return self._result(deal, reply, intent, proposal_url=url)
        target = S.NEGOTIATION if (intent == Intent.COUNTER_OFFER or decision.action == "counter") else S.PROPOSAL
        if state in (S.ENGAGED, S.DISCOVERY, S.PROPOSAL, S.NEGOTIATION):
            D.transition(s, deal, target, reason=f"negotiation: {decision.action}")
        self._msg(conv, "agent", reply, kind="negotiation_reply", intent=intent.value, action=decision.action, price_cents=decision.price_cents)
        return self._result(deal, reply, intent)

    # ------------------------------------------------------------------ proposal / payment helpers
    def _generate_proposal(self, deal, company, price_cents: int) -> str:
        narrative = ProposalAgent(self.s, self.svc).run(deal, company, price_cents)
        ps = ProposalService(self.s, self.svc.settings)
        prop, token = ps.generate(deal, narrative, price_cents)
        return ps.public_url(prop, token)

    def _payment_tool(self, deal) -> dict:
        from src.services.payments import PaymentService

        pay = PaymentService(self.s, self.svc.payment_provider, self.svc.settings.payment_currency).create_for_deal(deal)
        return {"payment_url": pay.payment_url, "amount_usd": pay.amount_cents / 100}
