"""Environment-driven configuration. No secrets live in the repository."""
from __future__ import annotations

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "development"
    database_url: str = "sqlite:///./salesagent.db"
    admin_api_key: SecretStr | None = None

    # LLM
    llm_provider: str = "groq"
    groq_api_key: SecretStr | None = None
    groq_model: str = "llama-3.3-70b-versatile"
    groq_base_url: str = "https://api.groq.com/openai/v1"
    llm_timeout_seconds: float = 45.0
    llm_max_retries: int = 3
    groq_input_cost_per_mtok: float = 0.0
    groq_output_cost_per_mtok: float = 0.0

    # Pricing (whole USD)
    default_min_price: int = 1000
    default_target_price: int = 3000
    default_max_price: int = 25000
    default_max_discount_percent: float = 15.0
    pricing_hourly_rate: int = 100

    # Outreach / compliance
    outreach_dry_run: bool = True
    auto_outreach: bool = False
    sender_name: str = ""
    sender_company: str = ""
    sender_email: str = ""
    sender_postal_address: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: SecretStr | None = None

    # Retell / calls
    retell_api_key: SecretStr | None = None
    retell_agent_id: str = ""
    retell_from_number: str = ""
    retell_dry_run: bool = True
    call_disclosure_mode: str = "always"
    call_require_consent: bool = True
    call_window_start_hour: int = 9
    call_window_end_hour: int = 17
    default_timezone: str = "America/New_York"

    # TTS
    tts_provider: str = "chatterbox"
    chatterbox_mode: str = "local"
    chatterbox_url: str = ""
    chatterbox_device: str = "cuda"
    chatterbox_voice_prompt_path: str = ""
    chatterbox_exaggeration: float = 0.5
    chatterbox_cfg_weight: float = 0.5
    tts_output_dir: str = "./data/audio"
    tts_timeout_seconds: float = 60.0

    # Payments
    payment_provider: str = "mock"
    stripe_secret_key: SecretStr | None = None
    stripe_webhook_secret: SecretStr | None = None
    mock_payment_webhook_secret: SecretStr = SecretStr("change-me")
    payment_currency: str = "usd"
    public_base_url: str = "http://localhost:8000"

    # Delivery
    delivery_target: str = "file"
    delivery_outbox_dir: str = "./data/delivery"
    orchestrator_url: str = ""
    orchestrator_token: SecretStr | None = None

    # Pipeline
    max_step_retries: int = 3


@lru_cache
def get_settings() -> Settings:
    return Settings()
