#!/usr/bin/env python3
"""
Famosi Evaluation Runner — runs all 100 scenarios through the real pipeline
and produces a pass/fail report with LLM-as-judge verdicts.

Usage:
    # Run all scenarios
    famosi_venv/bin/python tests/eval/run_eval.py

    # Run specific category
    famosi_venv/bin/python tests/eval/run_eval.py --category "Family & Privacy"

    # Run specific IDs
    famosi_venv/bin/python tests/eval/run_eval.py --ids 36,37,38,91,92

    # Save report
    famosi_venv/bin/python tests/eval/run_eval.py --output reports/eval.md

    # Dry-run (skip judge, just show what would run)
    famosi_venv/bin/python tests/eval/run_eval.py --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Ensure app is on the path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from tests.eval.scenarios import SCENARIOS, Scenario
from tests.eval.harness import run_scenario
from tests.eval.judge import judge, JudgeResult, Verdict

VERDICT_EMOJI = {
    "PASS": "✅",
    "PARTIAL": "⚠️",
    "FAIL": "❌",
    "ERROR": "💥",
}


# ---------------------------------------------------------------------------
# DB setup — uses a separate test schema to avoid polluting the dev DB
# ---------------------------------------------------------------------------

async def get_test_session(scenario_id: int) -> AsyncSession:
    """Create a fresh DB session for a single scenario (rolled back after)."""
    from app.config import settings
    engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
    return factory()


# ---------------------------------------------------------------------------
# Single scenario runner
# ---------------------------------------------------------------------------

async def run_one(scenario: Scenario, dry_run: bool = False) -> dict:
    """Run one scenario and return the full result dict."""
    sid = scenario["id"]
    name = scenario["name"]

    if dry_run:
        return {
            "id": sid,
            "category": scenario["category"],
            "name": name,
            "verdict": "SKIP",
            "score": 0.0,
            "reasoning": "Dry run",
            "bot_response": "",
            "messages": scenario["messages"],
            "expected": scenario["expected"],
            "key_pass": [],
            "key_fail": [],
            "error": None,
            "duration_s": 0.0,
        }

    t0 = time.monotonic()

    # Run scenario against the real pipeline (self-contained, manages its own DB)
    harness_result = await run_scenario(scenario)

    # Ask the judge
    judge_result: JudgeResult = await judge(
        scenario_name=name,
        user_messages=scenario["messages"],
        bot_response=harness_result["full_response"],
        expected_criteria=scenario["expected"],
        error=harness_result.get("error"),
    )

    duration = time.monotonic() - t0

    return {
        "id": sid,
        "category": scenario["category"],
        "name": name,
        "verdict": judge_result.verdict,
        "score": judge_result.score,
        "reasoning": judge_result.reasoning,
        "bot_response": harness_result["full_response"],
        "messages": scenario["messages"],
        "expected": scenario["expected"],
        "key_pass": judge_result.key_pass,
        "key_fail": judge_result.key_fail,
        "error": harness_result.get("error"),
        "duration_s": round(duration, 2),
    }


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def build_report(results: list[dict], elapsed_total: float) -> str:
    lines = []
    lines.append("# Famosi Evaluation Report")
    lines.append(f"\nGenerated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"Total time: {elapsed_total:.1f}s\n")

    # Summary counts
    counts: dict[str, int] = {"PASS": 0, "PARTIAL": 0, "FAIL": 0, "ERROR": 0, "SKIP": 0}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1

    total = len(results)
    avg_score = sum(r["score"] for r in results) / total if total else 0

    lines.append("## Summary\n")
    lines.append(f"| Verdict | Count | % |")
    lines.append(f"|---------|-------|---|")
    for v, emoji in VERDICT_EMOJI.items():
        n = counts.get(v, 0)
        pct = f"{100*n/total:.0f}%" if total else "0%"
        lines.append(f"| {emoji} {v} | {n} | {pct} |")
    lines.append(f"\n**Average score:** {avg_score:.2f} / 1.00")
    lines.append(f"**Pass rate:** {(counts['PASS']/total*100):.0f}% "
                 f"({counts['PASS']}/{total} scenarios)\n")

    # Per-category breakdown
    categories: dict[str, list[dict]] = {}
    for r in results:
        cat = r["category"]
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(r)

    lines.append("## Results by Category\n")
    for cat, cat_results in categories.items():
        cat_pass = sum(1 for r in cat_results if r["verdict"] == "PASS")
        lines.append(f"### {cat} ({cat_pass}/{len(cat_results)} passed)\n")
        for r in cat_results:
            emoji = VERDICT_EMOJI.get(r["verdict"], "❓")
            lines.append(f"{emoji} **#{r['id']}** {r['name']}")
            lines.append(f"   - *{r['reasoning']}*")
            if r["key_fail"]:
                lines.append(f"   - Missing: {', '.join(r['key_fail'])}")
            lines.append(f"   - Score: {r['score']:.2f} | {r['duration_s']}s")
            lines.append("")

    # Full failures detail
    failures = [r for r in results if r["verdict"] in ("FAIL", "ERROR")]
    if failures:
        lines.append("## Failed Scenarios (Detail)\n")
        for r in failures:
            lines.append(f"### #{r['id']} — {r['name']}\n")
            lines.append(f"**Verdict:** {VERDICT_EMOJI[r['verdict']]} {r['verdict']}")
            lines.append(f"**Reasoning:** {r['reasoning']}")
            if r.get("error"):
                lines.append(f"**Error:** `{r['error']}`")
            lines.append(f"\n**User said:**")
            for m in r["messages"]:
                lines.append(f"  > {m}")
            lines.append(f"\n**Bot responded:**\n```\n{r['bot_response'][:500]}\n```")
            lines.append(f"\n**Expected:**")
            for e in r["expected"]:
                lines.append(f"  - {e}")
            lines.append("")

    return "\n".join(lines)


def build_json_report(results: list[dict]) -> str:
    return json.dumps(results, indent=2, default=str)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description="Famosi evaluation runner")
    parser.add_argument("--category", help="Run only scenarios in this category")
    parser.add_argument("--ids", help="Comma-separated scenario IDs to run (e.g. 36,37,38)")
    parser.add_argument("--tags", help="Comma-separated tags to filter by (e.g. privacy,partner)")
    parser.add_argument("--output", help="Path to write Markdown report (default: stdout)")
    parser.add_argument("--json-output", help="Path to write JSON report")
    parser.add_argument("--dry-run", action="store_true", help="Skip execution, just list scenarios")
    parser.add_argument("--concurrency", type=int, default=3, help="Max parallel scenarios (default: 3)")
    args = parser.parse_args()

    # Filter scenarios
    to_run = SCENARIOS[:]

    if args.ids:
        id_set = {int(x.strip()) for x in args.ids.split(",")}
        to_run = [s for s in to_run if s["id"] in id_set]

    if args.category:
        to_run = [s for s in to_run if args.category.lower() in s["category"].lower()]

    if args.tags:
        tag_set = {t.strip().lower() for t in args.tags.split(",")}
        to_run = [s for s in to_run if any(t in s.get("tags", []) for t in tag_set)]

    if not to_run:
        print("No scenarios match the filters.")
        sys.exit(0)

    print(f"Running {len(to_run)} scenario(s)...\n")

    # Run with bounded concurrency
    semaphore = asyncio.Semaphore(args.concurrency)
    results = []
    t_start = time.monotonic()

    async def run_with_sem(scenario):
        async with semaphore:
            result = await run_one(scenario, dry_run=args.dry_run)
            emoji = VERDICT_EMOJI.get(result["verdict"], "❓")
            print(f"  {emoji} #{result['id']:3d} {result['name'][:60]:<60} "
                  f"[{result['verdict']}] {result['duration_s']}s")
            return result

    tasks = [run_with_sem(s) for s in to_run]
    results = await asyncio.gather(*tasks)
    results = sorted(results, key=lambda r: r["id"])

    elapsed = time.monotonic() - t_start

    # Print summary
    counts: dict[str, int] = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1

    print(f"\n{'='*70}")
    print(f"Results: {len(results)} scenarios in {elapsed:.1f}s")
    for v, emoji in VERDICT_EMOJI.items():
        n = counts.get(v, 0)
        if n:
            print(f"  {emoji} {v}: {n}")
    avg = sum(r["score"] for r in results) / len(results) if results else 0
    print(f"  Average score: {avg:.2f}")
    print(f"{'='*70}\n")

    # Write reports
    md_report = build_report(results, elapsed)

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(md_report)
        print(f"Markdown report written to: {args.output}")
    else:
        print(md_report)

    if args.json_output:
        Path(args.json_output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_output).write_text(build_json_report(results))
        print(f"JSON report written to: {args.json_output}")

    # Exit with non-zero if any failures
    if counts.get("FAIL", 0) > 0 or counts.get("ERROR", 0) > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
