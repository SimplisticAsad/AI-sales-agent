"""Groq Cloud (OpenAI-compatible API) provider. Only this module knows about Groq."""
from __future__ import annotations

import json
import time
from collections.abc import Iterator

import httpx

from src.services.llm import (
    LLMPermanentError, LLMRequest, LLMResponse, LLMTransientError, LLMUsage,
)


class GroqProvider:
    name = "groq"

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = "https://api.groq.com/openai/v1",
        timeout: float = 45.0,
        input_cost_per_mtok: float = 0.0,
        output_cost_per_mtok: float = 0.0,
        client: httpx.Client | None = None,
    ):
        if not api_key:
            raise LLMPermanentError("GROQ_API_KEY is not configured")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._in_cost, self._out_cost = input_cost_per_mtok, output_cost_per_mtok
        self._client = client or httpx.Client(timeout=timeout)
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def _payload(self, req: LLMRequest, stream: bool) -> dict:
        p: dict = {
            "model": req.model or self.model,
            "messages": [{"role": m.role, "content": m.content} for m in req.messages],
            "temperature": req.temperature,
            "stream": stream,
        }
        if req.max_tokens:
            p["max_tokens"] = req.max_tokens
        if req.json_mode and not stream:
            p["response_format"] = {"type": "json_object"}
        return p

    @staticmethod
    def _raise_for(resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        detail = resp.text[:300]
        if resp.status_code in (408, 409, 425, 429) or resp.status_code >= 500:
            ra = resp.headers.get("retry-after")
            raise LLMTransientError(f"groq {resp.status_code}: {detail}", float(ra) if ra and ra.replace(".", "").isdigit() else None)
        raise LLMPermanentError(f"groq {resp.status_code}: {detail}")

    def complete(self, request: LLMRequest) -> LLMResponse:
        t0 = time.monotonic()
        try:
            r = self._client.post(f"{self.base_url}/chat/completions", headers=self._headers, json=self._payload(request, False))
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise LLMTransientError(f"groq network error: {type(e).__name__}") from e
        self._raise_for(r)
        data = r.json()
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise LLMTransientError("groq returned malformed response") from e
        u = data.get("usage") or {}
        pin, pout = int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0))
        cost = (pin * self._in_cost + pout * self._out_cost) / 1_000_000
        return LLMResponse(
            content=content, model=data.get("model", self.model),
            usage=LLMUsage(pin, pout, cost), latency_ms=int((time.monotonic() - t0) * 1000),
        )

    def stream(self, request: LLMRequest) -> Iterator[str]:
        try:
            with self._client.stream(
                "POST", f"{self.base_url}/chat/completions", headers=self._headers, json=self._payload(request, True)
            ) as r:
                if r.status_code >= 400:
                    r.read()
                    self._raise_for(r)
                for line in r.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        return
                    try:
                        delta = json.loads(chunk)["choices"][0]["delta"].get("content")
                    except (KeyError, IndexError, json.JSONDecodeError):
                        continue
                    if delta:
                        yield delta
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise LLMTransientError(f"groq stream error: {type(e).__name__}") from e
