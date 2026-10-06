from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

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
from src.models.schemas import RawCandidate, SourceDoc
from src.services.container import Services
from src.services.llm import LLMService
from src.services.prompts import PromptManager
from src.services.tts import TTSService

SITE = "https://sunrisepm.example"
DOCS = [
    SourceDoc(url=SITE + "/", text=(
        "Sunrise Property Management manages 450 residential units across Austin, Texas. "
        "Call us at (512) 555-0100 to ask about availability and pricing. "
        "Schedule a tour online or book a showing with our leasing team. "
        "We are hiring a leasing agent to handle resident and prospect inquiries.")),
    SourceDoc(url=SITE + "/contact", text="Contact our leasing office for inquiries. Our team replies to every email personally."),
]
CANDIDATE = RawCandidate(
    company_name="Sunrise Property Management", website=SITE, location="Austin, TX", employee_estimate=35,
    text="Sunrise Property Management is a residential property management firm.", email="info@sunrisepm.example",
    phone="+15125550100", source="directory:test",
)


def make_settings(**kw) -> Settings:
    base = dict(
        database_url="sqlite://", admin_api_key="test-admin", llm_provider="mock", tts_provider="null",
        sender_name="Alex", sender_company="Acme AI", sender_email="alex@acme.example",
        sender_postal_address="1 Main St, Austin, TX 78701", outreach_dry_run=True, retell_dry_run=True,
        call_require_consent=True, mock_payment_webhook_secret="whsec-test", public_base_url="http://testserver",
        default_min_price=1000, default_target_price=3000, default_max_discount_percent=15, max_step_retries=3,
    )
    base.update(kw)
    return Settings(**base)


class Env:
    """Bundles everything a test needs."""

    def __init__(self, llm: MockLLMProvider | None = None, settings: Settings | None = None, fetcher=None):
        self.settings = settings or make_settings()
        self.engine = make_engine(self.settings.database_url)
        init_db(self.engine)
        self.factory = make_session_factory(self.engine)
        self.llm_provider = llm or MockLLMProvider()
        self.tts_provider = FakeTTSProvider()
        self.email = RecordingEmailProvider()
        self.retell = FakeRetellClient()
        self.pay = MockPaymentProvider(self.settings.mock_payment_webhook_secret.get_secret_value(), "http://testserver")
        self.delivery = RecordingDeliveryTarget()
        self.svc = Services(
            settings=self.settings, llm=LLMService(self.llm_provider, max_retries=2, base_backoff=0, sleep=lambda s: None),
            prompts=PromptManager(), tts=TTSService(self.tts_provider, "/tmp/claude-0/tts-test", 5),
            fetcher=fetcher or StaticWebFetcher({SITE: DOCS}), email=self.email,
            website_contact=ManualTaskWebsiteContactProvider(), retell=self.retell, payment_provider=self.pay,
            delivery_target=self.delivery,
        )
        self.app = create_app(self.settings, self.svc, self.factory)
        self.client = TestClient(self.app)
        self.h = {"X-API-Key": "test-admin"}

    def session(self):
        return self.factory()

    # convenience API helpers
    def post(self, path, **kw):
        return self.client.post(path, headers=self.h, **kw)

    def get(self, path, **kw):
        return self.client.get(path, headers=self.h, **kw)

    def seed_market(self) -> str:
        r = self.post("/markets", json={"business_goal": "Sell one-time AI agents to property managers"})
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def seed_prospect(self, market_id: str | None = None, **over) -> tuple[str, str]:
        body = {**CANDIDATE.model_dump(), **over}
        if market_id:
            body["market_profile_id"] = market_id
        r = self.post("/prospects", json=body)
        assert r.status_code == 201, r.text
        j = r.json()
        return j["prospect"]["id"], j["deal"]["id"]

    def to_solution_ready(self) -> tuple[str, str]:
        m = self.seed_market()
        pid, did = self.seed_prospect(m)
        r = self.post(f"/pipeline/{did}/run")
        assert r.status_code == 200, r.text
        assert self.get(f"/deals/{did}").json()["state"] == "SOLUTION_READY", r.json()
        return pid, did

    def to_outreach_sent(self) -> tuple[str, str]:
        pid, did = self.to_solution_ready()
        r = self.post(f"/outreach/{did}")
        assert r.status_code == 200, r.text
        return pid, did

    def say(self, did: str, text: str, channel: str = "email") -> dict:
        r = self.post(f"/conversations/{did}/messages", json={"text": text, "channel": channel})
        assert r.status_code == 200, r.text
        return r.json()

    def state(self, did: str) -> str:
        return self.get(f"/deals/{did}").json()["state"]


@pytest.fixture
def env():
    return Env()


def llm_with(**handlers) -> MockLLMProvider:
    return MockLLMProvider(handlers)
