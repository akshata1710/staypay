"""Checkout with idempotency keys.

Guarantee: any number of requests with the same Idempotency-Key produce at most one
reservation and at most one charge per installment attempt.

How:
1. INSERT the key with a UNIQUE/PK constraint. Exactly one request wins the insert and becomes
   the owner. Everyone else waits for the owner's stored response (or gets a 409 after a wait).
2. The owner works in "atomic phases" and records a recovery point after each one:
      started -> reservation_created -> finished
   If the owner crashes, a later retry with the same key takes over (after a staleness window)
   and resumes from the recovery point instead of creating a second reservation.
3. The charge uses its own deterministic processor idempotency key, so even a resumed request
   can't double-charge at the processor.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.clock import utcnow
from app.config import get_settings
from app.db.models import Charge, IdempotencyKey, Installment, PaymentPlan, Reservation
from app.domain.charging import execute_charge, start_attempt
from app.domain.events import record_event
from app.domain.plans import build_schedule, payment_deadline
from app.domain.views import plan_view
from app.observability.metrics import CHECKOUTS, IDEMPOTENT_REPLAYS
from app.processor.client import ProcessorClient

log = logging.getLogger(__name__)


# Validated request body for POST /checkout.
class CheckoutRequest(BaseModel):
    guest_id: str = Field(min_length=1, max_length=64)
    listing_id: str = Field(min_length=1, max_length=64)
    check_in: date
    total_amount: int = Field(gt=0, description="Minor units (cents)")
    currency: str = Field(min_length=3, max_length=3)
    installments: int = Field(default=1, ge=1)
    cancellation_policy: Literal["flexible", "moderate", "strict"] = "moderate"
    payment_method: str = Field(min_length=1, max_length=64)

    # Normalise the currency code to upper case ("usd" -> "USD").
    @field_validator("currency")
    @classmethod
    def upper(cls, v: str) -> str:
        return v.upper()


# HTTP status and body to return, plus whether it was replayed from a stored response.
class CheckoutResult(BaseModel):
    status_code: int
    body: dict
    replayed: bool = False


# Stable SHA-256 of the request body, used to detect a key reused with a different body.
def request_hash(req: CheckoutRequest) -> str:
    return hashlib.sha256(json.dumps(req.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()


# Entry point: claim the idempotency key, then either wait/replay or run the checkout as owner.
async def checkout(
    sm: async_sessionmaker, processor: ProcessorClient, key: str, req: CheckoutRequest, now: datetime | None = None
) -> CheckoutResult:
    settings = get_settings()
    if req.installments > settings.max_installments:
        return CheckoutResult(
            status_code=422, body={"error": "too_many_installments", "max": settings.max_installments}
        )
    h = request_hash(req)

    async with sm() as s, s.begin():
        # Try to insert the key; only the first request gets a row back and becomes the owner.
        won = (
            await s.execute(
                insert(IdempotencyKey)
                .values(key=key, request_hash=h)
                .on_conflict_do_nothing(index_elements=["key"])
                .returning(IdempotencyKey.key)
            )
        ).scalar_one_or_none()

    # Lost the race: wait for the owner's result, or take over if the owner looks dead.
    if won is None:
        outcome = await _wait_for_owner(sm, key, h)
        if isinstance(outcome, CheckoutResult):
            return outcome
        # else: we took over a stale key and continue as the owner

    try:
        # We own the key: do the actual checkout work.
        result = await _run_as_owner(sm, processor, key, req, now or utcnow())
    except Exception:
        # Let the next retry take over right away instead of waiting out the stale window.
        async with sm() as s, s.begin():
            await s.execute(
                update(IdempotencyKey).where(IdempotencyKey.key == key).values(locked_at=datetime(2000, 1, 1))
            )
        CHECKOUTS.labels("error").inc()
        raise
    # Count the checkout outcome by HTTP status for metrics.
    CHECKOUTS.labels({201: "created", 202: "pending", 402: "declined"}.get(result.status_code, "other")).inc()
    return result


# Poll the key until the owner finishes, goes stale (takeover), or we time out (409).
async def _wait_for_owner(sm: async_sessionmaker, key: str, h: str) -> CheckoutResult | Literal["takeover"]:
    settings = get_settings()
    deadline = asyncio.get_running_loop().time() + settings.idempotency_wait_s
    while True:
        async with sm() as s, s.begin():
            row = await s.get(IdempotencyKey, key)
            # Same key, different body: refuse instead of returning another request's result.
            if row.request_hash != h:
                return CheckoutResult(
                    status_code=422,
                    body={
                        "error": "idempotency_key_reused",
                        "message": "This Idempotency-Key was used with a different request body.",
                    },
                )
            # Owner finished: replay its stored response.
            if row.status == "completed":
                IDEMPOTENT_REPLAYS.inc()
                return CheckoutResult(status_code=row.response_code, body=row.response, replayed=True)
            # Owner silent past the stale window: atomically claim the key for ourselves.
            cutoff = utcnow() - timedelta(seconds=settings.idempotency_stale_after_s)
            took = (
                await s.execute(
                    update(IdempotencyKey)
                    .where(
                        IdempotencyKey.key == key,
                        IdempotencyKey.status == "in_progress",
                        IdempotencyKey.locked_at < cutoff,
                    )
                    .values(locked_at=utcnow())
                    .returning(IdempotencyKey.key)
                )
            ).scalar_one_or_none()
            if took:
                log.warning("idempotency.takeover", extra={"key": key, "recovery_point": row.recovery_point})
                return "takeover"
        # Waited too long: tell the client to retry later.
        if asyncio.get_running_loop().time() > deadline:
            return CheckoutResult(
                status_code=409,
                body={
                    "error": "request_in_progress",
                    "message": "A request with this Idempotency-Key is still being processed. Retry later.",
                },
            )
        await asyncio.sleep(0.05)


# Run the checkout in recoverable phases, recording progress on the idempotency key.
async def _run_as_owner(
    sm: async_sessionmaker, processor: ProcessorClient, key: str, req: CheckoutRequest, now: datetime
) -> CheckoutResult:
    settings = get_settings()

    # Phase 1: create reservation + plan + installments (unless a previous owner already did).
    async with sm() as s, s.begin():
        idem = (await s.execute(select(IdempotencyKey).where(IdempotencyKey.key == key).with_for_update())).scalar_one()
        # Only build the reservation if no earlier owner already did.
        if idem.recovery_point == "started":
            deadline = payment_deadline(req.check_in, settings.payment_deadline_days_before_checkin)
            try:
                schedule = build_schedule(req.total_amount, req.installments, now, deadline)
            # Schedule can't be built: store a 422 so retries with this key get the same answer.
            except ValueError as e:
                body = {"error": "invalid_plan", "message": str(e)}
                idem.status, idem.response_code, idem.response = "completed", 422, body
                return CheckoutResult(status_code=422, body=body)
            res = Reservation(
                guest_id=req.guest_id,
                listing_id=req.listing_id,
                check_in=req.check_in,
                total_amount=req.total_amount,
                currency=req.currency,
                cancellation_policy=req.cancellation_policy,
                payment_method=req.payment_method,
                created_at=now,
            )
            s.add(res)
            await s.flush()
            # Create the plan, then one Installment row per scheduled payment.
            plan = PaymentPlan(reservation_id=res.id, installments=req.installments, payment_deadline=deadline)
            s.add(plan)
            await s.flush()
            for p in schedule:
                s.add(
                    Installment(
                        plan_id=plan.id, number=p.number, amount=p.amount, due_at=p.due_at, next_attempt_at=p.due_at
                    )
                )
            record_event(
                s,
                res.id,
                "reservation.created",
                req.total_amount,
                installments=req.installments,
                policy=req.cancellation_policy,
            )
            # Mark phase 1 done so a takeover skips straight to charging.
            idem.reservation_id, idem.recovery_point = res.id, "reservation_created"
        reservation_id: uuid.UUID = idem.reservation_id

    # Phase 2: start (or find) the charge for installment #1.
    async with sm() as s, s.begin():
        plan = (await s.execute(select(PaymentPlan).where(PaymentPlan.reservation_id == reservation_id))).scalar_one()
        res = await s.get(Reservation, reservation_id)
        inst = (
            await s.execute(
                select(Installment).where(Installment.plan_id == plan.id, Installment.number == 1).with_for_update()
            )
        ).scalar_one()
        # First pass starts a new charge attempt; a resumed pass reuses the charge already started.
        if inst.status == "scheduled":
            charge = await start_attempt(s, inst, plan, res)
        else:
            charge = (
                await s.execute(
                    select(Charge).where(Charge.installment_id == inst.id).order_by(Charge.created_at.desc()).limit(1)
                )
            ).scalar_one()
        charge_id = charge.id

    # Phase 3: talk to the processor (no DB transaction open).
    status = await execute_charge(sm, processor, charge_id, now=now)

    # Phase 4: build and store the response.
    async with sm() as s, s.begin():
        view = await plan_view(s, reservation_id)
        assert view is not None
        first = view["plan"]["installments"][0]["charges"][-1]
        # Map the charge status to an HTTP code: 201 paid, 402 declined, 202 still unknown.
        if status == "succeeded":
            code = 201
        elif status == "declined":
            code = 402
            view["error"] = {"type": "card_declined", "decline_code": first["decline_code"]}
        else:
            code = 202  # processor outcome unknown; webhook/sweeper will settle it. Client polls /plan.
        idem = (await s.execute(select(IdempotencyKey).where(IdempotencyKey.key == key).with_for_update())).scalar_one()
        # Store the response so every later retry with this key replays it.
        idem.status, idem.recovery_point, idem.response_code, idem.response = "completed", "finished", code, view
    return CheckoutResult(status_code=code, body=view)
