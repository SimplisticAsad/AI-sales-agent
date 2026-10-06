"""Delivery handoff targets: where the project spec goes after payment (your existing dev orchestrator)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

import httpx


class DeliveryError(Exception):
    pass


class DeliveryTarget(Protocol):
    name: str

    def submit(self, spec: dict, idempotency_key: str) -> str:
        """Return an external reference for the created project."""
        ...


class FileDeliveryTarget:
    """Writes the spec to an outbox directory (mock orchestrator / file-based pickup)."""

    name = "file"

    def __init__(self, outbox: str | Path):
        self.outbox = Path(outbox)

    def submit(self, spec: dict, idempotency_key: str) -> str:
        self.outbox.mkdir(parents=True, exist_ok=True)
        p = self.outbox / f"{idempotency_key}.json"
        p.write_text(json.dumps(spec, indent=2, default=str))
        return str(p)


class HttpDeliveryTarget:
    name = "http"

    def __init__(self, url: str, token: str | None = None, client: httpx.Client | None = None):
        if not url:
            raise DeliveryError("ORCHESTRATOR_URL is not configured")
        self.url, self.token = url, token
        self._c = client or httpx.Client(timeout=30)

    def submit(self, spec: dict, idempotency_key: str) -> str:
        h = {"Idempotency-Key": idempotency_key}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        try:
            r = self._c.post(self.url, json=spec, headers=h)
        except httpx.HTTPError as e:
            raise DeliveryError(f"orchestrator unreachable: {type(e).__name__}") from e
        if r.status_code >= 400:
            raise DeliveryError(f"orchestrator error {r.status_code}")
        try:
            return str(r.json().get("id", idempotency_key))
        except ValueError:
            return idempotency_key


class RecordingDeliveryTarget:
    name = "recording"

    def __init__(self, fail: bool = False):
        self.specs: list[dict] = []
        self.fail = fail

    def submit(self, spec: dict, idempotency_key: str) -> str:
        if self.fail:
            raise DeliveryError("simulated orchestrator failure")
        self.specs.append(spec)
        return f"proj-{idempotency_key}"
