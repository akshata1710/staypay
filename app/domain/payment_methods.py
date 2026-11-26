"""Updating a reservation's card (SP-101).

A hard decline leaves an installment `failed` (or `abandoned` once retries run out) and tells the
guest to update their card. Updating it puts every such installment back to `scheduled`, due now,
so the charge worker picks it up on its next tick.

The new attempt gets a new processor idempotency key on its own: start_attempt() bumps `attempts`,
which is part of the key. Reusing the old key would just replay the old decline.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.clock import utcnow
from app.db.models import Installment, PaymentPlan, Reservation
from app.domain.events import record_event
from app.domain.state_machine import INSTALLMENT_TRANSITIONS, transition
from app.domain.views import plan_view

RESCHEDULABLE = ("failed", "abandoned")
# Cancelled reservations are closed; `payment_failed` means the card was declined at checkout, the
# plan is `failed` and the guest was told on the spot, so they check out again instead.
CLOSED_STATUSES = ("cancelled", "cancellation_review", "payment_failed")


class PaymentMethodConflict(Exception):
    """The card can't be changed right now (reservation closed, or a charge is in flight)."""


async def update_payment_method(
    sm: async_sessionmaker, reservation_id: uuid.UUID, payment_method: str, now: datetime | None = None
) -> dict:
    now = now or utcnow()
    async with sm() as s, s.begin():
        # Lock the reservation, then its installments: the same order cancel_reservation uses.
        res = (
            await s.execute(select(Reservation).where(Reservation.id == reservation_id).with_for_update())
        ).scalar_one_or_none()
        if res is None:
            raise LookupError("reservation not found")
        if res.status in CLOSED_STATUSES:
            raise PaymentMethodConflict(f"reservation is {res.status}")
        plan = (await s.execute(select(PaymentPlan).where(PaymentPlan.reservation_id == res.id))).scalar_one()
        insts = (
            (
                await s.execute(
                    select(Installment)
                    .where(Installment.plan_id == plan.id)
                    .order_by(Installment.number)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        # A charge in flight still uses the old card; changing it now would leave its outcome ambiguous.
        if any(i.status == "processing" for i in insts):
            raise PaymentMethodConflict("a payment is being processed")

        # Reschedule every failed/abandoned installment, due now. `attempts` keeps counting.
        rescheduled = []
        for i in insts:
            if i.status in RESCHEDULABLE:
                i.status = transition(INSTALLMENT_TRANSITIONS, i.status, "scheduled")
                i.next_attempt_at = now
                rescheduled.append(i.number)

        # Same card and nothing to retry: a repeated PUT changes nothing (no duplicate event).
        if payment_method != res.payment_method or rescheduled:
            res.payment_method = payment_method
            if rescheduled and res.status == "at_risk":
                res.status = "pending_payment"  # until the charge lands; success sets `confirmed`
            record_event(s, res.id, "payment_method.updated", None, rescheduled_installments=rescheduled)

    async with sm() as s:
        view = await plan_view(s, reservation_id)
    assert view is not None
    return view
