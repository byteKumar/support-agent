"""Eval harness CLI.

    uv run python -m support_agent.evals.harness --scenarios evals/scenarios.jsonl --dry-run
    uv run python -m support_agent.evals.harness --scenarios evals/scenarios.jsonl \
        --provider anthropic --yes
    uv run python -m support_agent.evals.harness --subset 25 --provider anthropic --yes   # CI

Rules built in:
- a paid run (provider anthropic) prints a cost estimate and refuses to start without --yes;
- FORMAT_EXAMPLES.jsonl only exercises the pipeline: no headline numbers are printed;
- fewer than 150 real scenarios prints a warning;
- the fake provider (escalates everything) is labelled as a floor, not a model result.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import random
import subprocess
import sys
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from support_agent.agent import system_prompt
from support_agent.config import Settings
from support_agent.evals.metrics import Metrics, compute
from support_agent.evals.runner import Observation, run_scenario
from support_agent.evals.scenarios import (
    MIN_SCENARIOS,
    Scenario,
    ScenarioFileError,
    load_scenarios,
    validate_against_store,
)
from support_agent.llm import AnthropicLLM, EscalateEverythingLLM, LLMClient
from support_agent.models import Customer, Order
from support_agent.tools import TOOL_DEFINITIONS

DEFAULT_SCENARIOS = "evals/scenarios.jsonl"
EXIT_NEEDS_YES = 2

# Cost-estimate assumptions (printed with every estimate)
EST_CALLS_PER_TICKET = 5
EST_HISTORY_GROWTH_TOKENS = 600
EST_OUTPUT_TOKENS_PER_CALL = 400
EST_SAFETY_FACTOR = 1.5


def is_format_only(path: Path, scenarios: list[Scenario]) -> bool:
    return path.name.upper().startswith("FORMAT_EXAMPLES") or any(
        s.format_example for s in scenarios
    )


def subset(scenarios: list[Scenario], n: int, seed: int = 0) -> list[Scenario]:
    """Deterministic subset, round-robin across categories so each kind is represented."""
    if n >= len(scenarios):
        return list(scenarios)
    rng = random.Random(seed)
    groups: dict[str, list[Scenario]] = defaultdict(list)
    for sc in sorted(scenarios, key=lambda s: s.id):
        groups[sc.category].append(sc)
    for g in groups.values():
        rng.shuffle(g)
    picked: list[Scenario] = []
    cats = sorted(groups)
    while len(picked) < n:
        for c in cats:
            if groups[c] and len(picked) < n:
                picked.append(groups[c].pop())
    return sorted(picked, key=lambda s: s.id)


def estimate_cost(n_runs: int, settings: Settings) -> dict:
    base_chars = len(system_prompt(settings)) + len(json.dumps(TOOL_DEFINITIONS)) + 600
    base_tokens = base_chars // 4  # rough chars-per-token rule; not a tokenizer count
    input_tokens = sum(
        base_tokens + EST_HISTORY_GROWTH_TOKENS * k for k in range(EST_CALLS_PER_TICKET)
    )
    output_tokens = EST_OUTPUT_TOKENS_PER_CALL * EST_CALLS_PER_TICKET
    per_ticket = (
        input_tokens * settings.price_input_per_mtok
        + output_tokens * settings.price_output_per_mtok
    ) / 1e6
    per_ticket *= EST_SAFETY_FACTOR
    return {
        "runs": n_runs,
        "model": settings.llm_model,
        "assumed_calls_per_ticket": EST_CALLS_PER_TICKET,
        "assumed_input_tokens_per_ticket": input_tokens,
        "assumed_output_tokens_per_ticket": output_tokens,
        "safety_factor": EST_SAFETY_FACTOR,
        "est_cost_per_ticket_usd": round(per_ticket, 5),
        "est_total_usd": round(per_ticket * n_runs, 4),
    }


def _git_commit() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return None


def _check_seed(engine) -> str | None:
    with sessionmaker(bind=engine)() as s:
        c1 = s.get(Customer, 1)
        o1 = s.get(Order, 1)
        if (
            c1 is None
            or o1 is None
            or o1.total_cents != 4500
            or c1.email != "avery.park1@example.com"
        ):
            return (
                "Store is not seeded as expected. Run: uv run python -m support_agent.seed --reset"
            )
    return None


def _fmt_pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def print_metrics(m: Metrics, label: str, out=print) -> None:
    out(f"\n== {label} ==")
    out(f"scenarios: {m.n} (adversarial {m.adversarial})")
    out(f"resolution accuracy:      {_fmt_pct(m.resolution_accuracy)}")
    out(f"policy-violation rate:    {_fmt_pct(m.policy_violation_rate)} ({m.violation_count})")
    out(f"wrong-refund rate:        {_fmt_pct(m.wrong_refund_rate)} ({m.wrong_refund_count})")
    out(f"escalation precision:     {_fmt_pct(m.escalation_precision)}")
    out(f"escalation recall:        {_fmt_pct(m.escalation_recall)}")
    out(
        f"tool-call success:        {_fmt_pct(m.tool_call_success_rate)} of {m.tool_calls}"
        f" (excl. guardrail refusals {_fmt_pct(m.tool_call_success_excl_guardrails)})"
    )
    out(f"tool expectations met:    {_fmt_pct(m.tool_expectations_met_rate)}")
    med = "n/a" if m.time_median_s is None else f"{m.time_median_s:.2f}s"
    p95 = "n/a" if m.time_p95_s is None else f"{m.time_p95_s:.2f}s"
    out(f"time per ticket:          median {med}, p95 {p95}")
    out(f"cost per ticket:          ${m.cost_per_ticket_usd:.5f} (total ${m.total_cost_usd:.4f})")
    out(f"prompt injection blocked: {m.injection_blocked} of {m.injection_total}")
    out(f"unexpected approvals:     {m.unexpected_approvals}; run errors: {m.run_errors}")


def make_llm_factory(provider: str, settings: Settings) -> Callable[[Scenario], LLMClient]:
    if provider == "anthropic":
        return lambda _sc: AnthropicLLM(settings.llm_model)
    return lambda _sc: EscalateEverythingLLM()


def run_eval(
    scenarios: list[Scenario],
    engine,
    settings: Settings,
    llm_factory: Callable[[Scenario], LLMClient],
    variants: list[str],
    out=print,
) -> dict[str, tuple[Metrics, list, list[Observation]]]:
    results = {}
    for variant in variants:
        obs: list[Observation] = []
        for i, sc in enumerate(scenarios, 1):
            o = run_scenario(engine, sc, llm_factory(sc), settings, variant)
            obs.append(o)
            out(
                f"[{variant} {i}/{len(scenarios)}] {sc.id}: {o.outcome} "
                f"({o.run_status}, {o.wall_seconds:.2f}s, ${o.cost_usd:.5f})"
            )
        metrics, scores = compute(scenarios, obs)
        results[variant] = (metrics, scores, obs)
    return results


def main(argv: list[str] | None = None, out=print) -> int:
    p = argparse.ArgumentParser(description="Run the scenario eval set")
    p.add_argument("--scenarios", default=DEFAULT_SCENARIOS)
    p.add_argument("--subset", type=int, default=None, help="run a deterministic subset of N")
    p.add_argument("--variant", default="baseline", choices=["baseline", "improved"])
    p.add_argument(
        "--provider",
        choices=["fake", "anthropic"],
        default=None,
        help="defaults to LLM_PROVIDER (fake unless set)",
    )
    p.add_argument("--dry-run", action="store_true", help="validate and estimate cost only")
    p.add_argument("--yes", action="store_true", help="confirm a paid run")
    p.add_argument("--database-url", default=None)
    p.add_argument("--out", default="eval_results")
    args = p.parse_args(argv)

    settings = Settings.from_env()
    if args.database_url:
        settings = dataclasses.replace(settings, database_url=args.database_url)
    provider = args.provider or settings.llm_provider
    path = Path(args.scenarios)
    if not path.exists():
        out(f"No scenario file at {path}. Write it first (evals/HOW_TO_WRITE_SCENARIOS.md),")
        out("or point --scenarios at evals/FORMAT_EXAMPLES.jsonl to smoke-test the pipeline.")
        return 1
    try:
        scenarios = load_scenarios(path)
    except ScenarioFileError as exc:
        out(f"Scenario file has errors:\n{exc}")
        return 1

    engine = create_engine(settings.database_url)
    seed_problem = _check_seed(engine)
    if seed_problem:
        out(seed_problem)
        return 1
    with sessionmaker(bind=engine)() as s:
        report = validate_against_store(scenarios, s, settings)
    for w in report.warnings:
        out(f"warning: {w}")
    if not report.ok:
        out("Scenario validation failed:")
        for e in report.errors:
            out(f"  error: {e}")
        return 1

    format_only = is_format_only(path, scenarios)
    if format_only:
        out("FORMAT EXAMPLES ONLY: this checks the pipeline. No headline numbers will be reported.")
    elif len(scenarios) < MIN_SCENARIOS:
        out(
            f"WARNING: only {len(scenarios)} scenarios; the spec asks for at least "
            f"{MIN_SCENARIOS}. Numbers from this file are not the project's results."
        )

    if args.subset:
        scenarios = subset(scenarios, args.subset)
    variants = [args.variant]
    n_runs = len(scenarios) * len(variants)

    est = estimate_cost(n_runs, settings)
    if provider == "anthropic" or args.dry_run:
        out("Cost estimate (assumptions, not a measurement): " + json.dumps(est))
    if args.dry_run:
        return 0
    if provider == "anthropic" and not args.yes:
        out("Refusing to call the paid API without --yes. Re-run with --yes to confirm.")
        return EXIT_NEEDS_YES

    if provider == "fake":
        out("Provider: FAKE (escalate-everything floor). These are not model results.")
    results = run_eval(
        scenarios, engine, settings, make_llm_factory(provider, settings), variants, out=out
    )

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for variant, (metrics, scores, obs) in results.items():
        payload = {
            "created_at": stamp,
            "git_commit": _git_commit(),
            "provider": provider,
            "model": settings.llm_model if provider == "anthropic" else "fake",
            "variant": variant,
            "scenario_file": str(path),
            "scenario_file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "format_only": format_only,
            "subset": args.subset,
            "metrics": None if format_only else metrics.as_dict(),
            "scores": [dataclasses.asdict(s) for s in scores],
            "observations": [o.as_dict() for o in obs],
        }
        target = out_dir / f"{stamp}-{provider}-{variant}.json"
        target.write_text(json.dumps(payload, indent=2, default=str))
        out(f"wrote {target}")
        if format_only:
            for s in scores:
                mark = "ok " if s.resolution_correct else "MISS"
                out(
                    f"  {mark} {s.scenario_id}: expected {s.expected_outcome}, got "
                    f"{s.actual_outcome} {s.notes}"
                )
        else:
            label = f"{variant} / {provider}" + (" (FAKE FLOOR)" if provider == "fake" else "")
            print_metrics(metrics, label, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
