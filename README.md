# support-agent

An LLM agent that handles customer-support tickets for a small online store
(Maple Street Goods, fictional) by calling real tools: it looks up orders,
checks the refund policy, issues refunds through Stripe's **test mode**, updates
the order and emails the customer (to a local MailHog sandbox). Refunds above a
threshold go to a **human approval queue**; the agent's run pauses and resumes
after the decision.

The point of the project is the part around the model: guardrails enforced in
code, a resumable agent loop, traces, and an eval harness that measures
resolution accuracy, policy violations, wrong refunds, escalation quality, time
and cost.

> Status: the system and the harness are built and tested with a scripted fake
> model. **No real-model numbers exist yet.** They need the scenario set and an
> API key (see "What Chaman must do next"). See RESULTS.md.

## Architecture

```
                 POST /tickets                       GET /admin (HTMX, basic auth)
 customer  ───────────────────►  FastAPI  ◄──────────────────────────────  human reviewer
                                    │                                  approve / reject
                                    ▼                                         │
                         ┌─────────────────────┐                              │
                         │ SupportAgent loop   │  resume(approval_id) ◄───────┘
                         │ (manual, resumable) │
                         │  step limit,        │──── messages.create ────► Claude (LLM_PROVIDER=anthropic)
                         │  retries, OTel spans│      tools strict=true     or FakeLLM (tests/dev/CI)
                         └─────────┬───────────┘
                     tool_use      │ one user message with all tool_results
                                   ▼
   ┌───────────┬──────────────┬──────────────┬──────────────┬──────────────┬─────────────┬──────────────────┐
   │ get_order │ get_customer │ check_policy │ issue_refund │ update_order │ send_email  │ escalate_to_human│
   └───────────┴──────────────┴──────┬───────┴──────┬───────┴──────────────┴──────┬──────┴──────────────────┘
     pydantic validation + ownership │ policy.py    │ over $100 -> approvals row, │ recipient fixed,
     checks in every tool            │ (POLICY.md)  │ run pauses                  │ leak check
                                     ▼              ▼                             ▼
                              PostgreSQL 16   PaymentGateway                  Mailer
                              store + agent_runs, FakeGateway | StripeGateway  MemoryMailer | SmtpMailer
                              tool_calls, approvals  (sk_test_ only)           -> MailHog :1025
```

Data: `customers, orders, order_items, payments, refunds, tickets` (the store) and
`agent_runs, tool_calls, approvals` (the agent's own record). Every ticket run has
an OpenTelemetry trace (`agent.run` > `llm.call` / `tool.<name>`) and the same
information in `agent_runs` / `tool_calls`.

### Guardrails and where they live

| Rule (POLICY.md) | Enforced in code by |
|---|---|
| Only the ticket's customer's data (1.1, 1.2) | `_owned_order` in every order tool; `get_customer` refuses other ids; same error for "not yours" and "does not exist" |
| 30-day window, status rules (2.x) | `policy.evaluate_refund`, called inside `issue_refund` |
| One refund per order (3.1) | `evaluate_refund` |
| One refund per ticket (3.2) | `issue_refund` (`TICKET_REFUND_LIMIT`) |
| Amount <= refundable (3.3) | `evaluate_refund` |
| Approval above $100 (3.5) | `issue_refund` creates an `approvals` row; the loop pauses |
| Hard cap $1,000 (3.6) | `evaluate_refund` (`OVER_TICKET_CAP`) |
| Email only to the customer, no other addresses (4.1) | `send_email` has no `to` field; `DATA_LEAK_BLOCKED` |
| Ticket text is data (4.2) | wrapped in `<ticket>` tags, tags inside the text neutralised, system prompt says so; the checks above hold even if the model is fooled |
| Policy re-checked when a human approves | `SupportAgent.resume` |
| No double refunds on retry | idempotency key `refund:ticket-T:order-O:amount` sent to Stripe and stored unique |

## Run it

Requirements: Docker (Colima is fine), [uv](https://docs.astral.sh/uv/), Python 3.12.

```bash
docker compose up -d db mailhog                # Postgres on 5435, MailHog on 1025 / 8025
uv sync
uv run python -m support_agent.seed --reset    # 500 customers, 2,000 orders, fixed seed
uv run pytest                                  # uses a separate support_agent_test DB
uv run uvicorn support_agent.api:create_app --factory --port 8002
```

Or everything in containers: `docker compose up -d --build`, then seed once with
`docker compose run --rm api uv run --no-sync python -m support_agent.seed --reset`.

Try it (the default provider is a fake that escalates every ticket, so this is free):

```bash
curl -s -X POST localhost:8002/tickets -H 'content-type: application/json' \
  -d '{"customer_id": 1, "subject": "Refund", "body": "Order #1 arrived damaged."}'
curl -s localhost:8002/tickets/<ticket_id>
open http://localhost:8002/admin        # admin / change-me (set ADMIN_PASSWORD)
open http://localhost:8025              # MailHog inbox
```

Configuration is all environment variables; see `.env.example`. Defaults are
free and safe: `LLM_PROVIDER=fake`, `PAYMENT_PROVIDER=fake`.

### Real model (costs money)

```bash
export LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=...   # your key, never committed
export LLM_MODEL=claude-haiku-5-5                       # default; $0.10 / $0.50 per 1M tokens
```

The loop sends `output_config={"effort": "low"}`, strict tool schemas, no forced
`tool_choice`, no `temperature`, no `thinking` parameter. Cost per run comes from
`response.usage` and is stored on `agent_runs.cost_usd`.

### Stripe (test mode only)

`PAYMENT_PROVIDER=stripe` with `STRIPE_SECRET_KEY=sk_test_...`. `StripeGateway`
raises at startup on anything that is not an `sk_test_` key (live keys are a
hard error). The seed creates one confirmed PaymentIntent per order with
`pm_card_visa` when a test key is present; otherwise it writes fake charge ids.
Evals always use `FakeGateway` so each scenario can be rolled back.

## Evals

```bash
# smoke-test the pipeline (no headline numbers are printed for these)
uv run python -m support_agent.evals.harness --scenarios evals/FORMAT_EXAMPLES.jsonl --provider fake

# the real set, once written
uv run python -m support_agent.evals.harness --scenarios evals/scenarios.jsonl --dry-run
uv run python -m support_agent.evals.harness --scenarios evals/scenarios.jsonl \
    --provider anthropic --compare baseline,improved --yes
uv run python -m support_agent.evals.harness --subset 25 --provider anthropic --yes   # CI subset
```

Metrics: resolution accuracy, policy-violation rate (re-derived from the
database, independent of the tools' own checks), wrong-refund rate (amount or
eligibility), escalation precision / recall, tool-call success rate (raw and
excluding guardrail refusals), median / p95 time per ticket, cost per ticket,
prompt-injection scenarios blocked. Each scenario runs in its own rolled-back
transaction. Approvals during evals are decided by the scenario's
`approval_decision`. Results are written to `eval_results/*.json`.

Variants: `baseline`, and `improved` = `REQUIRE_POLICY_CHECK` on (issue_refund
fails unless `check_policy` ran for that order earlier in the same run). Whether
it helps is unknown until it is measured.

Scenario format and how to write them: `evals/HOW_TO_WRITE_SCENARIOS.md`.

## Repo layout

```
src/support_agent/
  config.py  db.py  models.py  seed.py          settings, schema, deterministic seed
  policy.py  tools.py  payments.py  emailer.py  policy as code, the 7 tools, gateways
  llm.py  agent.py  approvals.py  tracing.py    model clients, loop, approval queue, OTel
  api.py  templates/                            FastAPI + HTMX admin page
  evals/ scenarios.py runner.py metrics.py harness.py
evals/FORMAT_EXAMPLES.jsonl  evals/HOW_TO_WRITE_SCENARIOS.md
tests/                         pytest against the compose Postgres and MailHog
POLICY.md  RESULTS.md  CUSTOMER_BRIEF.md  docs/INTERVIEW_NOTES.md
```

## What Chaman must do next (in order)

1. **Read the code and `docs/INTERVIEW_NOTES.md`**; make the "change it yourself"
   edit listed for each milestone so you can explain every line.
2. **Write the scenarios** in `evals/scenarios.jsonl` (150+, 30+ adversarial),
   following `evals/HOW_TO_WRITE_SCENARIOS.md`. Validate with `--dry-run`.
3. **Stripe test key**: create a Stripe account, put the *test* secret key in
   `.env` (`STRIPE_SECRET_KEY=sk_test_...`), seed with `PAYMENT_PROVIDER=stripe`,
   and check the test dashboard shows the charges.
4. **Anthropic key**: put it in your shell or `.env` yourself.
5. **Paid runs**: `--dry-run` first, read the estimate, then run
   `--compare baseline,improved --yes`. Keep total spend under about $15.
6. Fill **RESULTS.md** and the results section of **CUSTOMER_BRIEF.md** with the
   measured numbers, including what failed.
7. **Push**: `gh repo create byteKumar/support-agent --public --source . --push`.
   Optionally add `ANTHROPIC_API_KEY` as a repo secret to turn on the CI eval subset.
8. **Demo video** (3 minutes): ticket in, tool calls, approval in the admin page,
   resume, email in MailHog, eval numbers.

## Known limitations

- `create_all` instead of migrations (Alembic would be the next step).
- The agent runs in FastAPI background tasks, not a durable queue; a crash
  mid-run leaves the run in `running` (visible, but nothing retries it).
- The customer id on `POST /tickets` is trusted (it stands in for the helpdesk's
  authenticated sender).
- The store clock is frozen (`STORE_NOW`) so seed data and scenarios stay stable.
- The real Anthropic path is tested only against a stub client; its first real
  run is Chaman's.
- The `api` Docker image has not been built successfully yet: two attempts failed
  on DNS errors inside the Colima build (PyPI downloads), and the third was stopped
  because the disk was nearly full. Postgres and MailHog run in compose; the API
  was verified with `uvicorn` on the host. Build it with `docker compose build api`.
