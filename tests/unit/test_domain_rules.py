from datetime import UTC, datetime, timedelta

import pytest

from app.domain.history import BadCursor, decode_cursor, encode_cursor, page_is_immutable
from app.domain.plans import build_schedule, payment_deadline
from app.domain.reconciliation import Line, compare, parse_settlement
from app.domain.retry_policy import decide_retry, load_decline_policy
from app.domain.state_machine import (
    INSTALLMENT_TRANSITIONS,
    EventVerdict,
    InvalidTransition,
    judge_charge_event,
    transition,
)
from mockpay.signing import SignatureError, sign, verify

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


# ---- plans --------------------------------------------------------------------------------
def test_schedule_sums_exactly_and_puts_remainder_first():
    sched = build_schedule(100_001, 3, NOW, NOW + timedelta(days=30))
    assert sum(p.amount for p in sched) == 100_001
    assert [p.amount for p in sched] == [33_335, 33_333, 33_333]
    assert sched[0].due_at == NOW
    assert all(p.due_at < NOW + timedelta(days=30) for p in sched)


def test_single_installment_allowed_even_close_to_checkin():
    assert len(build_schedule(500, 1, NOW, NOW - timedelta(days=1))) == 1


def test_multi_installment_rejected_when_deadline_passed():
    with pytest.raises(ValueError):
        build_schedule(500, 2, NOW, NOW - timedelta(days=1))


def test_payment_deadline_is_days_before_checkin():
    assert payment_deadline(datetime(2026, 12, 10).date(), 3) == datetime(2026, 12, 7, tzinfo=UTC)


# ---- retry policy -------------------------------------------------------------------------
@pytest.mark.parametrize(
    "code,retryable",
    [
        ("insufficient_funds", True),
        ("processing_error", True),
        ("card_closed", False),
        ("stolen_card", False),
        ("totally_new_code", False),
        (None, False),
    ],
)
def test_decline_classification(code, retryable):
    assert load_decline_policy().is_retryable(code) is retryable


def test_retry_backoff_schedule():
    far = NOW + timedelta(days=30)
    d1 = decide_retry(decline_code="insufficient_funds", attempts_so_far=1, now=NOW, deadline=far)
    d2 = decide_retry(decline_code="insufficient_funds", attempts_so_far=2, now=NOW, deadline=far)
    assert (d1.retry, d1.next_attempt_at) == (True, NOW + timedelta(hours=12))
    assert d2.next_attempt_at == NOW + timedelta(hours=24)


def test_retry_stops_before_deadline_and_after_max_attempts():
    soon = NOW + timedelta(hours=6)
    assert (
        decide_retry(decline_code="insufficient_funds", attempts_so_far=1, now=NOW, deadline=soon).reason
        == "past_deadline"
    )
    assert (
        decide_retry(
            decline_code="insufficient_funds", attempts_so_far=5, now=NOW, deadline=NOW + timedelta(days=60)
        ).reason
        == "attempts_exhausted"
    )
    assert (
        decide_retry(decline_code="card_closed", attempts_so_far=1, now=NOW, deadline=NOW + timedelta(days=60)).reason
        == "non_retryable"
    )


# ---- state machine ------------------------------------------------------------------------
@pytest.mark.parametrize(
    "current,target,verdict",
    [
        ("pending", "succeeded", EventVerdict.APPLY),
        ("succeeded", "succeeded", EventVerdict.STALE),  # duplicate
        ("refunded", "succeeded", EventVerdict.STALE),  # late success after refund
        ("pending", "refunded", EventVerdict.TOO_EARLY),  # refund arrived before success
        ("succeeded", "refunded", EventVerdict.APPLY),
        ("declined", "succeeded", EventVerdict.STALE),
    ],
)
def test_charge_event_verdicts(current, target, verdict):
    assert judge_charge_event(current, target) is verdict


def test_invalid_installment_transition_raises():
    with pytest.raises(InvalidTransition):
        transition(INSTALLMENT_TRANSITIONS, "succeeded", "processing")


# ---- webhook signatures -------------------------------------------------------------------
def test_signature_roundtrip():
    body = b'{"id":"evt_1"}'
    verify("s3cret", body, sign("s3cret", body))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b, h: (b + b" ", h),  # body tampered
        lambda b, h: (b, h.replace("v1=", "v1=00")),  # signature tampered
        lambda b, h: (b, sign("s3cret", b, ts=1_000_000)),  # replayed old timestamp
        lambda b, h: (b, None),  # missing
        lambda b, h: (b, "garbage"),  # malformed
    ],
)
def test_signature_rejections(mutate):
    body = b'{"id":"evt_1"}'
    b, hdr = mutate(body, sign("s3cret", body))
    with pytest.raises(SignatureError):
        verify("s3cret", b, hdr)


# ---- history cursor -----------------------------------------------------------------------
def test_cursor_roundtrip_and_bad_cursor():
    assert decode_cursor(encode_cursor(NOW, 42)) == (NOW, 42)
    with pytest.raises(BadCursor):
        decode_cursor("not-a-cursor!!")


def test_only_full_old_pages_are_cacheable():
    old = {"has_more": True, "_last_occurred_at": NOW - timedelta(minutes=5)}
    assert page_is_immutable(old, 60, now=NOW)
    assert not page_is_immutable({**old, "has_more": False}, 60, now=NOW)  # last page: may grow
    assert not page_is_immutable({**old, "_last_occurred_at": NOW - timedelta(seconds=5)}, 60, now=NOW)


# ---- reconciliation compare ---------------------------------------------------------------
def test_reconciliation_reason_codes():
    L = lambda t, a, cur="USD", st="settled": Line(t, "charge", a, cur, st)  # noqa: E731
    ledger = {
        "a": L("a", 1000),
        "b": L("b", 1000),
        "c": L("c", 1000),
        "d": L("d", 1000),
        "e": L("e", 1000),
        "f": L("f", 1000),
    }
    settlement = {
        "a": L("a", 1000),
        "b": L("b", 1001),
        "c": L("c", 1500),
        "d": L("d", 1000, st="reversed"),
        "f": L("f", 1000, cur="EUR"),
        "x": L("x", 50),
    }
    matched, mism = compare(ledger, settlement)
    reasons = {m["txn"]: m["reason"] for m in mism}
    assert matched == 1
    assert reasons == {
        "b": "CURRENCY_ROUNDING",
        "c": "AMOUNT_MISMATCH",
        "d": "STATUS_MISMATCH",
        "e": "MISSING_IN_SETTLEMENT",
        "f": "AMOUNT_MISMATCH",
        "x": "MISSING_IN_LEDGER",
    }


def test_parse_settlement_csv():
    text = "processor_txn_id,type,amount,currency,status\nch_1,charge,1000,usd,settled\n"
    assert parse_settlement(text)["ch_1"] == Line("ch_1", "charge", 1000, "USD", "settled")
