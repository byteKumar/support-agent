from types import SimpleNamespace

import pytest

from support_agent.payments import FakeGateway, LiveKeyRefused, StripeGateway


def test_fake_gateway_idempotent():
    gw = FakeGateway()
    a = gw.refund("pi_1", 1000, "key-1", {})
    b = gw.refund("pi_1", 1000, "key-1", {})
    assert a.refund_id == b.refund_id
    assert len(gw.refunds_by_key) == 1


@pytest.mark.parametrize("key", ["sk_live_abc", "rk_live_abc", "pk_test_abc", "", "abc"])
def test_stripe_gateway_refuses_non_test_keys(key):
    with pytest.raises(LiveKeyRefused):
        StripeGateway(key, client=object())


def test_stripe_gateway_passes_idempotency_key_and_test_card():
    calls = {}

    class Recorder:
        def create(self, params, options):
            calls.setdefault("calls", []).append((params, options))
            return SimpleNamespace(id="re_123", status="succeeded", amount=params.get("amount"))

    fake_client = SimpleNamespace(
        v1=SimpleNamespace(refunds=Recorder(), payment_intents=Recorder())
    )
    gw = StripeGateway("sk_test_dummy", client=fake_client)
    gw.create_charge(500, "usd", "seed-charge-1", {"order_id": 1})
    result = gw.refund("pi_abc", 500, "refund:t1:o1:500", {"order_id": 1})
    (charge_params, charge_opts), (refund_params, refund_opts) = calls["calls"]
    assert charge_params["payment_method"] == "pm_card_visa"
    assert charge_opts == {"idempotency_key": "seed-charge-1"}
    assert refund_params["payment_intent"] == "pi_abc"
    assert refund_opts == {"idempotency_key": "refund:t1:o1:500"}
    assert result.refund_id == "re_123"
