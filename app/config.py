"""
Application configuration loaded from environment variables / .env file.

Two config classes:
  - LLMConfig  — per-tier model names, provider names, and token budgets
  - Settings   — all other environment variables (includes nested LLMConfig)

Provider values:
  - "openai"  → uses the `openai` Python package (nano/mini/extraction/router tiers)
  - "bedrock" → uses AWS Bedrock via `boto3` (reasoning/escalation tiers)

A module-level `settings = Settings()` singleton is exported for use
throughout the application.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from typing import Literal, Optional


class LLMConfig(BaseSettings):
    """Per-tier LLM model names, provider names, and token budget constants."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Model names (overridable via env) ---
    router_model: str = "gpt-4.1-nano"
    extraction_model: str = "gpt-4.1-nano"
    conversation_model: str = "gpt-4.1-mini"
    reasoning_model: str = "anthropic.claude-sonnet-4-5-20251001-v1:0"   # Bedrock model ID
    escalation_model: str = "anthropic.claude-sonnet-4-5-20251001-v1:0"  # Bedrock model ID
    embedding_model: str = "text-embedding-3-small"

    # --- Provider per tier: "openai" or "bedrock" ---
    llm_provider_nano: Literal["openai"] = "openai"
    llm_provider_mini: Literal["openai"] = "openai"
    llm_provider_reasoning: Literal["bedrock"] = "bedrock"
    llm_provider_escalation: Literal["bedrock"] = "bedrock"

    # --- Token budgets per tier ---
    nano_max_tokens: int = 512
    mini_max_tokens: int = 1024
    reasoning_max_tokens: int = 4096
    escalation_max_tokens: int = 4096


class Settings(BaseSettings):
    """All application environment variables."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Bot mode ---
    # "polling" — long-poll Telegram (no public URL needed, ideal for dev/EC2)
    # "webhook" — Telegram pushes updates to HTTPS endpoint (requires domain + TLS)
    bot_mode: Literal["polling", "webhook"] = "polling"

    # --- Telegram ---
    telegram_bot_token: str
    # Only required in webhook mode. Optional so polling mode starts without it.
    telegram_webhook_secret: Optional[str] = None
    # Full HTTPS URL Telegram will POST updates to, e.g. https://example.com/webhook
    # Only required when BOT_MODE=webhook.
    telegram_webhook_url: Optional[str] = None

    # --- OpenAI ---
    openai_api_key: str

    # --- AWS (Bedrock + S3) ---
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = "us-east-1"
    aws_bedrock_region: str = "us-east-1"
    s3_bucket_summaries: str = "famosi-summaries"
    s3_bucket_backups: str = "famosi-backups"

    # --- Database ---
    database_url: str

    # --- Redis ---
    redis_url: str = ""

    # --- Payment — Razorpay (India) ---
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""

    # --- Payment — Stripe (USA) ---
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_id: str = ""

    # --- App ---
    job_secret: str = ""
    current_policy_version: str = "1.0"
    log_level: str = "INFO"

    # --- Admin ---
    # Your personal Telegram user ID (integer).
    # Find yours by messaging @userinfobot on Telegram.
    # Users with this ID get the full admin interface instead of the normal bot flow.
    admin_telegram_user_id: int = 0

    # --- Nested LLM config ---
    llm: LLMConfig = Field(default_factory=LLMConfig)

    @property
    def is_admin(self) -> bool:
        """Convenience helper — returns True when admin_telegram_user_id is non-zero."""
        return self.admin_telegram_user_id != 0


settings = Settings()
