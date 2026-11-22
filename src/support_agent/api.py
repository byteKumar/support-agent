"""HTTP API and admin page.

    uv run uvicorn support_agent.api:create_app --factory --port 8002

POST /tickets            create a ticket and run the agent on it (in the background)
GET  /tickets/{id}       ticket, runs and the tool-call trace
GET  /admin              approval queue + recent runs (HTMX, basic auth)
POST /admin/approvals/{id}/approve|reject
GET  /admin/runs/{id}    one run's conversation and tool calls
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import select

from support_agent.agent import AgentDeps, SupportAgent
from support_agent.approvals import ApprovalError, decide, pending_approvals
from support_agent.config import Settings
from support_agent.db import SessionFactory, create_schema, make_engine, make_session_factory
from support_agent.emailer import SmtpMailer
from support_agent.llm import make_llm
from support_agent.models import AgentRun, Approval, Customer, Order, Ticket, ToolCall
from support_agent.payments import make_gateway
from support_agent.tracing import setup_tracing

log = logging.getLogger("support_agent.api")
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.filters["money"] = lambda cents: f"${cents / 100:,.2f}"


@dataclass
class AppDeps:
    settings: Settings
    session_factory: SessionFactory
    agent: SupportAgent


def build_default_deps() -> AppDeps:
    settings = Settings.from_env()
    engine = make_engine(settings.database_url)
    create_schema(engine)
    setup_tracing(settings)
    factory = make_session_factory(engine)
    agent = SupportAgent(
        AgentDeps(
            session_factory=factory,
            llm=make_llm(settings),
            gateway=make_gateway(settings.payment_provider, settings.stripe_secret_key),
            mailer=SmtpMailer(
                host=settings.smtp_host,
                port=settings.smtp_port,
                sender=settings.mail_from,
                allowed_domains=settings.mail_allowed_domains,
            ),
            settings=settings,
        )
    )
    return AppDeps(settings, factory, agent)


class TicketIn(BaseModel):
    customer_id: int = Field(ge=1)
    subject: str = Field(min_length=1, max_length=255)
    body: str = Field(min_length=1, max_length=20_000)


def create_app(deps: AppDeps | None = None) -> FastAPI:
    deps = deps or build_default_deps()
    app = FastAPI(title="support-agent")
    security = HTTPBasic()

    def admin(creds: HTTPBasicCredentials = Depends(security)) -> str:
        user_ok = secrets.compare_digest(creds.username, deps.settings.admin_user)
        pass_ok = secrets.compare_digest(creds.password, deps.settings.admin_password)
        if not (user_ok and pass_ok):
            raise HTTPException(401, "bad credentials", headers={"WWW-Authenticate": "Basic"})
        return creds.username

    def htmx_only(request: Request) -> None:
        # Cheap CSRF guard: a cross-site form cannot set this header without CORS.
        if request.headers.get("HX-Request") != "true":
            raise HTTPException(403, "state-changing admin calls must come from the admin page")

    def run_safely(fn, *args):
        try:
            fn(*args)
        except Exception:  # noqa: BLE001 - background task; the run row records failures
            log.exception("background agent task failed")

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "llm_provider": deps.settings.llm_provider}

    @app.post("/tickets", status_code=201)
    def create_ticket(body: TicketIn, background: BackgroundTasks):
        with deps.session_factory() as s:
            if s.get(Customer, body.customer_id) is None:
                raise HTTPException(404, "unknown customer")
            t = Ticket(
                customer_id=body.customer_id,
                subject=body.subject,
                body=body.body,
                status="open",
                created_at=deps.settings.store_now,
            )
            s.add(t)
            s.commit()
            ticket_id = t.id
        background.add_task(run_safely, deps.agent.run_ticket, ticket_id)
        return {"ticket_id": ticket_id, "status": "queued"}

    @app.get("/tickets/{ticket_id}")
    def get_ticket(ticket_id: int):
        with deps.session_factory() as s:
            t = s.get(Ticket, ticket_id)
            if t is None:
                raise HTTPException(404, "no such ticket")
            runs = s.scalars(select(AgentRun).where(AgentRun.ticket_id == t.id)).all()
            return {
                "ticket_id": t.id,
                "status": t.status,
                "escalation_reason": t.escalation_reason,
                "runs": [
                    {
                        "run_id": r.id,
                        "status": r.status,
                        "steps": r.steps,
                        "cost_usd": r.cost_usd,
                        "trace_id": r.trace_id,
                        "final_text": r.final_text,
                        "error": r.error,
                        "tool_calls": [
                            {"name": c.name, "input": c.input, "output": c.output}
                            for c in s.scalars(
                                select(ToolCall)
                                .where(ToolCall.run_id == r.id)
                                .order_by(ToolCall.id)
                            )
                        ],
                    }
                    for r in runs
                ],
            }

    def _approval_view(s, a: Approval) -> dict:
        ticket = s.get(Ticket, a.ticket_id)
        order = s.get(Order, a.order_id)
        customer = s.get(Customer, ticket.customer_id)
        return {"a": a, "ticket": ticket, "order": order, "customer": customer}

    @app.get("/admin", response_class=HTMLResponse)
    def admin_page(request: Request, user: str = Depends(admin)):
        with deps.session_factory() as s:
            pending = [_approval_view(s, a) for a in pending_approvals(s)]
            runs = s.scalars(select(AgentRun).order_by(AgentRun.id.desc()).limit(25)).all()
            return TEMPLATES.TemplateResponse(
                request, "admin.html", {"pending": pending, "runs": runs, "user": user}
            )

    def _decide(request, approval_id, decision, note, background, user):
        try:
            decide(deps.session_factory, approval_id, decision, user, note or None)
        except ApprovalError as exc:
            raise HTTPException(409, str(exc)) from exc
        background.add_task(run_safely, deps.agent.resume, approval_id)
        with deps.session_factory() as s:
            view = _approval_view(s, s.get(Approval, approval_id))
            return TEMPLATES.TemplateResponse(request, "_approval_row.html", view)

    @app.post(
        "/admin/approvals/{approval_id}/approve",
        response_class=HTMLResponse,
        dependencies=[Depends(htmx_only)],
    )
    def approve(
        request: Request,
        approval_id: int,
        background: BackgroundTasks,
        note: str = Form(""),
        user: str = Depends(admin),
    ):
        return _decide(request, approval_id, "approve", note, background, user)

    @app.post(
        "/admin/approvals/{approval_id}/reject",
        response_class=HTMLResponse,
        dependencies=[Depends(htmx_only)],
    )
    def reject(
        request: Request,
        approval_id: int,
        background: BackgroundTasks,
        note: str = Form(""),
        user: str = Depends(admin),
    ):
        return _decide(request, approval_id, "reject", note, background, user)

    @app.get("/admin/runs/{run_id}", response_class=HTMLResponse)
    def run_page(request: Request, run_id: int, user: str = Depends(admin)):
        with deps.session_factory() as s:
            run = s.get(AgentRun, run_id)
            if run is None:
                raise HTTPException(404, "no such run")
            calls = s.scalars(
                select(ToolCall).where(ToolCall.run_id == run_id).order_by(ToolCall.id)
            ).all()
            ticket = s.get(Ticket, run.ticket_id)
            return TEMPLATES.TemplateResponse(
                request, "run.html", {"run": run, "calls": calls, "ticket": ticket}
            )

    return app
