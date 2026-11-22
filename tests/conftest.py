"""Test setup.

Hard rules enforced here:
- every ANTHROPIC_* variable is removed, and building a real anthropic client raises;
- payments use FakeGateway, email goes to memory or local MailHog;
- the database is a separate `support_agent_test` DB on the compose Postgres (port 5435).
Each test runs inside an outer transaction that is rolled back afterwards.
"""

from __future__ import annotations

import os

for _key in list(os.environ):
    if _key.startswith("ANTHROPIC_") or _key.startswith("STRIPE_"):
        del os.environ[_key]
os.environ["LLM_PROVIDER"] = "fake"
os.environ["PAYMENT_PROVIDER"] = "fake"

import anthropic  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from support_agent.config import Settings  # noqa: E402
from support_agent.db import create_schema, drop_schema  # noqa: E402
from support_agent.seed import seed  # noqa: E402

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://support:support@localhost:5435/support_agent_test",
)


@pytest.fixture(autouse=True)
def _no_real_llm(monkeypatch):
    for key in list(os.environ):
        if key.startswith("ANTHROPIC_"):
            monkeypatch.delenv(key, raising=False)

    def _refuse(*args, **kwargs):
        raise RuntimeError("Tests must never build a real Anthropic client")

    monkeypatch.setattr(anthropic.Anthropic, "__init__", _refuse)


def _ensure_database(url: str) -> None:
    u = make_url(url)
    admin = create_engine(u.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": u.database}
        ).scalar()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{u.database}"'))
    admin.dispose()


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(database_url=TEST_DB_URL)


@pytest.fixture(scope="session")
def engine(settings):
    _ensure_database(TEST_DB_URL)
    eng = create_engine(TEST_DB_URL, future=True)
    drop_schema(eng)
    create_schema(eng)
    with sessionmaker(bind=eng)() as s:
        seed(s, now=settings.store_now)
        s.commit()
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine):
    """A session factory whose commits become savepoints inside one rolled-back transaction."""
    conn = engine.connect()
    outer = conn.begin()
    factory = sessionmaker(
        bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    yield factory
    outer.rollback()
    conn.close()


@pytest.fixture
def session(session_factory):
    with session_factory() as s:
        yield s
