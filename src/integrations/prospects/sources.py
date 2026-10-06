"""Prospect sources. We deliberately ship no scraper: plug in a licensed data provider or import your own list."""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Protocol

from src.models.schemas import MarketProfileOut, RawCandidate


class ProspectSource(Protocol):
    name: str

    def search(self, profile: MarketProfileOut, limit: int = 25) -> list[RawCandidate]: ...


class CsvProspectSource:
    """Columns: company_name,website,location,employee_estimate,text,email,phone,contact_page_url,contact_name,contact_role,source"""

    name = "csv"

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def search(self, profile: MarketProfileOut, limit: int = 25) -> list[RawCandidate]:
        out: list[RawCandidate] = []
        with self.path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                row = {k: (v or "").strip() or None for k, v in row.items() if k}
                if not row.get("company_name"):
                    continue
                if row.get("employee_estimate"):
                    row["employee_estimate"] = int(row["employee_estimate"])
                row["text"] = row.get("text") or ""
                row["source"] = row.get("source") or f"csv:{self.path.name}"
                out.append(RawCandidate(**row))
                if len(out) >= limit:
                    break
        return out


class StaticProspectSource:
    name = "static"

    def __init__(self, candidates: list[RawCandidate]):
        self.candidates = candidates

    def search(self, profile: MarketProfileOut, limit: int = 25) -> list[RawCandidate]:
        return self.candidates[:limit]
