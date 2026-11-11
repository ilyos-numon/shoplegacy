"""Real Toss Payments API with the test secret key; skips without SHOPLEGACY_TOSS_SECRET_KEY."""

import os
import re

import pytest

from shoplegacy.config import DEFAULT_TOSS_API_BASE
from shoplegacy.payments import PaymentError, TossPaymentProvider


@pytest.fixture(scope="module")
def secret() -> str:
    value = os.environ.get("SHOPLEGACY_TOSS_SECRET_KEY", "").strip()
    if not value:
        pytest.skip("SHOPLEGACY_TOSS_SECRET_KEY not set")
    return value


@pytest.fixture
def provider(secret: str) -> TossPaymentProvider:
    api_base = os.environ.get("SHOPLEGACY_TOSS_API_BASE", DEFAULT_TOSS_API_BASE)
    return TossPaymentProvider("", secret, api_base)


def test_toss_lookup_unknown_payment_key(provider: TossPaymentProvider) -> None:
    with pytest.raises(PaymentError) as err:
        provider.lookup("shoplegacy_no_such_payment_key")
    assert err.value.code == "NOT_FOUND_PAYMENT"


def test_toss_cancel_unknown_payment_key(provider: TossPaymentProvider) -> None:
    with pytest.raises(PaymentError) as err:
        provider.cancel("shoplegacy_no_such_payment_key", "live test")
    assert err.value.code == "NOT_FOUND_PAYMENT"


def test_toss_issue_billing_key_with_fake_auth_key(provider: TossPaymentProvider) -> None:
    with pytest.raises(PaymentError) as err:
        provider.issue_billing_key("shoplegacy_no_such_auth_key", "sl-live-test")
    code = err.value.code
    # a Toss error code from a 4xx body, not a transport or auth failure
    assert re.fullmatch(r"[A-Z][A-Z_]+", code), code
    assert code != "UNAUTHORIZED_KEY"
