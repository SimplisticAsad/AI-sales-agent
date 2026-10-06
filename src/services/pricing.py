"""Deterministic pricing. LLMs may explain a price; they can never set or change one.

All amounts are integer cents.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from src.config.settings import Settings

COMPLEXITY_MULT = {"low": 1.0, "medium": 1.15, "high": 1.35}
VERIFIED_INTEGRATION_FEE = 250_00
UNVERIFIED_INTEGRATION_FEE = 600_00  # custom/unknown integrations carry discovery risk
VALUE_CAP_RATIO = 0.5  # price never exceeds 50% of the estimated first-year value (keeps ROI demonstrable)


def usd(x: float) -> int:
    return int(round(x * 100))


def fmt(cents: int) -> str:
    return f"${cents / 100:,.0f}" if cents % 100 == 0 else f"${cents / 100:,.2f}"


@dataclass(frozen=True)
class PricingPolicy:
    target_cents: int
    minimum_cents: int
    max_discount_percent: float
    currency: str = "usd"
    payment_terms: str = "one_time_upfront"

    @classmethod
    def build(cls, target_cents: int, settings: Settings) -> PricingPolicy:
        abs_min = usd(settings.default_min_price)
        abs_max = usd(settings.default_max_price)
        target = max(abs_min, min(target_cents, abs_max))
        disc = max(0.0, min(settings.default_max_discount_percent, 100.0))
        floor = max(abs_min, math.ceil(target * (100 - disc) / 100))
        return cls(target, floor, disc, settings.payment_currency)

    def to_dict(self) -> dict:
        return asdict(self)


def size_multiplier(employees: int | None) -> float:
    if employees is None:
        return 1.0
    if employees <= 10:
        return 0.9
    if employees <= 50:
        return 1.0
    if employees <= 100:
        return 1.15
    return 1.3


def round_to(cents: int, step_cents: int = 50_00) -> int:
    return int(round(cents / step_cents)) * step_cents


def compute_target_price(
    *, effort_hours: int, complexity: str, verified_integrations: int, unverified_integrations: int,
    employee_estimate: int | None, annual_value_cents: int | None, settings: Settings,
) -> tuple[PricingPolicy, dict]:
    """Return (policy, breakdown). The breakdown is stored for 'why did it propose $X?' audits."""
    rate = usd(settings.pricing_hourly_rate)
    labor = effort_hours * rate
    integ = verified_integrations * VERIFIED_INTEGRATION_FEE + unverified_integrations * UNVERIFIED_INTEGRATION_FEE
    cm = COMPLEXITY_MULT.get(complexity, 1.15)
    sm = size_multiplier(employee_estimate)
    raw = int((labor + integ) * cm * sm)
    notes: list[str] = []
    if effort_hours <= 0:
        raw = usd(settings.default_target_price)
        notes.append("no effort estimate; used DEFAULT_TARGET_PRICE")
    value_cap = None
    if annual_value_cents and annual_value_cents > 0:
        value_cap = int(annual_value_cents * VALUE_CAP_RATIO)
        if raw > value_cap:
            notes.append("capped to 50% of estimated annual value")
            raw = value_cap
    rounded = round_to(raw)
    policy = PricingPolicy.build(rounded, settings)
    if policy.target_cents != rounded:
        notes.append(f"clamped into [{fmt(usd(settings.default_min_price))}, {fmt(usd(settings.default_max_price))}]")
    breakdown = {
        "hourly_rate_cents": rate, "effort_hours": effort_hours, "labor_cents": labor,
        "integration_fees_cents": integ, "verified_integrations": verified_integrations,
        "unverified_integrations": unverified_integrations, "complexity": complexity, "complexity_multiplier": cm,
        "employee_estimate": employee_estimate, "size_multiplier": sm, "raw_cents": raw,
        "value_cap_cents": value_cap, "target_cents": policy.target_cents, "floor_cents": policy.minimum_cents,
        "max_discount_percent": policy.max_discount_percent, "notes": notes,
    }
    return policy, breakdown
