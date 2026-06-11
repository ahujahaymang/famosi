"""
LLM-as-a-Judge for Famosi evaluation scenarios.

Uses GPT-4.1-mini to evaluate whether a bot response meets the expected criteria.
Returns a structured verdict: PASS / PARTIAL / FAIL with reasoning.

The judge is instructed to be strict about factual claims (e.g. gestational age,
safety guidance) and lenient about phrasing/tone variations.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

import openai

from app.config import settings

Verdict = Literal["PASS", "PARTIAL", "FAIL", "ERROR"]

_JUDGE_SYSTEM_PROMPT = """\
You are an expert evaluator for a pregnancy AI assistant called Famosi.
Your job is to determine whether the bot's response meets the expected criteria.

Evaluation rules:
1. PASS: The response clearly meets ALL the expected criteria.
2. PARTIAL: The response meets SOME but not all criteria, or partially meets multiple criteria.
3. FAIL: The response fails to meet the criteria in a meaningful way, gives wrong information, 
   or violates privacy (e.g., reveals private data that should be hidden).
4. Be strict about: medical accuracy, privacy enforcement, gestational age calculations, 
   role-appropriate framing (partner vs. mom).
5. Be lenient about: exact phrasing, response length, stylistic choices.
6. If the response is "(no response captured)" — FAIL unless the scenario expects silence.

Respond with a JSON object only:
{
  "verdict": "PASS" | "PARTIAL" | "FAIL",
  "score": 0.0 to 1.0,
  "reasoning": "one or two sentences explaining the verdict",
  "key_pass": ["criteria that were met"],
  "key_fail": ["criteria that were not met"]
}
"""


@dataclass
class JudgeResult:
    verdict: Verdict
    score: float
    reasoning: str
    key_pass: list[str]
    key_fail: list[str]
    raw_response: str = ""


async def judge(
    scenario_name: str,
    user_messages: list[str],
    bot_response: str,
    expected_criteria: list[str],
    error: str | None = None,
) -> JudgeResult:
    """
    Ask the judge LLM to evaluate the bot's response.

    Args:
        scenario_name: Short description of the scenario.
        user_messages: The messages the user sent.
        bot_response: The full text of what the bot responded.
        expected_criteria: List of criteria the response should meet.
        error: If the harness threw an exception, pass it here.
    """
    if error:
        return JudgeResult(
            verdict="ERROR",
            score=0.0,
            reasoning=f"Harness error: {error}",
            key_pass=[],
            key_fail=["Execution failed with an exception"],
            raw_response="",
        )

    criteria_text = "\n".join(f"- {c}" for c in expected_criteria)

    user_content = f"""
Scenario: {scenario_name}

User sent:
{chr(10).join(f'  > {m}' for m in user_messages)}

Bot responded:
{bot_response or '(no response)'}

Expected criteria (ALL must be met for PASS):
{criteria_text}
"""

    client = openai.AsyncOpenAI(api_key=settings.openai_api_key)

    try:
        response = await client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            response_format={"type": "json_object"},
            max_tokens=500,
            temperature=0,
        )

        raw = response.choices[0].message.content or "{}"
        data = json.loads(raw)

        return JudgeResult(
            verdict=data.get("verdict", "FAIL"),
            score=float(data.get("score", 0.0)),
            reasoning=data.get("reasoning", ""),
            key_pass=data.get("key_pass", []),
            key_fail=data.get("key_fail", []),
            raw_response=raw,
        )

    except Exception as exc:  # noqa: BLE001
        return JudgeResult(
            verdict="ERROR",
            score=0.0,
            reasoning=f"Judge LLM call failed: {exc}",
            key_pass=[],
            key_fail=["Judge could not evaluate"],
            raw_response="",
        )
