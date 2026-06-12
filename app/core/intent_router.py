"""
Intent Router — 4-way message classification using GPT-4.1 Nano.

Classifies every incoming user message into one of:
  - LOGGING            : user is recording health data
  - PERSONAL_DATA_QUERY: user is asking about their own logged records
  - KNOWLEDGE_QUESTION : user is asking a factual / medical pregnancy question
  - MIXED_QUERY        : requires both personal data and knowledge to answer
  - UNCLASSIFIED       : LLM response was malformed or intent was not recognised

After classification the router evaluates escalation criteria and may override
the routing tier to "escalation" when any of the following apply:
  - classification confidence < 0.6
  - the message contains a known danger keyword
  - a KNOWLEDGE_QUESTION is accompanied by a reported symptom severity ≥ 9

Requirements: 14.1, 14.2, 14.8
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal

import structlog

from app.core.llm_client import LLMClient

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

IntentLabel = Literal[
    "LOGGING",
    "PERSONAL_DATA_QUERY",
    "KNOWLEDGE_QUESTION",
    "MIXED_QUERY",
    "UNCLASSIFIED",
]

RoutingTier = Literal["nano", "mini", "reasoning", "escalation"]

_VALID_INTENTS: frozenset[str] = frozenset(
    {"LOGGING", "PERSONAL_DATA_QUERY", "KNOWLEDGE_QUESTION", "MIXED_QUERY"}
)

# Keywords whose presence in the user message triggers immediate escalation,
# regardless of intent or confidence.
_ESCALATION_KEYWORDS: tuple[str, ...] = (
    "bleeding",
    "preeclampsia",
    "contractions",
    "seizure",
    "chest pain",
)

# Confidence threshold below which we escalate instead of using standard routing.
_CONFIDENCE_THRESHOLD: float = 0.6

# Severity level at or above which a KNOWLEDGE_QUESTION is escalated.
_SEVERITY_ESCALATION_THRESHOLD: int = 9


@dataclass
class RouteResult:
    """
    The outcome of a single intent classification and routing decision.

    Attributes:
        intent:      Classified intent label (or UNCLASSIFIED).
        confidence:  Model's reported confidence (0.0–1.0).  None when the LLM
                     response could not be parsed.
        tier:        Resolved routing tier for downstream handler selection.
        escalated:   True when escalation criteria overrode the default tier.
        escalation_reason: Human-readable explanation when escalated is True.
    """

    intent: IntentLabel
    confidence: float | None
    tier: RoutingTier
    escalated: bool = False
    escalation_reason: str = ""


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are an intent classifier for a pregnancy assistant chatbot.

Classify the user message into EXACTLY one of the following intents:
- LOGGING           : The user is recording or scheduling something new — a meal eaten,
                      symptom experienced, exercise done, medication taken, weight measured,
                      water drunk, a question to ask their doctor, OR scheduling/creating
                      an appointment or reminder.
                      Examples: "I had oatmeal", "mild nausea today", "appointment tomorrow
                      at 10am", "remind me to take iron at 9am", "I feel exhausted today",
                      "ask my doctor about magnesium", "ask doctor if travel is safe",
                      "note for my doctor: check blood pressure".
                      IMPORTANT: "Ask doctor about X" or "Ask my doctor about X" means the
                      user wants to SAVE a question for their doctor visit — this is LOGGING.
                      But "What questions do I have for my doctor?" or "Show me my doctor
                      questions" is a PERSONAL_DATA_QUERY — retrieving saved questions.
- PERSONAL_DATA_QUERY: The user is asking about data they have previously logged
                       (e.g., "what did I eat yesterday?", "show my symptoms this week",
                       "what appointments do I have coming up?").
- KNOWLEDGE_QUESTION: The user is asking for advice, suggestions, recommendations, or
                      medical/factual pregnancy information. This includes:
                      - Safety questions: "is sushi safe?", "can I drink coffee?"
                      - Suggestion requests: "suggest breakfast", "what should I eat?"
                      - Craving questions: "I'm craving chocolate, is that okay?"
                      - Explanation requests: "why am I so tired?", "what's happening this week?"
                      - Development questions: "how big is my baby?"
                      - General pregnancy guidance of any kind.
- MIXED_QUERY       : Answering requires BOTH personal logged data AND general knowledge.
                      Example: "Am I eating enough iron?" needs meal history + nutrition facts.

Key distinction — LOGGING vs KNOWLEDGE_QUESTION:
- "I had oatmeal for breakfast" → LOGGING (reporting what happened)
- "Suggest something for breakfast" → KNOWLEDGE_QUESTION (asking for advice)
- "I'm craving chocolate" → KNOWLEDGE_QUESTION (seeking guidance on a craving)
- "I feel exhausted today" → LOGGING (reporting a symptom)
- "Why am I so tired?" → KNOWLEDGE_QUESTION (asking for explanation)

Rules:
1. You MUST respond with a JSON object and nothing else.
2. The JSON object MUST contain exactly two keys: "intent" and "confidence".
3. "intent" MUST be one of the four labels above, in UPPERCASE.
4. "confidence" MUST be a number between 0.0 and 1.0.

Example response:
{"intent": "LOGGING", "confidence": 0.95}
"""


# ---------------------------------------------------------------------------
# IntentRouter
# ---------------------------------------------------------------------------

class IntentRouter:
    """
    Classifies a user message into an intent category and resolves the
    appropriate LLM routing tier, escalating when safety criteria are met.

    Usage::

        router = IntentRouter(llm_client)
        result = await router.route(message, symptom_severity=None)
        if result.intent == "UNCLASSIFIED":
            # return error to user — do not invoke any pipeline (Req 14.8)
            ...
    """

    def __init__(self, llm_client: LLMClient) -> None:
        self._llm = llm_client

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def route(
        self,
        message: str,
        *,
        symptom_severity: int | None = None,
    ) -> RouteResult:
        """
        Classify *message* and determine the routing tier.

        Args:
            message:          The raw user message text.
            symptom_severity: If the caller has already extracted a symptom
                              severity from the message, pass it here so the
                              escalation check can apply the severity rule.
                              Accepts 1–10 (per Req 8.1).

        Returns:
            A :class:`RouteResult` with intent, confidence, and resolved tier.
        """
        log = logger.bind(message_len=len(message))

        # Step 1 — call the LLM for classification
        intent, confidence = await self._classify(message, log)

        # Step 2 — build a preliminary result
        result = RouteResult(
            intent=intent,
            confidence=confidence,
            tier=self._default_tier(intent),
        )

        # Step 3 — check escalation criteria and override tier if needed
        result = self._apply_escalation(result, message, symptom_severity, log)

        log.info(
            "intent_routed",
            intent=result.intent,
            confidence=result.confidence,
            tier=result.tier,
            escalated=result.escalated,
        )

        return result

    # ------------------------------------------------------------------
    # LLM call
    # ------------------------------------------------------------------

    async def _classify(
        self,
        message: str,
        log: structlog.BoundLogger,
    ) -> tuple[IntentLabel, float | None]:
        """
        Ask the LLM to classify the message.

        Returns:
            (intent_label, confidence) — intent is UNCLASSIFIED when the
            response cannot be parsed or contains an unknown label.
        """
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": message},
        ]

        try:
            response = await self._llm.complete(
                "nano",
                messages,
                response_format={"type": "json_object"},
            )
        except Exception:
            log.exception("llm_classification_error")
            return "UNCLASSIFIED", None

        return self._parse_response(response.content, log)

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_response(
        content: str,
        log: structlog.BoundLogger,
    ) -> tuple[IntentLabel, float | None]:
        """
        Parse the LLM JSON response into (intent, confidence).

        Returns ``("UNCLASSIFIED", None)`` when:
          - the JSON is malformed
          - the "intent" key is absent
          - the intent value is not one of the four valid labels
        """
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            log.warning("intent_json_parse_failed", raw_content_len=len(content))
            return "UNCLASSIFIED", None

        raw_intent = data.get("intent", "")
        confidence_raw = data.get("confidence")

        # Validate intent label
        if not isinstance(raw_intent, str) or raw_intent.upper() not in _VALID_INTENTS:
            log.warning("intent_label_invalid", raw_intent=raw_intent)
            return "UNCLASSIFIED", None

        # Parse confidence — default to None if missing or non-numeric
        confidence: float | None = None
        if confidence_raw is not None:
            try:
                confidence = float(confidence_raw)
                # Clamp to [0.0, 1.0] in case the model returns out-of-range values
                confidence = max(0.0, min(1.0, confidence))
            except (TypeError, ValueError):
                confidence = None

        return raw_intent.upper(), confidence  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Tier resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _default_tier(intent: IntentLabel) -> RoutingTier:
        """
        Map an intent label to its default downstream routing tier.

        UNCLASSIFIED does not have a real tier — we assign "nano" as a
        placeholder, but the dispatcher will never use it (Req 14.8).
        """
        tier_map: dict[IntentLabel, RoutingTier] = {
            "LOGGING": "nano",
            "PERSONAL_DATA_QUERY": "mini",
            "KNOWLEDGE_QUESTION": "reasoning",
            "MIXED_QUERY": "reasoning",
            "UNCLASSIFIED": "nano",
        }
        return tier_map[intent]

    # ------------------------------------------------------------------
    # Escalation
    # ------------------------------------------------------------------

    def _apply_escalation(
        self,
        result: RouteResult,
        message: str,
        symptom_severity: int | None,
        log: structlog.BoundLogger,
    ) -> RouteResult:
        """
        Check escalation criteria and override the tier to "escalation" when any
        of the following apply:

        1. Classification confidence < 0.6 — uncertain classifications are
           handled conservatively.
        2. The message contains a danger keyword (bleeding, preeclampsia,
           contractions, seizure, chest pain) — immediate clinical concern.
        3. The intent is KNOWLEDGE_QUESTION and the caller-provided symptom
           severity is ≥ 9 — high-severity symptom paired with a medical query.

        Already-UNCLASSIFIED messages are not escalated (no pipeline is
        invoked for them regardless).
        """
        if result.intent == "UNCLASSIFIED":
            return result

        escalation_reason = self._escalation_reason(
            message, result.confidence, result.intent, symptom_severity
        )

        if escalation_reason:
            log.warning(
                "escalation_triggered",
                reason=escalation_reason,
                original_tier=result.tier,
            )
            return RouteResult(
                intent=result.intent,
                confidence=result.confidence,
                tier="escalation",
                escalated=True,
                escalation_reason=escalation_reason,
            )

        return result

    @staticmethod
    def _escalation_reason(
        message: str,
        confidence: float | None,
        intent: IntentLabel,
        symptom_severity: int | None,
    ) -> str:
        """
        Return a non-empty reason string when escalation is warranted, or an
        empty string when normal routing should proceed.
        """
        # Rule 1 — low confidence
        if confidence is not None and confidence < _CONFIDENCE_THRESHOLD:
            return f"low_confidence:{confidence:.2f}"

        # Rule 2 — danger keyword in message
        lower_message = message.lower()
        for keyword in _ESCALATION_KEYWORDS:
            if keyword in lower_message:
                return f"escalation_keyword:{keyword}"

        # Rule 3 — high-severity knowledge question
        if (
            intent == "KNOWLEDGE_QUESTION"
            and symptom_severity is not None
            and symptom_severity >= _SEVERITY_ESCALATION_THRESHOLD
        ):
            return f"high_severity_knowledge_question:{symptom_severity}"

        return ""
