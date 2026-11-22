from __future__ import annotations

from support_agent.emailer import MemoryMailer
from support_agent.models import Ticket
from support_agent.payments import FakeGateway
from support_agent.tools import ToolContext


def make_ticket(session, settings, customer_id=1, body="help", subject="Help") -> Ticket:
    t = Ticket(
        customer_id=customer_id,
        subject=subject,
        body=body,
        status="open",
        created_at=settings.store_now,
    )
    session.add(t)
    session.flush()
    return t


def make_ctx(session, settings, customer_id=1, run_id=None, **kw) -> ToolContext:
    ticket = make_ticket(session, settings, customer_id)
    return ToolContext(
        session=session,
        ticket=ticket,
        settings=settings,
        gateway=kw.pop("gateway", FakeGateway()),
        mailer=kw.pop("mailer", MemoryMailer()),
        run_id=run_id,
        **kw,
    )
