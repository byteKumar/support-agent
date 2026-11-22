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


def make_agent(session_factory, settings, script, tracer=None, gateway=None, mailer=None):
    from support_agent.agent import AgentDeps, SupportAgent
    from support_agent.llm import FakeLLM

    llm = script if not isinstance(script, list) else FakeLLM(list(script))
    deps = AgentDeps(
        session_factory=session_factory,
        llm=llm,
        gateway=gateway or FakeGateway(),
        mailer=mailer or MemoryMailer(),
        settings=settings,
        tracer=tracer,
        sleep=lambda _s: None,
    )
    return SupportAgent(deps), deps


def new_ticket(session_factory, settings, customer_id, body, subject="Help") -> int:
    with session_factory() as s:
        t = make_ticket(s, settings, customer_id, body=body, subject=subject)
        s.commit()
        return t.id
