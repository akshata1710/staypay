"""Failure-injection scenarios. Each test is one row of the README's "Reliability tests that matter"."""

import asyncio
from datetime import timedelta

import pytest

from app.clock import utcnow
from app.domain import charging, checkout, webhooks

# Tag every test in this file with the `chaos` marker.
pytestmark = pytest.mark.chaos


def fast_timeouts(h):
    """Make processor timeouts quick so the tests stay fast."""
    h.processor.timeout_s = 0.1
    h.processor.max_attempts = 1


# 50 simultaneous checkouts with one key: one reservation, one charge, 49 replays.
async def test_50_concurrent_checkouts_same_key_exactly_one_charge(h):
    results = await asyncio.gather(*[h.checkout(key="double-tap-key-0001", guest="g1") for _ in range(50)])
    codes = [r.status_code for r in results]
    assert set(codes) == {201}, codes
    assert await h.scalar("select count(*) from reservations") == 1
    assert await h.scalar("select count(*) from charges") == 1
    assert len(h.mockpay.charges) == 1
    assert sum(r.headers.get("Idempotent-Replayed") == "true" for r in results) == 49


# Repeating the same key 30 times keeps returning 201 (replays are not throttled).
async def test_retries_with_same_key_are_not_rate_limited(h):
    for _ in range(30):
        assert (await h.checkout(key="patient-retry-key", guest="g2")).status_code == 201


# Worker dies after claiming, before the processor call: the sweeper charges exactly once.
async def test_worker_killed_after_claim_charge_completes_once_after_restart(h):
    await h.checkout(installments=2, days_out=60)
    t = utcnow() + timedelta(days=29)
    ids = await charging.claim_due_installments(h.sm, now=t)
    assert len(ids) == 1
    # *** worker dies here: charge row is `pending`, processor never called ***
    assert await charging.claim_due_installments(h.sm, now=t) == []  # nobody else grabs it
    recovered = await charging.recover_stuck_charges(h.sm, h.processor, now=t + timedelta(minutes=10))
    assert recovered == 1
    assert await h.scalar("select count(*) from charges where status='succeeded'") == 2
    assert len(h.mockpay.charges) == 2  # checkout + installment 2, nothing extra


# Processor charged but the worker died before saving: the replayed key does not charge again.
async def test_worker_killed_after_processor_call_is_not_charged_twice(h):
    await h.checkout(installments=2, days_out=60)
    t = utcnow() + timedelta(days=29)
    [cid] = await charging.claim_due_installments(h.sm, now=t)
    key = await h.scalar("select idempotency_key from charges where id = :i", i=cid)
    # The processor charged the card, then the worker died before saving the result.
    # (Same body the worker sends; a different body with the same key is rejected by the processor.)
    await h.processor.charge(
        key=key, amount=50_000, currency="USD", payment_method="pm_card_ok", metadata={"charge_id": str(cid)}
    )
    await charging.recover_stuck_charges(h.sm, h.processor, now=t + timedelta(minutes=10))
    assert await h.scalar("select status from charges where id = :i", i=cid) == "succeeded"
    assert len(h.mockpay.charges) == 2  # the replayed key did NOT create a third charge


# Processor hangs after charging: checkout returns 202 and the webhook later confirms it.
async def test_processor_timeout_at_checkout_then_webhook_confirms(h):
    fast_timeouts(h)
    await h.faults(timeout_rate=1.0, timeout_delay_s=0.5)
    r = await h.checkout()
    assert r.status_code == 202  # we don't know yet; never tell the guest "failed" here
    assert r.json()["plan"]["installments"][0]["charges"][0]["status"] == "pending"
    assert len(h.mockpay.charges) == 1  # ...but the money did move
    await h.settle_webhooks()
    plan = (await h.api.get(f"/reservations/{r.json()['reservation_id']}/plan")).json()
    assert plan["reservation_status"] == "confirmed"
    assert plan["plan"]["installments"][0]["charges"][0]["status"] == "succeeded"


# Checkout crashes before charging: a retry with the same key resumes, no second reservation.
async def test_checkout_crash_midway_retry_resumes_without_second_reservation(h, monkeypatch):
    real = checkout.execute_charge
    calls = {"n": 0}

    async def crash_once(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("pod OOM-killed")
        return await real(*a, **kw)

    monkeypatch.setattr(checkout, "execute_charge", crash_once)
    with pytest.raises(RuntimeError):
        await h.checkout(key="crashy-key-000001", guest="g3")
    r = await h.checkout(key="crashy-key-000001", guest="g3")
    assert r.status_code == 201
    assert await h.scalar("select count(*) from reservations") == 1
    assert await h.scalar("select count(*) from charges") == 1
    assert len(h.mockpay.charges) == 1


# All API calls time out and 20% of webhooks are dropped: backfill recovers every outcome.
async def test_20_percent_webhooks_dropped_backfill_restores_everything(h):
    fast_timeouts(h)
    # Every API call times out, so webhooks are the ONLY way we learn outcomes. Then drop 20% of them.
    await h.faults(timeout_rate=1.0, timeout_delay_s=0.3, webhook_drop_rate=0.2, seed=7)
    for _ in range(20):
        assert (await h.checkout()).status_code == 202
    await h.settle_webhooks()
    stuck = await h.scalar("select count(*) from charges where status='pending'")
    dropped = sum(d["result"] == "dropped" for d in h.mockpay.delivery_log)
    assert stuck == dropped > 0, "seed should drop some webhooks"

    await h.faults(timeout_rate=0.0)
    inserted = await webhooks.backfill(h.sm, h.processor, since=utcnow() - timedelta(hours=1))
    assert inserted == dropped
    await h.settle_webhooks()
    assert await h.scalar("select count(*) from charges where status='pending'") == 0
    assert await h.scalar("select count(*) from reservations where status='pending_payment'") == 0


# Refund webhook before success: deferred, final state is refunded, a late duplicate is ignored.
async def test_out_of_order_webhooks_final_state_is_correct(h):
    fast_timeouts(h)
    await h.faults(timeout_rate=1.0, timeout_delay_s=0.3, webhook_drop_rate=1.0)
    r = await h.checkout(total=40_000)
    assert r.status_code == 202
    await h.faults(timeout_rate=0.0)
    [ch] = h.mockpay.charges.values()
    # Support refunds from the processor dashboard; we haven't even seen the success yet.
    await h.processor.refund(key="dash-refund-1", processor_charge_id=ch["id"], amount=40_000)
    succeeded_evt, refunded_evt = h.mockpay.events
    assert (succeeded_evt["type"], refunded_evt["type"]) == ("charge.succeeded", "charge.refunded")

    # Deliver in the WRONG order.
    await webhooks.ingest(h.sm, refunded_evt)
    counts = await webhooks.process_inbox(h.sm)
    assert counts == {"too_early": 1}  # deferred, not dropped and not misapplied
    await webhooks.ingest(h.sm, succeeded_evt)
    await h.settle_webhooks()

    assert await h.scalar("select status from charges") == "refunded"
    assert await h.scalar("select amount_refunded from charges") == 40_000
    # A very late duplicate success must not drag it back to "succeeded".
    await webhooks.ingest(h.sm, {**succeeded_evt, "id": "evt_late_dup"})
    assert (await h.settle_webhooks()) == {"ignored_stale": 1}
    assert await h.scalar("select status from charges") == "refunded"


# Random delays, reordering and duplicates still converge to the right final states.
async def test_random_reorder_and_duplicates_converge(h):
    fast_timeouts(h)
    await h.faults(
        timeout_rate=1.0,
        timeout_delay_s=0.3,
        webhook_reorder=True,
        webhook_max_delay_ms=200,
        webhook_duplicate_rate=0.5,
        seed=3,
    )
    for pm in ["pm_card_ok"] * 6 + ["pm_card_closed"] * 4:
        await h.checkout(pm=pm)
    await h.settle_webhooks()
    assert dict(await h.rows("select status, count(*) from charges group by status")) == {"succeeded": 6, "declined": 4}
    assert await h.scalar("select count(*) from reservations where status='payment_failed'") == 4
