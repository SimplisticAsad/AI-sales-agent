"""Composition root: builds concrete providers from settings. Everything else depends on interfaces."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.config.settings import Settings
from src.services.llm import LLMService
from src.services.prompts import PromptManager
from src.services.tts import NullTTSProvider, TTSService, VoiceSettings


@dataclass
class Services:
    settings: Settings
    llm: LLMService
    prompts: PromptManager
    tts: TTSService
    fetcher: Any
    email: Any
    website_contact: Any
    retell: Any
    payment_provider: Any
    delivery_target: Any
    prospect_sources: dict[str, Any] = field(default_factory=dict)


def build_services(settings: Settings) -> Services:
    from src.integrations.delivery.targets import FileDeliveryTarget, HttpDeliveryTarget
    from src.integrations.outreach.providers import (
        ManualTaskWebsiteContactProvider, RecordingEmailProvider, SmtpEmailProvider,
    )
    from src.integrations.payments.mock import MockPaymentProvider
    from src.integrations.research.web import HttpWebFetcher

    # ---- LLM ----
    if settings.llm_provider == "mock":
        from src.integrations.mock.llm import MockLLMProvider

        provider = MockLLMProvider()
    else:
        from src.integrations.groq.provider import GroqProvider

        provider = GroqProvider(
            settings.groq_api_key.get_secret_value() if settings.groq_api_key else "", settings.groq_model,
            settings.groq_base_url, settings.llm_timeout_seconds, settings.groq_input_cost_per_mtok,
            settings.groq_output_cost_per_mtok,
        )
    llm = LLMService(provider, max_retries=settings.llm_max_retries)

    # ---- TTS ----
    if settings.tts_provider == "chatterbox":
        from src.integrations.chatterbox.provider import ChatterboxProvider

        tts_provider = ChatterboxProvider(settings.chatterbox_mode, settings.chatterbox_device, settings.chatterbox_url,
                                          settings.chatterbox_voice_prompt_path, settings.tts_timeout_seconds)
    else:
        tts_provider = NullTTSProvider()
    tts = TTSService(tts_provider, settings.tts_output_dir, settings.tts_timeout_seconds,
                     VoiceSettings(exaggeration=settings.chatterbox_exaggeration, cfg_weight=settings.chatterbox_cfg_weight))

    # ---- email: real SMTP only if configured; otherwise a recording stub (dry-run default keeps it unused) ----
    email = SmtpEmailProvider(settings.smtp_host, settings.smtp_port, settings.smtp_username,
                              settings.smtp_password.get_secret_value() if settings.smtp_password else "") \
        if settings.smtp_host else RecordingEmailProvider()

    # ---- Retell ----
    retell: Any = None
    if settings.retell_api_key:
        from src.integrations.retell.client import RetellClient

        retell = RetellClient(settings.retell_api_key.get_secret_value())

    # ---- payments ----
    if settings.payment_provider == "stripe":
        from src.integrations.payments.stripe import StripePaymentProvider

        base = settings.public_base_url.rstrip("/")
        pay = StripePaymentProvider(
            settings.stripe_secret_key.get_secret_value() if settings.stripe_secret_key else "",
            settings.stripe_webhook_secret.get_secret_value() if settings.stripe_webhook_secret else "",
            f"{base}/payment/success", f"{base}/payment/cancelled",
        )
    else:
        pay = MockPaymentProvider(settings.mock_payment_webhook_secret.get_secret_value(), settings.public_base_url)

    delivery = HttpDeliveryTarget(settings.orchestrator_url,
                                  settings.orchestrator_token.get_secret_value() if settings.orchestrator_token else None) \
        if settings.delivery_target == "http" else FileDeliveryTarget(settings.delivery_outbox_dir)

    return Services(settings, llm, PromptManager(), tts, HttpWebFetcher(), email, ManualTaskWebsiteContactProvider(),
                    retell, pay, delivery)
