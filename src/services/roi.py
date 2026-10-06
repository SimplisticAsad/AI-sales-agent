"""Deterministic ROI arithmetic. Inputs are labelled assumptions, never presented as facts."""
from __future__ import annotations

from src.models.schemas import ValueDriver
from src.services.pricing import usd

WEEKS_PER_YEAR = 50
AUTOMATION_CAPTURE_RATE = 0.6  # conservative: assume only 60% of the stated time can really be automated


def annual_value_cents(drivers: list[ValueDriver]) -> int:
    total = 0.0
    for d in drivers:
        if d.hours_saved_per_week and d.hourly_cost_usd:
            total += d.hours_saved_per_week * d.hourly_cost_usd * WEEKS_PER_YEAR * AUTOMATION_CAPTURE_RATE
        elif d.annual_value_usd:
            total += d.annual_value_usd * AUTOMATION_CAPTURE_RATE
    return usd(total)


def calculate_roi(
    *, hours_saved_per_week: float, hourly_cost_usd: float, price_cents: int, capture_rate: float = AUTOMATION_CAPTURE_RATE
) -> dict:
    annual = usd(hours_saved_per_week * hourly_cost_usd * WEEKS_PER_YEAR * capture_rate)
    payback_months = round(price_cents / (annual / 12), 1) if annual > 0 else None
    return {
        "annual_value_cents": annual, "price_cents": price_cents, "payback_months": payback_months,
        "first_year_net_cents": annual - price_cents,
        "assumptions": {
            "hours_saved_per_week": hours_saved_per_week, "hourly_cost_usd": hourly_cost_usd,
            "weeks_per_year": WEEKS_PER_YEAR, "capture_rate": capture_rate,
            "label": "ILLUSTRATIVE ESTIMATE from stated assumptions - not a guarantee",
        },
    }
