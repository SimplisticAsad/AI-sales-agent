"""Deterministic negotiation policy. The LLM supplies wording; this module decides every number and term.

Concession ladder: round 1 may discount up to 40% of the allowed maximum, round 2 up to 70%, round 3+ up to 100%
(i.e. the floor). Nothing below the floor is ever offered. Anything outside policy -> escalate to a human.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Literal

from src.models.schemas import ExtractedRequest
from src.services.pricing import PricingPolicy, fmt, usd

LADDER = (0.4, 0.7, 1.0)

Action = Literal["accept", "counter", "hold", "escalate", "quote"]

ALLOWED_PAYMENT_TERMS = {"one_time_upfront", "upfront", "full upfront", "paid in full", "one-time"}
_RISK_PATTERNS = {
    "refund": r"\brefund|money[- ]back|chargeback|cancel(?:lation)? (?:fee|policy)|cooling[- ]off",
    "guarantee": r"\bwarrant(?:y|ies)\b|\bSLA\b|uptime commitment|performance bond|money[- ]back guarantee",
    "legal": r"\b(indemnif|liabilit|non-?compete|exclusiv|governing law|arbitration|NDA\b|confidential(?:ity)? agreement|"
             r"ip ownership|source code escrow|intellectual property|lawsuit|attorney|lawyer|contract redline)",
    "payment_terms": r"\b(net[- ]?\d+|installments?|monthly (?:payments?|fees?|billing|plan)|pay monthly|per month|subscription|"
                     r"deposit|milestone payments?|pay later|pay after|on delivery|upon completion|50/50|payment plan|retainer)\b",
}


def risk_scan(text: str) -> set[str]:
    """Regex backstop in case the LLM extraction misses a risky request."""
    return {k for k, p in _RISK_PATTERNS.items() if re.search(p, text, re.I)}


@dataclass
class NegotiationPolicy:
    target_cents: int
    minimum_cents: int
    max_discount_percent: float
    payment_terms: str = "one_time_upfront"
    allowed_scope: list[str] = field(default_factory=list)
    escalation_rules: list[str] = field(default_factory=lambda: [
        "price below minimum after the floor was already offered",
        "payment terms other than one-time upfront",
        "scope outside the approved solution",
        "contract/legal terms, guarantees or refunds",
    ])

    @classmethod
    def from_pricing(cls, p: PricingPolicy, allowed_scope: list[str]) -> NegotiationPolicy:
        return cls(p.target_cents, p.minimum_cents, p.max_discount_percent, p.payment_terms, allowed_scope)

    def ladder_price(self, round_no: int) -> int:
        frac = LADDER[min(round_no, len(LADDER)) - 1]
        discount = self.target_cents * (self.max_discount_percent / 100) * frac
        return max(self.minimum_cents, int(round(self.target_cents - discount)))


@dataclass
class NegotiationDecision:
    action: Action
    round: int
    price_cents: int | None  # the ONLY price the agent may state this turn
    reason_codes: list[str]
    explanation: str  # internal, for audit
    escalate: bool = False
    escalation_code: str | None = None
    allowed_amounts_cents: list[int] = field(default_factory=list)
    talking_points: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class NegotiationState:
    round: int = 0
    last_offered_cents: int | None = None  # last price we stated
    floor_offered: bool = False
    below_floor_rounds: int = 0


class NegotiationEngine:
    def __init__(self, policy: NegotiationPolicy):
        self.policy = policy

    def quote(self) -> NegotiationDecision:
        p = self.policy
        return NegotiationDecision("quote", 0, p.target_cents, ["initial_quote"], "Stated target price.",
                                   allowed_amounts_cents=[p.target_cents])

    def evaluate(self, req: ExtractedRequest, st: NegotiationState, raw_text: str = "") -> NegotiationDecision:
        p = self.policy
        rnd = st.round + 1
        flags = risk_scan(raw_text)
        codes: list[str] = []

        # ---- hard policy escalations (never negotiable by the agent) ----
        if req.asks_for_refund or "refund" in flags:
            codes.append("REFUND_REQUEST")
        if "guarantee" in flags:  # contractual warranty/SLA language; plain "can you guarantee results?" is answered honestly
            codes.append("GUARANTEE_REQUEST")
        if req.asks_legal_question or req.contract_term_requests or "legal" in flags:
            codes.append("UNUSUAL_CONTRACT_TERMS")
        pt = (req.payment_terms_request or "").strip().lower()
        if (pt and pt not in ALLOWED_PAYMENT_TERMS) or "payment_terms" in flags:
            codes.append("PAYMENT_TERMS_OUTSIDE_POLICY")
        if req.scope_requests or req.asks_unsupported_integration:
            codes.append("SCOPE_OUTSIDE_APPROVED")
        if codes:
            return NegotiationDecision(
                "escalate", rnd, st.last_offered_cents, codes, f"Out-of-policy request: {', '.join(codes)}",
                escalate=True, escalation_code=codes[0],
                allowed_amounts_cents=[st.last_offered_cents] if st.last_offered_cents else [p.target_cents],
                talking_points=["Tell the prospect you must confirm this with a teammate; do not commit to anything."],
            )

        ladder = p.ladder_price(rnd)
        current = st.last_offered_cents or p.target_cents
        offered = usd(req.offered_price_usd) if req.offered_price_usd else None
        if offered is None and req.requested_discount_percent:
            offered = int(p.target_cents * (1 - req.requested_discount_percent / 100))

        if offered is None:  # asking about price with no counter
            return NegotiationDecision("hold", rnd, current, ["no_counter"], "Restated current price.",
                                       allowed_amounts_cents=[current])

        if offered >= current:
            return NegotiationDecision("accept", rnd, current, ["offer_meets_quote"],
                                       "Offer at/above our current price; accept at our quoted price.",
                                       allowed_amounts_cents=[current])
        if offered >= ladder:
            return NegotiationDecision("accept", rnd, offered, ["offer_within_ladder"],
                                       f"Offer {fmt(offered)} is at/above this round's allowed price {fmt(ladder)}.",
                                       allowed_amounts_cents=[offered])
        # offer is below what we may concede this round
        if offered < p.minimum_cents and st.floor_offered and st.below_floor_rounds >= 1:
            return NegotiationDecision(
                "escalate", rnd, st.last_offered_cents, ["PRICE_BELOW_MINIMUM"],
                f"Prospect holds at {fmt(offered)} below minimum {fmt(p.minimum_cents)} after floor was offered.",
                escalate=True, escalation_code="PRICE_BELOW_MINIMUM",
                allowed_amounts_cents=[st.last_offered_cents or p.minimum_cents],
                talking_points=["Say you cannot go lower yourself and will ask a teammate; promise nothing."],
            )
        counter = max(ladder, p.minimum_cents)
        counter = min(counter, current)  # never counter higher than what is already on the table
        return NegotiationDecision(
            "counter" if counter < current else "hold", rnd, counter,
            ["below_ladder" if offered >= p.minimum_cents else "below_minimum"],
            f"Offer {fmt(offered)} < ladder {fmt(ladder)}; countered {fmt(counter)}.",
            allowed_amounts_cents=[counter],
            talking_points=["Anchor on value/ROI; trade scope or timing rather than price where possible."],
        )

    @staticmethod
    def advance(st: NegotiationState, req: ExtractedRequest, d: NegotiationDecision, policy: NegotiationPolicy) -> None:
        """Update negotiation memory after a decision."""
        st.round = d.round
        if d.price_cents and d.action in ("counter", "hold", "accept", "quote"):
            st.last_offered_cents = d.price_cents
        if d.price_cents == policy.minimum_cents:
            st.floor_offered = True
        if req.offered_price_usd and usd(req.offered_price_usd) < policy.minimum_cents:
            st.below_floor_rounds += 1
