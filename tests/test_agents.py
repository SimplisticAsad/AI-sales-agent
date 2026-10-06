"""Agent behaviour: qualification, evidence, pain detection, solution, pricing, outreach."""
from __future__ import annotations

import pytest

from src.agents.outreach.agent import OutreachError
from src.database.models import AgentRun, PainPoint, Prospect, Research
from src.models.enums import DealState as S
from src.models.schemas import SourceDoc, Claim
from src.integrations.research.web import StaticWebFetcher
from src.services.evidence import verify_claims
from tests.conftest import CANDIDATE, DOCS, Env, SITE, llm_with


# ----------------------------------------------------------------------------- qualification
def test_good_prospect_qualifies_and_progresses(env: Env):
    pid, did = env.to_solution_ready()
    q = env.get(f"/prospects/{pid}").json()["prospect"]["qualification"]
    assert q["decision"] == "qualified" and q["verified_facts"] >= 1


def test_out_of_icp_prospect_is_disqualified(env: Env):
    m = env.seed_market()
    pid, did = env.seed_prospect(m, employee_estimate=5000, location="Berlin, Germany", email=None, phone=None)
    r = env.post(f"/qualify/{did}").json()
    assert r["qualified"] is False and r["deal_state"] == "DISQUALIFIED"
    reasons = " ".join(r["qualification"]["reasons"])
    assert "outside" in reasons and "contact path" in reasons
    assert env.post(f"/pipeline/{did}/advance").json()["step"] is None  # terminal: nothing further runs


def test_no_sources_means_no_facts_and_no_fabrication():
    e = Env(fetcher=StaticWebFetcher([]))
    m = e.seed_market()
    pid, did = e.seed_prospect(m)
    r = e.post(f"/qualify/{did}").json()
    assert r["qualified"] is False and "no verified facts" in " ".join(r["qualification"]["reasons"])
    dossier = e.get(f"/prospects/{pid}").json()["research"]["dossier"]
    assert dossier["claims"] == [] and dossier["unknowns"]
    assert all(c.agent != "researcher" for c in e.llm_provider.calls)  # LLM not even asked to "research" nothing


def test_hunter_never_invents_contacts():
    e = Env()
    m = e.seed_market()
    pid, did = e.seed_prospect(m, email=None, phone=None, contact_page_url=None, contact_name=None)
    contacts = e.get(f"/prospects/{pid}").json()["contacts"]
    assert contacts == []  # LLM supplied nothing, so no contact exists
    # a malformed email from the source is dropped rather than "fixed"
    pid2, _ = e.seed_prospect(m, company_name="Other Co", website="https://other.example", email="not-an-email", phone="+15125550111")
    c = e.get(f"/prospects/{pid2}").json()["contacts"][0]
    assert c["email"] is None and c["phone"] == "+15125550111" and c["source"]


def test_duplicate_prospect_is_deduped(env: Env):
    m = env.seed_market()
    a = env.seed_prospect(m)
    b = env.seed_prospect(m, website="http://www.sunrisepm.example/")
    assert a == b


# ----------------------------------------------------------------------------- evidence (FACT / INFERENCE / UNKNOWN)
def test_fact_without_verifiable_quote_is_downgraded():
    docs = [SourceDoc(url="https://x.example/", text="We manage 450 units in Austin.")]
    claims = verify_claims([
        Claim(id="c1", statement="manages 450 units", label="FACT", source_url="https://x.example/", quote="We manage 450 units"),
        Claim(id="c2", statement="has 900 units", label="FACT", source_url="https://x.example/", quote="We manage 900 units"),
        Claim(id="c3", statement="uses Yardi", label="FACT", source_url=None, quote=None),
        Claim(id="c4", statement="elsewhere", label="FACT", source_url="https://evil.example/", quote="We manage 450 units"),
        Claim(id="c5", statement="headcount?", label="UNKNOWN", source_url="https://x.example/", quote="x"),
    ], docs)
    labels = {c.id: c.label.value for c in claims}
    assert labels == {"c1": "FACT", "c2": "INFERENCE", "c3": "INFERENCE", "c4": "INFERENCE", "c5": "UNKNOWN"}
    assert "downgraded" in claims[1].note and claims[4].source_url is None


def test_llm_fabricated_fact_is_downgraded_in_pipeline():
    def lying_researcher(v, r):
        return {"summary": "x", "claims": [
            {"id": "c1", "category": "size", "statement": "Has 5,000 employees", "label": "FACT",
             "source_url": SITE + "/", "quote": "employs five thousand people worldwide"}], "unknowns": []}
    e = Env(llm_with(researcher=lying_researcher))
    m = e.seed_market()
    pid, did = e.seed_prospect(m)
    e.post(f"/research/{did}")
    c = e.get(f"/prospects/{pid}").json()["research"]["dossier"]["claims"][0]
    assert c["label"] == "INFERENCE"


# ----------------------------------------------------------------------------- pain detection
def pain(ev, conf=0.8, auto=0.8, score=8):
    return {"problem": "Manual inquiries", "evidence_claim_ids": ev, "confidence": conf, "business_impact": "time",
            "automation_potential": auto, "criteria": {k: score for k in (
                "frequency", "cost", "repetitiveness", "business_impact", "feasibility", "integration_availability", "roi_demonstrability")}}


@pytest.mark.parametrize("p,reason", [
    (pain(["zzz"]), "no valid evidence"),
    (pain(["c1"], auto=0.2), "automation potential"),
    (pain(["c1"], score=3), "priority"),
    (pain(["c1"], conf=0.2), "confidence"),
])
def test_weak_pains_are_rejected_and_prospect_disqualified(p, reason):
    e = Env(llm_with(pain_detector=lambda v, r: {"pains": [p], "decision_summary": "x"}))
    m = e.seed_market()
    pid, did = e.seed_prospect(m)
    e.post(f"/pipeline/{did}/run")
    assert e.state(did) == "DISQUALIFIED"
    rows = e.get(f"/prospects/{pid}").json()["pain_points"]
    assert rows and reason in rows[0]["rejected_reason"] and rows[0]["selected"] is False


def test_inference_only_pain_confidence_is_capped():
    def research(v, r):
        return {"summary": "", "claims": [{"id": "c1", "category": "opportunity", "statement": "probably busy", "label": "INFERENCE"},
                                          {"id": "c2", "category": "channels", "statement": "Call us at (512) 555-0100", "label": "FACT",
                                           "source_url": SITE + "/", "quote": "Call us at (512) 555-0100"}], "unknowns": []}
    e = Env(llm_with(researcher=research, pain_detector=lambda v, r: {"pains": [pain(["c1"], conf=0.95)]}))
    m = e.seed_market()
    pid, did = e.seed_prospect(m)
    e.post(f"/pipeline/{did}/run")
    p = e.get(f"/prospects/{pid}").json()["pain_points"][0]
    assert p["confidence"] == 0.5  # inference-only evidence can never exceed 0.5


# ----------------------------------------------------------------------------- solution + pricing
def test_unverified_integrations_flagged_and_priced_higher():
    e = Env()
    pid, did = e.to_solution_ready()
    sol = e.get(f"/prospects/{pid}").json()["solution"]["spec"]
    ints = {i["name"]: i["verified"] for i in sol["integrations"]}
    assert ints == {"Google Calendar": True, "HubSpot": True, "Yardi": False}
    h = e.get(f"/deals/{did}/history").json()
    b = h["offer"]["breakdown"]
    assert b["unverified_integrations"] == 1 and b["verified_integrations"] == 2
    assert h["offer"]["min_price_cents"] <= h["offer"]["target_price_cents"]


def test_pricing_ignores_llm_attempts_to_set_prices():
    bad = lambda v, r: {"value_rationale": "Honestly we could do this for $50 or even $10,000.", "decision_summary": "x"}
    e = Env(llm_with(pricing=bad))
    pid, did = e.to_solution_ready()
    h = e.get(f"/deals/{did}/history").json()
    assert "$50" not in h["offer"]["rationale"]  # rejected by guard; deterministic text used
    assert h["offer"]["target_price_cents"] >= 100_000


def test_prices_follow_configured_policy():
    from tests.conftest import make_settings
    e = Env(settings=make_settings(default_min_price=5000, default_max_discount_percent=10))
    pid, did = e.to_solution_ready()
    o = e.get(f"/deals/{did}/history").json()["offer"]
    assert o["min_price_cents"] >= 500_000 and o["max_discount_percent"] == 10


# ----------------------------------------------------------------------------- outreach
def test_outreach_is_personalised_from_verified_facts(env: Env):
    pid, did = env.to_solution_ready()
    msg = env.post(f"/outreach/{did}").json()["message"]
    assert msg["meta"]["evidence_claim_ids"]
    claims = {c["id"]: c for c in env.get(f"/prospects/{pid}").json()["research"]["dossier"]["claims"]}
    assert all(claims[i]["label"] == "FACT" for i in msg["meta"]["evidence_claim_ids"])
    assert "AI automation company" not in msg["content"]
    assert "$" not in msg["content"].split("--")[0]  # no pricing in a first touch


@pytest.mark.parametrize("draft", [
    {"subject": "Hi", "body": "We are an AI automation company that can help.", "evidence_claim_ids": ["c1"]},
    {"subject": "Hi", "body": "Saw your site. We guarantee 10x results.", "evidence_claim_ids": ["c1"]},
    {"subject": "Hi", "body": "Great offer: only $999 today.", "evidence_claim_ids": ["c1"]},
    {"subject": "Hi", "body": "Nice site.", "evidence_claim_ids": ["not-a-fact"]},
])
def test_bad_outreach_drafts_are_blocked(draft):
    e = Env(llm_with(outreach=lambda v, r: {**draft, "decision_summary": "x"}))
    pid, did = e.to_solution_ready()
    r = e.post(f"/outreach/{did}")
    assert r.status_code == 422 and "guardrails" in r.json()["detail"]
    assert e.state(did) == "SOLUTION_READY" and e.email.sent == []


def test_outreach_blocked_without_sender_identity_or_for_suppressed():
    from tests.conftest import make_settings
    e = Env(settings=make_settings(sender_postal_address=""))
    pid, did = e.to_solution_ready()
    r = e.post(f"/outreach/{did}")
    assert r.status_code == 409 and "CAN-SPAM" in r.json()["detail"]


def test_real_send_goes_through_provider_and_respects_dry_run_flag():
    from tests.conftest import make_settings
    e = Env(settings=make_settings(outreach_dry_run=False))
    pid, did = e.to_solution_ready()
    r = e.post(f"/outreach/{did}").json()
    assert r["message"]["status"] == "sent" and len(e.email.sent) == 1
    assert e.email.sent[0]["to"] == "info@sunrisepm.example"


def test_contact_form_fallback_is_a_manual_task_never_auto_submitted():
    from tests.conftest import make_settings
    e = Env(settings=make_settings(outreach_dry_run=False))
    m = e.seed_market()
    pid, did = e.seed_prospect(m, email=None, contact_page_url=SITE + "/contact")
    e.post(f"/pipeline/{did}/run")
    r = e.post(f"/outreach/{did}").json()
    assert r["message"]["status"] == "manual_task" and e.email.sent == []
