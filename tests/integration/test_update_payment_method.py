"""SP-101: guests can update their card, which reschedules failed/abandoned installments."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.clock import utcnow
from app.db.models import Installment, PaymentEvent, Reservation
from app.domain import charging

pytestmark = pytest.mark.integration


async def _put_card(h, res_id: str, pm: str):
    return await h.api.put(f"/reservations/{res_id}/payment-method", json={"payment_method": pm})


async def _fail_second_installment(h) -> str:
    """Checkout OK, then the card is closed when installment 2 is charged: failed + at_risk."""
    r = await h.checkout(installments=2, days_out=60)
    assert r.status_code == 201, r.text
    res_id = r.json()["reservation_id"]
    async with h.sm() as s, s.begin():
        (await s.get(Reservation, res_id)).payment_method = "pm_card_closed"
    t = utcnow() + timedelta(days=29)
    [cid] = await charging.claim_due_installments(h.sm, now=t)
    assert await charging.execute_charge(h.sm, h.processor, cid, now=t) == "declined"
    return res_id


async def _installment_2(h) -> Installment:
    async with h.sm() as s:
        return (await s.execute(select(Installment).where(Installment.number == 2))).scalar_one()


async def test_closed_card_then_new_card_charges_with_a_new_key(h):
    res_id = await _fail_second_installment(h)
    inst = await _installment_2(h)
    assert (inst.status, inst.attempts) == ("failed", 1)

    r = await _put_card(h, res_id, "pm_card_ok")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reservation_status"] == "pending_payment"
    assert body["plan"]["installments"][1]["status"] == "scheduled"
    inst = await _installment_2(h)
    assert inst.attempts == 1  # attempts keeps counting; it is not reset
    assert inst.next_attempt_at <= utcnow()  # due right away
    async with h.sm() as s:
        events = (await s.execute(select(PaymentEvent).where(PaymentEvent.type == "payment_method.updated"))).scalars()
        [event] = list(events)
    assert event.payload["rescheduled_installments"] == [2]

    # The charge worker picks it up and the new card succeeds.
    [cid] = await charging.claim_due_installments(h.sm, now=utcnow() + timedelta(seconds=1))
    assert await charging.execute_charge(h.sm, h.processor, cid) == "succeeded"

    inst = await _installment_2(h)
    assert (inst.status, inst.attempts) == ("succeeded", 2)
    plan = (await h.api.get(f"/reservations/{res_id}/plan")).json()
    assert plan["reservation_status"] == "confirmed"
    assert plan["plan"]["status"] == "completed"
    # The processor saw a NEW idempotency key for the new attempt (reusing a1 would replay the decline).
    keys = [c["idempotency_key"] for c in h.mockpay.charges.values() if ":2:" in c["idempotency_key"]]
    assert sorted(k.rsplit(":", 1)[1] for k in keys) == ["a1", "a2"]


async def test_abandoned_installment_is_rescheduled_too(h):
    res_id = await _fail_second_installment(h)
    async with h.sm() as s, s.begin():
        inst = (await s.execute(select(Installment).where(Installment.number == 2))).scalar_one()
        inst.status = "abandoned"  # e.g. soft declines ran out of retries
    r = await _put_card(h, res_id, "pm_card_ok")
    assert r.status_code == 200, r.text
    assert (await _installment_2(h)).status == "scheduled"


async def test_same_card_twice_is_a_no_op(h):
    res_id = await _fail_second_installment(h)
    assert (await _put_card(h, res_id, "pm_card_ok")).status_code == 200
    r = await _put_card(h, res_id, "pm_card_ok")
    assert r.status_code == 200
    assert await h.scalar("select count(*) from payment_events where type = 'payment_method.updated'") == 1


async def test_healthy_reservation_just_changes_the_card(h):
    r = await h.checkout(installments=2, days_out=60)
    res_id = r.json()["reservation_id"]
    r = await _put_card(h, res_id, "pm_card_soft_then_ok")
    assert r.status_code == 200, r.text
    assert r.json()["reservation_status"] == "confirmed"
    assert (await _installment_2(h)).status == "scheduled"
    assert await h.scalar("select payment_method from reservations") == "pm_card_soft_then_ok"


async def test_rejected_while_a_charge_is_in_flight(h):
    r = await h.checkout(installments=2, days_out=60)
    res_id = r.json()["reservation_id"]
    await charging.claim_due_installments(h.sm, now=utcnow() + timedelta(days=29))  # installment 2 -> processing
    r = await _put_card(h, res_id, "pm_card_ok")
    assert r.status_code == 409
    assert await h.scalar("select count(*) from payment_events where type = 'payment_method.updated'") == 0


async def test_rejected_when_cancelled(h):
    res_id = await _fail_second_installment(h)
    assert (await h.api.post(f"/reservations/{res_id}/cancel", json={})).status_code == 200
    assert (await _put_card(h, res_id, "pm_card_ok")).status_code == 409


async def test_rejected_when_declined_at_checkout(h):
    r = await h.checkout(pm="pm_card_closed", installments=2)
    assert r.status_code == 402
    assert (await _put_card(h, r.json()["reservation_id"], "pm_card_ok")).status_code == 409


async def test_unknown_reservation_is_404(h):
    r = await _put_card(h, "00000000-0000-0000-0000-000000000000", "pm_card_ok")
    assert r.status_code == 404
    assert r.json()["detail"] == "reservation not found"  # ours, not FastAPI's "Not Found" for a missing route
