"""CLI: `python -m src.cli demo` runs the whole sales lifecycle offline (mock LLM, in-memory DB, recording adapters).
`python -m src.cli serve` starts the API."""
from __future__ import annotations

import argparse
import json
import sys

from fastapi.testclient import TestClient

DEMO_SITE = "https://sunrisepm.example"
DEMO_TEXT = (
    "Sunrise Property Management manages 450 residential units across Austin, Texas. "
    "Call us at (512) 555-0100 to ask about availability and pricing. "
    "Schedule a tour online or book a showing with our leasing team. "
    "We are hiring a leasing agent to handle resident and prospect inquiries."
)


def demo() -> int:
    from src.api.app import create_app
    from src.config.settings import Settings
    from src.database.base import init_db, make_engine, make_session_factory
    from src.integrations.delivery.targets import RecordingDeliveryTarget
    from src.integrations.mock.llm import MockLLMProvider
    from src.integrations.mock.tts import FakeTTSProvider
    from src.integrations.outreach.providers import ManualTaskWebsiteContactProvider, RecordingEmailProvider
    from src.integrations.payments.mock import MockPaymentProvider
    from src.integrations.research.web import StaticWebFetcher
    from src.integrations.retell.client import FakeRetellClient
    from src.models.schemas import SourceDoc
    from src.services.container import Services
    from src.services.llm import LLMService
    from src.services.prompts import PromptManager
    from src.services.tts import TTSService

    st = Settings(database_url="sqlite://", admin_api_key="demo", llm_provider="mock", tts_provider="null",
                  sender_name="Alex", sender_company="Acme AI", sender_email="alex@acme.example",
                  sender_postal_address="1 Main St, Austin, TX 78701", public_base_url="http://demo",
                  mock_payment_webhook_secret="demo-secret")
    eng = make_engine(st.database_url)
    init_db(eng)
    pay = MockPaymentProvider("demo-secret", "http://demo")
    svc = Services(st, LLMService(MockLLMProvider()), PromptManager(), TTSService(FakeTTSProvider(), "./data/audio"),
                   StaticWebFetcher([SourceDoc(url=DEMO_SITE + "/", text=DEMO_TEXT)]), RecordingEmailProvider(),
                   ManualTaskWebsiteContactProvider(), FakeRetellClient(), pay, RecordingDeliveryTarget())
    c = TestClient(create_app(st, svc, make_session_factory(eng)))
    h = {"X-API-Key": "demo"}

    def step(title, resp):
        print(f"\n=== {title}")
        return resp

    def say(did, text):
        r = c.post(f"/conversations/{did}/messages", headers=h, json={"text": text}).json()
        print(f"  PROSPECT: {text}\n  AGENT   : {r['reply']}   [state={r['deal_state']}{' ESCALATED' if r['escalated'] else ''}]")
        return r

    step("1. Market selector", None)
    m = c.post("/markets", headers=h, json={"business_goal": "Sell one-time AI agents to property managers"}).json()
    print(" ICP:", json.dumps(m["profile"])[:200])
    step("2. Prospect hunter (imported record)", None)
    p = c.post("/prospects", headers=h, json={
        "market_profile_id": m["id"], "company_name": "Sunrise Property Management", "website": DEMO_SITE,
        "location": "Austin, TX", "employee_estimate": 35, "text": "Sunrise Property Management is a residential property manager.",
        "email": "info@sunrisepm.example", "phone": "+15125550100", "source": "demo-import"}).json()
    did = p["deal"]["id"]
    print(f" score={p['prospect']['qualification_score']}  state={p['deal']['state']}")
    step("3-6. Research -> qualify -> pain -> solution -> pricing", None)
    for s in c.post(f"/pipeline/{did}/run", headers=h).json():
        print("  ", s["step"], "->", s["state"])
    d = c.get(f"/deals/{did}/history", headers=h).json()
    print(" pain    :", d["prospect"]["pain_points"][0]["problem"])
    print(" solution:", d["prospect"]["solution"]["name"], "| needs validation:", d["prospect"]["solution"]["spec"]["requires_validation"])
    o = d["offer"]
    print(f" price   : target ${o['target_price_cents']/100:,.0f}  floor(internal) ${o['min_price_cents']/100:,.0f}")
    step("7. Outreach (dry-run)", None)
    print(c.post(f"/outreach/{did}", headers=h).json()["message"]["content"])
    step("8-13. Sales conversation, negotiation, proposal", None)
    say(did, "Thanks for reaching out. How would this work with our team?")
    say(did, "How much would it cost?")
    say(did, "That's a bit high. Can you do $1,000?")
    say(did, "Can you guarantee results?")
    r = say(did, "Sounds good, let's do it.")
    url = r["proposal_url"]
    path, token = url.split("http://demo")[1].split("?token=")
    step("14-17. Client accepts -> one-time payment request -> verified webhook -> PAID", None)
    acc = c.post(path.replace("/public", "/accept"), json={"token": token}).json()
    print(f" payment url: {acc['payment_url']}  amount ${acc['amount_cents']/100:,.0f}")
    ref = c.get(f"/deals/{did}/history", headers=h).json()["payments"][0]["provider_ref"]
    body, hdr = pay.simulate_payment(ref)
    print(" webhook ->", c.post("/webhooks/payment", content=body, headers=hdr).json())
    step("18-19. Delivery handoff", None)
    hist = c.get(f"/deals/{did}/history", headers=h).json()
    print(" deal state:", hist["deal"]["state"], "| delivery:", hist["delivery"][0]["status"], hist["delivery"][0]["external_ref"])
    step("20. Audit trail", None)
    for e in hist["events"]:
        if e["kind"] == "state_change":
            print(f"  {e['from_state']:>16} -> {e['to_state']:<16} {e['detail'].get('reason', '')[:70]}")
    print("\n agent runs:", len(hist["agent_runs"]), "| e.g.", next(r for r in hist["agent_runs"] if r["agent"] == "pricing")["decision_summary"][:150])
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="salesagent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo", help="run the offline end-to-end simulation")
    sv = sub.add_parser("serve", help="start the API")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    if a.cmd == "demo":
        return demo()
    import uvicorn

    uvicorn.run("src.api.app:app", host=a.host, port=a.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
