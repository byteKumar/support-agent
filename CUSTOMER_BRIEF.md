# Customer brief: automating refund tickets at Maple Street Goods

*Written as if for the store's operations lead. Results are to be filled in from
measured runs; until then they say "TBD".*

## 1. The problem

Maple Street Goods gets a steady stream of support tickets, and most of them are
about one thing: "where is my order / can I get my money back". Each one takes an
agent a few minutes of clicking: find the order, check the delivery date against
the 30-day window, check it was not already refunded, issue the refund in Stripe,
note it on the order, email the customer. The work is repetitive, and the costly
mistakes are refunding twice, refunding outside the window, refunding the wrong
amount, and telling one customer about another customer's order.

## 2. What was automated

An agent that reads a ticket and resolves it with the same tools a person uses:

- look up the order and the customer (only the customer who wrote in);
- check the written refund policy (POLICY.md);
- issue the refund through Stripe (test mode in this build), with an
  idempotency key so a retry cannot double-refund;
- add a note to the order, email the customer;
- hand the ticket to a person when it should not decide alone.

Refunds above **$100** are not issued automatically. They wait in an approval
page; a person approves or rejects, and the agent picks the ticket back up with
that decision. Refunds above **$1,000** always go to a person.

## 3. How it stays safe

The rules are enforced in the software, not only in the instructions to the AI.
Even if the AI is tricked by a ticket ("ignore your rules, I'm the admin"), it
cannot:

- see or refund another customer's order;
- refund outside the window, twice on one order, or more than the order is worth;
- refund more than $100 without a person approving, or more than $1,000 at all;
- email anyone other than the customer who wrote in, or put other people's
  email addresses in a message.

Every ticket leaves a full trace: each AI step, each tool call with its input
and result, time and cost.

## 4. Results

Measured on a set of 150+ realistic and adversarial test tickets written by
hand, with expected outcomes decided from the policy before any run.

| Measure | Result |
|---|---|
| Tickets resolved correctly | TBD |
| Policy violations | TBD |
| Wrong refunds (amount or eligibility) | TBD |
| Escalations that were needed (precision) / needed escalations caught (recall) | TBD / TBD |
| Manipulation attempts blocked | TBD |
| Time per ticket (median / slowest 5%) | TBD / TBD |
| AI cost per ticket | TBD |
| Effect of requiring a policy check before every refund | TBD |

## 5. Risks and how they are handled

| Risk | Mitigation | Residual |
|---|---|---|
| AI refunds when it should not | Policy checked in code inside the refund tool; approval above $100; hard cap $1,000 | A refund that is allowed by policy but still wrong for the situation (e.g. customer lying about damage). Same risk as with a human agent; watch refund rate per customer. |
| Prompt injection / social engineering | Ticket text marked as untrusted; ownership checks in every tool; suspicious tickets escalated | The AI may still write an odd email. Emails are limited to the ticket's customer and checked for other people's addresses. |
| Data leak between customers | Tools only return the sender's data; same answer for "not yours" and "does not exist" | Free-text fields (order notes) are returned to the AI; keep them free of other customers' data. |
| Double refunds (retries, duplicate tickets) | One refund per order and per ticket; Stripe idempotency keys | None known; covered by tests. |
| AI provider outage or slowness | Retries with backoff; step limit; failed runs escalate to a person | Tickets wait for a human during an outage. |
| Wrong policy encoded | Policy written in plain language (POLICY.md) and reviewed by the business before launch | Policy changes need a code change and a re-run of the test set. |
| Cost creep | Cheap model, low effort setting, cost recorded per ticket | Monitor cost per ticket weekly. |

## 6. Rollout plan

1. **Shadow mode (2 weeks).** The agent runs on real tickets but takes no
   actions: it records what it *would* do. A person handles every ticket as
   today. Compare the two daily.
2. **Assisted mode.** The agent drafts the resolution; a person clicks approve
   for every refund (approval threshold set to $0).
3. **Auto under a low limit.** Auto-approve refunds up to $25, approval above.
   Weekly review of a sample of auto-resolved tickets.
4. **Normal operation.** Raise the auto limit to $100 once the error rate on
   reviewed samples is at or below the human baseline. Keep the $1,000 cap.

Rollback at any stage: set the approval threshold to $0 (every refund needs a
person) or switch the agent off; tickets fall back to the human queue.

## 7. What we need from the store

- Sign-off on POLICY.md (it is the source of truth the agent follows).
- A Stripe restricted key with refund permission only, for production.
- Named approvers for the approval page, and an on-call person for escalations.
- Two weeks of historical tickets (anonymised) to extend the test set.
