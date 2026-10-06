"""AgentRun tracing + log redaction. Answers: why did it contact / price / reject / escalate?"""
from __future__ import annotations

import re
import time
from contextlib import contextmanager
from typing import Any

from sqlalchemy.orm import Session

from src.database.base import utcnow
from src.database.models import AgentRun
from src.services.llm import CallInfo

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
_SECRET = re.compile(r"(?i)\b(sk|gsk|key|rk|whsec|pk)[_-][A-Za-z0-9_\-]{12,}")


def redact(text: str, limit: int = 600) -> str:
    t = _SECRET.sub("[secret]", text)
    t = _EMAIL.sub("[email]", t)
    t = _PHONE.sub("[phone]", t)
    return t if len(t) <= limit else t[:limit] + "…"


class Trace:
    def __init__(self, run: AgentRun):
        self.run = run
        self.info = CallInfo()
        self._t0 = time.monotonic()

    def tool_call(self, name: str, arguments: dict, result: Any) -> None:
        self.run.tool_calls = [
            *self.run.tool_calls,
            {"name": name, "arguments": redact(str(arguments), 400), "result": redact(str(result), 400)},
        ]

    def set_output(self, output: Any, decision_summary: str | None = None) -> None:
        self.run.output = output
        if decision_summary is not None:
            self.run.decision_summary = decision_summary[:1000]


@contextmanager
def trace_run(
    session: Session, *, agent: str, prospect_id: str | None = None, deal_id: str | None = None,
    input_summary: str = "", prompt_name: str | None = None, prompt_version: str | None = None,
):
    run = AgentRun(
        agent=agent, prospect_id=prospect_id, deal_id=deal_id, input_summary=redact(input_summary),
        prompt_name=prompt_name, prompt_version=prompt_version,
    )
    session.add(run)
    session.commit()  # visible even if the process dies mid-run
    tr = Trace(run)
    try:
        yield tr
        run.status = "success"
    except Exception as e:
        run.status = "failed"
        run.error = redact(f"{type(e).__name__}: {e}", 800)
        raise
    finally:
        run.model = tr.info.model or run.model
        run.tokens_in, run.tokens_out = tr.info.usage.prompt_tokens, tr.info.usage.completion_tokens
        run.cost_usd = tr.info.usage.cost_usd
        run.latency_ms = int((time.monotonic() - tr._t0) * 1000)
        if run.started_at is None:  # pragma: no cover
            run.started_at = utcnow()
        session.commit()
