"""Resumable pipeline. Each step reads persisted state, is idempotent, and commits - so a crash resumes from the
last committed state. Steps never trust LLM-reported status; transitions are decided here."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.agents.pain_detector.agent import PainDetectorAgent
from src.agents.pricing.agent import PricingAgent
from src.agents.researcher.agent import ResearcherAgent
from src.agents.solution_architect.agent import SolutionArchitectAgent
from src.database.models import Contact, PainPoint, Prospect
from src.models.enums import ClaimLabel
from src.models.enums import DealState as S
from src.services import deals as D
from src.services.container import Services
from src.services.delivery import DeliveryService
from src.services.outreach import OutreachBlocked, OutreachService
from src.services.payments import PaymentService
from src.services.proposals import ProposalService
from src.services.qualification import QUALIFY_MIN_SCORE
from src.services.queries import latest_proposal, latest_research, latest_solution, selected_pain

log = logging.getLogger("pipeline")


@dataclass
class StepResult:
    step: str | None
    state: str
    detail: str = ""


class Pipeline:
    def __init__(self, session: Session, services: Services):
        self.s, self.svc = session, services

    # ------------------------------------------------------------------ individual steps
    def research(self, deal):
        prospect = self.s.get(Prospect, deal.prospect_id)
        D.transition(self.s, deal, S.RESEARCHING, reason="research started")
        return ResearcherAgent(self.s, self.svc).run(prospect)

    def qualify(self, deal) -> bool:
        prospect = self.s.get(Prospect, deal.prospect_id)
        research = latest_research(self.s, prospect.id)
        base = prospect.qualification or {}
        facts = [c for c in (research.dossier["claims"] if research else []) if c["label"] == ClaimLabel.FACT.value]
        contact_path = bool(self.s.scalar(select(Contact).where(
            Contact.company_id == prospect.company_id,
            (Contact.email.is_not(None)) | (Contact.phone.is_not(None)) | (Contact.contact_page_url.is_not(None)))))
        reasons = list(base.get("reasons", []))
        ok = True
        if (prospect.qualification_score or 0) < QUALIFY_MIN_SCORE:
            ok = False
            reasons.append(f"ICP score {prospect.qualification_score} < {QUALIFY_MIN_SCORE}")
        if not contact_path:
            ok = False
            reasons.append("no sourced contact path")
        if not facts:
            ok = False
            reasons.append("research produced no verified facts")
        prospect.qualification = {**base, "stage": "qualification", "decision": "qualified" if ok else "disqualified",
                                  "reasons": reasons, "verified_facts": len(facts)}
        why = (f"qualified: ICP score {prospect.qualification_score}, {len(facts)} verified fact(s), sourced contact path"
               if ok else "disqualified: " + "; ".join(r for r in reasons if "unknown" not in r)[:400])
        D.transition(self.s, deal, S.QUALIFIED if ok else S.DISQUALIFIED, reason=why, score=prospect.qualification_score)
        return ok

    def detect_pain(self, deal) -> PainPoint | None:
        prospect = self.s.get(Prospect, deal.prospect_id)
        rows = PainDetectorAgent(self.s, self.svc).run(prospect, latest_research(self.s, prospect.id))
        best = next((r for r in rows if r.selected), None)
        if best:
            D.transition(self.s, deal, S.PAIN_IDENTIFIED, reason=best.problem[:200], priority=best.priority_score)
        else:
            D.transition(self.s, deal, S.DISQUALIFIED, reason="no viable pain point",
                         rejected=[r.rejected_reason for r in rows][:5])
        return best

    def design_and_price(self, deal):
        prospect = self.s.get(Prospect, deal.prospect_id)
        pain = selected_pain(self.s, prospect.id)
        sol = latest_solution(self.s, prospect.id) or SolutionArchitectAgent(self.s, self.svc).run(
            prospect, pain, latest_research(self.s, prospect.id))
        offer = PricingAgent(self.s, self.svc).run(deal, sol)
        D.transition(self.s, deal, S.SOLUTION_READY, reason="solution designed and priced", offer_id=offer.id,
                     target_cents=offer.target_price_cents)
        return offer

    def outreach(self, deal):
        return OutreachService(self.s, self.svc).send_initial(deal)

    # ------------------------------------------------------------------ orchestration
    def advance(self, deal) -> StepResult:
        """Run the single next automated step for the deal's current state."""
        state = S(deal.state)
        steps = {
            S.DISCOVERED: ("research", self.research),
            S.RESEARCHING: ("qualify", self._research_then_qualify),
            S.QUALIFIED: ("detect_pain", self.detect_pain),
            S.PAIN_IDENTIFIED: ("design_and_price", self.design_and_price),
        }
        if state == S.SOLUTION_READY and self.svc.settings.auto_outreach:
            steps[S.SOLUTION_READY] = ("outreach", self.outreach)
        if state == S.PAID:
            steps[S.PAID] = ("handoff", self.handoff)
        if state == S.FAILED:  # sticky: only an explicit ops retry (POST /pipeline/{id}/retry) resumes it
            return StepResult(None, deal.state, f"FAILED at {deal.resume_state}: {deal.last_error}; manual retry required")
        if state not in steps:
            return StepResult(None, deal.state, "waiting for external event or manual trigger")
        name, fn = steps[state]
        deal_id = deal.id
        try:
            fn(deal)
            deal.retry_count, deal.last_error = 0, None
            self.s.commit()
            return StepResult(name, deal.state)
        except OutreachBlocked as e:
            self.s.commit()  # keeps the compliance event
            return StepResult(None, deal.state, f"outreach blocked: {e}")
        except Exception as e:
            log.exception("step %s failed for deal %s", name, deal_id)
            self.s.rollback()
            deal = D.get_deal(self.s, deal_id)
            deal.retry_count += 1
            deal.last_error = f"{name}: {type(e).__name__}: {str(e)[:300]}"
            D.record_event(self.s, deal, "step_failed", step=name, error=deal.last_error, retry=deal.retry_count)
            if deal.retry_count >= self.svc.settings.max_step_retries and S(deal.state) in (
                    S.DISCOVERED, S.RESEARCHING, S.QUALIFIED, S.PAIN_IDENTIFIED, S.SOLUTION_READY, S.PAID, S.DELIVERY):
                D.transition(self.s, deal, S.FAILED, reason=f"{name} failed {deal.retry_count}x")
            self.s.commit()
            return StepResult(name, deal.state, f"failed: {deal.last_error}")

    def _research_then_qualify(self, deal):
        """Resume point: RESEARCHING with research done -> qualify; without research -> research again."""
        if latest_research(self.s, deal.prospect_id):
            return self.qualify(deal)
        self.research(deal)

    def run_until_blocked(self, deal, max_steps: int = 10) -> list[StepResult]:
        out: list[StepResult] = []
        for _ in range(max_steps):
            r = self.advance(deal)
            out.append(r)
            deal = D.get_deal(self.s, deal.id)
            if r.step is None or r.detail.startswith("failed"):
                break
        return out

    def retry(self, deal) -> StepResult:
        """Human/ops retry of a FAILED deal: return to the state it failed in and re-run."""
        if S(deal.state) != S.FAILED or not deal.resume_state:
            return StepResult(None, deal.state, "not in FAILED")
        D.transition(self.s, deal, S(deal.resume_state), actor="ops", reason="manual retry")
        deal.retry_count = 0
        self.s.commit()
        return self.advance(deal)

    # ------------------------------------------------------------------ commercial steps
    def accept_proposal(self, proposal_id: str, *, token: str | None, admin: bool) -> dict:
        from src.database.models import Proposal

        prop = self.s.get(Proposal, proposal_id)
        if not prop:
            raise LookupError("proposal not found")
        ps = ProposalService(self.s, self.svc.settings)
        if not admin and not ps.token_ok(prop, token):
            raise PermissionError("invalid acceptance token")
        ps.accept(prop, actor="client" if not admin else "admin")
        deal = D.get_deal(self.s, prop.deal_id)
        pay = PaymentService(self.s, self.svc.payment_provider, self.svc.settings.payment_currency).create_for_deal(deal)
        self.s.commit()
        return {"proposal_id": prop.id, "deal_id": deal.id, "deal_state": deal.state, "payment_id": pay.id,
                "payment_url": pay.payment_url, "amount_cents": pay.amount_cents, "currency": pay.currency}

    def handoff(self, deal):
        return DeliveryService(self.s, self.svc.delivery_target).handoff(deal)

    def on_paid(self, deal) -> None:
        self.handoff(deal)
