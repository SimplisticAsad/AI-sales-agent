"""Retell AI integration layer: REST client + webhook signature verification. No business logic here."""
from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

RETELL_API = "https://api.retellai.com"


class RetellError(Exception):
    pass


@dataclass
class OutboundCall:
    call_id: str
    status: str = "registered"


class RetellClientProtocol(Protocol):
    def create_phone_call(self, *, to_number: str, from_number: str, agent_id: str | None, metadata: dict,
                          dynamic_variables: dict[str, str]) -> OutboundCall: ...

    def verify_signature(self, body: bytes, signature: str) -> bool: ...


class RetellClient:
    def __init__(self, api_key: str, tolerance_s: int = 300, client: httpx.Client | None = None):
        if not api_key:
            raise RetellError("RETELL_API_KEY is not configured")
        self._key, self.tolerance = api_key, tolerance_s
        self._c = client or httpx.Client(timeout=30)

    def create_phone_call(self, *, to_number, from_number, agent_id, metadata, dynamic_variables) -> OutboundCall:
        payload = {"from_number": from_number, "to_number": to_number, "metadata": metadata,
                   "retell_llm_dynamic_variables": dynamic_variables}
        if agent_id:
            payload["override_agent_id"] = agent_id
        try:
            r = self._c.post(f"{RETELL_API}/v2/create-phone-call", json=payload,
                             headers={"Authorization": f"Bearer {self._key}"})
        except httpx.HTTPError as e:
            raise RetellError(f"retell unreachable: {type(e).__name__}") from e
        if r.status_code >= 400:
            raise RetellError(f"retell error {r.status_code}: {r.text[:200]}")
        j = r.json()
        return OutboundCall(j["call_id"], j.get("call_status", "registered"))

    def verify_signature(self, body: bytes, signature: str) -> bool:
        """Retell signs webhooks as `v=<ms timestamp>,d=<hex HMAC-SHA256(api_key, body + timestamp)>`.

        NOTE: confirm this scheme against Retell's current docs when going live; the check fails closed.
        """
        try:
            parts = dict(p.split("=", 1) for p in signature.split(","))
            ts, digest = parts["v"], parts["d"]
        except (ValueError, KeyError):
            return False
        expected = hmac.new(self._key.encode(), body + ts.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, digest):
            return False
        try:
            age = abs(time.time() * 1000 - int(ts)) / 1000
        except ValueError:
            return False
        return age <= self.tolerance


class FakeRetellClient:
    """Test double; signs/verifies with a shared key using the same scheme."""

    def __init__(self, api_key: str = "test-retell-key", fail: bool = False):
        self._key, self.fail = api_key, fail
        self.calls: list[dict] = []

    def create_phone_call(self, *, to_number, from_number, agent_id, metadata, dynamic_variables) -> OutboundCall:
        if self.fail:
            raise RetellError("simulated retell failure")
        self.calls.append({"to": to_number, "from": from_number, "metadata": metadata, "vars": dynamic_variables})
        return OutboundCall(f"call_fake_{len(self.calls)}")

    def sign(self, body: bytes) -> str:
        ts = str(int(time.time() * 1000))
        return f"v={ts},d=" + hmac.new(self._key.encode(), body + ts.encode(), hashlib.sha256).hexdigest()

    def verify_signature(self, body: bytes, signature: str) -> bool:
        return RetellClient.verify_signature(self, body, signature)  # type: ignore[arg-type]

    tolerance = 300
