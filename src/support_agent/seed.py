"""Deterministic seed data for the store.

    uv run python -m support_agent.seed --reset

Uses a fixed random seed and the frozen STORE_NOW clock, so every run produces
identical rows. The first few customers/orders are hand-placed "anchors" with
known edge cases; FORMAT_EXAMPLES.jsonl and the tests reference them by id.

If STRIPE_SECRET_KEY holds an sk_test_ key, each order also gets a real
Stripe test-mode PaymentIntent (pm_card_visa). Otherwise charge ids are fake.
"""

from __future__ import annotations

import argparse
import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, text
from sqlalchemy.orm import Session

from support_agent.config import Settings
from support_agent.models import (
    AgentRun,
    Approval,
    Customer,
    Order,
    OrderItem,
    Payment,
    Refund,
    Ticket,
    ToolCall,
)
from support_agent.payments import PaymentGateway, StripeGateway, check_test_key

RNG_SEED = 42

FIRST = [
    "Avery",
    "Jordan",
    "Sam",
    "Taylor",
    "Morgan",
    "Riley",
    "Casey",
    "Jamie",
    "Quinn",
    "Drew",
    "Alex",
    "Robin",
    "Skyler",
    "Reese",
    "Rowan",
    "Emerson",
    "Hayden",
    "Parker",
    "Kendall",
    "Logan",
    "Priya",
    "Wei",
    "Mateo",
    "Fatima",
    "Noah",
    "Olivia",
    "Liam",
    "Amara",
    "Kenji",
    "Sofia",
    "Diego",
    "Aisha",
    "Lucas",
    "Mei",
    "Omar",
    "Elena",
]
LAST = [
    "Park",
    "Lee",
    "Rivera",
    "Chen",
    "Patel",
    "Nguyen",
    "Garcia",
    "Smith",
    "Johnson",
    "Brown",
    "Kim",
    "Martinez",
    "Lopez",
    "Wilson",
    "Anderson",
    "Thomas",
    "Moore",
    "Jackson",
    "Martin",
    "Thompson",
    "White",
    "Harris",
    "Clark",
    "Lewis",
    "Walker",
    "Hall",
    "Young",
    "Allen",
    "King",
    "Wright",
    "Scott",
    "Green",
    "Baker",
    "Adams",
]

CATALOG = [
    ("SKU-MUG", "Ceramic mug", 1400),
    ("SKU-TEE", "Cotton t-shirt", 2200),
    ("SKU-CAP", "Baseball cap", 1800),
    ("SKU-BTL", "Insulated bottle", 2900),
    ("SKU-NBK", "Dot-grid notebook", 1200),
    ("SKU-PEN", "Gel pen 5-pack", 800),
    ("SKU-HDP", "Hoodie", 5500),
    ("SKU-BAG", "Canvas tote", 2500),
    ("SKU-LMP", "Desk lamp", 4500),
    ("SKU-KBD", "Mechanical keyboard", 11900),
    ("SKU-MSE", "Wireless mouse", 3900),
    ("SKU-HPH", "Over-ear headphones", 17900),
    ("SKU-SPK", "Bluetooth speaker", 7900),
    ("SKU-CHR", "USB-C charger", 3400),
    ("SKU-CBL", "Braided cable", 1500),
    ("SKU-PLT", "Plant pot", 1900),
    ("SKU-CND", "Soy candle", 2100),
    ("SKU-BLN", "Wool blanket", 8900),
    ("SKU-BKP", "Laptop backpack", 9500),
    ("SKU-WTC", "Analog watch", 14900),
]
HIGH_VALUE = [
    ("SKU-ESP", "Espresso machine", 64900),
    ("SKU-CHA", "Ergonomic chair", 48900),
    ("SKU-MON", "27-inch monitor", 39900),
    ("SKU-BIK", "Commuter e-bike", 145000),
]

TICKET_TEMPLATES = [
    ("Where is my order?", "Hi, order #{oid} hasn't arrived yet. Can you check?"),
    ("Refund request", "I'd like a refund for order #{oid}, the item was not what I expected."),
    ("Damaged item", "Order #{oid} arrived damaged. Please help."),
    ("Wrong size", "I got the wrong size in order #{oid}. Can I return it?"),
    ("Cancel order", "Please cancel order #{oid} if it hasn't shipped."),
]


@dataclass
class SeedSummary:
    customers: int
    orders: int
    items: int
    refunds: int
    tickets: int
    stripe_charges: int


def _email(first: str, last: str, n: int) -> str:
    return f"{first.lower()}.{last.lower()}{n}@example.com"


def reset_store(session: Session) -> None:
    for model in (ToolCall, Approval, AgentRun, Refund, Payment, OrderItem):
        session.execute(delete(model))
    session.execute(delete(Ticket))
    session.execute(delete(Order))
    session.execute(delete(Customer))
    session.flush()


class _Builder:
    def __init__(self, session: Session, now: datetime, gateway: PaymentGateway | None):
        self.s = session
        self.now = now
        self.gateway = gateway
        self.items = 0
        self.refunds = 0
        self.stripe_charges = 0
        self.next_order_id = 1
        self.next_ticket_id = 1

    def customer(self, cid: int, first: str, last: str, days_ago: int) -> Customer:
        c = Customer(
            id=cid,
            email=_email(first, last, cid),
            name=f"{first} {last}",
            created_at=self.now - timedelta(days=days_ago),
        )
        self.s.add(c)
        return c

    def order(
        self,
        customer_id: int,
        lines: list[tuple[str, str, int, int, int]],  # sku, name, qty, shipped, price
        placed_days_ago: float,
        status: str,
        delivered_days_ago: float | None = None,
        refund_cents: int = 0,
        refund_reason: str = "",
    ) -> Order:
        oid = self.next_order_id
        self.next_order_id += 1
        placed = self.now - timedelta(days=placed_days_ago)
        delivered = (
            self.now - timedelta(days=delivered_days_ago)
            if delivered_days_ago is not None
            else None
        )
        shipped = None
        if status in {"shipped", "partially_shipped"} or delivered is not None:
            shipped = placed + timedelta(days=1)
        total = sum(q * p for _, _, q, _, p in lines)
        order = Order(
            id=oid,
            customer_id=customer_id,
            status=status,
            placed_at=placed,
            shipped_at=shipped,
            delivered_at=delivered,
            total_cents=total,
            currency="usd",
            notes="",
        )
        self.s.add(order)
        for sku, name, qty, shipped_qty, price in lines:
            self.s.add(
                OrderItem(
                    order_id=oid,
                    sku=sku,
                    name=name,
                    quantity=qty,
                    shipped_quantity=shipped_qty,
                    unit_price_cents=price,
                )
            )
            self.items += 1
        charge_id = f"pi_fake_{oid:06d}"
        provider = "fake"
        if self.gateway is not None:
            charge_id = self.gateway.create_charge(
                total, "usd", f"seed-charge-{oid}", {"order_id": oid}
            )
            provider = self.gateway.name
            self.stripe_charges += 1
        payment = Payment(
            order_id=oid,
            provider=provider,
            charge_id=charge_id,
            amount_cents=total,
            status="succeeded",
            created_at=placed,
        )
        self.s.add(payment)
        self.s.flush()
        if refund_cents:
            refund_id = f"re_fake_seed_{oid:06d}"
            if self.gateway is not None:
                refund_id = self.gateway.refund(
                    charge_id, refund_cents, f"seed-refund-{oid}", {"order_id": oid}
                ).refund_id
            self.s.add(
                Refund(
                    order_id=oid,
                    payment_id=payment.id,
                    ticket_id=None,
                    amount_cents=refund_cents,
                    provider_refund_id=refund_id,
                    idempotency_key=f"seed-refund-{oid}",
                    status="succeeded",
                    reason=refund_reason or "seeded historical refund",
                    created_at=(delivered or placed) + timedelta(days=1),
                )
            )
            self.refunds += 1
        return order

    def ticket(self, customer_id: int, subject: str, body: str, days_ago: float) -> Ticket:
        t = Ticket(
            id=self.next_ticket_id,
            customer_id=customer_id,
            subject=subject,
            body=body,
            status="open",
            created_at=self.now - timedelta(days=days_ago),
        )
        self.next_ticket_id += 1
        self.s.add(t)
        return t


def _anchors(b: _Builder) -> None:
    """Hand-placed records with known edge cases. Ids are stable on purpose."""
    b.customer(1, "Avery", "Park", 400)
    b.customer(2, "Jordan", "Lee", 380)
    b.customer(3, "Sam", "Rivera", 300)
    b.customer(4, "Taylor", "Chen", 250)
    b.s.flush()
    # 1: delivered 5 days ago, $45 -> simple refund, under auto-approve limit
    b.order(
        1,
        [("SKU-BAG", "Canvas tote", 1, 1, 2500), ("SKU-CHR", "USB-C charger", 1, 1, 2000)],
        9,
        "delivered",
        delivered_days_ago=5,
    )
    # 2: delivered 60 days ago, $80 -> past the 30-day window
    b.order(
        1, [("SKU-SPK", "Bluetooth speaker", 1, 1, 8000)], 64, "delivered", delivered_days_ago=60
    )
    # 3: delivered 3 days ago, $350 -> over auto-approve limit, needs human approval
    b.order(
        2, [("SKU-HPH", "Over-ear headphones", 2, 2, 17500)], 7, "delivered", delivered_days_ago=3
    )
    # 4: delivered 10 days ago, $60, already fully refunded
    b.order(
        2,
        [("SKU-HDP", "Hoodie", 1, 1, 6000)],
        14,
        "refunded",
        delivered_days_ago=10,
        refund_cents=6000,
        refund_reason="damaged on arrival",
    )
    # 5: partial shipment, $120: keyboard-ish $70 shipped, $50 still unshipped
    b.order(
        3,
        [("SKU-LMP", "Desk lamp", 1, 1, 7000), ("SKU-BLN", "Wool blanket", 1, 0, 5000)],
        4,
        "partially_shipped",
    )
    # 6: shipped, in transit, $30
    b.order(3, [("SKU-BTL", "Insulated bottle", 1, 1, 3000)], 3, "shipped")
    # 7: high value $1,450 delivered 20 days ago -> above the per-ticket cap
    b.order(
        4, [("SKU-BIK", "Commuter e-bike", 1, 1, 145000)], 26, "delivered", delivered_days_ago=20
    )
    # 8: placed, not shipped yet, $25 -> cancellation refund
    b.order(4, [("SKU-BAG", "Canvas tote", 1, 0, 2500)], 1, "placed")
    # Duplicate tickets: customer 1 wrote twice about order 1
    body = "Hi, the tote in order #1 has a torn strap. I want my money back."
    b.ticket(1, "Torn strap", body, 1.0)
    b.ticket(1, "Torn strap", body, 0.98)


def seed(
    session: Session,
    *,
    now: datetime,
    gateway: PaymentGateway | None = None,
    n_customers: int = 500,
    n_orders: int = 2000,
    rng_seed: int = RNG_SEED,
) -> SeedSummary:
    rng = random.Random(rng_seed)
    b = _Builder(session, now, gateway)
    _anchors(b)

    for cid in range(5, n_customers + 1):
        b.customer(cid, rng.choice(FIRST), rng.choice(LAST), rng.randint(30, 900))
    session.flush()

    while b.next_order_id <= n_orders:
        cid = rng.randint(5, n_customers)
        n_lines = rng.choices([1, 2, 3, 4], weights=[45, 30, 15, 10])[0]
        products = rng.sample(CATALOG, n_lines)
        if rng.random() < 0.06:
            products[0] = rng.choice(HIGH_VALUE)
        lines = [
            (sku, name, rng.choice([1, 1, 1, 2, 3]), 0, price) for sku, name, price in products
        ]
        age = round(rng.uniform(0.2, 120), 2)
        r = rng.random()
        delivered_ago = None
        refund = 0
        if r < 0.02:
            status = "cancelled"
            refund = sum(q * p for _, _, q, _, p in lines)
        elif age < 2:
            status = "placed"
        elif r < 0.06 and len(lines) > 1:
            status = "partially_shipped"
            lines = [(s, n, q, q if i == 0 else 0, p) for i, (s, n, q, _, p) in enumerate(lines)]
        elif age < 6:
            status = "shipped"
            lines = [(s, n, q, q, p) for s, n, q, _, p in lines]
        else:
            delivered_ago = round(age - rng.uniform(2, 5), 2)
            lines = [(s, n, q, q, p) for s, n, q, _, p in lines]
            total = sum(q * p for _, _, q, _, p in lines)
            r2 = rng.random()
            if r2 < 0.06:
                status, refund = "refunded", total
            elif r2 < 0.08 and len(lines) > 1:
                status, refund = "partially_refunded", lines[0][2] * lines[0][4]
            else:
                status = "delivered"
        b.order(
            cid,
            lines,
            age,
            status,
            delivered_days_ago=delivered_ago,
            refund_cents=refund,
            refund_reason="seeded historical refund",
        )

    # Historical tickets, including some exact duplicates sent minutes apart.
    for _ in range(150):
        oid = rng.randint(9, n_orders)
        order = session.get(Order, oid)
        subject, tmpl = rng.choice(TICKET_TEMPLATES)
        days = round(rng.uniform(0, 20), 2)
        t = b.ticket(order.customer_id, subject, tmpl.format(oid=oid), days)
        if rng.random() < 0.1:
            b.ticket(order.customer_id, subject, t.body, max(days - 0.01, 0))
    session.flush()
    _sync_sequences(session)
    return SeedSummary(
        customers=n_customers,
        orders=b.next_order_id - 1,
        items=b.items,
        refunds=b.refunds,
        tickets=b.next_ticket_id - 1,
        stripe_charges=b.stripe_charges,
    )


def _sync_sequences(session: Session) -> None:
    """Explicit ids were inserted; move Postgres sequences past them."""
    if session.get_bind().dialect.name != "postgresql":
        return
    for table in ("customers", "orders", "tickets"):
        session.execute(
            text(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"(SELECT COALESCE(MAX(id), 1) FROM {table}))"
            )
        )


def _stripe_gateway_if_configured(settings: Settings, allow: bool) -> PaymentGateway | None:
    key = settings.stripe_secret_key
    if not allow or not key or key == "sk_test_replace_me":
        return None
    check_test_key(key)  # hard error on live keys
    return StripeGateway(key)


def main() -> None:
    from support_agent.db import create_schema, make_engine, make_session_factory

    parser = argparse.ArgumentParser(description="Seed the store database")
    parser.add_argument("--reset", action="store_true", help="delete existing rows first")
    parser.add_argument("--no-stripe", action="store_true", help="never call Stripe")
    args = parser.parse_args()

    settings = Settings.from_env()
    engine = make_engine(settings.database_url)
    create_schema(engine)
    gateway = _stripe_gateway_if_configured(settings, allow=not args.no_stripe)
    with make_session_factory(engine)() as session:
        if args.reset:
            reset_store(session)
        summary = seed(session, now=settings.store_now, gateway=gateway)
        session.commit()
    mode = "Stripe test mode" if gateway else "fake charge ids"
    print(f"Seeded {summary} using {mode}")


if __name__ == "__main__":
    main()
