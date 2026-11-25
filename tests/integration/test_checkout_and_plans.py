from datetime import timedelta

import pytest
from sqlalchemy import select

from app.clock import utcnow
from app.db.models import Installment, Notification
from app.domain import charging

pytestmark = pytest.mark.integration


async def test_checkout_success_creates_one_charge(h):
    r = await h.checkout(total=50_000)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["reservation_status"] == "confirmed"
    assert body["plan"]["installments"][0]["charges"][0]["status"] == "succeeded"
    assert await h.scalar("select count(*) from charges") == 1
    assert len(h.mockpay.charges) == 1


async def test_same_key_returns_stored_response(h):
    r1 = await h.checkout(key="key-replay-0001", guest="g1")
    r2 = await h.checkout(key="key-replay-0001", guest="g1")
    assert r1.status_code == r2.status_code == 201
    assert r1.json() == r2.json()
    assert r2.headers.get("Idempotent-Replayed") == "true"
    assert await h.scalar("select count(*) from reservations") == 1


async def test_same_key_different_body_is_rejected(h):
    await h.checkout(key="key-reuse-00001", guest="g1", total=1000)
    r = await h.checkout(key="key-reuse-00001", guest="g1", total=2000)
    assert r.status_code == 422
    assert r.json()["error"] == "idempotency_key_reused"


async def test_missing_idempotency_key_is_422(h):
    r = await h.api.post("/checkout", json={})
    assert r.status_code == 422


async def test_hard_decline_at_checkout_fails_fast(h):
    r = await h.checkout(pm="pm_card_closed", installments=3)
    assert r.status_code == 402
    body = r.json()
    assert body["error"]["decline_code"] == "card_closed"
    assert body["reservation_status"] == "payment_failed"
    assert [i["status"] for i in body["plan"]["installments"]] == ["failed", "cancelled", "cancelled"]


async def test_installment_plan_schedule(h):
    r = await h.checkout(installments=4, total=100_003, days_out=90)
    plan = r.json()["plan"]
    assert sum(i["amount"] for i in plan["installments"]) == 100_003
    assert [i["status"] for i in plan["installments"]] == ["succeeded", "scheduled", "scheduled", "scheduled"]


async def test_charge_worker_charges_due_installments(h):
    r = await h.checkout(installments=3, days_out=60)
    res_id = r.json()["reservation_id"]
    # Nothing is due yet.
    assert await charging.claim_due_installments(h.sm) == []
    # Fast-forward past every due date.
    future = utcnow() + timedelta(days=58)
    ids = await charging.claim_due_installments(h.sm, now=future)
    assert len(ids) == 2
    for cid in ids:
        assert await charging.execute_charge(h.sm, h.processor, cid, now=future) == "succeeded"
    plan = (await h.api.get(f"/reservations/{res_id}/plan")).json()
    assert plan["plan"]["status"] == "completed"


async def test_soft_decline_is_retried_then_recovers(h):
    # pm_card_soft_then_ok declines twice (insufficient_funds) then succeeds.
    # Use pm_card_ok for checkout, then switch the reservation's card to the flaky one.
    r = await h.checkout(installments=2, days_out=60)
    res_id = r.json()["reservation_id"]
    async with h.sm() as s, s.begin():
        from app.db.models import Reservation

        res = await s.get(Reservation, res_id)
        res.payment_method = "pm_card_soft_then_ok"

    t = utcnow() + timedelta(days=29)
    for expected in ("declined", "declined", "succeeded"):
        ids = await charging.claim_due_installments(h.sm, now=t)
        assert len(ids) == 1
        assert await charging.execute_charge(h.sm, h.processor, ids[0], now=t) == expected
        async with h.sm() as s:
            inst = (await s.execute(select(Installment).where(Installment.number == 2))).scalar_one()
        if expected == "declined":
            assert inst.status == "retrying" and inst.next_attempt_at > t
            t = inst.next_attempt_at
    assert inst.status == "succeeded" and inst.attempts == 3
    # Each attempt used its own processor idempotency key.
    assert await h.scalar("select count(distinct idempotency_key) from charges") == 4


async def test_hard_decline_on_later_installment_notifies_guest_without_retry(h):
    r = await h.checkout(installments=2, days_out=60)
    res_id = r.json()["reservation_id"]
    async with h.sm() as s, s.begin():
        from app.db.models import Reservation

        (await s.get(Reservation, res_id)).payment_method = "pm_card_closed"
    t = utcnow() + timedelta(days=29)
    ids = await charging.claim_due_installments(h.sm, now=t)
    await charging.execute_charge(h.sm, h.processor, ids[0], now=t)
    async with h.sm() as s:
        inst = (await s.execute(select(Installment).where(Installment.number == 2))).scalar_one()
        notes = (await s.execute(select(Notification.kind))).scalars().all()
    assert inst.status == "failed" and inst.next_attempt_at is not None  # not re-queued
    assert await charging.claim_due_installments(h.sm, now=t + timedelta(days=5)) == []
    assert notes == ["update_payment_method"]
    plan = (await h.api.get(f"/reservations/{res_id}/plan")).json()
    assert plan["reservation_status"] == "at_risk"


async def test_retry_gives_up_at_payment_deadline(h):
    r = await h.checkout(installments=2, days_out=8)  # deadline is 5 days out
    res_id = r.json()["reservation_id"]
    async with h.sm() as s, s.begin():
        from app.db.models import Reservation

        (await s.get(Reservation, res_id)).payment_method = "pm_card_insufficient_funds"
    t = utcnow() + timedelta(days=4, hours=20)
    ids = await charging.claim_due_installments(h.sm, now=t)
    await charging.execute_charge(h.sm, h.processor, ids[0], now=t)
    async with h.sm() as s:
        inst = (await s.execute(select(Installment).where(Installment.number == 2))).scalar_one()
        notes = (await s.execute(select(Notification.kind))).scalars().all()
    assert inst.status == "abandoned"
    assert notes == ["payment_abandoned"]


async def test_checkout_rate_limit(h):
    for i in range(10):
        await h.checkout(guest="spammer", total=100 + i)
    r = await h.checkout(guest="spammer", total=999)
    assert r.status_code == 429
