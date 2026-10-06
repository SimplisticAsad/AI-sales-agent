"""Outreach channel adapters. We only use permitted channels: email (sourced address) and a *manual* task for
website contact forms (we never auto-submit forms or scrape-and-blast)."""
from __future__ import annotations

import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Protocol


class OutreachSendError(Exception):
    pass


@dataclass
class SendResult:
    status: str  # sent | manual_task
    external_id: str | None = None
    detail: dict = field(default_factory=dict)


class EmailProvider(Protocol):
    name: str

    def send(self, *, to: str, subject: str, body: str, from_email: str, from_name: str, list_unsubscribe: str) -> SendResult: ...


class SmtpEmailProvider:
    name = "smtp"

    def __init__(self, host: str, port: int, username: str, password: str):
        if not host:
            raise OutreachSendError("SMTP_HOST is not configured")
        self.host, self.port, self.user, self.pw = host, port, username, password

    def send(self, *, to, subject, body, from_email, from_name, list_unsubscribe) -> SendResult:
        m = EmailMessage()
        m["To"], m["Subject"], m["From"] = to, subject, f"{from_name} <{from_email}>"
        m["Message-ID"] = make_msgid(domain=from_email.rsplit("@", 1)[-1])
        m["List-Unsubscribe"] = f"<mailto:{list_unsubscribe}?subject=unsubscribe>"
        m.set_content(body)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=30) as s:
                s.starttls()
                if self.user:
                    s.login(self.user, self.pw)
                s.send_message(m)
        except (smtplib.SMTPException, OSError) as e:
            raise OutreachSendError(f"smtp failure: {type(e).__name__}") from e
        return SendResult("sent", m["Message-ID"])


class RecordingEmailProvider:
    name = "recording"

    def __init__(self, fail: bool = False):
        self.sent: list[dict] = []
        self.fail = fail

    def send(self, *, to, subject, body, from_email, from_name, list_unsubscribe) -> SendResult:
        if self.fail:
            raise OutreachSendError("simulated email failure")
        self.sent.append({"to": to, "subject": subject, "body": body})
        return SendResult("sent", f"rec-{len(self.sent)}")


class WebsiteContactProvider(Protocol):
    name: str

    def submit(self, *, url: str, subject: str, body: str) -> SendResult: ...


class ManualTaskWebsiteContactProvider:
    """Does NOT submit forms. Produces a task for a human to paste the message into the site's contact form."""

    name = "manual_website_contact"

    def submit(self, *, url, subject, body) -> SendResult:
        return SendResult("manual_task", None, {"contact_page_url": url, "instructions": "Paste message into the site's contact form"})
