"""SQLAlchemy 2 models for the store and for the agent's own bookkeeping.

Money is always integer cents. Timestamps are timezone-aware UTC.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------- store tables


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    orders: Mapped[list[Order]] = relationship(back_populates="customer")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    # placed | shipped | partially_shipped | delivered | cancelled
    # | refunded | partially_refunded
    status: Mapped[str] = mapped_column(String(32))
    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    total_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="usd")
    notes: Mapped[str] = mapped_column(Text, default="")
    return_requested: Mapped[bool] = mapped_column(Boolean, default=False)

    customer: Mapped[Customer] = relationship(back_populates="orders")
    items: Mapped[list[OrderItem]] = relationship(back_populates="order", order_by="OrderItem.id")
    payment: Mapped[Payment | None] = relationship(back_populates="order")
    refunds: Mapped[list[Refund]] = relationship(back_populates="order", order_by="Refund.id")


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    sku: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(255))
    quantity: Mapped[int] = mapped_column(Integer)
    shipped_quantity: Mapped[int] = mapped_column(Integer)
    unit_price_cents: Mapped[int] = mapped_column(Integer)

    order: Mapped[Order] = relationship(back_populates="items")


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), unique=True)
    provider: Mapped[str] = mapped_column(String(16))  # fake | stripe
    charge_id: Mapped[str] = mapped_column(String(64))  # Stripe PaymentIntent id
    amount_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))  # succeeded
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    order: Mapped[Order] = relationship(back_populates="payment")


class Refund(Base):
    __tablename__ = "refunds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id"))
    ticket_id: Mapped[int | None] = mapped_column(ForeignKey("tickets.id"))
    amount_cents: Mapped[int] = mapped_column(Integer)
    provider_refund_id: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    order: Mapped[Order] = relationship(back_populates="refunds")


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # The authenticated sender. The agent may only act on this customer's data.
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    subject: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    # open | in_progress | awaiting_approval | resolved | escalated
    status: Mapped[str] = mapped_column(String(32), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    escalation_reason: Mapped[str | None] = mapped_column(Text)


# ---------------------------------------------------------------- agent tables


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    # running | awaiting_approval | completed | escalated | failed
    status: Mapped[str] = mapped_column(String(32))
    variant: Mapped[str] = mapped_column(String(32), default="baseline")
    model: Mapped[str] = mapped_column(String(64))
    messages: Mapped[list] = mapped_column(JSON, default=list)
    # tool_result blocks already computed in the step that paused for approval
    pending_results: Mapped[list] = mapped_column(JSON, default=list)
    steps: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    final_text: Mapped[str | None] = mapped_column(Text)
    stop_reason: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text)
    trace_id: Mapped[str | None] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active_seconds: Mapped[float] = mapped_column(Float, default=0.0)


class ToolCall(Base):
    __tablename__ = "tool_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    step: Mapped[int] = mapped_column(Integer)
    tool_use_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(64))
    input: Mapped[dict] = mapped_column(JSON)
    output: Mapped[dict] = mapped_column(JSON)
    is_error: Mapped[bool] = mapped_column(Boolean)
    duration_ms: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Approval(Base):
    __tablename__ = "approvals"
    __table_args__ = (UniqueConstraint("run_id", "tool_use_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"))
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    tool_use_id: Mapped[str] = mapped_column(String(64))
    amount_cents: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    decided_by: Mapped[str | None] = mapped_column(String(64))
    decision_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
