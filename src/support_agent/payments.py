"""Payment gateway interface.

FakeGateway is used in tests, dev and evals. StripeGateway talks to Stripe's
*test mode* through the official SDK and refuses to start with anything that
is not an `sk_test_` key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class GatewayError(Exception):
    """A payment provider call failed."""


class LiveKeyRefused(RuntimeError):
    """Raised when someone tries to start the Stripe gateway with a live key."""


@dataclass
class RefundResult:
    refund_id: str
    status: str
    amount_cents: int


class PaymentGateway(Protocol):
    name: str

    def create_charge(
        self, amount_cents: int, currency: str, idempotency_key: str, metadata: dict
    ) -> str: ...

    def refund(
        self, charge_id: str, amount_cents: int, idempotency_key: str, metadata: dict
    ) -> RefundResult: ...


@dataclass
class FakeGateway:
    """In-memory gateway that honours idempotency keys like Stripe does."""

    name: str = "fake"
    charges: dict[str, int] = field(default_factory=dict)
    refunds_by_key: dict[str, RefundResult] = field(default_factory=dict)
    refund_calls: int = 0
    fail_next: bool = False

    def create_charge(self, amount_cents, currency, idempotency_key, metadata):
        charge_id = f"pi_fake_{idempotency_key.replace(':', '_')}"
        self.charges[charge_id] = amount_cents
        return charge_id

    def refund(self, charge_id, amount_cents, idempotency_key, metadata):
        self.refund_calls += 1
        if self.fail_next:
            self.fail_next = False
            raise GatewayError("simulated provider outage")
        if idempotency_key in self.refunds_by_key:
            return self.refunds_by_key[idempotency_key]
        result = RefundResult(
            refund_id=f"re_fake_{len(self.refunds_by_key) + 1:06d}",
            status="succeeded",
            amount_cents=amount_cents,
        )
        self.refunds_by_key[idempotency_key] = result
        return result


def check_test_key(secret_key: str) -> None:
    if not secret_key:
        raise LiveKeyRefused("STRIPE_SECRET_KEY is empty; StripeGateway needs a test key")
    if secret_key.startswith(("sk_live_", "rk_live_")):
        raise LiveKeyRefused("Live Stripe keys are refused. Use an sk_test_ key.")
    if not secret_key.startswith("sk_test_"):
        raise LiveKeyRefused("StripeGateway only accepts keys starting with sk_test_")


class StripeGateway:
    """Stripe test-mode gateway. Charges are PaymentIntents confirmed with pm_card_visa."""

    name = "stripe"

    def __init__(self, secret_key: str, client=None):
        check_test_key(secret_key)
        if client is None:
            import stripe

            client = stripe.StripeClient(secret_key)
        self._client = client

    def create_charge(self, amount_cents, currency, idempotency_key, metadata):
        try:
            intent = self._client.v1.payment_intents.create(
                params={
                    "amount": amount_cents,
                    "currency": currency,
                    "payment_method": "pm_card_visa",
                    "confirm": True,
                    "automatic_payment_methods": {
                        "enabled": True,
                        "allow_redirects": "never",
                    },
                    "metadata": {k: str(v) for k, v in metadata.items()},
                },
                options={"idempotency_key": idempotency_key},
            )
        except Exception as exc:  # stripe.StripeError and network errors
            raise GatewayError(str(exc)) from exc
        return intent.id

    def refund(self, charge_id, amount_cents, idempotency_key, metadata):
        try:
            refund = self._client.v1.refunds.create(
                params={
                    "payment_intent": charge_id,
                    "amount": amount_cents,
                    "metadata": {k: str(v) for k, v in metadata.items()},
                },
                options={"idempotency_key": idempotency_key},
            )
        except Exception as exc:
            raise GatewayError(str(exc)) from exc
        return RefundResult(refund_id=refund.id, status=refund.status, amount_cents=refund.amount)


def make_gateway(provider: str, stripe_secret_key: str = "") -> PaymentGateway:
    if provider == "stripe":
        return StripeGateway(stripe_secret_key)
    return FakeGateway()
