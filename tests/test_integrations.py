"""Integration behaviour: Groq failures/timeouts, resumability, Retell, TTS, payment webhooks."""
from __future__ import annotations

import hashlib
import hmac
import json
import time

import httpx
import pytest
from pydantic import BaseModel

from src.integrations.groq.provider import GroqProvider
from src.integrations.payments.base import InvalidSignature, PaymentError
from src.integrations.payments.stripe import StripePaymentProvider
from src.integrations.retell.client import RetellClient
from src.integrations.mock.llm import MockLLMProvider
from src.services.llm import (
    LLMMessage, LLMOutputError, LLMPermanentError, LLMRequest, LLMService, LLMTransientError,
)
from src.services.tts import TTSError, TTSService
from tests.conftest import Env, llm_with


# ============================================================================ Groq / LLM service
def groq(handler, **kw) -> GroqProvider:
    return GroqProvider("gsk_test", "llama-test", client=httpx.Client(transport=httpx.MockTransport(handler)), **kw)


def ok_body(content='{"a": 1}'):
    return {"model": "llama-test", "choices": [{"message": {"content": content}}], "usage": {"prompt_tokens": 1000, "completion_tokens": 500}}


REQ = LLMRequest([LLMMessage("user", "hi")], json_mode=True, agent="x")


def test_groq_success_parses_usage_and_logs_cost():
    seen = {}

    def h(request: httpx.Request):
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=ok_body())

    r = groq(h, input_cost_per_mtok=0.6, output_cost_per_mtok=0.8).complete(REQ)
    assert r.content == '{"a": 1}' and r.usage.prompt_tokens == 1000
    assert abs(r.usage.cost_usd - (1000 * 0.6 + 500 * 0.8) / 1e6) < 1e-12
    assert seen["auth"] == "Bearer gsk_test" and seen["body"]["response_format"] == {"type": "json_object"}
    assert seen["body"]["model"] == "llama-test"


def test_groq_error_classification():
    with pytest.raises(LLMTransientError) as e:
        groq(lambda r: httpx.Response(429, headers={"retry-after": "2"}, text="slow down")).complete(REQ)
    assert e.value.retry_after == 2.0
    with pytest.raises(LLMTransientError):
        groq(lambda r: httpx.Response(503, text="down")).complete(REQ)
    with pytest.raises(LLMPermanentError):
        groq(lambda r: httpx.Response(401, text="bad key")).complete(REQ)
    with pytest.raises(LLMPermanentError):
        GroqProvider("", "m")


def test_groq_network_timeout_is_transient():
    def h(request):
        raise httpx.ReadTimeout("slow", request=request)
    with pytest.raises(LLMTransientError):
        groq(h).complete(REQ)


def test_groq_streaming_yields_deltas():
    sse = "".join(f'data: {json.dumps({"choices": [{"delta": {"content": w}}]})}\n\n' for w in ["Hel", "lo"]) + "data: [DONE]\n\n"
    p = groq(lambda r: httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"}))
    assert "".join(LLMService(p).stream(REQ)) == "Hello"


class Out(BaseModel):
    a: int


def service(provider, retries=2):
    delays = []
    return LLMService(provider, max_retries=retries, base_backoff=1, sleep=delays.append), delays


def test_llm_service_retries_transient_failures_with_backoff_then_succeeds():
    p = MockLLMProvider(fail_times=2, raw_override='{"a": 7}')
    svc, delays = service(p)
    out, info = svc.generate_structured(Out, system="s", user="u", agent="x")
    assert out.a == 7 and info.attempts == 3 and len(delays) == 2 and delays[1] > delays[0] - 0.3


def test_llm_service_gives_up_after_max_retries_and_does_not_retry_permanent():
    svc, _ = service(MockLLMProvider(fail_times=99, raw_override="{}"))
    with pytest.raises(LLMTransientError):
        svc.generate_structured(Out, system="s", user="u", agent="x")

    class Perm:
        name = "p"
        calls = 0

        def complete(self, r):
            Perm.calls += 1
            raise LLMPermanentError("401")
    svc, _ = service(Perm())
    with pytest.raises(LLMPermanentError):
        svc.generate_structured(Out, system="s", user="u", agent="x")
    assert Perm.calls == 1


def test_structured_output_repair_and_failure():
    class Flaky:
        name = "f"

        def __init__(self, outs):
            self.outs, self.reqs = list(outs), []

        def complete(self, r):
            from src.services.llm import LLMResponse
            self.reqs.append(r)
            return LLMResponse(self.outs.pop(0), "m")

    p = Flaky(["```json\nnot json\n```", '```json\n{"a": 3}\n```'])
    out, info = service(p)[0].generate_structured(Out, system="s", user="u", agent="x")
    assert out.a == 3 and info.repairs == 1 and "invalid" in p.reqs[1].messages[-1].content
    with pytest.raises(LLMOutputError):
        service(Flaky(['{"a": "x"}', '{"a": "y"}']))[0].generate_structured(Out, system="s", user="u", agent="x")


# ============================================================================ pipeline resilience / resumability
def test_groq_outage_marks_step_failed_and_pipeline_is_resumable():
    e = Env(MockLLMProvider(fail_times=10_000))
    m = e.seed_market.__func__  # noqa - market creation also needs an LLM; seed directly instead
    from src.database.models import MarketProfile
    with e.session() as s:
        s.add(MarketProfile(name="pm", profile={"industry": "property management", "geography": "United States",
                                                  "employee_min": 10, "employee_max": 100}))
        s.commit()
        mid = s.query(MarketProfile).first().id
    # hunter needs the LLM too -> add the prospect with the LLM healthy, then break it
    e.llm_provider.fail_times = 0
    pid, did = e.seed_prospect(mid)
    e.llm_provider.fail_times = 10_000
    r = e.post(f"/pipeline/{did}/run").json()
    assert r[-1]["detail"].startswith("failed")
    d = e.get(f"/deals/{did}").json()
    assert d["retry_count"] == 1 and "researcher" not in (d["last_error"] or "") and d["state"] in ("DISCOVERED", "RESEARCHING")
    for _ in range(4):
        e.post(f"/pipeline/{did}/advance")
    assert e.state(did) == "FAILED"
    # provider recovers -> ops retry resumes from where it failed
    e.llm_provider.fail_times = 0
    e.post(f"/pipeline/{did}/retry")
    e.post(f"/pipeline/{did}/run")
    assert e.state(did) == "SOLUTION_READY"
    failed_runs = [x for x in e.get("/agent-runs", params={"agent": "researcher"}).json() if x["status"] == "failed"]
    assert failed_runs and all(x["error"] for x in failed_runs)


def test_crash_between_steps_resumes_from_last_committed_state():
    e = Env()
    m = e.seed_market()
    pid, did = e.seed_prospect(m)
    assert e.post(f"/pipeline/{did}/advance").json()["step"] == "research"
    assert e.state(did) == "RESEARCHING"
    # "restart": brand-new app + services over the same database
    e2 = Env.__new__(Env)
    e2.__dict__.update(e.__dict__)
    from fastapi.testclient import TestClient
    from src.api.app import create_app
    e2.app = create_app(e.settings, e.svc, e.factory)
    e2.client = TestClient(e2.app)
    steps = [s["step"] for s in e2.post(f"/pipeline/{did}/run").json()]
    assert steps[:3] == ["qualify", "detect_pain", "design_and_price"]  # did NOT re-research
    assert e2.state(did) == "SOLUTION_READY"
    runs = [r["agent"] for r in e2.get("/agent-runs", params={"deal_id": did, "limit": 500}).json()] + \
           [r["agent"] for r in e2.get("/agent-runs", params={"prospect_id": pid, "limit": 500}).json()]
    assert runs.count("researcher") <= 2  # one run (listed under the prospect query only)


def test_agent_runs_record_failures_without_secrets():
    e = Env(llm_with(pain_detector=lambda v, r: (_ for _ in ()).throw(RuntimeError("boom key gsk_abcdefghijklmnop42 user a@b.com"))))
    m = e.seed_market()
    pid, did = e.seed_prospect(m)
    e.post(f"/pipeline/{did}/run")
    run = next(r for r in e.get("/agent-runs", params={"agent": "pain_detector"}).json())
    assert run["status"] == "failed" and "gsk_abcdefghijklmnop42" not in run["error"] and "a@b.com" not in run["error"]
    assert run["latency_ms"] is not None and run["prompt_version"]


# ============================================================================ TTS
def test_tts_failure_is_non_fatal_and_slow_provider_times_out(tmp_path):
    from src.integrations.mock.tts import FakeTTSProvider
    bad = TTSService(FakeTTSProvider(fail=True), tmp_path, timeout_s=2)
    assert bad.try_synthesize("hello", key="k") is None
    with pytest.raises(TTSError):
        bad.synthesize("hello", key="k")
    with pytest.raises(TTSError):
        bad.synthesize("   ", key="k")

    class Slow:
        name = "slow"

        def synthesize(self, text, voice, out):
            time.sleep(1.0)
    with pytest.raises(TTSError, match="timed out"):
        TTSService(Slow(), tmp_path, timeout_s=0.1).synthesize("hello", key="s")

    class Buggy:
        name = "buggy"

        def synthesize(self, *a):
            raise ZeroDivisionError("bug")
    assert TTSService(Buggy(), tmp_path).try_synthesize("hello", key="b") is None
    good = TTSService(FakeTTSProvider(), tmp_path).synthesize("hello", key="../../evil name")
    assert good.path.parent == tmp_path and good.path.exists()


def test_tts_preview_endpoint_and_graceful_503(env: Env):
    r = env.post("/tts/preview", json={"text": "Hello there", "key": "demo"})
    assert r.status_code == 200 and r.json()["provider"] == "fake"
    env.tts_provider.fail = True
    assert env.post("/tts/preview", json={"text": "Hello there"}).status_code == 503


def test_chatterbox_http_provider(tmp_path):
    import wave, io
    from src.integrations.chatterbox.provider import ChatterboxProvider
    from src.services.tts import VoiceSettings
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(24000), w.writeframes(b"\0\0" * 2400)
    seen = {}

    def h(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, content=buf.getvalue())
    p = ChatterboxProvider("http", url="http://tts.local", client=httpx.Client(transport=httpx.MockTransport(h)))
    res = TTSService(p, tmp_path).synthesize("Hi", key="cb", voice=VoiceSettings(exaggeration=0.7))
    assert res.sample_rate == 24000 and seen["exaggeration"] == 0.7
    down = ChatterboxProvider("http", url="http://tts.local", client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    with pytest.raises(TTSError):
        TTSService(down, tmp_path).synthesize("Hi", key="cb2")
    assert TTSService(ChatterboxProvider("local"), tmp_path).try_synthesize("Hi", key="x") is None  # torch/chatterbox absent -> graceful


# ============================================================================ Retell
def retell_post(e: Env, payload: dict, sig: str | None = None):
    body = json.dumps(payload).encode()
    return e.client.post("/webhooks/retell", content=body, headers={"x-retell-signature": sig if sig is not None else e.retell.sign(body)})


def test_retell_webhook_signature_idempotency_and_matching():
    e = Env()
    pid, did = e.to_outreach_sent()
    ev = {"event": "call_started", "call": {"call_id": "call_1", "metadata": {"deal_id": did}, "direction": "outbound"}}
    assert retell_post(e, ev, sig="v=1,d=bad").status_code == 401
    assert e.client.post("/webhooks/retell", content=b"{}").status_code == 401
    assert retell_post(e, ev).json()["status"] == "ok"
    assert retell_post(e, ev).json()["status"] == "duplicate"
    end = {"event": "call_ended", "call": {"call_id": "call_1", "metadata": {"deal_id": did}, "disconnection_reason": "user_hangup",
                                          "call_analysis": {"call_summary": "Interested; wants a proposal."}}}
    assert retell_post(e, end).json()["status"] == "ok"
    convs = e.get(f"/deals/{did}/history").json()["conversations"]
    v = next(c for c in convs if c["channel"] == "voice")
    assert v["status"] == "closed" and v["summary"].startswith("Interested")
    assert retell_post(e, {"event": "call_started", "call": {"call_id": "call_zzz"}}).json()["status"] == "unmatched"


def test_retell_signature_scheme_and_replay_protection():
    c = RetellClient("key123")
    body = b'{"event":"call_ended"}'
    ts = str(int(time.time() * 1000))
    sig = f"v={ts},d=" + hmac.new(b"key123", body + ts.encode(), hashlib.sha256).hexdigest()
    assert c.verify_signature(body, sig)
    assert not c.verify_signature(body + b"x", sig)
    old = str(int((time.time() - 3600) * 1000))
    assert not c.verify_signature(body, f"v={old},d=" + hmac.new(b"key123", body + old.encode(), hashlib.sha256).hexdigest())
    assert not c.verify_signature(body, "garbage")


def test_retell_outbound_call_dry_run_real_and_failure():
    from tests.conftest import make_settings
    e = Env(settings=make_settings(retell_dry_run=False))
    pid, did = e.to_outreach_sent()
    from src.database.models import Contact
    with e.session() as s:
        s.query(Contact).first().call_consent = True
        s.commit()
    import src.services.compliance as comp
    from datetime import datetime, timezone
    orig = comp.ComplianceService.can_call
    comp.ComplianceService.can_call = lambda self, c, co, now=None: orig(self, c, co, datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc))
    try:
        r = e.post(f"/calls/{did}").json()
        assert r["status"] == "started" and r["call_id"].startswith("call_fake")
        assert e.retell.calls[0]["vars"]["disclosure"].startswith("Hi, this is an AI assistant")
        assert r["opening_audio"]  # Chatterbox/TTS path exercised via TTSService (fake provider here)
        e.retell.fail = True
        assert e.post(f"/calls/{did}").status_code == 502
    finally:
        comp.ComplianceService.can_call = orig


def test_retell_websocket_drives_the_same_sales_workflow():
    e = Env()
    pid, did = e.to_outreach_sent()
    from src.database.models import Conversation
    with e.session() as s:
        s.add(Conversation(deal_id=did, channel="voice", external_id="call_ws"))
        s.commit()
    with pytest.raises(Exception):
        with e.client.websocket_connect("/retell/llm/unknown_call") as ws:
            ws.receive_json()
    with e.client.websocket_connect("/retell/llm/call_ws") as ws:
        first = ws.receive_json()
        assert "AI assistant" in first["content"]  # disclosure_mode=always
        ws.send_json({"interaction_type": "ping_pong", "timestamp": 1})
        assert ws.receive_json()["response_type"] == "ping_pong"
        ws.send_json({"interaction_type": "response_required", "response_id": 1,
                      "transcript": [{"role": "agent", "content": first["content"]}, {"role": "user", "content": "Are you a real person?"}]})
        r = ws.receive_json()
        assert r["response_id"] == 1 and "AI assistant" in r["content"] and r["content_complete"]
        ws.send_json({"interaction_type": "response_required", "response_id": 2,
                      "transcript": [{"role": "user", "content": "How much does it cost?"}]})
        assert "$" in ws.receive_json()["content"]
        ws.send_json({"interaction_type": "response_required", "response_id": 3,
                      "transcript": [{"role": "user", "content": "Please stop calling me and remove me."}]})
        last = ws.receive_json()
        assert last["end_call"] is True
    assert e.state(did) == "DO_NOT_CONTACT"


# ============================================================================ payments
def to_payment_pending(e: Env):
    pid, did = e.to_outreach_sent()
    e.say(did, "price?")
    r = e.say(did, "sounds good")
    path, token = r["proposal_url"].split("testserver")[1].split("?token=")
    acc = e.client.post(path.replace("/public", "/accept"), json={"token": token}).json()
    ref = e.get(f"/deals/{did}/history").json()["payments"][0]["provider_ref"]
    return did, ref, acc


def test_payment_requires_signed_webhook_and_ignores_unsigned_calls():
    e = Env()
    did, ref, acc = to_payment_pending(e)
    body, h = e.pay.simulate_payment(ref)
    assert e.client.post("/webhooks/payment", content=body, headers={"x-mock-signature": "0" * 64}).status_code == 400
    assert e.client.post("/webhooks/payment", content=body).status_code == 400
    assert e.client.post("/webhooks/payment", content=body + b" ", headers=h).status_code == 400  # body tampered
    assert e.state(did) == "PAYMENT_PENDING"
    assert e.delivery.specs == []


def test_duplicate_payment_webhook_is_idempotent():
    e = Env()
    did, ref, _ = to_payment_pending(e)
    body, h = e.pay.simulate_payment(ref, event_id="evt_dup")
    a = e.client.post("/webhooks/payment", content=body, headers=h).json()
    b = e.client.post("/webhooks/payment", content=body, headers=h).json()
    assert a["status"] == "paid" and b["status"] == "duplicate"
    body2, h2 = e.pay.simulate_payment(ref, event_id="evt_other")  # a *different* event id for the same payment
    assert e.client.post("/webhooks/payment", content=body2, headers=h2).json()["status"] == "already_paid"
    assert len(e.delivery.specs) == 1
    assert [p["status"] for p in e.get(f"/deals/{did}/history").json()["payments"]] == ["paid"]
    assert len(e.get(f"/deals/{did}/history").json()["delivery"]) == 1


def test_amount_mismatch_or_unconfirmed_by_provider_is_not_paid():
    e = Env()
    did, ref, _ = to_payment_pending(e)
    body, h = e.pay.simulate_payment(ref, amount_cents=100)  # webhook says $1.00
    assert e.client.post("/webhooks/payment", content=body, headers=h).json()["status"] == "mismatch"
    assert e.state(did) == "ESCALATED"
    assert any(x["reason_code"] == "PAYMENT_VERIFICATION_FAILED" for x in e.get("/escalations").json())
    # provider API disagrees with a (validly signed) webhook
    e2 = Env()
    did2, ref2, _ = to_payment_pending(e2)
    body, h = e2.pay.simulate_payment(ref2, mark_paid=False)
    r = e2.client.post("/webhooks/payment", content=body, headers=h).json()
    assert r["status"] == "mismatch" and "unpaid" in r["problems"][0]
    assert e2.delivery.specs == []


def test_failed_payment_event_does_not_mark_paid_and_unknown_payment_is_ignored():
    e = Env()
    did, ref, _ = to_payment_pending(e)
    body, h = e.pay.simulate_payment(ref, kind="payment.failed")
    assert e.client.post("/webhooks/payment", content=body, headers=h).json()["status"] == "failed"
    assert e.state(did) == "PAYMENT_PENDING"
    e.pay.requests["mock_ghost"] = {"payment_id": "nope", "amount_cents": 1, "currency": "usd"}
    body, h = e.pay.simulate_payment("mock_ghost")
    assert e.client.post("/webhooks/payment", content=body, headers=h).json()["status"] == "unknown_payment"


def test_payment_provider_outage_returns_5xx_and_event_is_retryable():
    e = Env()
    did, ref, _ = to_payment_pending(e)
    body, h = e.pay.simulate_payment(ref, event_id="evt_retry")
    orig = e.pay.fetch_status

    def down(r):
        raise PaymentError("network timeout")
    e.pay.fetch_status = down
    assert e.client.post("/webhooks/payment", content=body, headers=h).status_code == 503
    assert e.state(did) == "PAYMENT_PENDING"
    e.pay.fetch_status = orig  # provider retries the same event later
    assert e.client.post("/webhooks/payment", content=body, headers=h).json()["status"] == "paid"


def test_payment_request_is_idempotent_and_requires_acceptance():
    e = Env()
    pid, did = e.to_outreach_sent()
    assert e.post(f"/payments/{did}").status_code == 409  # nothing accepted yet
    did, ref, acc = to_payment_pending(Env())  # separate env: full flow
    e2 = Env()
    did2, ref2, acc2 = to_payment_pending(e2)
    again = e2.post(f"/payments/{did2}").json()
    assert again["provider_ref"] == ref2 and len(e2.get(f"/deals/{did2}/history").json()["payments"]) == 1


def test_stripe_signature_verification_and_event_mapping():
    p = StripePaymentProvider("sk_test_x", "whsec_x", "http://s", "http://c")
    payload = json.dumps({"id": "evt_1", "type": "checkout.session.completed", "data": {"object": {
        "id": "cs_1", "payment_status": "paid", "amount_total": 300000, "currency": "usd", "metadata": {"payment_id": "pay_1"}}}}).encode()
    ts = str(int(time.time()))

    def sig(t, secret="whsec_x", body=payload):
        return f"t={t},v1=" + hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    ev = p.parse_webhook(payload, {"Stripe-Signature": sig(ts)})
    assert (ev.kind, ev.payment_id, ev.amount_cents, ev.provider_ref) == ("succeeded", "pay_1", 300000, "cs_1")
    for bad in (sig(ts, "wrong"), sig(str(int(time.time()) - 4000)), "", "t=1"):
        with pytest.raises(InvalidSignature):
            p.parse_webhook(payload, {"stripe-signature": bad})
    unpaid = payload.replace(b'"paid"', b'"unpaid"')
    assert p.parse_webhook(unpaid, {"stripe-signature": sig(ts, body=unpaid)}).kind == "ignored"


def test_stripe_checkout_creation_is_one_time_and_idempotent_and_handles_timeouts():
    seen = {}

    def h(request: httpx.Request):
        seen["form"] = dict(httpx.QueryParams(request.content.decode()))
        seen["idem"] = request.headers["idempotency-key"]
        return httpx.Response(200, json={"id": "cs_9", "url": "https://checkout.stripe.com/c/pay/cs_9"})
    p = StripePaymentProvider("sk_test_x", "whsec_x", "http://s", "http://c", client=httpx.Client(transport=httpx.MockTransport(h)))
    r = p.create_payment_request(payment_id="pay_1", amount_cents=300000, currency="usd", description="d", idempotency_key="k1")
    assert r.provider_ref == "cs_9" and seen["form"]["mode"] == "payment" and seen["idem"] == "k1"
    assert seen["form"]["line_items[0][price_data][unit_amount]"] == "300000"
    assert not any("recurring" in k or "subscription" in k for k in seen["form"])

    def boom(request):
        raise httpx.ConnectTimeout("t", request=request)
    p2 = StripePaymentProvider("sk_test_x", "whsec_x", "http://s", "http://c", client=httpx.Client(transport=httpx.MockTransport(boom)))
    with pytest.raises(PaymentError):
        p2.create_payment_request(payment_id="p", amount_cents=1, currency="usd", description="d", idempotency_key="k")
    with pytest.raises(PaymentError):
        p2.fetch_status("cs_9")


# ============================================================================ delivery handoff
def test_delivery_failure_keeps_deal_paid_and_handoff_is_retryable():
    e = Env()
    e.delivery.fail = True
    did, ref, _ = to_payment_pending(e)
    body, h = e.pay.simulate_payment(ref)
    assert e.client.post("/webhooks/payment", content=body, headers=h).json()["status"] == "paid"
    assert e.state(did) == "PAID"  # payment is real even if downstream is down
    assert e.get(f"/deals/{did}/history").json()["delivery"][0]["status"] == "failed"
    e.delivery.fail = False
    assert e.post(f"/deals/{did}/handoff").json()["status"] == "handed_off"
    assert e.state(did) == "DELIVERY" and len(e.delivery.specs) == 1
    assert e.post(f"/deals/{did}/handoff").json()["status"] == "handed_off" and len(e.delivery.specs) == 1  # idempotent


def test_delivery_spec_refuses_without_verified_payment():
    from src.integrations.delivery.targets import DeliveryError
    from src.services.delivery import DeliveryService
    e = Env()
    pid, did = e.to_outreach_sent()
    with e.session() as s:
        from src.database.models import Deal
        with pytest.raises(DeliveryError):
            DeliveryService(s, e.delivery).build_spec(s.get(Deal, did))


# ============================================================================ API security
def test_admin_endpoints_require_key_and_fail_closed():
    e = Env()
    assert e.client.get("/deals").status_code == 401
    assert e.client.get("/deals", headers={"X-API-Key": "nope"}).status_code == 401
    assert e.client.get("/deals", headers={"Authorization": "Bearer test-admin"}).status_code == 200
    assert e.client.get("/health").json()["status"] == "ok"
    from tests.conftest import make_settings
    e2 = Env(settings=make_settings(admin_api_key=None))
    assert e2.client.get("/deals", headers={"X-API-Key": ""}).status_code == 503
    # the proposal-accept route is public but needs the secret token
    assert e.client.post("/proposals/xyz/accept", json={"token": "t"}).status_code == 404


def test_prospect_view_never_leaks_the_price_floor(env: Env):
    pid, did = env.to_solution_ready()
    text = env.get(f"/prospects/{pid}").text
    floor = env.get(f"/deals/{did}/history").json()["offer"]["min_price_cents"]
    assert '"min_price_cents":"hidden"' in text.replace(" ", "")
    assert env.get(f"/deals/{did}").json().get("min_price_cents") is None
