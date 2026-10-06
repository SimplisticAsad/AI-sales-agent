from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).parent


@dataclass(frozen=True)
class CapabilityCatalog:
    verified_integrations: tuple[str, ...]
    approved_service_scope: tuple[str, ...]

    def is_verified(self, name: str) -> bool:
        n = name.strip().lower()
        return any(n == v.lower() or v.lower() in n for v in self.verified_integrations)


@lru_cache
def get_catalog() -> CapabilityCatalog:
    d = json.loads((_DIR / "capabilities.json").read_text())
    return CapabilityCatalog(tuple(d["verified_integrations"]), tuple(d["approved_service_scope"]))


@lru_cache
def get_proposal_terms() -> dict:
    return json.loads((_DIR / "proposal_terms.json").read_text())
