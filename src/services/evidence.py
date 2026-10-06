"""Deterministic verification of research claims: a FACT must be traceable to fetched source text."""
from __future__ import annotations

import re

from src.models.enums import ClaimLabel
from src.models.schemas import Claim, SourceDoc

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[‘’“”]")


def _norm(t: str) -> str:
    t = _PUNCT.sub("'", t).replace("–", "-").replace("—", "-")
    return _WS.sub(" ", t).strip().lower()


def verify_claims(claims: list[Claim], docs: list[SourceDoc]) -> list[Claim]:
    by_url = {d.url: _norm(d.text) for d in docs}
    seen: set[str] = set()
    out: list[Claim] = []
    for i, c in enumerate(claims, 1):
        c = c.model_copy()
        if not c.id or c.id in seen:
            c.id = f"c{i}"
            while c.id in seen:
                c.id += "x"
        seen.add(c.id)
        if c.label == ClaimLabel.FACT:
            quote = _norm(c.quote or "")
            if not (c.source_url in by_url and len(quote) >= 8 and quote in by_url[c.source_url]):
                c.label = ClaimLabel.INFERENCE
                c.note = ((c.note + " | ") if c.note else "") + "downgraded from FACT: quote not verifiable in source"
        elif c.label == ClaimLabel.UNKNOWN:
            c.source_url = c.quote = None
        out.append(c)
    return out
