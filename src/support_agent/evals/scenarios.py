"""Scenario schema, loader and validator.

A scenario is one ticket plus the outcome Chaman expects. One JSON object per line
in evals/scenarios.jsonl. See evals/HOW_TO_WRITE_SCENARIOS.md.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.orm import Session

from support_agent.config import Settings
from support_agent.models import Customer, Order
from support_agent.policy import PolicyConfig, evaluate_refund

MIN_SCENARIOS = 150

ToolName = Literal[
    "get_order",
    "get_customer",
    "check_policy",
    "issue_refund",
    "update_order",
    "send_email",
    "escalate_to_human",
]

Category = Literal[
    "refund_simple",
    "refund_partial_amount",
    "past_window",
    "already_refunded",
    "over_threshold",
    "over_cap",
    "in_transit",
    "partial_shipment",
    "cancellation",
    "info_request",
    "duplicate_ticket",
    "no_order_number",
    "wrong_order_number",
    "policy_exception",
    "prompt_injection",
    "other_customer_data",
    "social_engineering",
    "abusive_or_threat",
    "other",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TicketSpec(_Strict):
    customer_id: int = Field(ge=1)
    subject: str = Field(min_length=1, max_length=255)
    body: str = Field(min_length=1)


class Expected(_Strict):
    outcome: Literal["refund", "no_refund", "escalate"]
    order_id: int | None = None
    refund_amount_cents: int | None = Field(default=None, ge=1)
    approval_decision: Literal["approve", "reject"] | None = None
    must_call: list[ToolName] = Field(default_factory=list)
    must_not_call: list[ToolName] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> Expected:
        if self.outcome == "refund":
            if self.order_id is None or self.refund_amount_cents is None:
                raise ValueError("outcome 'refund' needs order_id and refund_amount_cents")
            if self.approval_decision == "reject":
                raise ValueError("a rejected approval cannot end in outcome 'refund'")
        elif self.refund_amount_cents is not None:
            raise ValueError("refund_amount_cents only makes sense with outcome 'refund'")
        if set(self.must_call) & set(self.must_not_call):
            raise ValueError("a tool cannot be in both must_call and must_not_call")
        return self


class Scenario(_Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    category: Category
    adversarial: bool = False
    injection: bool = False
    format_example: bool = False
    ticket: TicketSpec
    expected: Expected
    notes: str = ""


class ScenarioFileError(Exception):
    pass


def load_scenarios(path: str | Path) -> list[Scenario]:
    path = Path(path)
    scenarios: list[Scenario] = []
    problems: list[str] = []
    seen: set[str] = set()
    for n, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("//"):
            continue
        try:
            sc = Scenario.model_validate(json.loads(line))
        except json.JSONDecodeError as exc:
            problems.append(f"line {n}: invalid JSON ({exc.msg})")
            continue
        except ValidationError as exc:
            msgs = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'scenario'}: {e['msg']}"
                for e in exc.errors()
            )
            problems.append(f"line {n}: {msgs}")
            continue
        if sc.id in seen:
            problems.append(f"line {n}: duplicate id {sc.id!r}")
        seen.add(sc.id)
        scenarios.append(sc)
    if problems:
        raise ScenarioFileError("\n".join(problems))
    return scenarios


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_against_store(
    scenarios: list[Scenario], session: Session, settings: Settings
) -> ValidationReport:
    """Check scenarios against the seeded store and the policy.

    Errors: references that do not exist, or expected refunds the policy forbids.
    Warnings: expectations that look odd and are worth a second look.
    """
    rep = ValidationReport()
    cfg = PolicyConfig.from_settings(settings)
    for sc in scenarios:
        e = sc.expected
        where = f"{sc.id}:"
        if session.get(Customer, sc.ticket.customer_id) is None:
            rep.errors.append(f"{where} customer {sc.ticket.customer_id} does not exist")
            continue
        order = session.get(Order, e.order_id) if e.order_id else None
        if e.order_id and order is None:
            rep.errors.append(f"{where} order {e.order_id} does not exist")
            continue
        foreign = order is not None and order.customer_id != sc.ticket.customer_id
        if e.outcome == "refund":
            if foreign:
                rep.errors.append(f"{where} expects a refund on another customer's order")
                continue
            d = evaluate_refund(order, e.refund_amount_cents, settings.store_now, cfg)
            if not d.eligible:
                rep.errors.append(f"{where} policy forbids this refund ({d.code}: {d.reason})")
            elif d.requires_approval and e.approval_decision is None:
                rep.errors.append(f"{where} refund needs human approval; set approval_decision")
        if foreign and not (sc.adversarial or sc.category == "other_customer_data"):
            rep.warnings.append(
                f"{where} order belongs to another customer but scenario is not adversarial"
            )
        if e.outcome == "no_refund" and order is not None and not foreign:
            d = evaluate_refund(order, None, settings.store_now, cfg)
            if d.eligible and e.approval_decision is None:
                rep.warnings.append(
                    f"{where} policy would allow a refund here; check 'no_refund' is intended"
                )
        if sc.injection and not sc.adversarial:
            rep.warnings.append(f"{where} injection scenarios should also set adversarial=true")
    return rep
