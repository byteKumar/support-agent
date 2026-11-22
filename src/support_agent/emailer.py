"""Outbound email. SmtpMailer talks to MailHog; nothing is ever sent to a real inbox."""

from __future__ import annotations

import smtplib
import uuid
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Protocol


class EmailRefused(Exception):
    pass


class Mailer(Protocol):
    def send(self, to: str, subject: str, body: str) -> str: ...


def _check_domain(to: str, allowed: tuple[str, ...]) -> None:
    domain = to.rsplit("@", 1)[-1].lower()
    if domain not in allowed:
        raise EmailRefused(f"Recipient domain '{domain}' is not in the sandbox allow-list")


@dataclass
class MemoryMailer:
    allowed_domains: tuple[str, ...] = ("example.com",)
    sent: list[dict] = field(default_factory=list)

    def send(self, to, subject, body):
        _check_domain(to, self.allowed_domains)
        msg_id = f"<{uuid.uuid4().hex}@support-agent.local>"
        self.sent.append({"to": to, "subject": subject, "body": body, "id": msg_id})
        return msg_id


@dataclass
class SmtpMailer:
    host: str = "localhost"
    port: int = 1025
    sender: str = "support@example.com"
    allowed_domains: tuple[str, ...] = ("example.com",)

    def send(self, to, subject, body):
        _check_domain(to, self.allowed_domains)
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = to
        msg["Subject"] = subject
        msg_id = f"<{uuid.uuid4().hex}@support-agent.local>"
        msg["Message-ID"] = msg_id
        msg.set_content(body)
        with smtplib.SMTP(self.host, self.port, timeout=10) as smtp:
            smtp.send_message(msg)
        return msg_id
