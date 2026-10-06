from __future__ import annotations

from contextlib import contextmanager
from typing import Any, TypeVar

from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.services.container import Services
from src.services.observability import Trace, trace_run

T = TypeVar("T", bound=BaseModel)


class BaseAgent:
    """One agent = one prompt + one output schema + deterministic post-processing. No shared mega-prompt."""

    name: str = "agent"
    prompt_name: str = ""

    def __init__(self, session: Session, services: Services):
        self.s = session
        self.svc = services

    @contextmanager
    def trace(self, *, prospect_id: str | None = None, deal_id: str | None = None, input_summary: str = ""):
        with trace_run(
            self.s, agent=self.name, prospect_id=prospect_id, deal_id=deal_id, input_summary=input_summary,
            prompt_name=self.prompt_name, prompt_version=self.svc.prompts.version(self.prompt_name),
        ) as tr:
            yield tr

    def ask(
        self, tr: Trace, output_model: type[T], variables: dict[str, Any], user: str, *, temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> T:
        prompt = self.svc.prompts.load(self.prompt_name, **variables)
        out, info = self.svc.llm.generate_structured(
            output_model, system=prompt.text, user=user, agent=self.name, temperature=temperature,
            max_tokens=max_tokens, metadata={"variables": variables},
        )
        tr.info.usage.add(info.usage)
        tr.info.model = info.model or tr.info.model
        return out
