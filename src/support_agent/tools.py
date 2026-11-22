"""The seven support tools.

Every tool:
- validates its own input with a strict pydantic model (no extra fields, no type coercion);
- scopes data to the customer who sent the ticket (ownership is checked in code);
- returns a JSON envelope: {"ok": true, ...} or {"ok": false, "error": {"code", "message"}}.

TOOL_DEFINITIONS is what Claude sees. The schemas are written by hand so they
meet strict tool use rules (every property required, additionalProperties false);
tests check that they match the pydantic models.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from support_agent.config import Settings
from support_agent.emailer import EmailRefused, Mailer
from support_agent.models import Approval, Customer, Order, Refund, Ticket
from support_agent.payments import GatewayError, PaymentGateway
from support_agent.policy import PolicyConfig, evaluate_refund


class ToolError(Exception):
    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


@dataclass
class ToolContext:
    session: Session
    ticket: Ticket
    settings: Settings
    gateway: PaymentGateway
    mailer: Mailer
    run_id: int | None = None
    tool_use_id: str | None = None
    now: datetime | None = None

    def __post_init__(self):
        if self.now is None:
            self.now = self.settings.store_now

    @property
    def policy(self) -> PolicyConfig:
        return PolicyConfig.from_settings(self.settings)


@dataclass
class ToolOutcome:
    result: dict
    is_error: bool
    pending_approval_id: int | None = None
    escalated: bool = False
    duration_ms: float = 0.0
    extra: dict = field(default_factory=dict)


# ---------------------------------------------------------------- input models


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class GetOrderInput(_Strict):
    order_id: int = Field(ge=1)


class GetCustomerInput(_Strict):
    customer_id: int = Field(ge=1)


class CheckPolicyInput(_Strict):
    order_id: int = Field(ge=1)
    amount_cents: int | None = Field(default=..., ge=1)


class IssueRefundInput(_Strict):
    order_id: int = Field(ge=1)
    amount_cents: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=500)


class UpdateOrderInput(_Strict):
    order_id: int = Field(ge=1)
    action: Literal["add_note", "mark_return_requested"]
    note: str = Field(min_length=1, max_length=1000)


class SendEmailInput(_Strict):
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=5000)


EscalationCategory = Literal[
    "policy_exception", "over_limit", "suspected_fraud", "needs_human_judgment", "other"
]


class EscalateInput(_Strict):
    reason: str = Field(min_length=3, max_length=1000)
    category: EscalationCategory


# ---------------------------------------------------------------- helpers


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _owned_order(ctx: ToolContext, order_id: int) -> Order:
    order = ctx.session.get(Order, order_id)
    # Same answer for "does not exist" and "belongs to someone else": no existence oracle.
    if order is None or order.customer_id != ctx.ticket.customer_id:
        raise ToolError(
            "ORDER_NOT_FOUND",
            "No order with that id belongs to the customer who sent this ticket.",
        )
    return order


def _order_view(order: Order) -> dict:
    return {
        "order_id": order.id,
        "status": order.status,
        "placed_at": _iso(order.placed_at),
        "shipped_at": _iso(order.shipped_at),
        "delivered_at": _iso(order.delivered_at),
        "total_cents": order.total_cents,
        "currency": order.currency,
        "return_requested": order.return_requested,
        "items": [
            {
                "sku": i.sku,
                "name": i.name,
                "quantity": i.quantity,
                "shipped_quantity": i.shipped_quantity,
                "unit_price_cents": i.unit_price_cents,
            }
            for i in order.items
        ],
        "refunds": [
            {"amount_cents": r.amount_cents, "created_at": _iso(r.created_at), "reason": r.reason}
            for r in order.refunds
        ],
        "payment_status": order.payment.status if order.payment else None,
    }


# ---------------------------------------------------------------- tools


def get_order(ctx: ToolContext, inp: GetOrderInput) -> dict:
    return {"order": _order_view(_owned_order(ctx, inp.order_id))}


def get_customer(ctx: ToolContext, inp: GetCustomerInput) -> dict:
    if inp.customer_id != ctx.ticket.customer_id:
        raise ToolError(
            "FORBIDDEN",
            "You may only look up the customer who sent this ticket "
            f"(customer_id {ctx.ticket.customer_id}).",
        )
    c = ctx.session.get(Customer, inp.customer_id)
    orders = ctx.session.scalars(
        select(Order).where(Order.customer_id == c.id).order_by(Order.placed_at.desc()).limit(20)
    ).all()
    other_tickets = ctx.session.scalars(
        select(Ticket)
        .where(Ticket.customer_id == c.id, Ticket.id != ctx.ticket.id)
        .order_by(Ticket.created_at.desc())
        .limit(10)
    ).all()
    return {
        "customer": {
            "customer_id": c.id,
            "name": c.name,
            "email": c.email,
            "customer_since": _iso(c.created_at),
        },
        "recent_orders": [
            {
                "order_id": o.id,
                "status": o.status,
                "placed_at": _iso(o.placed_at),
                "total_cents": o.total_cents,
            }
            for o in orders
        ],
        "other_tickets": [
            {
                "ticket_id": t.id,
                "subject": t.subject,
                "status": t.status,
                "created_at": _iso(t.created_at),
                "body_preview": t.body[:160],
            }
            for t in other_tickets
        ],
    }


def check_policy(ctx: ToolContext, inp: CheckPolicyInput) -> dict:
    order = _owned_order(ctx, inp.order_id)
    decision = evaluate_refund(order, inp.amount_cents, ctx.now, ctx.policy)
    return {
        "decision": decision.as_dict(),
        "auto_approve_limit_cents": ctx.policy.auto_approve_limit_cents,
        "refund_window_days": ctx.policy.window_days,
    }


def _request_approval(ctx: ToolContext, order: Order, amount: int, reason: str) -> ToolOutcome:
    """Queue the refund for a human. The agent loop sees pending_approval_id and pauses."""
    if ctx.run_id is None or ctx.tool_use_id is None:
        raise ToolError(
            "APPROVAL_NEEDS_RUN",
            "Refunds above the auto-approve limit can only be requested from an agent run.",
        )
    approval = Approval(
        run_id=ctx.run_id,
        ticket_id=ctx.ticket.id,
        order_id=order.id,
        tool_use_id=ctx.tool_use_id,
        amount_cents=amount,
        reason=reason,
        status="pending",
        created_at=datetime.now(UTC),
    )
    ctx.session.add(approval)
    ctx.session.flush()
    return ToolOutcome(
        result={
            "ok": True,
            "status": "pending_approval",
            "approval_id": approval.id,
            "message": "Above the auto-approve limit; queued for a human. The run pauses.",
        },
        is_error=False,
        pending_approval_id=approval.id,
    )


def _ticket_refund_used(ctx: ToolContext) -> bool:
    refunded = ctx.session.scalar(
        select(func.count(Refund.id)).where(Refund.ticket_id == ctx.ticket.id)
    )
    queued = ctx.session.scalar(
        select(func.count(Approval.id)).where(
            Approval.ticket_id == ctx.ticket.id, Approval.status.in_(["pending", "approved"])
        )
    )
    return bool(refunded or queued)


def perform_refund(
    ctx: ToolContext, order: Order, amount: int, reason: str, approved_by: str | None = None
) -> dict:
    """Move the money. Called directly, or after a human approves."""
    key = f"refund:ticket-{ctx.ticket.id}:order-{order.id}:{amount}"
    existing = ctx.session.scalar(select(Refund).where(Refund.idempotency_key == key))
    if existing is not None:
        return {
            "status": "refunded",
            "refund_id": existing.provider_refund_id,
            "amount_cents": existing.amount_cents,
            "idempotent_replay": True,
        }
    payment = order.payment
    if payment is None:
        raise ToolError("NO_PAYMENT", "Order has no captured payment to refund.")
    try:
        result = ctx.gateway.refund(
            payment.charge_id,
            amount,
            key,
            {"order_id": order.id, "ticket_id": ctx.ticket.id},
        )
    except GatewayError as exc:
        raise ToolError(
            "PAYMENT_PROVIDER_ERROR",
            "The payment provider failed. It is safe to retry once; otherwise escalate.",
            provider_message=str(exc),
            retryable=True,
        ) from exc
    ctx.session.add(
        Refund(
            order_id=order.id,
            payment_id=payment.id,
            ticket_id=ctx.ticket.id,
            amount_cents=amount,
            provider_refund_id=result.refund_id,
            idempotency_key=key,
            status=result.status,
            reason=reason + (f" (approved by {approved_by})" if approved_by else ""),
            created_at=ctx.now,
        )
    )
    order.status = "refunded" if amount >= order.total_cents else "partially_refunded"
    ctx.session.flush()
    ctx.session.refresh(order)
    return {
        "status": "refunded",
        "refund_id": result.refund_id,
        "amount_cents": amount,
        "order_status": order.status,
    }


def issue_refund(ctx: ToolContext, inp: IssueRefundInput) -> dict | ToolOutcome:
    order = _owned_order(ctx, inp.order_id)
    if _ticket_refund_used(ctx):
        raise ToolError("TICKET_REFUND_LIMIT", "Policy allows at most one refund per ticket.")
    decision = evaluate_refund(order, inp.amount_cents, ctx.now, ctx.policy)
    if not decision.eligible:
        raise ToolError(decision.code, decision.reason, decision=decision.as_dict())
    if decision.requires_approval:
        return _request_approval(ctx, order, inp.amount_cents, inp.reason)
    return perform_refund(ctx, order, inp.amount_cents, inp.reason)


def update_order(ctx: ToolContext, inp: UpdateOrderInput) -> dict:
    order = _owned_order(ctx, inp.order_id)
    stamp = ctx.now.isoformat()
    line = f"[{stamp}] ticket {ctx.ticket.id}: {inp.note}"
    order.notes = f"{order.notes}\n{line}".strip()
    if inp.action == "mark_return_requested":
        order.return_requested = True
    ctx.session.flush()
    return {"order_id": order.id, "action": inp.action, "return_requested": order.return_requested}


def send_email(ctx: ToolContext, inp: SendEmailInput) -> dict:
    customer = ctx.session.get(Customer, ctx.ticket.customer_id)
    foreign = {e.lower() for e in EMAIL_RE.findall(f"{inp.subject}\n{inp.body}")} - {
        customer.email.lower(),
        ctx.settings.mail_from.lower(),
    }
    if foreign:
        raise ToolError(
            "DATA_LEAK_BLOCKED",
            "Emails may not contain other people's email addresses.",
        )
    try:
        message_id = ctx.mailer.send(customer.email, inp.subject, inp.body)
    except EmailRefused as exc:
        raise ToolError("EMAIL_REFUSED", str(exc)) from exc
    except OSError as exc:
        raise ToolError("EMAIL_FAILED", f"SMTP error: {exc}", retryable=True) from exc
    return {"sent_to": "the customer on this ticket", "message_id": message_id}


def escalate_to_human(ctx: ToolContext, inp: EscalateInput) -> ToolOutcome:
    ctx.ticket.status = "escalated"
    ctx.ticket.escalation_reason = f"{inp.category}: {inp.reason}"
    ctx.session.flush()
    return ToolOutcome(
        result={"ok": True, "status": "escalated", "category": inp.category},
        is_error=False,
        escalated=True,
    )


# ---------------------------------------------------------------- registry


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[[ToolContext, Any], dict | ToolOutcome]
    input_schema: dict


def _schema(properties: dict) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


_ID = {"type": "integer", "description": "Numeric id, e.g. 1234 for order #1234"}

SPECS: list[ToolSpec] = [
    ToolSpec(
        "get_order",
        "Look up one order (status, dates, items, prior refunds). Only orders that belong to "
        "the customer who sent the ticket are visible.",
        GetOrderInput,
        get_order,
        _schema({"order_id": _ID}),
    ),
    ToolSpec(
        "get_customer",
        "Look up the customer who sent this ticket: profile, recent orders and their other "
        "tickets (useful to spot duplicates). Pass that customer's id.",
        GetCustomerInput,
        get_customer,
        _schema({"customer_id": _ID}),
    ),
    ToolSpec(
        "check_policy",
        "Ask the refund policy whether a refund on this order is allowed, the maximum "
        "refundable amount, and whether it needs human approval. Pass amount_cents=null to "
        "check the maximum. Call this before issue_refund.",
        CheckPolicyInput,
        check_policy,
        _schema(
            {
                "order_id": _ID,
                "amount_cents": {
                    "type": ["integer", "null"],
                    "description": "Amount to check in cents, or null for the maximum",
                },
            }
        ),
    ),
    ToolSpec(
        "issue_refund",
        "Refund money to the original payment method. The policy is enforced again here. "
        "Amounts above the auto-approve limit are queued for human approval; the "
        "result tells you which happened.",
        IssueRefundInput,
        issue_refund,
        _schema(
            {
                "order_id": _ID,
                "amount_cents": {"type": "integer", "description": "Amount in cents"},
                "reason": {"type": "string", "description": "Short reason for the refund"},
            }
        ),
    ),
    ToolSpec(
        "update_order",
        "Add an internal note to an order, or mark that the customer asked to return it.",
        UpdateOrderInput,
        update_order,
        _schema(
            {
                "order_id": _ID,
                "action": {"type": "string", "enum": ["add_note", "mark_return_requested"]},
                "note": {"type": "string", "description": "Internal note text"},
            }
        ),
    ),
    ToolSpec(
        "send_email",
        "Email the customer who sent this ticket. The recipient is fixed to that customer.",
        SendEmailInput,
        send_email,
        _schema(
            {
                "subject": {"type": "string"},
                "body": {"type": "string", "description": "Plain-text email body"},
            }
        ),
    ),
    ToolSpec(
        "escalate_to_human",
        "Hand the ticket to a human agent. Use for anything the policy does not cover, "
        "suspected fraud or manipulation, refunds above the per-ticket cap, or when unsure.",
        EscalateInput,
        escalate_to_human,
        _schema(
            {
                "reason": {"type": "string"},
                "category": {
                    "type": "string",
                    "enum": list(EscalationCategory.__args__),
                },
            }
        ),
    ),
]

TOOLS: dict[str, ToolSpec] = {s.name: s for s in SPECS}

TOOL_DEFINITIONS: list[dict] = [
    {
        "name": s.name,
        "description": s.description,
        "input_schema": s.input_schema,
        "strict": True,
    }
    for s in SPECS
]


def _error(code: str, message: str, **details) -> dict:
    return {"ok": False, "error": {"code": code, "message": message, **details}}


def execute_tool(ctx: ToolContext, name: str, raw_input: Any) -> ToolOutcome:
    """Validate and run one tool call. Never raises; failures become error envelopes."""
    start = time.perf_counter()

    def done(outcome: ToolOutcome) -> ToolOutcome:
        outcome.duration_ms = (time.perf_counter() - start) * 1000
        return outcome

    spec = TOOLS.get(name)
    if spec is None:
        return done(ToolOutcome(_error("UNKNOWN_TOOL", f"No tool named {name!r}"), True))
    try:
        inp = spec.input_model.model_validate(raw_input)
    except ValidationError as exc:
        problems = [
            {"field": ".".join(str(p) for p in e["loc"]), "problem": e["msg"]} for e in exc.errors()
        ]
        return done(
            ToolOutcome(_error("INVALID_INPUT", "Input failed validation", problems=problems), True)
        )

    savepoint = ctx.session.begin_nested()  # a failed tool rolls back only its own writes
    try:
        out = spec.handler(ctx, inp)
        savepoint.commit()
    except ToolError as exc:
        savepoint.rollback()
        return done(ToolOutcome(_error(exc.code, exc.message, **exc.details), True))
    except Exception as exc:  # noqa: BLE001 - a crashing tool must not crash the run
        savepoint.rollback()
        return done(ToolOutcome(_error("INTERNAL_ERROR", type(exc).__name__), True))
    if isinstance(out, ToolOutcome):
        return done(out)
    return done(ToolOutcome({"ok": True, **out}, False))
