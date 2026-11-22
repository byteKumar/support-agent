"""Engine and session helpers."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from support_agent.models import Base

SessionFactory = Callable[[], Session]


def make_engine(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True, future=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def create_schema(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def drop_schema(engine: Engine) -> None:
    Base.metadata.drop_all(engine)
