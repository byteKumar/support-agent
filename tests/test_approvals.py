import json
from datetime import timedelta

import pytest

from support_agent.approvals import ApprovalError, decide, decide_and_resume, pending_approvals
from support_agent.llm import FakeLLM, text_turn, tool_turn
from support_agent.models import AgentRun, Approval, Order, OrderItem, Payment, Refund, Ticket
from support_agent.payments import FakeGateway
from support_agent.tools import execute_tool
from tests.helpers import make_agent, make_ctx, new_ticket


def _pause_on_order_3(session_factory, settings, gateway=None):
    tid = new_ticket(session_factory, settings, 2, "Headphones (order #3) are broken, refund?")
    script = [
        tool_turn(("check_policy", {"order_id": 3, "amount_cents": 35000})),
        tool_turn(("issue_refund", {"order_id": 3, "amount_cents": 35000, "reason": "broken"})),
    ]
    agent, deps = make_agent(session_factory, settings, script, gateway=gateway)
    run_id = agent.run_ticket(tid)
    return tid, run_id, deps


def test_over_threshold_pauses_run(session_factory, settings):
    gw = FakeGateway()
    tid, run_id, _ = _pause_on_order_3(session_factory, settings, gw)
    with session_factory() as s:
        run = s.get(AgentRun, run_id)
        assert run.status == "awaiting_approval"
        assert s.get(Ticket, tid).status == "awaiting_approval"
        (approval,) = pending_approvals(s)
        assert approval.amount_cents == 35000 and approval.order_id == 3
        assert run.messages[-1]["role"] == "assistant"
        assert run.pending_results[0]["tool_use_id"] == approval.tool_use_id
    assert gw.refund_calls == 0


def _resume_agent(session_factory, settings, gw, script):
    """A fresh agent object, as if the process restarted while waiting for a human."""
    agent, deps = make_agent(session_factory, settings, FakeLLM(script), gateway=gw)
    return agent, deps


def test_approve_resumes_and_refunds(session_factory, settings):
    gw = FakeGateway()
    tid, run_id, _ = _pause_on_order_3(session_factory, settings, gw)
    with session_factory() as s:
        approval_id = pending_approvals(s)[0].id
    agent, deps = _resume_agent(
        session_factory,
        settings,
        gw,
        [tool_turn(("send_email", {"subject": "Refund", "body": "Approved."})), text_turn("done")],
    )
    decide_and_resume(agent, session_factory, approval_id, "approve", "ops@example.com")
    with session_factory() as s:
        run = s.get(AgentRun, run_id)
        assert run.status == "completed"
        refund = s.query(Refund).filter_by(ticket_id=tid).one()
        assert refund.amount_cents == 35000 and "ops@example.com" in refund.reason
        assert s.get(Order, 3).status == "refunded"
    sent = deps.llm.calls[0]["messages"][-1]["content"][0]
    assert json.loads(sent["content"])["status"] == "refunded"
    assert sent["is_error"] is False
    assert gw.refund_calls == 1


def test_reject_resumes_without_refund(session_factory, settings):
    gw = FakeGateway()
    tid, run_id, _ = _pause_on_order_3(session_factory, settings, gw)
    with session_factory() as s:
        approval_id = pending_approvals(s)[0].id
    agent, deps = _resume_agent(session_factory, settings, gw, [text_turn("told them no")])
    decide_and_resume(agent, session_factory, approval_id, "reject", "ops", "looks like abuse")
    with session_factory() as s:
        assert s.get(AgentRun, run_id).status == "completed"
        assert s.query(Refund).filter_by(ticket_id=tid).count() == 0
    sent = deps.llm.calls[0]["messages"][-1]["content"][0]
    assert json.loads(sent["content"])["error"]["code"] == "APPROVAL_REJECTED"
    assert gw.refund_calls == 0


def test_decision_only_once(session_factory, settings):
    _pause_on_order_3(session_factory, settings)
    with session_factory() as s:
        approval_id = pending_approvals(s)[0].id
    decide(session_factory, approval_id, "approve", "a")
    with pytest.raises(ApprovalError):
        decide(session_factory, approval_id, "reject", "b")


def test_policy_rechecked_at_approval_time(session_factory, settings):
    gw = FakeGateway()
    _, run_id, _ = _pause_on_order_3(session_factory, settings, gw)
    with session_factory() as s:
        approval_id = pending_approvals(s)[0].id
        order = s.get(Order, 3)
        s.add(
            Refund(
                order_id=3,
                payment_id=order.payment.id,
                amount_cents=100,
                provider_refund_id="re_manual",
                idempotency_key="manual-1",
                status="succeeded",
                reason="refunded by phone",
                created_at=settings.store_now,
            )
        )
        s.commit()
    agent, deps = _resume_agent(session_factory, settings, gw, [text_turn("ok")])
    decide_and_resume(agent, session_factory, approval_id, "approve", "ops")
    sent = deps.llm.calls[0]["messages"][-1]["content"][0]
    assert json.loads(sent["content"])["error"]["code"] == "ALREADY_REFUNDED"
    assert gw.refund_calls == 0


def test_one_refund_per_ticket(session, settings):
    extra = Order(
        customer_id=1,
        status="delivered",
        placed_at=settings.store_now - timedelta(days=4),
        delivered_at=settings.store_now - timedelta(days=2),
        total_cents=3000,
        currency="usd",
        notes="",
    )
    session.add(extra)
    session.flush()
    session.add(
        OrderItem(
            order_id=extra.id,
            sku="SKU-MUG",
            name="Mug",
            quantity=1,
            shipped_quantity=1,
            unit_price_cents=3000,
        )
    )
    session.add(
        Payment(
            order_id=extra.id,
            provider="fake",
            charge_id="pi_x",
            amount_cents=3000,
            status="succeeded",
            created_at=settings.store_now,
        )
    )
    session.flush()
    ctx = make_ctx(session, settings, customer_id=1)
    first = execute_tool(
        ctx, "issue_refund", {"order_id": 1, "amount_cents": 4500, "reason": "a b"}
    )
    assert not first.is_error
    second = execute_tool(
        ctx, "issue_refund", {"order_id": extra.id, "amount_cents": 3000, "reason": "a b"}
    )
    assert second.result["error"]["code"] == "TICKET_REFUND_LIMIT"


def test_duplicate_ticket_cannot_double_refund(session, settings):
    ctx1 = make_ctx(session, settings, customer_id=1)
    ctx2 = make_ctx(session, settings, customer_id=1)
    args = {"order_id": 1, "amount_cents": 4500, "reason": "torn strap"}
    assert not execute_tool(ctx1, "issue_refund", args).is_error
    assert execute_tool(ctx2, "issue_refund", args).result["error"]["code"] == "ALREADY_REFUNDED"


def test_over_cap_never_queued(session, settings):
    ctx = make_ctx(session, settings, customer_id=4, run_id=None)
    out = execute_tool(
        ctx, "issue_refund", {"order_id": 7, "amount_cents": 145000, "reason": "abc"}
    )
    assert out.result["error"]["code"] == "OVER_TICKET_CAP"
    assert session.query(Approval).count() == 0


def test_over_threshold_outside_a_run_is_refused(session, settings):
    ctx = make_ctx(session, settings, customer_id=2)
    out = execute_tool(ctx, "issue_refund", {"order_id": 3, "amount_cents": 35000, "reason": "abc"})
    assert out.result["error"]["code"] == "APPROVAL_NEEDS_RUN"


def test_email_with_other_customers_address_blocked(session, settings):
    ctx = make_ctx(session, settings, customer_id=1)
    out = execute_tool(
        ctx, "send_email", {"subject": "Info", "body": "Order 3 belongs to jordan.lee2@example.com"}
    )
    assert out.result["error"]["code"] == "DATA_LEAK_BLOCKED"
    assert ctx.mailer.sent == []
    ok = execute_tool(
        ctx, "send_email", {"subject": "Hi", "body": "Reply to avery.park1@example.com"}
    )
    assert not ok.is_error


def test_require_policy_check_option(session_factory, settings):
    import dataclasses

    strict = dataclasses.replace(settings, require_policy_check=True)
    skip = [
        tool_turn(("issue_refund", {"order_id": 1, "amount_cents": 4500, "reason": "torn"})),
        text_turn("done"),
    ]
    tid = new_ticket(session_factory, strict, 1, "refund order 1")
    agent, _ = make_agent(session_factory, strict, skip)
    run_id = agent.run_ticket(tid)
    with session_factory() as s:
        from support_agent.models import ToolCall

        call = s.query(ToolCall).filter_by(run_id=run_id).one()
        assert call.output["error"]["code"] == "POLICY_CHECK_REQUIRED"
        assert s.query(Refund).filter_by(ticket_id=tid).count() == 0

    wrong_order_checked = [
        tool_turn(("check_policy", {"order_id": 2, "amount_cents": None})),
        tool_turn(("issue_refund", {"order_id": 1, "amount_cents": 4500, "reason": "torn"})),
        tool_turn(("check_policy", {"order_id": 1, "amount_cents": 4500})),
        tool_turn(("issue_refund", {"order_id": 1, "amount_cents": 4500, "reason": "torn"})),
        text_turn("done"),
    ]
    tid = new_ticket(session_factory, strict, 1, "refund order 1")
    agent, _ = make_agent(session_factory, strict, wrong_order_checked)
    run_id = agent.run_ticket(tid)
    with session_factory() as s:
        from support_agent.models import ToolCall

        codes = [
            (c.output.get("error") or {}).get("code")
            for c in s.query(ToolCall).filter_by(run_id=run_id).order_by(ToolCall.id)
        ]
        assert codes == [None, "POLICY_CHECK_REQUIRED", None, None]
        assert s.query(Refund).filter_by(ticket_id=tid).count() == 1


def test_improved_variant_turns_on_policy_check(session_factory, settings):
    tid = new_ticket(session_factory, settings, 1, "refund order 1")
    script = [
        tool_turn(("issue_refund", {"order_id": 1, "amount_cents": 4500, "reason": "torn"})),
        text_turn("done"),
    ]
    agent, _ = make_agent(session_factory, settings, script)
    agent.run_ticket(tid, variant="improved")
    with session_factory() as s:
        assert s.query(Refund).filter_by(ticket_id=tid).count() == 0
