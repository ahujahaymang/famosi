"""
Unit tests for app/core/intent_router.py

Tests every classification path with realistic human messages:
  - LOGGING intents (meals, symptoms, exercise, medications, weight, water, questions)
  - PERSONAL_DATA_QUERY intents
  - KNOWLEDGE_QUESTION intents
  - MIXED_QUERY intents
  - UNCLASSIFIED on bad LLM JSON
  - Escalation triggers (danger keywords, low confidence, high severity)

Requirements: 14.1, 14.2, 14.8
"""
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.intent_router import IntentRouter, RouteResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_llm_client(intent: str, confidence: float) -> MagicMock:
    """Return a mock LLMClient whose complete() returns the given intent JSON."""
    client = MagicMock()
    response = MagicMock()
    response.content = json.dumps({"intent": intent, "confidence": confidence})
    response.model = "gpt-4.1-nano"
    response.tokens_used = 50
    client.complete = AsyncMock(return_value=response)
    return client


def _make_broken_llm_client() -> MagicMock:
    """Return a mock LLMClient that raises on complete()."""
    client = MagicMock()
    client.complete = AsyncMock(side_effect=RuntimeError("LLM unavailable"))
    return client


def _make_bad_json_client() -> MagicMock:
    """Return a mock LLMClient that returns malformed JSON."""
    client = MagicMock()
    response = MagicMock()
    response.content = "not json at all"
    client.complete = AsyncMock(return_value=response)
    return client


# ---------------------------------------------------------------------------
# Basic intent classification
# ---------------------------------------------------------------------------

class TestLoggingIntents:
    """Human messages that should classify as LOGGING."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("message", [
        "I had oatmeal with banana and honey for breakfast",
        "Just ate rice and dal for lunch, felt pretty good",
        "Had a glass of warm milk before bed",
        "Took my prenatal vitamin this morning",
        "Iron tablet 65mg taken after dinner",
        "Walked for 30 minutes in the park today",
        "I weigh 68.5 kg this morning",
        "Drank about 2 litres of water today",
        "Feeling a bit nauseous this morning, maybe a 4 out of 10",
        "Had mild back pain after sitting for too long, severity 3",
        "I want to ask my doctor about the anomaly scan next week",
        "I don't eat meat, I'm vegetarian",
    ])
    async def test_logging_messages(self, message: str):
        client = _make_llm_client("LOGGING", 0.95)
        router = IntentRouter(client)
        result = await router.route(message)
        assert result.intent == "LOGGING"
        assert result.tier in ("nano", "escalation")


class TestPersonalDataQueryIntents:
    """Human messages that should classify as PERSONAL_DATA_QUERY."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("message", [
        "What did I eat yesterday?",
        "Show me my symptoms from this week",
        "How much water have I logged today?",
        "What medications have I taken this month?",
        "What was my weight last Monday?",
        "Did I exercise at all this week?",
        "What questions did I save for my doctor?",
        "Show me everything I logged yesterday",
    ])
    async def test_personal_data_query_messages(self, message: str):
        client = _make_llm_client("PERSONAL_DATA_QUERY", 0.92)
        router = IntentRouter(client)
        result = await router.route(message)
        assert result.intent == "PERSONAL_DATA_QUERY"
        assert result.tier in ("mini", "escalation")


class TestKnowledgeQuestionIntents:
    """Human messages that should classify as KNOWLEDGE_QUESTION."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("message", [
        "Is it safe to eat sushi during pregnancy?",
        "What should I expect at week 20?",
        "Can I exercise in the third trimester?",
        "Is it normal to feel so tired at 8 weeks?",
        "What foods are high in folic acid?",
        "How much iron do I need per day when pregnant?",
        "What happens at a 20-week anatomy scan?",
        "Is headache normal in first trimester?",
    ])
    async def test_knowledge_question_messages(self, message: str):
        client = _make_llm_client("KNOWLEDGE_QUESTION", 0.90)
        router = IntentRouter(client)
        result = await router.route(message)
        assert result.intent == "KNOWLEDGE_QUESTION"
        assert result.tier in ("reasoning", "escalation")


class TestMixedQueryIntents:
    """Messages requiring both personal data and knowledge."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("message", [
        "Am I getting enough iron based on what I've been eating?",
        "Is my symptom pattern this week anything to worry about?",
        "Based on my diet, what nutrients am I missing?",
    ])
    async def test_mixed_query_messages(self, message: str):
        client = _make_llm_client("MIXED_QUERY", 0.88)
        router = IntentRouter(client)
        result = await router.route(message)
        assert result.intent == "MIXED_QUERY"
        assert result.tier in ("reasoning", "escalation")


# ---------------------------------------------------------------------------
# UNCLASSIFIED handling
# ---------------------------------------------------------------------------

class TestUnclassifiedHandling:

    @pytest.mark.asyncio
    async def test_bad_json_returns_unclassified(self):
        client = _make_bad_json_client()
        router = IntentRouter(client)
        result = await router.route("some message")
        assert result.intent == "UNCLASSIFIED"
        assert result.confidence is None

    @pytest.mark.asyncio
    async def test_llm_exception_returns_unclassified(self):
        client = _make_broken_llm_client()
        router = IntentRouter(client)
        result = await router.route("some message")
        assert result.intent == "UNCLASSIFIED"

    @pytest.mark.asyncio
    async def test_unknown_intent_label_returns_unclassified(self):
        """LLM returning an unknown label must yield UNCLASSIFIED."""
        client = MagicMock()
        response = MagicMock()
        response.content = json.dumps({"intent": "UNKNOWN_LABEL", "confidence": 0.9})
        client.complete = AsyncMock(return_value=response)
        router = IntentRouter(client)
        result = await router.route("some message")
        assert result.intent == "UNCLASSIFIED"

    @pytest.mark.asyncio
    async def test_unclassified_is_never_escalated(self):
        """Unclassified messages must not be escalated — no pipeline is invoked."""
        client = _make_bad_json_client()
        router = IntentRouter(client)
        result = await router.route("bleeding and seizure")  # would escalate if classified
        assert result.intent == "UNCLASSIFIED"
        assert result.escalated is False


# ---------------------------------------------------------------------------
# Escalation triggers
# ---------------------------------------------------------------------------

class TestEscalationTriggers:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("danger_word", [
        "bleeding",
        "preeclampsia",
        "contractions",
        "seizure",
        "chest pain",
    ])
    async def test_danger_keyword_triggers_escalation(self, danger_word: str):
        client = _make_llm_client("KNOWLEDGE_QUESTION", 0.95)
        router = IntentRouter(client)
        result = await router.route(f"I am experiencing {danger_word}")
        assert result.escalated is True
        assert result.tier == "escalation"
        assert danger_word in result.escalation_reason

    @pytest.mark.asyncio
    async def test_low_confidence_triggers_escalation(self):
        client = _make_llm_client("LOGGING", 0.45)  # below 0.6 threshold
        router = IntentRouter(client)
        result = await router.route("something ambiguous")
        assert result.escalated is True
        assert result.tier == "escalation"
        assert "low_confidence" in result.escalation_reason

    @pytest.mark.asyncio
    async def test_high_severity_knowledge_question_escalates(self):
        client = _make_llm_client("KNOWLEDGE_QUESTION", 0.9)
        router = IntentRouter(client)
        result = await router.route("Is this normal?", symptom_severity=9)
        assert result.escalated is True
        assert result.tier == "escalation"

    @pytest.mark.asyncio
    async def test_high_severity_logging_does_not_escalate(self):
        """Severity escalation only applies to KNOWLEDGE_QUESTION, not LOGGING."""
        client = _make_llm_client("LOGGING", 0.9)
        router = IntentRouter(client)
        result = await router.route("Severe cramps", symptom_severity=9)
        assert result.escalated is False

    @pytest.mark.asyncio
    async def test_normal_confidence_no_escalation(self):
        client = _make_llm_client("LOGGING", 0.85)
        router = IntentRouter(client)
        result = await router.route("I ate rice for lunch")
        assert result.escalated is False
        assert result.tier == "nano"

    @pytest.mark.asyncio
    async def test_confidence_exactly_at_threshold_no_escalation(self):
        """Confidence of exactly 0.6 must NOT escalate."""
        client = _make_llm_client("LOGGING", 0.6)
        router = IntentRouter(client)
        result = await router.route("I had some food")
        assert result.escalated is False

    @pytest.mark.asyncio
    async def test_confidence_just_below_threshold_escalates(self):
        """Confidence of 0.59 must escalate."""
        client = _make_llm_client("LOGGING", 0.59)
        router = IntentRouter(client)
        result = await router.route("something")
        assert result.escalated is True


# ---------------------------------------------------------------------------
# Tier routing
# ---------------------------------------------------------------------------

class TestTierRouting:

    @pytest.mark.asyncio
    async def test_logging_routes_to_nano(self):
        client = _make_llm_client("LOGGING", 0.9)
        router = IntentRouter(client)
        result = await router.route("I ate lunch")
        assert result.tier == "nano"

    @pytest.mark.asyncio
    async def test_personal_data_query_routes_to_mini(self):
        client = _make_llm_client("PERSONAL_DATA_QUERY", 0.9)
        router = IntentRouter(client)
        result = await router.route("What did I eat?")
        assert result.tier == "mini"

    @pytest.mark.asyncio
    async def test_knowledge_question_routes_to_reasoning(self):
        client = _make_llm_client("KNOWLEDGE_QUESTION", 0.9)
        router = IntentRouter(client)
        result = await router.route("Is sushi safe?")
        assert result.tier == "reasoning"

    @pytest.mark.asyncio
    async def test_mixed_query_routes_to_reasoning(self):
        client = _make_llm_client("MIXED_QUERY", 0.9)
        router = IntentRouter(client)
        result = await router.route("Am I eating enough iron?")
        assert result.tier == "reasoning"
