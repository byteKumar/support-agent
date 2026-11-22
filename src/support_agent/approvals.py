"""Human approval queue: list pending refunds, record a decision, resume the run."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from support_agent.models import Approval


class ApprovalError(Exception):
    pass


def pending_approvals(session: Session) -> list[Approval]:
    return list(
        session.scalars(
            select(Approval).where(Approval.status == "pending").order_by(Approval.created_at)
        )
    )


def decide(
    session_factory: Callable[[], Session],
    approval_id: int,
    decision: Literal["approve", "reject"],
    decided_by: str,
    note: str | None = None,
) -> Approval:
    """Record a decision exactly once. The row lock stops two reviewers (or a double
    click) from both deciding the same approval."""
    if decision not in ("approve", "reject"):
        raise ApprovalError("decision must be approve or reject")
    with session_factory() as s:
        approval = s.scalar(select(Approval).where(Approval.id == approval_id).with_for_update())
        if approval is None:
            raise ApprovalError("approval not found")
        if approval.status != "pending":
            raise ApprovalError(f"approval already {approval.status}")
        approval.status = "approved" if decision == "approve" else "rejected"
        approval.decided_by = decided_by
        approval.decision_note = note
        approval.decided_at = datetime.now(UTC)
        s.commit()
        return approval


def decide_and_resume(agent, session_factory, approval_id, decision, decided_by, note=None):
    decide(session_factory, approval_id, decision, decided_by, note)
    return agent.resume(approval_id)
