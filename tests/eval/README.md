# Famosi Evaluation Suite

End-to-end evaluation of all 100 user scenarios using LLM-as-a-judge.

## How it works

1. Each scenario defines:
   - **input** — what the user says (or a sequence of messages)
   - **setup** — DB state required before the test (pre-logged data, user role, etc.)
   - **expected** — natural language criteria for the judge to evaluate against
   - **category** — grouping for reporting

2. The **harness** (`harness.py`) routes each input through the real app pipeline:
   - Calls `IntentRouter` → appropriate handler
   - Uses a real test DB (PostgreSQL, isolated per run)
   - Returns the bot's actual response text

3. The **judge** (`judge.py`) sends the actual response + expected criteria to GPT-4.1-mini and asks:
   - Does the response meet the criteria? (PASS / PARTIAL / FAIL)
   - Brief reasoning

4. The **runner** (`run_eval.py`) orchestrates all scenarios and produces a JSON + Markdown report.

## Running

```bash
# Run all scenarios
cd /Users/haymang/workspace/Famosi
famosi_venv/bin/python tests/eval/run_eval.py

# Run a specific category
famosi_venv/bin/python tests/eval/run_eval.py --category "Logging & Confirmation"

# Run specific scenario IDs
famosi_venv/bin/python tests/eval/run_eval.py --ids 36,37,38,39,40

# Output report
famosi_venv/bin/python tests/eval/run_eval.py --output reports/eval_$(date +%Y%m%d).md
```

## Requirements

- `DATABASE_URL` must point to a test PostgreSQL instance
- `OPENAI_API_KEY` must be set (used for real extraction + judge)
- Bedrock optional (falls back to mini tier)

## Report format

```
Category: Pregnancy Intelligence
  ✅ 36 - How many weeks pregnant: PASS (correctly computed 8w 3d from LMP)
  ✅ 39 - Can I eat sushi: PASS (week-aware response, mentions raw fish risk)
  ⚠️ 48 - What nutrients am I missing: PARTIAL (answered but no meal history in test DB)
  ❌ 43 - Is spotting normal: FAIL (did not escalate to medical guidance)

Summary: 87/100 passed, 8 partial, 5 failed
```
