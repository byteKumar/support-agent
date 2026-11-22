"""Settings read from environment variables. One place for every knob."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime

DEFAULT_DB = "postgresql+psycopg://support:support@localhost:5435/support_agent"
DEFAULT_STORE_NOW = "2026-11-01T12:00:00+00:00"


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


@dataclass
class Settings:
    database_url: str = DEFAULT_DB
    llm_provider: str = "fake"
    llm_model: str = "claude-haiku-5-5"
    price_input_per_mtok: float = 0.10
    price_output_per_mtok: float = 0.50
    payment_provider: str = "fake"
    stripe_secret_key: str = ""
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    mail_from: str = "support@example.com"
    mail_allowed_domains: tuple[str, ...] = ("example.com",)
    refund_window_days: int = 30
    auto_approve_limit_cents: int = 10_000
    ticket_refund_cap_cents: int = 100_000
    store_now: datetime = field(default_factory=lambda: datetime.fromisoformat(DEFAULT_STORE_NOW))
    max_steps: int = 12
    require_policy_check: bool = False
    admin_user: str = "admin"
    admin_password: str = "change-me"
    otel_console: bool = False
    otlp_endpoint: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        now_raw = os.environ.get("STORE_NOW") or DEFAULT_STORE_NOW
        store_now = datetime.fromisoformat(now_raw)
        if store_now.tzinfo is None:
            store_now = store_now.replace(tzinfo=UTC)
        domains = os.environ.get("MAIL_ALLOWED_DOMAINS", "example.com")
        return cls(
            database_url=os.environ.get("DATABASE_URL", DEFAULT_DB),
            llm_provider=os.environ.get("LLM_PROVIDER", "fake").lower(),
            llm_model=os.environ.get("LLM_MODEL", "claude-haiku-5-5"),
            price_input_per_mtok=_float("LLM_PRICE_INPUT_PER_MTOK", 0.10),
            price_output_per_mtok=_float("LLM_PRICE_OUTPUT_PER_MTOK", 0.50),
            payment_provider=os.environ.get("PAYMENT_PROVIDER", "fake").lower(),
            stripe_secret_key=os.environ.get("STRIPE_SECRET_KEY", ""),
            smtp_host=os.environ.get("SMTP_HOST", "localhost"),
            smtp_port=_int("SMTP_PORT", 1025),
            mail_from=os.environ.get("MAIL_FROM", "support@example.com"),
            mail_allowed_domains=tuple(d.strip().lower() for d in domains.split(",") if d.strip()),
            refund_window_days=_int("REFUND_WINDOW_DAYS", 30),
            auto_approve_limit_cents=_int("AUTO_APPROVE_LIMIT_CENTS", 10_000),
            ticket_refund_cap_cents=_int("TICKET_REFUND_CAP_CENTS", 100_000),
            store_now=store_now,
            max_steps=_int("MAX_STEPS", 12),
            require_policy_check=_bool("REQUIRE_POLICY_CHECK"),
            admin_user=os.environ.get("ADMIN_USER", "admin"),
            admin_password=os.environ.get("ADMIN_PASSWORD", "change-me"),
            otel_console=_bool("OTEL_CONSOLE"),
            otlp_endpoint=os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
        )
