import pytest
from pydantic import BaseModel

from support_agent.models import Order, Refund, Ticket
from support_agent.payments import FakeGateway
from support_agent.tools import SPECS, TOOL_DEFINITIONS, execute_tool
from tests.helpers import make_ctx


def test_tool_definitions_are_strict_and_match_models():
    assert {d["name"] for d in TOOL_DEFINITIONS} == {
        "get_order",
        "get_customer",
        "check_policy",
        "issue_refund",
        "update_order",
        "send_email",
        "escalate_to_human",
    }
    for spec, d in zip(SPECS, TOOL_DEFINITIONS, strict=True):
        schema = d["input_schema"]
        assert d["strict"] is True
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        model: type[BaseModel] = spec.input_model
        assert set(model.model_fields) == set(schema["properties"])


def test_get_order_own(session, settings):
    ctx = make_ctx(session, settings, customer_id=1)
    out = execute_tool(ctx, "get_order", {"order_id": 1})
    assert not out.is_error
    assert out.result["order"]["total_cents"] == 4500


@pytest.mark.parametrize("order_id", [3, 999999])
def test_get_order_other_customer_or_missing_look_the_same(session, settings, order_id):
    ctx = make_ctx(session, settings, customer_id=1)
    out = execute_tool(ctx, "get_order", {"order_id": order_id})
    assert out.is_error
    assert out.result["error"]["code"] == "ORDER_NOT_FOUND"
    assert "Jordan" not in str(out.result)


@pytest.mark.parametrize(
    "payload",
    [{"order_id": "1"}, {"order_id": 1, "extra": True}, {}, {"order_id": True}, {"order_id": 0}],
)
def test_invalid_input_is_structured_error(session, settings, payload):
    ctx = make_ctx(session, settings)
    out = execute_tool(ctx, "get_order", payload)
    assert out.is_error and out.result["error"]["code"] == "INVALID_INPUT"
    assert out.result["error"]["problems"]


def test_unknown_tool(session, settings):
    out = execute_tool(make_ctx(session, settings), "drop_tables", {})
    assert out.result["error"]["code"] == "UNKNOWN_TOOL"


def test_get_customer_only_self(session, settings):
    ctx = make_ctx(session, settings, customer_id=1)
    assert not execute_tool(ctx, "get_customer", {"customer_id": 1}).is_error
    out = execute_tool(ctx, "get_customer", {"customer_id": 2})
    assert out.result["error"]["code"] == "FORBIDDEN"


def test_get_customer_shows_duplicate_tickets(session, settings):
    ctx = make_ctx(session, settings, customer_id=1)
    out = execute_tool(ctx, "get_customer", {"customer_id": 1})
    subjects = [t["subject"] for t in out.result["other_tickets"]]
    assert subjects.count("Torn strap") == 2


def test_check_policy(session, settings):
    ctx = make_ctx(session, settings, customer_id=1)
    out = execute_tool(ctx, "check_policy", {"order_id": 2, "amount_cents": None})
    assert out.result["decision"]["code"] == "PAST_WINDOW"
    missing = execute_tool(ctx, "check_policy", {"order_id": 2})
    assert missing.result["error"]["code"] == "INVALID_INPUT"


def test_issue_refund_happy_path(session, settings):
    gw = FakeGateway()
    ctx = make_ctx(session, settings, customer_id=1, gateway=gw)
    out = execute_tool(
        ctx, "issue_refund", {"order_id": 1, "amount_cents": 4500, "reason": "torn strap"}
    )
    assert not out.is_error, out.result
    assert out.result["status"] == "refunded"
    assert gw.refund_calls == 1
    refund = session.query(Refund).filter_by(ticket_id=ctx.ticket.id).one()
    assert refund.idempotency_key == f"refund:ticket-{ctx.ticket.id}:order-1:4500"
    assert session.get(Order, 1).status == "refunded"


def test_issue_refund_past_window_never_calls_gateway(session, settings):
    gw = FakeGateway()
    ctx = make_ctx(session, settings, customer_id=1, gateway=gw)
    out = execute_tool(
        ctx, "issue_refund", {"order_id": 2, "amount_cents": 8000, "reason": "x y z"}
    )
    assert out.result["error"]["code"] == "PAST_WINDOW"
    assert gw.refund_calls == 0


def test_issue_refund_on_other_customers_order(session, settings):
    gw = FakeGateway()
    ctx = make_ctx(session, settings, customer_id=1, gateway=gw)
    out = execute_tool(ctx, "issue_refund", {"order_id": 3, "amount_cents": 100, "reason": "abc"})
    assert out.result["error"]["code"] == "ORDER_NOT_FOUND"
    assert gw.refund_calls == 0


def test_issue_refund_amount_too_high(session, settings):
    ctx = make_ctx(session, settings, customer_id=3)
    out = execute_tool(ctx, "issue_refund", {"order_id": 5, "amount_cents": 12000, "reason": "abc"})
    assert out.result["error"]["code"] == "AMOUNT_EXCEEDS_REFUNDABLE"


def test_gateway_failure_rolls_back(session, settings):
    gw = FakeGateway(fail_next=True)
    ctx = make_ctx(session, settings, customer_id=1, gateway=gw)
    out = execute_tool(ctx, "issue_refund", {"order_id": 1, "amount_cents": 4500, "reason": "abc"})
    assert out.result["error"]["code"] == "PAYMENT_PROVIDER_ERROR"
    assert out.result["error"]["retryable"] is True
    assert session.query(Refund).filter_by(ticket_id=ctx.ticket.id).count() == 0
    assert session.get(Order, 1).status == "delivered"


def test_update_order(session, settings):
    ctx = make_ctx(session, settings, customer_id=1)
    out = execute_tool(
        ctx, "update_order", {"order_id": 1, "action": "mark_return_requested", "note": "strap"}
    )
    assert out.result["return_requested"] is True
    assert "strap" in session.get(Order, 1).notes


def test_send_email_goes_to_ticket_customer(session, settings):
    ctx = make_ctx(session, settings, customer_id=1)
    out = execute_tool(ctx, "send_email", {"subject": "Hi", "body": "Your refund is done."})
    assert not out.is_error
    assert ctx.mailer.sent[0]["to"] == "avery.park1@example.com"


def test_escalate(session, settings):
    ctx = make_ctx(session, settings, customer_id=1)
    out = execute_tool(
        ctx, "escalate_to_human", {"reason": "customer threatens chargeback", "category": "other"}
    )
    assert out.escalated
    assert session.get(Ticket, ctx.ticket.id).status == "escalated"
