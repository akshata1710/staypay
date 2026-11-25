from datetime import date, timedelta

import pytest

from app.domain.money import format_money
from app.domain.refunds import PaidCharge, check_in_datetime, compute_refund


@pytest.mark.parametrize(
    "minor,currency,expected",
    [
        (120_000, "USD", "1,200.00 USD"),
        (12_000, "JPY", "12,000 JPY"),
        (1_500, "KWD", "1.500 KWD"),
        (5, "usd", "0.05 USD"),
    ],
)
def test_format_money(minor, currency, expected):
    assert format_money(minor, currency) == expected


def test_unknown_currency_fails_loudly():
    with pytest.raises(ValueError):
        format_money(100, "XYZ")


def test_refund_explanation_in_jpy():
    check_in = date(2026, 12, 20)
    booked = check_in_datetime(check_in) - timedelta(days=60)
    d = compute_refund(
        policy_name="flexible",
        total_amount=12_000,
        currency="JPY",
        check_in=check_in,
        booked_at=booked,
        cancelled_at=check_in_datetime(check_in) - timedelta(days=5),
        charges=[PaidCharge("ch1", 12_000, 0, booked)],
    )
    assert d.total_refund == 12_000
    assert "refund is 12,000 JPY" in d.explanation
