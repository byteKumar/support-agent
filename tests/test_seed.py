import hashlib

from sqlalchemy import func, select

from support_agent.models import Customer, Order, OrderItem, Payment, Refund, Ticket
from support_agent.seed import reset_store, seed


def _fingerprint(session) -> str:
    rows = session.execute(
        select(Order.id, Order.customer_id, Order.status, Order.total_cents).order_by(Order.id)
    ).all()
    return hashlib.sha256(repr(rows).encode()).hexdigest()


def test_counts(session):
    assert session.scalar(select(func.count(Customer.id))) == 500
    assert session.scalar(select(func.count(Order.id))) == 2000
    assert session.scalar(select(func.count(Payment.id))) == 2000
    assert session.scalar(select(func.count(Ticket.id))) >= 150


def test_every_customer_email_is_example_dot_com(session):
    emails = session.scalars(select(Customer.email)).all()
    assert all(e.endswith("@example.com") for e in emails)


def test_edge_cases_present(session):
    statuses = dict(
        session.execute(select(Order.status, func.count()).group_by(Order.status)).all()
    )
    for status in (
        "delivered",
        "shipped",
        "placed",
        "partially_shipped",
        "refunded",
        "partially_refunded",
        "cancelled",
    ):
        assert statuses.get(status, 0) > 0, status
    high_value = session.scalar(select(func.count()).where(Order.total_cents > 40000))
    assert high_value > 20
    unshipped = session.scalar(
        select(func.count()).where(OrderItem.shipped_quantity < OrderItem.quantity)
    )
    assert unshipped > 0
    dup = session.execute(
        select(Ticket.customer_id, Ticket.body, func.count())
        .group_by(Ticket.customer_id, Ticket.body)
        .having(func.count() > 1)
    ).all()
    assert len(dup) >= 2


def test_anchor_orders(session, settings):
    o1, o2, o3, o4 = (session.get(Order, i) for i in (1, 2, 3, 4))
    assert o1.customer_id == 1 and o1.total_cents == 4500 and o1.status == "delivered"
    assert (settings.store_now - o2.delivered_at).days == 60
    assert o3.customer_id == 2 and o3.total_cents == 35000
    assert o4.status == "refunded" and len(o4.refunds) == 1
    o5 = session.get(Order, 5)
    assert o5.status == "partially_shipped"
    assert sum(i.shipped_quantity < i.quantity for i in o5.items) == 1
    assert session.get(Order, 7).total_cents == 145000


def test_seed_is_deterministic(session, settings):
    before = _fingerprint(session)
    reset_store(session)
    seed(session, now=settings.store_now)
    assert _fingerprint(session) == before
    assert session.scalar(select(func.count(Refund.id))) > 50
