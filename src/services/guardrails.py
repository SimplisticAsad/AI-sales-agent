"""Post-generation checks on anything an agent says to a prospect."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)(\s?[kK]\b)?")
_FORBIDDEN = {
    "guarantee": r"\bguarantee[sd]?\b|\b100%\b|risk[- ]free|promise (?:you )?(?:results|roi|savings)",
    "refund_promise": r"\brefund(?:s|ed)?\b|money[- ]back",
    "human_claim": r"\bi(?:'m| am) (?:a )?(?:real |actual )?(?:human|person|man|woman)\b|\bnot an? (?:ai|bot|robot)\b",
    "fake_social_proof": r"\b(?:our|other) (?:clients|customers) (?:include|like you|such as|have seen)\b|\btestimonial|case study",
    "legal_claim": r"\blegally (?:binding|required|compliant)\b|\bcontract (?:states|says)\b",
}


def money_amounts_cents(text: str) -> list[int]:
    out = []
    for m in _MONEY.finditer(text):
        v = float(m.group(1).replace(",", ""))
        if m.group(2):
            v *= 1000
        out.append(int(round(v * 100)))
    return out


@dataclass
class GuardResult:
    ok: bool
    violations: list[str] = field(default_factory=list)

    def feedback(self) -> str:
        return "; ".join(self.violations)


_NEGATED = re.compile(
    r"(?:can'?t|cannot|can not|won'?t|will not|do not|don'?t|doesn'?t|unable to|not able to|never|no|not)\s+(?:\w+\s+){0,3}?"
    r"(?:(?:guarantee[sd]?|refunds?|promise)\b\s*)+", re.I)


def check_reply(
    text: str, *, allowed_amounts_cents: list[int] | None = None, allow_money: bool = True, extra_forbidden: dict | None = None
) -> GuardResult:
    v: list[str] = []
    allowed = set(allowed_amounts_cents or [])
    for amt in money_amounts_cents(text):
        if not allow_money:
            v.append("no prices may appear in this message")
            break
        if amt not in allowed:
            v.append(f"unauthorised amount ${amt / 100:,.2f}; you may only state: "
                     f"{sorted(f'${a / 100:,.2f}' for a in allowed) or 'no amounts'}")
    scanned = _NEGATED.sub(" ", text)  # "I can't guarantee results" is honest, not a violation
    for name, pat in {**_FORBIDDEN, **(extra_forbidden or {})}.items():
        if re.search(pat, scanned, re.I):
            v.append(f"forbidden content: {name}")
    return GuardResult(not v, v)


_AI_QUESTION = re.compile(
    r"\b(are you|is this|am i (?:talking|speaking) (?:to|with)) (?:a |an )?(?:real |actual )?(human|person|robot|bot|ai|machine|automated|recording|live)\b"
    r"|\bis (?:this|that) (?:a )?(?:real person|live person|robot|bot|ai|recording)\b|\bwho(?:'s| is) (?:this|calling)\b.*\b(bot|ai|robot)\b",
    re.I,
)


def asks_if_ai(text: str) -> bool:
    return bool(_AI_QUESTION.search(text))
