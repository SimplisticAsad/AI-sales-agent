"""OutreachService: channel adapters + compliance gate. Idempotent per deal."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.agents.outreach.agent import OutreachAgent
from src.database.models import Company, Conversation, Deal, Message, Prospect
from src.integrations.outreach.providers import OutreachSendError
from src.models.enums import DealState as S
from src.services import deals as D
from src.services.compliance import ComplianceService
from src.services.container import Services
from src.services.queries import conversation_for, primary_contact


class OutreachBlocked(Exception):
    pass


class OutreachService:
    def __init__(self, session: Session, services: Services):
        self.s, self.svc = session, services

    def _already_sent(self, deal: Deal) -> Message | None:
        for m in self.s.scalars(select(Message).join(Conversation, Message.conversation_id == Conversation.id)
                                .where(Conversation.deal_id == deal.id, Message.role == "agent")):
            if m.meta.get("kind") == "initial_outreach" and m.status in ("sent", "dry_run", "manual_task"):
                return m
        return None

    def send_initial(self, deal: Deal) -> Message:
        if (m := self._already_sent(deal)):
            return m
        cfg = self.svc.settings
        company = self.s.get(Company, self.s.get(Prospect, deal.prospect_id).company_id)
        contact = primary_contact(self.s, company.id)
        comp = ComplianceService(self.s, cfg)
        gate = comp.can_email(contact)
        channel = "email"
        if not gate.allowed:
            # fallback: website contact form as a HUMAN task (never auto-submitted)
            if contact and contact.contact_page_url and not comp.is_suppressed(contact):
                channel = "website_contact"
            else:
                D.record_event(self.s, deal, "compliance", action="outreach_blocked", reason=gate.reason)
                raise OutreachBlocked(gate.reason)
        draft = OutreachAgent(self.s, self.svc).draft(deal, channel="email" if channel == "email" else "website contact form")
        body = draft.body.strip() + (comp.email_footer() if channel == "email" else "\n\n(Sent by an AI sales assistant. Reply STOP to opt out.)")
        conv = conversation_for(self.s, deal.id, channel)
        meta = {"kind": "initial_outreach", "subject": draft.subject, "evidence_claim_ids": draft.evidence_claim_ids,
                "to": contact.email if channel == "email" else contact.contact_page_url}
        status = "dry_run"
        if not cfg.outreach_dry_run:
            try:
                if channel == "email":
                    res = self.svc.email.send(to=contact.email, subject=draft.subject, body=body, from_email=cfg.sender_email,
                                              from_name=cfg.sender_name or cfg.sender_company, list_unsubscribe=cfg.sender_email)
                else:
                    res = self.svc.website_contact.submit(url=contact.contact_page_url, subject=draft.subject, body=body)
                status, meta["external_id"], meta["provider_detail"] = res.status, res.external_id, res.detail
            except OutreachSendError as e:
                D.record_event(self.s, deal, "outreach_failed", error=str(e))
                raise
        msg = Message(conversation_id=conv.id, role="agent", content=body, status=status, meta=meta)
        self.s.add(msg)
        self.s.flush()
        D.record_event(self.s, deal, "outreach", channel=channel, status=status, dry_run=cfg.outreach_dry_run, message_id=msg.id)
        if S(deal.state) == S.SOLUTION_READY:
            D.transition(self.s, deal, S.OUTREACH_SENT, reason=f"initial outreach ({channel}, {status})")
        return msg
