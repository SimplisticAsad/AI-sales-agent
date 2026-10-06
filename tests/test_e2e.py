"""End-to-end: discovered -> researched -> qualified -> contacted -> responds -> discovery -> solution -> negotiation
-> accepted -> payment -> verified -> delivery project created."""
from __future__ import annotations

from src.database.models import Message
from tests.conftest import Env


def test_full_sales_lifecycle(env: Env):
    # 1-6: discover, research, qualify, pain, solution, price
    pid, did = env.to_solution_ready()
    prospect = env.get(f"/prospects/{pid}").json()
    assert prospect["prospect"]["qualification_score"] >= 55
    assert prospect["research"]["dossier"]["claims"], "dossier has claims"
    assert any(c["label"] == "FACT" and c["source_url"] and c["quote"] for c in prospect["research"]["dossier"]["claims"])
    assert prospect["pain_points"][0]["selected"] is True
    assert prospect["solution"]["spec"]["requires_validation"] == ["Yardi"]  # unverified integration is flagged, not promised
    deal = env.get(f"/deals/{did}").json()
    assert deal["quoted_price_cents"] and deal["quoted_price_cents"] >= 100_000
    assert "min_price_cents" not in str(prospect["offer"]) or prospect["offer"]["min_price_cents"] == "hidden"

    # 7: personalised outreach (dry-run: recorded, nothing sent)
    out = env.post(f"/outreach/{did}").json()
    assert out["dry_run"] is True and out["message"]["status"] == "dry_run"
    assert "Reply STOP" in out["message"]["content"] and "1 Main St" in out["message"]["content"]
    assert env.email.sent == []
    assert env.state(did) == "OUTREACH_SENT"
    assert env.post(f"/outreach/{did}").json()["message"]["id"] == out["message"]["id"]  # idempotent

    # 8: prospect responds -> discovery
    r = env.say(did, "Thanks for reaching out. How would this work with our team?")
    assert r["deal_state"] == "DISCOVERY" and r["reply"]

    # 9: price inquiry -> quote
    r = env.say(did, "How much would it cost?")
    assert r["deal_state"] == "PROPOSAL"
    quote = deal["quoted_price_cents"]
    assert f"${quote // 100:,}" in r["reply"]

    # 10: negotiation within limits
    r = env.say(did, "That is a bit high. Can you do $1,000?")
    assert r["deal_state"] == "NEGOTIATION" and not r["escalated"]
    hist = env.get(f"/deals/{did}/history").json()
    floor = hist["offer"]["min_price_cents"]
    for n in hist["negotiations"]:
        assert n["decision"]["price_cents"] >= floor
    r = env.say(did, "Sounds good, let's do it.")
    assert r["proposal_url"] and "proposals/" in r["proposal_url"]
    assert env.state(did) == "PROPOSAL"
    agreed = env.get(f"/deals/{did}").json()["agreed_price_cents"]
    assert hist["offer"]["min_price_cents"] <= agreed <= quote

    # 11: client accepts proposal via token link -> payment request (one-time)
    url = r["proposal_url"]
    path, token = url.split("testserver")[1].split("?token=")
    view = env.client.get(path, params={"token": token}).json()
    assert view["content"]["price"]["type"] == "one_time" and "upfront" in view["content"]["payment_terms"]
    assert env.client.get(path, params={"token": "bad"}).status_code == 403
    acc = env.client.post(path.replace("/public", "/accept"), json={"token": token})
    assert acc.status_code == 200, acc.text
    pay = acc.json()
    assert pay["amount_cents"] == agreed and env.state(did) == "PAYMENT_PENDING"

    # a prospect *claiming* to have paid changes nothing
    env.say(did, "I paid just now!")
    assert env.state(did) == "PAYMENT_PENDING"

    # 12: verified webhook -> PAID -> delivery handoff
    pay_row = env.get(f"/deals/{did}/history").json()["payments"][0]
    body, headers = env.pay.simulate_payment(pay_row["provider_ref"])
    w = env.client.post("/webhooks/payment", content=body, headers=headers)
    assert w.status_code == 200 and w.json()["status"] == "paid", w.text
    assert env.state(did) == "DELIVERY"
    assert len(env.delivery.specs) == 1
    spec = env.delivery.specs[0]
    assert spec["accepted_price"]["amount_cents"] == agreed and spec["deliverables"] and spec["client"]["company"]
    assert spec["integrations_requiring_validation"] == ["Yardi"]

    # 13: complete history is inspectable
    h = env.get(f"/deals/{did}/history").json()
    states = [e["to_state"] for e in h["events"] if e["kind"] == "state_change"]
    for s in ["RESEARCHING", "QUALIFIED", "PAIN_IDENTIFIED", "SOLUTION_READY", "OUTREACH_SENT", "ENGAGED", "DISCOVERY",
              "PROPOSAL", "NEGOTIATION", "ACCEPTED", "PAYMENT_PENDING", "PAID", "DELIVERY"]:
        assert s in states, s
    agents = {r["agent"] for r in h["agent_runs"]}
    assert {"prospect_hunter", "researcher", "pain_detector", "solution_architect", "pricing", "outreach", "sales",
            "negotiation", "proposal"} <= agents
    pricing_run = next(r for r in h["agent_runs"] if r["agent"] == "pricing")
    assert "Target" in pricing_run["decision_summary"] and pricing_run["prompt_version"]
    assert env.post(f"/deals/{did}/complete").json()["state"] == "COMPLETED"
