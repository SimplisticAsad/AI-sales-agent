"""Deterministic qualification scoring against the ICP. LLMs never decide qualification."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.models.schemas import MarketProfileOut

_US = re.compile(
    r"\b(usa|u\.s\.a?\.?|united states|alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|"
    r"hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|mississippi|"
    r"missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|new york|north carolina|north dakota|ohio|oklahoma|"
    r"oregon|pennsylvania|rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|virginia|washington|west virginia|"
    r"wisconsin|wyoming)\b|,\s*(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|"
    r"NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)\b",
    re.I,
)

QUALIFY_MIN_SCORE = 55.0


@dataclass
class ScoreResult:
    score: float
    breakdown: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    has_contact_path: bool = False


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z]{3,}", s.lower().replace("_", " "))}


def score_prospect(
    profile: MarketProfileOut, *, industry: str | None, location: str | None, employees: int | None,
    website: str | None, has_email: bool, has_phone: bool, has_contact_page: bool, decision_maker: str | None,
) -> ScoreResult:
    b: dict[str, float] = {}
    why: list[str] = []
    # industry (30)
    if industry:
        overlap = _tokens(industry) & _tokens(profile.industry)
        b["industry"] = 30.0 if overlap else 5.0
        why.append(f"industry {'matches' if overlap else 'does not match'} ICP ({industry!r})")
    else:
        b["industry"] = 0.0
        why.append("industry unknown")
    # size (25)
    if employees is None:
        b["size"] = 8.0
        why.append("employee count unknown (partial credit)")
    elif profile.employee_min <= employees <= profile.employee_max:
        b["size"] = 25.0
        why.append(f"{employees} employees within {profile.employee_min}-{profile.employee_max}")
    else:
        b["size"] = 0.0
        why.append(f"{employees} employees outside {profile.employee_min}-{profile.employee_max}")
    # geography (15)
    if location and "united states" in profile.geography.lower():
        b["geography"] = 15.0 if _US.search(location) else 0.0
    elif location:
        b["geography"] = 8.0
    else:
        b["geography"] = 4.0
    why.append(f"geography score {b['geography']:.0f} for {location!r}")
    b["website"] = 10.0 if website else 0.0
    path = has_email or has_phone or has_contact_page
    b["contact_path"] = 10.0 if path else 0.0
    if not path:
        why.append("no sourced contact path")
    b["decision_maker"] = 10.0 if decision_maker else 0.0
    return ScoreResult(round(sum(b.values()), 1), b, why, path)
