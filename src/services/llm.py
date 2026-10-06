"""Provider-agnostic LLM service: retries, JSON validation/repair, usage + cost accounting."""
from __future__ import annotations

import json
import logging
import random
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

log = logging.getLogger("llm")
T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    pass


class LLMTransientError(LLMError):
    """Timeouts, 429, 5xx, connection errors: safe to retry."""

    def __init__(self, msg: str, retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


class LLMPermanentError(LLMError):
    """Auth errors, bad request: do not retry."""


class LLMOutputError(LLMError):
    """Model never produced valid structured output."""


@dataclass
class LLMMessage:
    role: str  # system | user | assistant
    content: str


@dataclass
class LLMRequest:
    messages: list[LLMMessage]
    model: str | None = None
    temperature: float = 0.2
    max_tokens: int | None = None
    json_mode: bool = False
    agent: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)  # never sent to a provider; for mocks/observability


@dataclass
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, other: LLMUsage) -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.cost_usd += other.cost_usd


@dataclass
class LLMResponse:
    content: str
    model: str
    usage: LLMUsage = field(default_factory=LLMUsage)
    latency_ms: int = 0


class LLMProvider(Protocol):
    name: str

    def complete(self, request: LLMRequest) -> LLMResponse: ...

    def stream(self, request: LLMRequest) -> Iterator[str]: ...


@dataclass
class CallInfo:
    """Aggregated accounting for one structured call (including retries/repairs)."""

    model: str = ""
    usage: LLMUsage = field(default_factory=LLMUsage)
    latency_ms: int = 0
    attempts: int = 0
    repairs: int = 0


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)


def extract_json(text: str) -> Any:
    t = _FENCE.sub("", text.strip())
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start != -1 and end > start:
            return json.loads(t[start : end + 1])
        raise


class LLMService:
    def __init__(
        self,
        provider: LLMProvider,
        *,
        max_retries: int = 3,
        base_backoff: float = 1.0,
        max_repairs: int = 1,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.provider = provider
        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self.max_repairs = max_repairs
        self._sleep = sleep

    # ------------------------------------------------------------ raw completion with retry
    def complete(self, request: LLMRequest, info: CallInfo | None = None) -> LLMResponse:
        info = info if info is not None else CallInfo()
        for attempt in range(self.max_retries + 1):
            info.attempts += 1
            try:
                resp = self.provider.complete(request)
            except LLMTransientError as e:
                if attempt >= self.max_retries:
                    log.error("llm transient failure, giving up: %s", e)
                    raise
                delay = e.retry_after if e.retry_after is not None else self.base_backoff * (2**attempt)
                delay = min(delay, 30.0) + random.uniform(0, 0.25)
                log.warning("llm transient failure (%s); retry %d in %.1fs", e, attempt + 1, delay)
                self._sleep(delay)
                continue
            info.model = resp.model
            info.usage.add(resp.usage)
            info.latency_ms += resp.latency_ms
            log.info(
                "llm call agent=%s model=%s tokens_in=%d tokens_out=%d cost_usd=%.6f latency_ms=%d",
                request.agent, resp.model, resp.usage.prompt_tokens, resp.usage.completion_tokens,
                resp.usage.cost_usd, resp.latency_ms,
            )
            return resp
        raise LLMTransientError("unreachable")  # pragma: no cover

    # ------------------------------------------------------------ streaming (voice replies)
    def stream(self, request: LLMRequest) -> Iterator[str]:
        """Retries only while nothing has been yielded (a half-spoken sentence cannot be retried)."""
        for attempt in range(self.max_retries + 1):
            started = False
            try:
                for chunk in self.provider.stream(request):
                    started = True
                    yield chunk
                return
            except LLMTransientError as e:
                if started or attempt >= self.max_retries:
                    raise
                self._sleep(min(e.retry_after or self.base_backoff * (2**attempt), 30.0))

    # ------------------------------------------------------------ structured output
    def generate_structured(
        self,
        output_model: type[T],
        *,
        system: str,
        user: str,
        agent: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[T, CallInfo]:
        info = CallInfo()
        schema_hint = json.dumps(output_model.model_json_schema(), separators=(",", ":"))
        messages = [
            LLMMessage("system", system + "\n\nReturn ONLY one JSON object matching this JSON Schema:\n" + schema_hint),
            LLMMessage("user", user),
        ]
        last_err: Exception | None = None
        for repair in range(self.max_repairs + 1):
            req = LLMRequest(
                messages=list(messages), temperature=temperature, max_tokens=max_tokens,
                json_mode=True, agent=agent, metadata=metadata or {},
            )
            resp = self.complete(req, info)
            try:
                return output_model.model_validate(extract_json(resp.content)), info
            except (json.JSONDecodeError, ValidationError) as e:
                last_err = e
                info.repairs += 1
                log.warning("invalid structured output from %s (repair %d): %s", agent, repair, str(e)[:300])
                messages += [
                    LLMMessage("assistant", resp.content),
                    LLMMessage("user", f"That output was invalid: {str(e)[:600]}\nReturn ONLY the corrected JSON object."),
                ]
        raise LLMOutputError(f"{agent}: no valid structured output: {str(last_err)[:300]}")
