"""Outbound call orchestration through the Retell integration layer (thin; policy comes from compliance)."""
from __future__ import annotations

from sqlalchemy.orm import Session

from src.database.models import Company, Deal, Prospect
from src.integrations.retell.client import RetellError
from src.services import deals as D
from src.services.compliance import ComplianceService
from src.services.container import Services
from src.services.pricing import fmt
from src.services.queries import conversation_for, latest_offer, latest_solution, primary_contact


class CallBlocked(Exception):
    pass


class CallService:
    def __init__(self, session: Session, services: Services):
        self.s, self.svc = session, services

    def start_outbound(self, deal: Deal) -> dict:
        cfg = self.svc.settings
        company = self.s.get(Company, self.s.get(Prospect, deal.prospect_id).company_id)
        contact = primary_contact(self.s, company.id)
        comp = ComplianceService(self.s, cfg)
        gate = comp.can_call(contact, company)
        if not gate.allowed:
            D.record_event(self.s, deal, "compliance", action="call_blocked", reason=gate.reason)
            raise CallBlocked(gate.reason)
        sol = latest_solution(self.s, deal.prospect_id)
        offer = latest_offer(self.s, deal.id)
        dyn = {"deal_id": deal.id, "company_name": company.name, "contact_name": contact.name or "",
               "solution_name": sol.name if sol else "", "disclosure": comp.call_disclosure()}
        # Optional audio rendering of our opening line through TTSService (Chatterbox); failure is non-fatal.
        audio = self.svc.tts.try_synthesize(dyn["disclosure"], key=f"call_{deal.id}")
        opening_audio = str(audio.path) if audio else None
        if cfg.retell_dry_run:
            conv = conversation_for(self.s, deal.id, "voice", f"dry_run:{deal.id}")
            D.record_event(self.s, deal, "call", status="dry_run", conversation_id=conv.id)
            return {"status": "dry_run", "conversation_id": conv.id, "dynamic_variables": dyn, "opening_audio": opening_audio}
        try:
            call = self.svc.retell.create_phone_call(
                to_number=contact.phone, from_number=cfg.retell_from_number, agent_id=cfg.retell_agent_id or None,
                metadata={"deal_id": deal.id}, dynamic_variables=dyn,
            )
        except RetellError as e:
            D.record_event(self.s, deal, "call_failed", error=str(e))
            raise
        conv = conversation_for(self.s, deal.id, "voice", call.call_id)
        D.record_event(self.s, deal, "call", status="started", call_id=call.call_id, conversation_id=conv.id)
        return {"status": "started", "call_id": call.call_id, "conversation_id": conv.id, "opening_audio": opening_audio}
