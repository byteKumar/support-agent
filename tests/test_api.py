from fastapi.testclient import TestClient

from support_agent.api import AppDeps, create_app
from support_agent.llm import FakeLLM, text_turn, tool_turn
from support_agent.models import AgentRun, Approval, Refund
from tests.helpers import make_agent

AUTH = ("admin", "change-me")
HX = {"HX-Request": "true"}


def _client(session_factory, settings, script):
    agent, deps = make_agent(session_factory, settings, FakeLLM(list(script)))
    app = create_app(AppDeps(settings, session_factory, agent))
    return TestClient(app), deps


def test_healthz(session_factory, settings):
    client, _ = _client(session_factory, settings, [])
    assert client.get("/healthz").json()["ok"] is True


def test_ticket_flow(session_factory, settings):
    script = [
        tool_turn(("check_policy", {"order_id": 1, "amount_cents": 4500})),
        tool_turn(("issue_refund", {"order_id": 1, "amount_cents": 4500, "reason": "torn"})),
        text_turn("refunded"),
    ]
    client, _ = _client(session_factory, settings, script)
    r = client.post("/tickets", json={"customer_id": 1, "subject": "Refund", "body": "order #1"})
    assert r.status_code == 201
    tid = r.json()["ticket_id"]
    body = client.get(f"/tickets/{tid}").json()
    assert body["status"] == "resolved"
    assert [c["name"] for c in body["runs"][0]["tool_calls"]] == ["check_policy", "issue_refund"]


def test_ticket_validation(session_factory, settings):
    client, _ = _client(session_factory, settings, [])
    assert (
        client.post("/tickets", json={"customer_id": 1, "subject": "", "body": "x"}).status_code
        == 422
    )
    assert (
        client.post(
            "/tickets", json={"customer_id": 99999, "subject": "a", "body": "x"}
        ).status_code
        == 404
    )


def _pause(client):
    r = client.post("/tickets", json={"customer_id": 2, "subject": "Broken", "body": "order #3"})
    return r.json()["ticket_id"]


PAUSE_THEN_FINISH = [
    tool_turn(("issue_refund", {"order_id": 3, "amount_cents": 35000, "reason": "broken"})),
    text_turn("done"),
]


def test_admin_requires_auth(session_factory, settings):
    client, _ = _client(session_factory, settings, [])
    assert client.get("/admin").status_code == 401
    assert client.get("/admin", auth=("admin", "wrong")).status_code == 401


def test_admin_approve_resumes_run(session_factory, settings):
    client, deps = _client(session_factory, settings, PAUSE_THEN_FINISH)
    tid = _pause(client)
    page = client.get("/admin", auth=AUTH).text
    assert "$350.00" in page and "Approve" in page
    with session_factory() as s:
        approval = s.query(Approval).filter_by(ticket_id=tid).one()
    # without the HTMX header the call is refused (CSRF guard)
    assert client.post(f"/admin/approvals/{approval.id}/approve", auth=AUTH).status_code == 403
    r = client.post(
        f"/admin/approvals/{approval.id}/approve",
        auth=AUTH,
        headers=HX,
        data={"note": "receipt checked"},
    )
    assert r.status_code == 200 and "approved" in r.text
    with session_factory() as s:
        assert s.query(Refund).filter_by(ticket_id=tid).one().amount_cents == 35000
        assert s.get(AgentRun, approval.run_id).status == "completed"
    again = client.post(f"/admin/approvals/{approval.id}/reject", auth=AUTH, headers=HX)
    assert again.status_code == 409


def test_admin_reject(session_factory, settings):
    client, _ = _client(session_factory, settings, PAUSE_THEN_FINISH)
    tid = _pause(client)
    with session_factory() as s:
        approval = s.query(Approval).filter_by(ticket_id=tid).one()
    r = client.post(f"/admin/approvals/{approval.id}/reject", auth=AUTH, headers=HX)
    assert "rejected" in r.text
    with session_factory() as s:
        assert s.query(Refund).filter_by(ticket_id=tid).count() == 0


def test_run_page_escapes_ticket_text(session_factory, settings):
    client, _ = _client(session_factory, settings, [text_turn("ok")])
    r = client.post(
        "/tickets", json={"customer_id": 1, "subject": "x", "body": "<script>alert(1)</script>"}
    )
    tid = r.json()["ticket_id"]
    run_id = client.get(f"/tickets/{tid}").json()["runs"][0]["run_id"]
    page = client.get(f"/admin/runs/{run_id}", auth=AUTH)
    assert page.status_code == 200
    assert "<script>alert(1)</script>" not in page.text
