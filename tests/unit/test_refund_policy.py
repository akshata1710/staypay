"""Table-driven tests for compute_refund(). Each row is a case a support agent would recognise."""

from datetime import UTC, date, datetime, timedelta

import pytest

from app.domain.refunds import PaidCharge, check_in_datetime, compute_refund

CHECK_IN = date(2026, 12, 20)
CI = check_in_datetime(CHECK_IN)  # 15:00 UTC on check-in day
BOOKED_LONG_AGO = CI - timedelta(days=60)
TOTAL = 100_000  # $1,000.00


def paid(*amounts, refunded=0, disputed=False):
    base = BOOKED_LONG_AGO
    return [
        PaidCharge(f"ch{i}", a, refunded if i == 0 else 0, base + timedelta(days=i), disputed)
        for i, a in enumerate(amounts)
    ]


def run(policy, hours_before, charges, booked_at=BOOKED_LONG_AGO):
    return compute_refund(
        policy_name=policy,
        total_amount=TOTAL,
        currency="USD",
        check_in=CHECK_IN,
        booked_at=booked_at,
        cancelled_at=CI - timedelta(hours=hours_before),
        charges=charges,
    )


@pytest.mark.parametrize(
    "policy,hours_before,expected_refund,rule",
    [
        # flexible: full refund until 24h before
        ("flexible", 25, 100_000, "flexible>=24h"),
        ("flexible", 24, 100_000, "flexible>=24h"),  # exactly on the boundary counts
        ("flexible", 23, 0, "flexible>=0h"),
        # moderate: full refund until 5 days before, then 50%
        ("moderate", 121, 100_000, "moderate>=120h"),
        ("moderate", 119, 50_000, "moderate>=0h"),
        ("moderate", 1, 50_000, "moderate>=0h"),  # cancel 1 hour before check-in
        # strict: 14d full, 7d half, then nothing
        ("strict", 24 * 15, 100_000, "strict>=336h"),
        ("strict", 24 * 10, 50_000, "strict>=168h"),
        ("strict", 24 * 6, 0, "strict>=0h"),
    ],
)
def test_policy_tiers_fully_paid(policy, hours_before, expected_refund, rule):
    d = run(policy, hours_before, paid(TOTAL))
    assert (d.route, d.total_refund, d.rule) == ("auto", expected_refund, rule)


def test_cancel_one_hour_before_policy_deadline_vs_one_hour_after():
    assert run("moderate", 121, paid(TOTAL)).total_refund == 100_000
    assert run("moderate", 119, paid(TOTAL)).total_refund == 50_000


def test_cancel_after_first_installment_paid_less_than_owed():
    # Paid 25% of the total, moderate policy late cancel: guest owes 50%. Nothing to refund.
    d = run("moderate", 48, paid(25_000))
    assert d.total_refund == 0 and d.allocations == []
    assert "shortfall is not collected" in d.explanation


def test_cancel_after_two_of_four_installments_partial_refund():
    # Paid 75%, owes 50% -> refund 25%, taken from the newest charge first.
    d = run("moderate", 48, paid(25_000, 25_000, 25_000))
    assert d.total_refund == 25_000
    assert d.allocations == [{"charge_id": "ch2", "amount": 25_000}]


def test_allocation_spans_multiple_charges_newest_first():
    d = run("flexible", 48, paid(40_000, 30_000, 30_000))
    assert d.allocations == [
        {"charge_id": "ch2", "amount": 30_000},
        {"charge_id": "ch1", "amount": 30_000},
        {"charge_id": "ch0", "amount": 40_000},
    ]


def test_grace_period_beats_strict_policy():
    booked = CI - timedelta(days=30)
    d = run("strict", 24 * 29, paid(TOTAL), booked_at=booked)  # cancelled 1 day after booking
    assert (d.total_refund, d.rule) == (100_000, "grace_period")


def test_grace_period_needs_checkin_at_least_14_days_away():
    booked = CI - timedelta(days=10)
    d = run("strict", 24 * 9, paid(TOTAL), booked_at=booked)
    assert d.rule == "strict>=168h"


def test_already_partially_refunded_charge_is_not_refunded_twice():
    d = run("flexible", 48, paid(TOTAL, refunded=30_000))
    assert d.total_refund == 70_000


def test_disputed_charge_goes_to_manual_review():
    d = run("flexible", 48, paid(TOTAL, disputed=True))
    assert (d.route, d.rule, d.total_refund) == ("manual_review", "disputed_charge", 0)


def test_partial_stay_goes_to_manual_review():
    d = run("flexible", -24, paid(TOTAL))
    assert (d.route, d.rule) == ("manual_review", "after_check_in")


def test_explanation_is_readable_for_support():
    d = run("moderate", 48, paid(TOTAL))
    assert d.explanation.startswith("Moderate policy, cancelled 48.0h before check-in")
    assert "refund is 500.00 USD" in d.explanation


def test_unknown_policy_raises():
    with pytest.raises(ValueError):
        compute_refund(
            policy_name="nope",
            total_amount=1,
            currency="USD",
            check_in=CHECK_IN,
            booked_at=BOOKED_LONG_AGO,
            cancelled_at=datetime.now(UTC),
            charges=[],
        )
