"""Deterministic offline LLM used for tests and `LLM_PROVIDER=mock` demos. It reads the structured prompt variables
(request.metadata['variables']) and returns schema-valid JSON. Override any agent via `handlers`."""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from typing import Any

from src.services.llm import LLMRequest, LLMResponse, LLMUsage

Handler = Callable[[dict[str, Any], LLMRequest], dict]
_SOURCE = re.compile(r'URL: (\S+)\n(.*?)\n</untrusted_data>', re.S)
_SENT = re.compile(r"(?<=[.!?])\s+")
_MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)")


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT.split(text) if len(s.strip()) > 20]


def market_selector(v, r):
    return {"industry": "property management", "geography": "United States", "employee_min": 10, "employee_max": 100,
            "target_problems": ["tenant inquiries", "appointment scheduling", "lead qualification"],
            "decision_maker_titles": ["Owner", "Operations Manager", "Property Manager"],
            "min_qualification_criteria": ["has public website", "publishes a phone number or contact form"],
            "exclusions": [], "decision_summary": "Property managers have repetitive inquiry/scheduling work that maps to approved scope."}


def prospect_hunter(v, r):
    text = v["candidate"].lower()
    ind = v["market_profile"].get("industry") or ""
    toks = [t for t in re.findall(r"[a-z]{4,}", ind.lower())]
    return {"extracted": {"industry": ind if toks and any(t in text for t in toks) else None, "decision_maker": None,
                          "decision_maker_role": None, "problem_hints": [], "decision_summary": "mock extraction"}}


_KEYS = [
    ("channels", ("call us", "phone", "inquir", "contact form", "email us", "reach us")),
    ("workflow", ("schedule", "tour", "appointment", "book a")),
    ("hiring", ("hiring", "careers", "join our team", "open positions")),
    ("tools", ("appfolio", "yardi", "buildium", "hubspot", "salesforce", "quickbooks")),
]


def researcher(v, r):
    docs = _SOURCE.findall(v["source_documents"])
    claims, n = [], 0
    for url, text in docs:
        for cat, kws in _KEYS:
            for s in _sentences(text):
                if any(k in s.lower() for k in kws):
                    n += 1
                    claims.append({"id": f"c{n}", "category": cat, "statement": s[:200], "label": "FACT", "source_url": url, "quote": s[:160]})
                    break
    if docs:
        n += 1
        t0 = _sentences(docs[0][1])[:1] or [docs[0][1][:150]]
        claims.insert(0, {"id": f"c{n}", "category": "what_they_do", "statement": t0[0][:200], "label": "FACT",
                          "source_url": docs[0][0], "quote": t0[0][:160]})
    if any(c["category"] in ("channels", "workflow") for c in claims):
        n += 1
        claims.append({"id": f"c{n}", "category": "repetitive", "statement": "Staff likely answer the same inquiries and book appointments by hand",
                       "label": "INFERENCE", "note": "inferred from channel/workflow facts"})
    return {"summary": "mock dossier", "claims": claims, "unknowns": ["software stack not confirmed", "exact headcount"],
            "decision_summary": f"{len(claims)} claims from {len(docs)} documents"}


def pain_detector(v, r):
    claims = v["company_dossier"]["claims"]
    ev = [c["id"] for c in claims if c["category"] in ("channels", "workflow") and c["label"] == "FACT"]
    if not ev:
        return {"pains": [], "decision_summary": "no well-supported pain"}
    return {"pains": [{
        "problem": "Staff manually handle repetitive inbound inquiries and appointment scheduling", "evidence_claim_ids": ev,
        "confidence": 0.75, "business_impact": "Staff time spent on repetitive replies; slower response to new leads",
        "automation_potential": 0.8,
        "criteria": {"frequency": 8, "cost": 6, "repetitiveness": 9, "business_impact": 7, "feasibility": 8,
                     "integration_availability": 8, "roi_demonstrability": 6}}], "decision_summary": "Inquiry/scheduling workload is evidenced by public contact + booking flows"}


def solution_architect(v, r):
    return {"name": "AI Inquiry & Scheduling Agent", "summary": "An AI agent that answers common inquiries, qualifies leads and books appointments.",
            "problem_addressed": v["identified_problem"]["problem"],
            "capabilities": ["Answer common questions", "Qualify prospects", "Schedule appointments", "Log to CRM", "Escalate complex requests"],
            "integrations": [{"name": "Google Calendar", "purpose": "scheduling"}, {"name": "HubSpot", "purpose": "CRM logging"},
                             {"name": "Yardi", "purpose": "availability lookup"}],
            "data_sources": ["Company FAQ", "Availability feed"], "workflow_steps": ["Inquiry arrives", "Agent answers/qualifies", "Books appointment", "Logs to CRM"],
            "human_escalation_points": ["Complaints", "Legal or pricing exceptions"], "complexity": "medium", "effort_hours": 40,
            "expected_value": "Frees staff time from repetitive inquiries (assumption-based)",
            "value_drivers": [{"driver": "Staff time on inquiries", "hours_saved_per_week": 10, "hourly_cost_usd": 30, "basis": "assumption; confirm in discovery"}],
            "assumptions": ["Client can provide CRM access"], "decision_summary": "Minimal agent covering evidenced workflow"}


def pricing(v, r):
    return {"value_rationale": f"The fee reflects roughly {v['pricing_summary']['effort_hours']} hours of build work plus integrations. "
                               "Value depends on your actual inquiry volume.", "decision_summary": "mock rationale"}


def outreach(v, r):
    facts = v["company_dossier"]
    f = facts[0]
    return {"subject": f"Quick idea for {v['company_name']}",
            "body": f"Hi - I noticed that \"{f['statement'][:140]}\". That usually means staff spend time on repetitive inquiries and scheduling. "
                    f"One option is {v['solution']['name']}, which could free up that time. Worth a 10-minute look?",
            "evidence_claim_ids": [f["id"]], "decision_summary": "Led with the strongest verified fact"}


def sales(v, r):
    msg = v["prospect_message"].lower()
    ext = {"offered_price_usd": None, "requested_discount_percent": None, "payment_terms_request": None, "scope_requests": [],
           "contract_term_requests": [], "asks_for_refund": False, "asks_for_guarantee": False, "asks_legal_question": False,
           "asks_unsupported_integration": []}
    out = {"tool_calls": [], "reply": None, "intent": "other", "extracted": ext, "objection_type": None, "buying_intent": 0.5,
           "next_action": "continue discovery", "decision_summary": "mock sales turn", "confidence": 0.7}
    m = _MONEY.search(msg)
    if "roi" in msg and not v["tool_results"]:
        out["tool_calls"] = [{"name": "calculate_roi", "arguments": {"hours_saved_per_week": 10, "hourly_cost_usd": 30}}]
        return out
    if "roi" in msg and v["tool_results"]:
        res = v["tool_results"][-1]["result"]
        out.update(intent="question", reply=f"Using your numbers (an illustrative estimate, not a guarantee), that is roughly ${res['annual_value_usd']:,.0f} a year in staff time.")
        return out
    if any(k in msg for k in ("unsubscribe", "stop emailing", "remove me", "do not contact")):
        out["intent"] = "opt_out"
    elif any(k in msg for k in ("talk to a person", "speak to a human", "real person please", "manager")):
        out["intent"] = "request_human"
    elif "not interested" in msg or "no thanks" in msg:
        out["intent"] = "reject"
    elif "refund" in msg:
        ext["asks_for_refund"] = True
        out["intent"] = "question"
    elif any(k in msg for k in ("net 30", "installment", "monthly payment", "pay later")):
        ext["payment_terms_request"] = "net30"
        out["intent"] = "counter_offer"
    elif "guarantee" in msg:
        ext["asks_for_guarantee"] = True
        out.update(intent="objection", objection_type="guarantee",
                   reply="Fair question. I can't guarantee specific results, but I can show you exactly what the agent would handle and what to measure.")
    elif any(k in msg for k in ("salesforce", "quickbooks", "netsuite", "sap ")):
        names = [k for k in ("salesforce", "quickbooks", "netsuite", "sap") if k in msg]
        ext["asks_unsupported_integration"] = [n.title() for n in names]
        out["intent"] = "question"
    elif m and any(k in msg for k in ("can you do", "how about", "offer", "budget", "would you take", "could you do", "only pay")):
        ext["offered_price_usd"] = float(m.group(1).replace(",", ""))
        out["intent"] = "counter_offer"
    elif any(k in msg for k in ("discount", "cheaper", "too expensive", "too much")):
        ext["requested_discount_percent"] = 30.0 if "30" in msg else None
        out["intent"] = "counter_offer" if ext["requested_discount_percent"] else "objection"
        if out["intent"] == "objection":
            out.update(objection_type="price", reply="Understood - cost matters. What would a good outcome be worth to you, so we can check the price against it?")
    elif any(k in msg for k in ("how much", "price", "cost", "pricing")):
        out["intent"] = "price_inquiry"
    elif any(k in msg for k in ("sounds good", "let's do it", "lets do it", "i accept", "we'll take it", "go ahead", "deal", "agreed")):
        out["intent"] = "accept"
    elif any(k in msg for k in ("already have", "build ourselves", "don't trust", "think about")):
        out.update(intent="objection", objection_type="other",
                   reply="That's reasonable. Could you tell me what's working well today, so I can be clear on where an agent would genuinely help?")
    elif "?" in msg:
        out.update(intent="question", reply="Good question. In short, the agent would handle the repetitive inquiries and hand anything complex to your team. What does your inquiry volume look like?")
    else:
        out.update(intent="interest", reply="Thanks for getting back to me. How are inquiries and scheduling handled today?")
    return out


def negotiation(v, r):
    d = v["decision"]
    if d["action"] == "escalate":
        return {"reply": "That's something I need to confirm with a teammate rather than guess at, so I'll check and come back to you.", "decision_summary": "escalated"}
    price = f"${d['price_usd']:,.0f}"
    if d["action"] == "accept":
        return {"reply": f"Wonderful - {price}, one-time and paid upfront. I'll send a written proposal now.", "decision_summary": "accepted"}
    return {"reply": f"For this project the price is {price}, one-time and paid upfront. Happy to walk through the value.", "decision_summary": d["action"]}


def proposal(v, r):
    s = v["solution"]
    return {"client_problem": v["identified_problem"], "proposed_solution": s["summary"], "features": s["capabilities"],
            "integrations": [i["name"] for i in s["integrations"] if i["verified"]],
            "deliverables": [f"Working {s['name']}", "Configuration and testing", "Handover documentation"],
            "timeline": "Estimated 2 weeks after payment and access are confirmed.", "assumptions": s.get("assumptions", []),
            "exclusions": [], "special_requirements": [], "conversation_summary": "Prospect agreed in principle after discussing scope and price.",
            "decision_summary": "mock proposal"}


DEFAULT_HANDLERS: dict[str, Handler] = {
    "market_selector": market_selector, "prospect_hunter": prospect_hunter, "researcher": researcher,
    "pain_detector": pain_detector, "solution_architect": solution_architect, "pricing": pricing, "outreach": outreach,
    "sales": sales, "negotiation": negotiation, "proposal": proposal,
}


class MockLLMProvider:
    name = "mock"

    def __init__(self, handlers: dict[str, Handler] | None = None, fail_times: int = 0, raw_override: str | None = None):
        self.handlers = {**DEFAULT_HANDLERS, **(handlers or {})}
        self.calls: list[LLMRequest] = []
        self.fail_times = fail_times
        self.raw_override = raw_override

    def complete(self, request: LLMRequest) -> LLMResponse:
        from src.services.llm import LLMTransientError

        self.calls.append(request)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise LLMTransientError("simulated provider outage")
        if self.raw_override is not None:
            return LLMResponse(self.raw_override, "mock", LLMUsage(10, 10, 0.0), 1)
        h = self.handlers.get(request.agent or "")
        if not h:
            raise KeyError(f"no mock handler for agent {request.agent!r}")
        out = h(request.metadata.get("variables", {}), request)
        return LLMResponse(json.dumps(out), "mock-llm", LLMUsage(100, 50, 0.0), 1)

    def stream(self, request: LLMRequest) -> Iterator[str]:
        for w in self.complete(request).content.split(" "):
            yield w + " "
