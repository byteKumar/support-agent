# Interview notes

One section per milestone: what was built, why, the trade-offs, and one thing
you should change or explain yourself before you talk about it. Do the "your
turn" items: an interviewer can tell the difference between code you read and
code you changed.

---

## M1. Store schema, seed data, Stripe test charges (`Add store data and seed`)

**Built.** SQLAlchemy 2 models (`models.py`) for the store (customers, orders,
order_items, payments, refunds, tickets) and for the agent (agent_runs,
tool_calls, approvals). `seed.py` creates 500 customers and 2,000 orders with
`random.Random(42)`, plus eight hand-placed "anchor" orders with known edge
cases (simple refund, past window, over threshold, already refunded, partial
shipment, in transit, over the cap, unshipped). Also: duplicate tickets,
cancelled and partially refunded orders, ~6% high-value orders. With an
`sk_test_` key the seed creates a confirmed PaymentIntent per order with
`pm_card_visa`; without one it writes `pi_fake_...` ids.

**Why.** Evals need stable ids and stable dates. Two choices make that work:
a fixed random seed, and a **frozen store clock** (`STORE_NOW`), so "delivered
60 days ago" is still 60 days ago next month.

**Trade-offs.**
- Money as integer cents everywhere (no float rounding).
- `create_all` instead of Alembic migrations: fine for a demo, not for production.
- The frozen clock is unrealistic; in production `now` would be the real time
  and scenarios would be generated relative to it.
- Explicit ids in the seed mean Postgres sequences must be moved past them
  (`_sync_sequences`); forgetting this is a classic bug.

**Your turn.** Add a ninth anchor (e.g. an order delivered exactly 30 days
before `STORE_NOW`) and a test for it. Be ready to explain why day 30 is inside
the window in `policy.py` (`.days` truncates).

---

## M2. Tools with validation (`Add support tools`)

**Built.** Seven tools in `tools.py`. Each has a pydantic input model with
`extra="forbid"` and `strict=True` (no `"1"` -> `1` coercion), a hand-written
JSON schema for Claude with `strict: true`, `additionalProperties: false` and
every property in `required` (optional values are nullable, e.g.
`amount_cents: ["integer","null"]`). Every result is an envelope:
`{"ok": true, ...}` or `{"ok": false, "error": {"code", "message"}}`.
`execute_tool` never raises; each call runs in a savepoint so a failing tool
rolls back only its own writes. `policy.py` holds the policy as one pure
function, `evaluate_refund(order, amount, now, cfg)`.

**Why.** The model is not trusted to validate anything. Ownership
(`_owned_order`) is checked in code, and "not yours" and "does not exist" return
the same error so the agent cannot be used to probe which order ids exist.
`send_email` has no `to` field at all: the safest parameter is the one that
does not exist.

**Trade-offs.**
- Hand-written schemas can drift from the pydantic models; a test
  (`test_tool_definitions_are_strict_and_match_models`) catches that.
- Structured error codes are for the model *and* for the evals (they count
  guardrail refusals separately from malformed calls).
- `get_customer` takes a `customer_id` even though there is only one valid
  value. It is there on purpose: it gives an injection something to try, and the
  code refuses it.

**Your turn.** Explain why `PAYMENT_PROVIDER_ERROR` is marked `retryable` and
why that is safe (look at the idempotency key in `perform_refund`). Then add a
new error case yourself, e.g. refuse refunds with an empty `reason` after
stripping whitespace.

---

## M3. Agent loop with traces (`Add agent loop`)

**Built.** `agent.py`: a manual loop over `messages.create`. On `end_turn` the
run completes; on `tool_use` it runs every `tool_use` block and sends **one**
user message with all `tool_result` blocks; on `refusal`, `max_tokens` or
anything unexpected it stops and escalates. Step limit (`MAX_STEPS=12`),
retries with exponential backoff on `RateLimitError`, `APIConnectionError` and
5xx (the SDK's own retries are turned off so every attempt is visible in the
trace). OpenTelemetry spans: `agent.run` > `llm.call` and `tool.<name>`, plus
rows in `agent_runs` and `tool_calls`. Cost is computed from `response.usage`.
`llm.py` has the `LLMClient` interface, `AnthropicLLM` (the only code that can
spend money) and `FakeLLM`, which plays back scripted turns.

**Why manual and not the SDK tool runner.** A run must stop in the middle of a
tool call, persist the conversation as JSON, wait hours for a human, and resume
in another process. The tool runner owns the loop in memory; that does not fit.

**Prompt injection.** The ticket is wrapped in `<ticket>` tags, any
`<ticket>`/`</ticket>` inside the customer's text is replaced, and the system
prompt says the text is data. That lowers the odds the model is fooled; the
ownership checks make it not matter much if it is.

**Trade-offs.**
- Request: `model` from `LLM_MODEL` (default `claude-haiku-5-5`),
  `output_config={"effort": "low"}`, strict tools, no `tool_choice`, no
  `temperature`, no `thinking`. Haiku 5.5 thinks adaptively by default; low effort
  keeps it cheap. Whether low effort is enough is an eval question.
- Content blocks are stored with `model_dump(exclude_none=True)` and sent back
  unchanged, including any thinking blocks.
- Prompt caching is not used. The system prompt plus tools is short; measure
  first, then decide.

**Your turn.** Change `MAX_STEPS` handling so the run gets one final model call
without tools to write a summary before escalating, and update
`test_step_limit_escalates`. Be ready to explain why all tool results must go
back in a single user message.

---

## M4. Guardrails and approvals (`Add guardrails and approvals`)

**Built.** `issue_refund` above `AUTO_APPROVE_LIMIT_CENTS` creates an
`approvals` row and returns `pending_approval`; the loop stores the other tool
results with a placeholder, sets the run to `awaiting_approval` and returns.
`approvals.decide` locks the row (`SELECT ... FOR UPDATE`) so a decision happens
once. `SupportAgent.resume` turns the decision into the tool result for the
paused call and continues the loop. On approve it **re-checks the policy** before
moving money (the order may have been refunded by phone while the run waited).
Also added: one refund per ticket, and `send_email` refuses bodies containing
other email addresses.

**Why.** "Approval required above $X" is only real if the code cannot skip it.
The tool decides, not the model. Re-checking at approval time closes the gap
between "checked" and "used".

**Trade-offs.**
- The pause happens after the whole step; other tool calls in the same step
  still run. Simpler than pausing mid-step, and those are read-only in practice.
- A rejected approval goes back to the model as an error result
  (`APPROVAL_REJECTED`), so the model writes the customer message itself.
- Time waiting for a human is not counted in `active_seconds`.

**Your turn.** Add an "approval expires after 48 hours" rule: a pending
approval older than that is auto-rejected and the run resumes. Decide where the
check runs (a cron job? on page load?) and defend it.

---

## M5. Scenario evals and harness (`Add scenario evals`)

**Built.** `evals/scenarios.py` (schema, loader with line-numbered errors,
validator that checks ids against the seed and expected refunds against the
policy), `runner.py` (each scenario in its own rolled-back transaction, with
FakeGateway and MemoryMailer; approvals auto-decided by the scenario's
`approval_decision`), `metrics.py`, and the `harness.py` CLI. Four labelled
format examples. **The real 150 scenarios are yours to write.**

**Metrics, defined.**
- Resolution accuracy: actual outcome (`refund` if any refund, else `escalate`
  if escalated, else `no_refund`) equals expected, and for refunds the order and
  amount match exactly.
- Policy violations: re-derived from database rows by `find_violations`, which
  does not reuse the tools' checks, so a bug in a tool shows up.
- Wrong refund: a refund where none was expected (eligibility) or with the wrong
  order or amount.
- Escalation precision/recall: positives are scenarios expecting `escalate`.
- Tool-call success: raw, and excluding guardrail refusals (a refused refund is
  the system working, not a failure).
- Injection blocked: injection scenarios with no violation and no wrong refund.

**Safety rails in the harness.** A paid run prints an estimate and exits with
code 2 unless `--yes`; `--dry-run` only estimates; FORMAT_EXAMPLES never produce
headline numbers; under 150 scenarios prints a warning; the fake provider is
labelled a floor.

**Trade-offs.**
- Outcome is judged from database effects, not from the text the model wrote.
  Cheap and objective, but it cannot tell a rude email from a kind one.
- The cost estimate uses a chars/4 rule and a guessed 5 calls per ticket; it is
  labelled as an assumption.

**Your turn.** Add a metric you think matters (e.g. "sent exactly one email"
or "asked for clarification when no order number was given") and justify it.

---

## M6. One eval-driven improvement (`Add required policy-check option`)

**Built.** `REQUIRE_POLICY_CHECK=1` (and the `improved` variant): `issue_refund`
fails with `POLICY_CHECK_REQUIRED` unless `check_policy` succeeded for that same
order earlier in the run (looked up in `tool_calls`). `--compare
baseline,improved` runs both on the same scenarios and prints the deltas.

**Not claimed.** There is no before/after number yet. The hypothesis: forcing a
look before money moves makes the model explain decisions using the policy's
actual numbers and makes it less likely to pick the wrong amount. It could also
cost an extra step per refund. Only the run can say.

**Trade-offs.** Without this flag, the refund tool already enforces the policy,
so the option cannot reduce *violations* much; its expected effect is on wrong
amounts and on explanation quality. Saying that out loud in an interview shows
you understand what each layer does.

**Your turn.** After your baseline run, look at the misses and pick the
improvement the data points to. It may not be this one. Commit it as
"Improve <thing> from evals" with the before/after numbers.

---

## M7. CI, Docker Compose, admin page (`Add CI and admin page`)

**Built.** FastAPI app (`api.py`): `POST /tickets`, `GET /tickets/{id}`,
`/admin` (HTMX, basic auth) listing pending refunds with approve/reject buttons
and recent runs, and a run page showing every tool call and the conversation.
POSTs require the `HX-Request` header as a cheap CSRF guard. Templates
auto-escape ticket text. Dockerfile and an `api` service in compose. CI: ruff,
format check and pytest with Postgres and MailHog service containers; an
eval-subset job that runs only if the `ANTHROPIC_API_KEY` secret exists and
`evals/scenarios.jsonl` is committed.

**Trade-offs.**
- Background tasks instead of a queue (Celery/RQ/a Postgres job table). A crash
  mid-run leaves a `running` row; a real deployment needs a worker and a sweeper.
- Basic auth with one shared password: fine for a demo, not for a team.
- The eval subset job costs money on every push once enabled; consider limiting
  it to `workflow_dispatch` or `main`.

**Your turn.** Move the agent run out of the request process: a `jobs` table and
a worker loop with `SELECT ... FOR UPDATE SKIP LOCKED`. You built that pattern
in pg-durable-queue; connecting the two projects is a good story.

---

## M8. README, RESULTS, customer brief (`Add results and customer brief`)

**Built.** README (architecture, guardrail table, how to run, what is left),
POLICY.md, RESULTS.md (template with blanks), CUSTOMER_BRIEF.md (problem,
automation, safety, risks, rollout plan; results TBD).

**Your turn.** Fill RESULTS.md and the brief from your own runs, record the demo
video, and write the "what surprised you" line honestly. That line is often the
best part of the interview.
