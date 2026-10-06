"""Deterministic business rules: pricing, negotiation, guardrails, state machine, prompts."""
from __future__ import annotations

import pytest

from src.models.enums import DealState as S
from src.models.schemas import ExtractedRequest
from src.services.guardrails import asks_if_ai, check_reply, money_amounts_cents
from src.services.negotiation import NegotiationEngine, NegotiationPolicy, NegotiationState, risk_scan
from src.services.pricing import PricingPolicy, compute_target_price, usd
from src.services.prompts import PromptError, PromptManager, PROMPTS_DIR
from src.services.state_machine import PIPELINE_ORDER, can_transition
from tests.conftest import make_settings

CFG = make_settings()


# ----------------------------------------------------------------------------- pricing
def test_floor_matches_spec_example():
    cfg = make_settings(default_min_price=1000, default_max_discount_percent=15)
    p = PricingPolicy.build(usd(3500), cfg)
    assert p.target_cents == 350_000 and p.minimum_cents == 297_500  # $3,500 target, 15% -> $2,975


def test_absolute_minimum_cannot_be_undercut_by_discount():
    p = PricingPolicy.build(usd(1100), CFG)  # 15% off 1100 = 935 < 1000 -> floor stays 1000
    assert p.minimum_cents == 100_000


def test_target_is_clamped_into_configured_range():
    assert PricingPolicy.build(usd(10), CFG).target_cents == 100_000
    assert PricingPolicy.build(usd(10**6), CFG).target_cents == usd(CFG.default_max_price)


def test_compute_price_uses_rules_and_value_cap():
    pol, b = compute_target_price(effort_hours=40, complexity="medium", verified_integrations=2, unverified_integrations=1,
                                  employee_estimate=35, annual_value_cents=None, settings=CFG)
    assert b["labor_cents"] == 400_000 and b["integration_fees_cents"] == 2 * 25_000 + 60_000
    assert pol.minimum_cents <= pol.target_cents
    capped, b2 = compute_target_price(effort_hours=40, complexity="medium", verified_integrations=2, unverified_integrations=1,
                                      employee_estimate=35, annual_value_cents=usd(4000), settings=CFG)
    assert capped.target_cents <= max(usd(2000), usd(CFG.default_min_price))
    assert any("capped" in n for n in b2["notes"])


# ----------------------------------------------------------------------------- negotiation
def engine(target=350_000, disc=15.0):
    p = PricingPolicy.build(target, make_settings(default_max_discount_percent=disc))
    return NegotiationEngine(NegotiationPolicy.from_pricing(p, ["x"])), p


def offer(usd_price: float, **kw) -> ExtractedRequest:
    return ExtractedRequest(offered_price_usd=usd_price, **kw)


def test_never_counters_below_floor_for_any_offer():
    eng, pol = engine()
    for amount in (1, 500, 1000, 2000, 2974, 2975, 3000, 3300, 3499, 3500, 9000):
        st = NegotiationState()
        for _ in range(6):
            d = eng.evaluate(offer(amount), st, "")
            if d.price_cents is not None:
                assert d.price_cents >= pol.minimum_cents, (amount, d)
            NegotiationEngine.advance(st, offer(amount), d, eng.policy)


def test_concession_ladder_and_acceptance():
    eng, pol = engine()
    st = NegotiationState()
    d1 = eng.evaluate(offer(2000), st, "")
    assert d1.action == "counter" and d1.price_cents == eng.policy.ladder_price(1) and d1.price_cents > pol.minimum_cents
    NegotiationEngine.advance(st, offer(2000), d1, eng.policy)
    d2 = eng.evaluate(offer(2000), st, "")
    assert d2.price_cents < d1.price_cents
    NegotiationEngine.advance(st, offer(2000), d2, eng.policy)
    d3 = eng.evaluate(offer(3000), st, "")  # 3000 >= floor, within round-3 ladder (floor)
    assert d3.action == "accept" and d3.price_cents == 300_000
    assert eng.evaluate(offer(4000), NegotiationState(), "").price_cents == pol.target_cents  # never above our quote


def test_holding_below_floor_after_floor_offered_escalates():
    eng, pol = engine()
    st, req = NegotiationState(), offer(1000)
    actions = []
    for _ in range(5):
        d = eng.evaluate(req, st, "")
        actions.append(d.action)
        NegotiationEngine.advance(st, req, d, eng.policy)
        if d.escalate:
            break
    assert actions[-1] == "escalate" and "PRICE_BELOW_MINIMUM" in d.reason_codes
    assert all(a != "accept" for a in actions)


@pytest.mark.parametrize("req,text,code", [
    (ExtractedRequest(payment_terms_request="net30"), "", "PAYMENT_TERMS_OUTSIDE_POLICY"),
    (ExtractedRequest(), "can we pay in installments?", "PAYMENT_TERMS_OUTSIDE_POLICY"),
    (ExtractedRequest(asks_for_refund=True), "", "REFUND_REQUEST"),
    (ExtractedRequest(), "what is your refund policy", "REFUND_REQUEST"),
    (ExtractedRequest(contract_term_requests=["IP ownership"]), "", "UNUSUAL_CONTRACT_TERMS"),
    (ExtractedRequest(), "we need you to indemnify us and add a non-compete", "UNUSUAL_CONTRACT_TERMS"),
    (ExtractedRequest(scope_requests=["also build a mobile app"]), "", "SCOPE_OUTSIDE_APPROVED"),
    (ExtractedRequest(asks_unsupported_integration=["NetSuite"]), "", "SCOPE_OUTSIDE_APPROVED"),
    (ExtractedRequest(), "we need a 99.9% uptime SLA", "GUARANTEE_REQUEST"),
])
def test_out_of_policy_requests_escalate(req, text, code):
    eng, _ = engine()
    d = eng.evaluate(req, NegotiationState(), text)
    assert d.action == "escalate" and d.escalate and code in d.reason_codes


def test_risk_scan_has_no_false_positive_on_ordinary_text():
    assert risk_scan("We get monthly reports and love the product. How does onboarding work?") == set()


# ----------------------------------------------------------------------------- guardrails
def test_reply_guard_money_and_promises():
    assert check_reply("It is $3,500 one-time.", allowed_amounts_cents=[350_000]).ok
    assert not check_reply("I can do $2,500 for you", allowed_amounts_cents=[350_000]).ok
    assert not check_reply("We guarantee a 10x return").ok
    assert not check_reply("Full refund if you are unhappy").ok
    assert not check_reply("I'm a real person, not an AI").ok
    assert not check_reply("Our clients include Acme and Globex").ok
    assert check_reply("I can't guarantee specific results, and I can't promise refunds.").ok  # honest negation is fine
    assert money_amounts_cents("$3k and $1,250.50") == [300_000, 125_050]
    assert not check_reply("price is $10", allow_money=False).ok


def test_ai_question_detection():
    assert asks_if_ai("Wait, am I talking to a robot?")
    assert asks_if_ai("Is this a real person?")
    assert asks_if_ai("are you a bot")
    assert not asks_if_ai("Do you work with a robot vacuum company?")


# ----------------------------------------------------------------------------- state machine
def test_pipeline_order_is_walkable_and_skips_are_rejected():
    for a, b in zip(PIPELINE_ORDER, PIPELINE_ORDER[1:]):
        if (a, b) in {(S.DISCOVERY, S.PROPOSAL)}:
            continue
        assert can_transition(a, b) or (a, b) == (S.ENGAGED, S.DISCOVERY), (a, b)
    assert not can_transition(S.QUALIFIED, S.PAID)
    assert not can_transition(S.NEGOTIATION, S.PAID)  # only the payment path may reach PAID
    assert not can_transition(S.PAYMENT_PENDING, S.DELIVERY)
    assert can_transition(S.PAYMENT_PENDING, S.PAID)
    assert not can_transition(S.COMPLETED, S.DISCOVERED)
    assert can_transition(S.NEGOTIATION, S.ESCALATED) and can_transition(S.NEGOTIATION, S.DO_NOT_CONTACT)
    assert not can_transition(S.ESCALATED, S.PAID)
    assert can_transition(S.ESCALATED, S.NEGOTIATION, resume_state=S.NEGOTIATION)


# ----------------------------------------------------------------------------- prompts
AGENT_PROMPTS = ["market_selector", "prospect_hunter", "researcher", "pain_detector", "solution_architect", "pricing",
                 "outreach", "sales", "negotiation", "proposal"]
SECTIONS = ["ROLE", "OBJECTIVE", "AVAILABLE CONTEXT", "TOOLS", "RULES", "PROHIBITED BEHAVIOR", "DECISION PROCESS",
            "OUTPUT FORMAT", "ESCALATION CONDITIONS"]


@pytest.mark.parametrize("name", AGENT_PROMPTS)
def test_every_prompt_exists_with_required_sections(name):
    text = (PROMPTS_DIR / name / "system.md").read_text()
    for sec in SECTIONS:
        assert f"# {sec}" in text, f"{name} missing {sec}"


def test_prompt_manager_injection_and_strictness():
    pm = PromptManager()
    vars_ = pm.variables("sales")
    assert {"company_name", "company_dossier", "identified_problem", "solution", "conversation_history"} <= vars_
    r = pm.load("negotiation", company_name="Acme", solution={"a": 1}, decision={"action": "hold"},
                conversation_history=[], prospect_message="{{company_name}}")
    assert "Acme" in r.text and "\"action\": \"hold\"" in r.text
    assert "{{company_name}}" in r.text  # injected values are never re-interpolated
    assert len(r.version) == 12
    with pytest.raises(PromptError):
        pm.load("negotiation", company_name="x")
    with pytest.raises(PromptError):
        pm.load("../etc/passwd")


def test_prompt_fragments_load_from_repo_not_source():
    pm = PromptManager()
    assert "AI" in pm.fragment("sales", "disclosure_text")
    with pytest.raises(PromptError):
        pm.fragment("sales", "../system")


def test_no_system_prompt_text_is_hardcoded_in_source():
    import pathlib, re
    hits = []
    for f in pathlib.Path("src").rglob("*.py"):
        if "mock" in f.parts:
            continue  # the offline mock LLM produces canned *outputs*, not prompts
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if re.search(r"\bYou are (a|an|the)\b", line):
                hits.append(f"{f}:{n}: {line.strip()[:80]}")
    assert hits == []
