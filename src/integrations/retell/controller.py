"""Voice/Call Controller: translates Retell events <-> our ConversationWorkflow. No sales logic lives here.

    Retell (audio, STT, TTS, telephony) -> RetellCallController -> ConversationWorkflow -> SalesAgent -> tools/state
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.database.models import Contact, Conversation, ProcessedWebhook
from src.services import deals as D
from src.services.compliance import ComplianceService
from src.services.container import Services
from src.workflows.conversation import ConversationWorkflow

log = logging.getLogger("retell")


@dataclass
class VoiceReply:
    content: str
    end_call: bool = False


class RetellCallController:
    def __init__(self, session: Session, services: Services):
        self.s, self.svc = session, services

    # ---------------------------------------------------------------- webhook events
    def handle_webhook(self, payload: dict) -> dict:
        event = payload.get("event", "")
        call = payload.get("call", {}) or {}
        call_id = call.get("call_id")
        if not call_id:
            return {"status": "ignored", "reason": "no call_id"}
        try:
            with self.s.begin_nested():
                self.s.add(ProcessedWebhook(provider="retell", event_id=f"{event}:{call_id}"))
                self.s.flush()
        except IntegrityError:
            return {"status": "duplicate"}
        deal_id = (call.get("metadata") or {}).get("deal_id") or self._deal_from_numbers(call)
        if not deal_id:
            log.warning("retell event %s for unknown deal (call %s)", event, call_id)
            return {"status": "unmatched"}
        deal = D.get_deal(self.s, deal_id)
        conv = self._conversation(deal_id, call_id)
        if event == "call_started":
            D.record_event(self.s, deal, "call", status="started", call_id=call_id, direction=call.get("direction"))
        elif event in ("call_ended", "call_analyzed"):
            conv.status = "closed" if event == "call_ended" else conv.status
            analysis = call.get("call_analysis") or {}
            if analysis.get("call_summary"):
                conv.summary = analysis["call_summary"][:2000]
            D.record_event(self.s, deal, "call", status=event, call_id=call_id,
                           disconnection_reason=call.get("disconnection_reason"), duration_ms=call.get("duration_ms"))
        return {"status": "ok", "deal_id": deal_id}

    def _conversation(self, deal_id: str, call_id: str) -> Conversation:
        conv = self.s.scalar(select(Conversation).where(Conversation.external_id == call_id))
        if not conv:
            conv = Conversation(deal_id=deal_id, channel="voice", external_id=call_id)
            self.s.add(conv)
            self.s.flush()
        return conv

    def _deal_from_numbers(self, call: dict) -> str | None:
        """Inbound calls: match the caller to a known contact (only known prospects are served)."""
        num = call.get("from_number") if call.get("direction") == "inbound" else call.get("to_number")
        if not num:
            return None
        c = self.s.scalar(select(Contact).where(Contact.phone == num))
        if not c:
            return None
        from src.database.models import Prospect

        p = self.s.scalar(select(Prospect).where(Prospect.company_id == c.company_id))
        return p.deal.id if p and p.deal else None

    # ---------------------------------------------------------------- live turns (custom-LLM websocket)
    def known_call(self, call_id: str) -> bool:
        return self.s.scalar(select(Conversation.id).where(Conversation.external_id == call_id)) is not None

    def opening_line(self, call_id: str) -> VoiceReply | None:
        conv = self.s.scalar(select(Conversation).where(Conversation.external_id == call_id))
        if not conv:
            return None
        cfg = self.svc.settings
        comp = ComplianceService(self.s, cfg)
        if cfg.call_disclosure_mode == "always":
            return VoiceReply(comp.call_disclosure() + " Is now an okay time for a quick question?")
        return None

    def handle_turn(self, call_id: str, transcript: list[dict]) -> VoiceReply:
        conv = self.s.scalar(select(Conversation).where(Conversation.external_id == call_id))
        if not conv:
            return VoiceReply("I'm sorry, I can't continue this call. Goodbye.", end_call=True)
        last_user = next((t["content"] for t in reversed(transcript) if t.get("role") == "user" and t.get("content", "").strip()), "")
        if not last_user:
            return VoiceReply("Sorry, I didn't catch that. Could you say it again?")
        res = ConversationWorkflow(self.s, self.svc).handle_inbound(conv.deal_id, last_user, "voice", call_id)
        self.s.commit()
        return VoiceReply(res.reply or "Thank you for your time. Goodbye.", end_call=res.ended)
