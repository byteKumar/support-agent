# Results

Nothing below the line has been measured with a real model yet. Every blank says
so on purpose. Fill each one from a real run (`eval_results/*.json`), never from
an estimate, and report failed or surprising runs too.

What *has* been checked so far: the test suite (`uv run pytest`) passes against
the compose Postgres and MailHog, using the scripted `FakeLLM`. That shows the
plumbing works; it says nothing about how well the model handles tickets.

## Results template (spec section 5)

```
SCENARIOS (written by Chaman): total=not measured yet (needs scenarios / API key) (adversarial not measured yet (needs scenarios / API key))
BEFORE -> AFTER improvement: resolution accuracy not measured yet (needs scenarios / API key) ; policy violations not measured yet (needs scenarios / API key) ; wrong refunds not measured yet (needs scenarios / API key)
ESCALATION precision/recall: not measured yet (needs scenarios / API key) ; tool-call success not measured yet (needs scenarios / API key)
TIME per ticket median/p95: not measured yet (needs scenarios / API key) ; COST per ticket not measured yet (needs scenarios / API key) ; total API spend not measured yet (needs scenarios / API key)
PROMPT-INJECTION scenarios blocked: not measured yet (needs scenarios / API key)
CI passing? yes on GitHub (tests; eval subset skips until a key and scenarios exist) ; demo video: not recorded yet
IMPROVEMENT MADE AND WHY: REQUIRE_POLICY_CHECK option built (issue_refund refuses unless check_policy ran for that order in the same run); effect not measured yet (needs scenarios / API key)
ANYTHING THAT FAILED OR SURPRISED YOU: not measured yet (needs scenarios / API key)
REPO pushed? yes, https://github.com/byteKumar/support-agent
```

## How to fill it

```bash
uv run python -m support_agent.evals.harness --scenarios evals/scenarios.jsonl --dry-run
uv run python -m support_agent.evals.harness --scenarios evals/scenarios.jsonl \
    --provider anthropic --compare baseline,improved --yes
```

| Field | Where it comes from |
|---|---|
| total / adversarial | `metrics.n`, `metrics.adversarial` |
| resolution accuracy | `metrics.resolution_accuracy` (baseline -> improved) |
| policy violations | `metrics.policy_violation_rate` |
| wrong refunds | `metrics.wrong_refund_count` |
| escalation precision / recall | `metrics.escalation_precision`, `metrics.escalation_recall` |
| tool-call success | `metrics.tool_call_success_rate` (also report the excl.-guardrails number) |
| time median / p95 | `metrics.time_median_s`, `metrics.time_p95_s` |
| cost per ticket | `metrics.cost_per_ticket_usd` |
| total API spend | sum of `metrics.total_cost_usd` over every paid run, plus the Anthropic console figure |
| injection blocked | `metrics.injection_blocked` of `metrics.injection_total` |

## Run log

| date | git commit | variant | scenarios | model | cost | file |
|---|---|---|---|---|---|---|
| | | | | | | |
