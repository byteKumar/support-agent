from support_agent.models import Order
from support_agent.policy import PolicyConfig, evaluate_refund

CFG = PolicyConfig()


def _eval(session, settings, oid, amount=None):
    return evaluate_refund(session.get(Order, oid), amount, settings.store_now, CFG)


def test_simple_refund_eligible_no_approval(session, settings):
    d = _eval(session, settings, 1, 4500)
    assert d.eligible and not d.requires_approval and d.max_refundable_cents == 4500


def test_past_window(session, settings):
    d = _eval(session, settings, 2)
    assert not d.eligible and d.code == "PAST_WINDOW" and d.days_since_delivery == 60


def test_over_threshold_requires_approval(session, settings):
    d = _eval(session, settings, 3, 35000)
    assert d.eligible and d.requires_approval


def test_already_refunded(session, settings):
    assert _eval(session, settings, 4).code == "ALREADY_REFUNDED"


def test_partial_shipment_only_unshipped(session, settings):
    d = _eval(session, settings, 5)
    assert d.eligible and d.max_refundable_cents == 5000
    assert _eval(session, settings, 5, 12000).code == "AMOUNT_EXCEEDS_REFUNDABLE"


def test_in_transit(session, settings):
    assert _eval(session, settings, 6).code == "IN_TRANSIT"


def test_over_ticket_cap(session, settings):
    assert _eval(session, settings, 7, 145000).code == "OVER_TICKET_CAP"


def test_unshipped_order_cancellation(session, settings):
    d = _eval(session, settings, 8)
    assert d.eligible and d.max_refundable_cents == 2500


def test_zero_amount(session, settings):
    assert _eval(session, settings, 1, 0).code == "INVALID_AMOUNT"


def test_window_boundary(session, settings):
    from datetime import timedelta

    order = session.get(Order, 1)
    order.delivered_at = settings.store_now - timedelta(days=30, hours=1)
    assert evaluate_refund(order, None, settings.store_now, CFG).eligible
    order.delivered_at = settings.store_now - timedelta(days=31, hours=1)
    assert evaluate_refund(order, None, settings.store_now, CFG).code == "PAST_WINDOW"
