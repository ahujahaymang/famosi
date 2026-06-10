"""
LLM client abstraction layer.

Dispatches completion requests to the correct backend (OpenAI or Bedrock/Anthropic)
based on the tier configuration in LLMConfig.

Supported providers (set via LLM_PROVIDER_* env vars):
  - "openai"    → openai async client  (nano, mini tiers)
  - "bedrock"   → AWS Bedrock via boto3 (reasoning, escalation tiers)
  - "anthropic" → direct Anthropic async client (optional, for non-Bedrock deployments)

Usage:
    client = LLMClient()
    response = await client.complete("nano", messages)
    response = await client.complete("reasoning", messages)

Requirements: 14.1, 8.1
"""

from __future__ import annotations

import json
import asyncio
from dataclasses import dataclass
from typing import Literal

import structlog

from app.config import settings

logger = structlog.get_logger(__name__)

# Tier names supported by the client
TierName = Literal["nano", "mini", "reasoning", "escalation"]


@dataclass
class LLMResponse:
    """Structured response returned by every LLMClient completion call."""

    content: str
    model: str
    tokens_used: int


class LLMClient:
    """
    Unified LLM client that routes completion calls to the correct backend
    based on the provider configured for each model tier.

    Thread-safe: the OpenAI and Anthropic async clients are created lazily
    and reused across requests.
    """

    def __init__(self) -> None:
        self._openai_client = None
        self._anthropic_client = None
        self._bedrock_client = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def complete(
        self,
        tier: TierName,
        messages: list[dict],
        response_format: dict | None = None,
    ) -> LLMResponse:
        """
        Send a chat completion request for the given tier.

        Args:
            tier: One of "nano", "mini", "reasoning", or "escalation".
            messages: OpenAI-style list of {"role": ..., "content": ...} dicts.
            response_format: Optional format hint (e.g. {"type": "json_object"}).
                             Only applied when the backend supports it (OpenAI).

        Returns:
            LLMResponse with the assistant's content, resolved model name,
            and total tokens used.

        Raises:
            ValueError: If the tier is unknown or the provider is unsupported.
            Various provider SDK exceptions on API errors.
        """
        model, provider = self._resolve(tier)
        max_tokens = self._max_tokens(tier)

        log = logger.bind(tier=tier, model=model, provider=provider)

        if provider == "openai":
            log.debug("dispatching_to_openai")
            return await self._openai_complete(
                model, messages, response_format, max_tokens
            )
        elif provider in ("anthropic", "bedrock"):
            log.debug("dispatching_to_anthropic_backend", via=provider)
            return await self._anthropic_complete(
                model, messages, max_tokens, via_bedrock=(provider == "bedrock")
            )
        else:
            raise ValueError(
                f"Unsupported LLM provider '{provider}' for tier '{tier}'. "
                "Valid values: 'openai', 'anthropic', 'bedrock'."
            )

    # ------------------------------------------------------------------
    # Tier resolution helpers
    # ------------------------------------------------------------------

    def _resolve(self, tier: TierName) -> tuple[str, str]:
        """
        Map a tier name to (model_name, provider) from LLMConfig.

        Returns:
            A (model_name, provider) tuple.

        Raises:
            ValueError: If the tier name is not recognised.
        """
        cfg = settings.llm

        tier_map: dict[str, tuple[str, str]] = {
            "nano": (cfg.router_model, cfg.llm_provider_nano),
            "mini": (cfg.conversation_model, cfg.llm_provider_mini),
            "reasoning": (cfg.reasoning_model, cfg.llm_provider_reasoning),
            "escalation": (cfg.escalation_model, cfg.llm_provider_escalation),
        }

        if tier not in tier_map:
            raise ValueError(
                f"Unknown LLM tier '{tier}'. "
                f"Valid tiers: {', '.join(tier_map)}."
            )

        return tier_map[tier]

    def _max_tokens(self, tier: TierName) -> int:
        """Return the token budget for the given tier from LLMConfig."""
        cfg = settings.llm
        budget_map: dict[str, int] = {
            "nano": cfg.nano_max_tokens,
            "mini": cfg.mini_max_tokens,
            "reasoning": cfg.reasoning_max_tokens,
            "escalation": cfg.escalation_max_tokens,
        }
        return budget_map[tier]

    # ------------------------------------------------------------------
    # OpenAI backend
    # ------------------------------------------------------------------

    def _get_openai_client(self):
        """Lazily initialise and return the shared async OpenAI client."""
        if self._openai_client is None:
            import openai  # imported lazily to keep startup fast when unused

            self._openai_client = openai.AsyncOpenAI(
                api_key=settings.openai_api_key
            )
        return self._openai_client

    async def _openai_complete(
        self,
        model: str,
        messages: list[dict],
        response_format: dict | None,
        max_tokens: int,
    ) -> LLMResponse:
        """
        Call the OpenAI chat completions API.

        Supports response_format for JSON mode ({"type": "json_object"}).
        Enforces the tier's max_tokens budget.
        """
        client = self._get_openai_client()

        kwargs: dict = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format

        response = await client.chat.completions.create(**kwargs)

        content = response.choices[0].message.content or ""
        tokens_used = response.usage.total_tokens if response.usage else 0

        return LLMResponse(
            content=content,
            model=response.model,
            tokens_used=tokens_used,
        )

    # ------------------------------------------------------------------
    # Anthropic / Bedrock backend
    # ------------------------------------------------------------------

    def _get_anthropic_client(self):
        """Lazily initialise and return the shared async Anthropic client."""
        if self._anthropic_client is None:
            import anthropic  # optional dependency; only required for direct API use

            self._anthropic_client = anthropic.AsyncAnthropic(
                api_key=getattr(settings, "anthropic_api_key", None)
            )
        return self._anthropic_client

    def _get_bedrock_client(self):
        """Lazily initialise and return the boto3 bedrock-runtime client."""
        if self._bedrock_client is None:
            import boto3

            self._bedrock_client = boto3.client(
                "bedrock-runtime",
                region_name=settings.aws_bedrock_region,
                aws_access_key_id=settings.aws_access_key_id or None,
                aws_secret_access_key=settings.aws_secret_access_key or None,
            )
        return self._bedrock_client

    async def _anthropic_complete(
        self,
        model: str,
        messages: list[dict],
        max_tokens: int,
        *,
        via_bedrock: bool = False,
    ) -> LLMResponse:
        """
        Call the Anthropic API either directly or through AWS Bedrock.

        The Anthropic Messages API separates the system prompt from the
        conversation turns. This method extracts any leading system message
        from the messages list automatically.

        Args:
            model: Model name or Bedrock model ID.
            messages: OpenAI-style messages list. System messages are extracted.
            max_tokens: Hard token ceiling enforced by the provider.
            via_bedrock: When True, routes through AWS Bedrock instead of the
                         direct Anthropic API.
        """
        # Separate system prompt from user/assistant turns
        system_prompt: str | None = None
        conversation: list[dict] = []

        for msg in messages:
            if msg.get("role") == "system":
                # Concatenate multiple system messages if present
                if system_prompt is None:
                    system_prompt = msg["content"]
                else:
                    system_prompt += "\n\n" + msg["content"]
            else:
                conversation.append(msg)

        if via_bedrock:
            return await self._bedrock_invoke(
                model, conversation, system_prompt, max_tokens
            )
        else:
            return await self._direct_anthropic_invoke(
                model, conversation, system_prompt, max_tokens
            )

    async def _direct_anthropic_invoke(
        self,
        model: str,
        messages: list[dict],
        system_prompt: str | None,
        max_tokens: int,
    ) -> LLMResponse:
        """Direct Anthropic API call using the anthropic async client."""
        client = self._get_anthropic_client()

        kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system_prompt:
            kwargs["system"] = system_prompt

        response = await client.messages.create(**kwargs)

        content = response.content[0].text if response.content else ""
        tokens_used = (
            response.usage.input_tokens + response.usage.output_tokens
            if response.usage
            else 0
        )

        return LLMResponse(
            content=content,
            model=response.model,
            tokens_used=tokens_used,
        )

    async def _bedrock_invoke(
        self,
        model: str,
        messages: list[dict],
        system_prompt: str | None,
        max_tokens: int,
    ) -> LLMResponse:
        """
        Call Anthropic Claude through AWS Bedrock using the Converse API.

        boto3 is synchronous; we run it in a thread pool executor so it
        doesn't block the event loop.
        """
        bedrock = self._get_bedrock_client()

        # Build the Converse API request body
        converse_messages = [
            {"role": msg["role"], "content": [{"text": msg["content"]}]}
            for msg in messages
        ]

        kwargs: dict = {
            "modelId": model,
            "messages": converse_messages,
            "inferenceConfig": {"maxTokens": max_tokens},
        }
        if system_prompt:
            kwargs["system"] = [{"text": system_prompt}]

        # Run the blocking boto3 call in a thread pool
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: bedrock.converse(**kwargs),
        )

        content = (
            response["output"]["message"]["content"][0].get("text", "")
            if response.get("output", {}).get("message", {}).get("content")
            else ""
        )
        usage = response.get("usage", {})
        tokens_used = usage.get("inputTokens", 0) + usage.get("outputTokens", 0)
        model_used = response.get("modelId", model)

        return LLMResponse(
            content=content,
            model=model_used,
            tokens_used=tokens_used,
        )
