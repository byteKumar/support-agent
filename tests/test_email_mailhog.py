"""Integration test against the compose MailHog (SMTP 1025, API 8025)."""

import json
import os
import socket
import urllib.request
import uuid

import pytest

from support_agent.emailer import EmailRefused, SmtpMailer

HOST = os.environ.get("SMTP_HOST", "localhost")


def _mailhog_up() -> bool:
    try:
        socket.create_connection((HOST, 1025), timeout=1).close()
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _mailhog_up(), reason="MailHog not running (docker compose up mailhog)")
def test_smtp_mailer_delivers_to_mailhog():
    subject = f"test-{uuid.uuid4().hex[:8]}"
    SmtpMailer(host=HOST, port=1025).send("avery.park1@example.com", subject, "hello")
    with urllib.request.urlopen(
        f"http://{HOST}:8025/api/v2/search?kind=containing&query={subject}"
    ) as r:
        data = json.load(r)
    assert data["total"] >= 1


def test_real_domains_refused():
    with pytest.raises(EmailRefused):
        SmtpMailer(host="localhost", port=1).send("someone@gmail.com", "x", "y")
