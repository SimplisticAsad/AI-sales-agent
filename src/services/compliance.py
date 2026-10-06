"""Compliance gate: opt-outs, suppression, call consent/quiet hours, AI disclosure."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config.settings import Settings
from src.database.models import Company, Contact, Suppression

_OPT_OUT = re.compile(
    r"\b(unsubscribe|opt[- ]?out|remove me|take me off|do not (?:contact|call|email|text)|don'?t (?:contact|call|email|text) (?:me|us)|"
    r"stop (?:emailing|calling|contacting|texting|messaging)|stop (?:sending|bothering)|leave me alone|"
    r"never (?:contact|email|call) (?:me|us) again|remove (?:my|our) (?:email|number|name))\b",
    re.I,
)


def detect_opt_out(text: str) -> bool:
    t = text.strip()
    return bool(_OPT_OUT.search(t)) or t.lower().strip(" .!") in {"stop", "unsubscribe", "remove"}


@dataclass
class Gate:
    allowed: bool
    reason: str = ""


def _domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower()


class ComplianceService:
    def __init__(self, session: Session, settings: Settings):
        self.s, self.cfg = session, settings

    def add_suppression(self, contact: Contact | None, reason: str = "opt_out") -> None:
        if not contact:
            return
        for kind, val in (("email", contact.email and contact.email.lower()), ("phone", contact.phone)):
            if val and not self.s.scalar(select(Suppression).where(Suppression.kind == kind, Suppression.value == val)):
                self.s.add(Suppression(kind=kind, value=val, reason=reason))
        self.s.flush()

    def is_suppressed(self, contact: Contact) -> str | None:
        checks = []
        if contact.email:
            checks += [("email", contact.email.lower()), ("domain", _domain(contact.email))]
        if contact.phone:
            checks.append(("phone", contact.phone))
        for kind, val in checks:
            if self.s.scalar(select(Suppression).where(Suppression.kind == kind, Suppression.value == val)):
                return f"{kind} is on the suppression list"
        return None

    def can_email(self, contact: Contact | None) -> Gate:
        if not contact or not contact.email:
            return Gate(False, "no email address")
        if not contact.source:
            return Gate(False, "contact has no recorded source (will not email unsourced addresses)")
        if (r := self.is_suppressed(contact)):
            return Gate(False, r)
        if not (self.cfg.sender_postal_address and self.cfg.sender_email and self.cfg.sender_company):
            return Gate(False, "SENDER_COMPANY / SENDER_EMAIL / SENDER_POSTAL_ADDRESS must be configured (CAN-SPAM)")
        return Gate(True)

    def can_call(self, contact: Contact | None, company: Company | None, now: datetime | None = None) -> Gate:
        if not contact or not contact.phone:
            return Gate(False, "no phone number")
        if not contact.source:
            return Gate(False, "phone number has no recorded source")
        if (r := self.is_suppressed(contact)):
            return Gate(False, r)
        if self.cfg.call_require_consent and not contact.call_consent:
            return Gate(False, "no recorded consent for an AI-voice call (CALL_REQUIRE_CONSENT=true)")
        tzname = (company.timezone if company and company.timezone else self.cfg.default_timezone)
        local = (now or datetime.now(ZoneInfo("UTC"))).astimezone(ZoneInfo(tzname))
        if local.weekday() >= 5 or not (self.cfg.call_window_start_hour <= local.hour < self.cfg.call_window_end_hour):
            return Gate(False, f"outside permitted calling window in {tzname} (local {local:%a %H:%M})")
        return Gate(True)

    def email_footer(self) -> str:
        c = self.cfg
        return (
            f"\n\n--\n{c.sender_name or c.sender_company}, {c.sender_company}\n{c.sender_postal_address}\n"
            "This is an automated outreach message sent by an AI sales assistant working for the company above.\n"
            "Reply STOP or \"unsubscribe\" and we will not contact you again."
        )

    def call_disclosure(self) -> str:
        c = self.cfg
        return f"Hi, this is an AI assistant calling on behalf of {c.sender_company or 'our company'}."
