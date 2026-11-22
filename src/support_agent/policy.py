"""The refund policy from POLICY.md, as code.

Tools call `evaluate_refund` before moving money, so the policy holds even if
the model ignores the prompt. The model can also call it through check_policy.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime

from support_agent.config import Settings
from support_agent.models import Order


@dataclass(frozen=True)
class PolicyConfig:
    window_days: int = 30
    auto_approve_limit_cents: int = 10_000
    ticket_refund_cap_cents: int = 100_000
    max_refunds_per_order: int = 1

    @classmethod
    def from_settings(cls, s: Settings) -> PolicyConfig:
        return cls(
            window_days=s.refund_window_days,
            auto_approve_limit_cents=s.auto_approve_limit_cents,
            ticket_refund_cap_cents=s.ticket_refund_cap_cents,
        )


@dataclass
class PolicyDecision:
    order_id: int
    eligible: bool
    code: str
    reason: str
    max_refundable_cents: int
    requested_cents: int | None = None
    requires_approval: bool = False
    days_since_delivery: int | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _unshipped_value(order: Order) -> int:
    return sum((i.quantity - i.shipped_quantity) * i.unit_price_cents for i in order.items)


def evaluate_refund(
    order: Order, amount_cents: int | None, now: datetime, cfg: PolicyConfig
) -> PolicyDecision:
    def deny(code: str, reason: str, max_cents: int = 0, **kw) -> PolicyDecision:
        return PolicyDecision(order.id, False, code, reason, max_cents, amount_cents, **kw)

    if order.status == "cancelled":
        return deny("ORDER_CANCELLED", "Order was cancelled and already refunded.")
    if len(order.refunds) >= cfg.max_refunds_per_order or order.status in {
        "refunded",
        "partially_refunded",
    }:
        return deny("ALREADY_REFUNDED", "Policy allows at most one refund per order.")

    days = None
    notes: list[str] = []
    if order.status == "placed":
        max_cents = order.total_cents
        notes.append("Not shipped yet: a refund cancels the order.")
    elif order.status == "partially_shipped":
        max_cents = _unshipped_value(order)
        notes.append("Partial shipment: only the unshipped items can be refunded now.")
    elif order.status == "shipped":
        return deny(
            "IN_TRANSIT",
            "Order is in transit. Refunds are possible after delivery, within the window.",
        )
    elif order.status == "delivered":
        if order.delivered_at is None:
            return deny("NO_DELIVERY_DATE", "Delivery date unknown; needs a human.")
        days = (now - order.delivered_at).days
        if days > cfg.window_days:
            return deny(
                "PAST_WINDOW",
                f"Delivered {days} days ago; the refund window is {cfg.window_days} days.",
                days_since_delivery=days,
            )
        max_cents = order.total_cents
    else:
        return deny("UNSUPPORTED_STATUS", f"Order status '{order.status}' is not refundable.")

    amount = max_cents if amount_cents is None else amount_cents
    if amount <= 0:
        return deny(
            "INVALID_AMOUNT", "Refund amount must be positive.", max_cents, days_since_delivery=days
        )
    if amount > max_cents:
        return deny(
            "AMOUNT_EXCEEDS_REFUNDABLE",
            f"At most {max_cents} cents can be refunded on this order.",
            max_cents,
            days_since_delivery=days,
        )
    if amount > cfg.ticket_refund_cap_cents:
        return deny(
            "OVER_TICKET_CAP",
            f"Refunds above {cfg.ticket_refund_cap_cents} cents cannot be issued by the "
            "agent. Escalate to a human.",
            max_cents,
            days_since_delivery=days,
        )
    return PolicyDecision(
        order_id=order.id,
        eligible=True,
        code="ELIGIBLE",
        reason="Refund allowed by policy.",
        max_refundable_cents=max_cents,
        requested_cents=amount_cents,
        requires_approval=amount > cfg.auto_approve_limit_cents,
        days_since_delivery=days,
        notes=notes,
    )
