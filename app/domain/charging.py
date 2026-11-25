"""Charging installments.

The flow for every charge attempt is split into phases so a crash at any point is recoverable:

  Phase 1 (DB tx):   lock installment, mark it processing, insert a `pending` charge row with a
                     deterministic idempotency key. COMMIT.
  Phase 2 (network): call the processor with that key. No DB transaction is held open here.
  Phase 3 (DB tx):   lock the charge, apply the outcome through the state machine. COMMIT.

If the process dies between 1 and 3, the charge stays `pending`. The stuck-charge sweeper
re-sends the SAME key; the processor answers with the original result, so the guest is
charged exactly once. The processor's webhook can also finish the job (whichever comes first
wins; the other is ignored as stale).

Idempotency key: f"{plan_id}:{installment_number}:a{attempt}". The attempt number is part of
the key because a retry after a soft decline is a NEW charge attempt; reusing the old key would
just replay the old decline.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clock import utcnow
from app.config import get_settings
from app.db.models import Charge, Installment, PaymentPlan, Reservation
from app.domain.events import notify, record_event
from app.domain.retry_policy import decide_retry
from app.domain.state_machine import INSTALLMENT_TRANSITIONS, EventVerdict, judge_charge_event, transition
from app.observability.metrics import ABANDONED_INSTALLMENTS, CHARGES, RECOVERED_PAYMENTS
from app.processor.client import Outcome, ProcessorClient

log = logging.getLogger(__name__)


# Deterministic processor idempotency key for one attempt of one installment.
def charge_key(plan_id: uuid.UUID, number: int, attempt: int) -> str:
    return f"{plan_id}:{number}:a{attempt}"


async def start_attempt(s: AsyncSession, inst: Installment, plan: PaymentPlan, res: Reservation) -> Charge:
    """Phase 1. Caller must hold a row lock on `inst`."""
    inst.status = transition(INSTALLMENT_TRANSITIONS, inst.status, "processing")
    inst.attempts += 1
    # Insert the pending charge row carrying this attempt's idempotency key.
    charge = Charge(
        installment_id=inst.id,
        reservation_id=res.id,
        idempotency_key=charge_key(plan.id, inst.number, inst.attempts),
        amount=inst.amount,
        currency=res.currency,
        status="pending",
    )
    s.add(charge)
    await s.flush()
    return charge


async def claim_due_installments(
    sm: async_sessionmaker, now: datetime | None = None, limit: int = 50
) -> list[uuid.UUID]:
    """Claim installments that are due. SKIP LOCKED lets many workers run without double-claiming."""
    now = now or utcnow()
    async with sm() as s, s.begin():
        rows = (
            await s.execute(
                select(Installment, PaymentPlan, Reservation)
                .join(PaymentPlan, PaymentPlan.id == Installment.plan_id)
                .join(Reservation, Reservation.id == PaymentPlan.reservation_id)
                .where(
                    Installment.status.in_(("scheduled", "retrying")),
                    Installment.next_attempt_at <= now,
                    PaymentPlan.status == "active",
                )
                .order_by(Installment.next_attempt_at)
                .limit(limit)
                .with_for_update(of=Installment, skip_locked=True)
            )
        ).all()
        # Start an attempt (phase 1) for every claimed installment in this same transaction.
        charges = [await start_attempt(s, inst, plan, res) for inst, plan, res in rows]
        return [c.id for c in charges]


async def execute_charge(
    sm: async_sessionmaker, processor: ProcessorClient, charge_id: uuid.UUID, now: datetime | None = None
) -> str:
    """Phase 2 + 3. Returns the resulting charge status (or 'pending' if still unknown)."""
    async with sm() as s:
        charge = await s.get(Charge, charge_id)
        res = await s.get(Reservation, charge.reservation_id)
        # Already settled (by a webhook or an earlier run): nothing to do.
        if charge.status != "pending":
            return charge.status
        key, amount, currency, pm = charge.idempotency_key, charge.amount, res.currency, res.payment_method

    # Phase 2: call the processor with no DB transaction open.
    result = await processor.charge(
        key=key, amount=amount, currency=currency, payment_method=pm, metadata={"charge_id": str(charge_id)}
    )
    # No definite answer: leave it pending for the webhook or sweeper to settle.
    if result.outcome in (Outcome.UNKNOWN, Outcome.REJECTED):
        level = logging.ERROR if result.outcome is Outcome.REJECTED else logging.WARNING
        log.log(level, "charge.outcome_unknown", extra={"charge_id": str(charge_id), "err": result.error})
        CHARGES.labels(result.outcome.value).inc()
        return "pending"

    # Phase 3: lock the charge and apply the definite outcome.
    target = "succeeded" if result.outcome is Outcome.SUCCEEDED else "declined"
    async with sm() as s, s.begin():
        charge = (await s.execute(select(Charge).where(Charge.id == charge_id).with_for_update())).scalar_one()
        await apply_charge_outcome(s, charge, target, result.processor_id, result.decline_code, now=now, source="api")
        return charge.status


async def apply_charge_outcome(
    s: AsyncSession,
    charge: Charge,
    target: str,
    processor_id: str | None,
    decline_code: str | None,
    *,
    now: datetime | None = None,
    source: str,
) -> EventVerdict:
    """Apply a processor result to a LOCKED charge row. Used by both the API path and webhooks."""
    now = now or utcnow()
    # Ask the state machine whether this outcome is new, stale, or early; only new ones are applied.
    verdict = judge_charge_event(charge.status, target)
    if verdict is not EventVerdict.APPLY:
        log.info(
            "charge.outcome_skipped",
            extra={
                "charge_id": str(charge.id),
                "current": charge.status,
                "target": target,
                "verdict": verdict.value,
                "source": source,
            },
        )
        return verdict

    # Record the outcome on the charge row.
    charge.status = target
    charge.processor_txn_id = charge.processor_txn_id or processor_id
    charge.decline_code = decline_code
    CHARGES.labels(target).inc()

    # Lock the installment and reservation that this charge pays for.
    inst = (
        await s.execute(select(Installment).where(Installment.id == charge.installment_id).with_for_update())
    ).scalar_one()
    plan = (await s.execute(select(PaymentPlan).where(PaymentPlan.id == inst.plan_id))).scalar_one()
    res = (
        await s.execute(select(Reservation).where(Reservation.id == plan.reservation_id).with_for_update())
    ).scalar_one()

    if inst.status != "processing":
        # e.g. reservation was cancelled while the charge was in flight. Ledger still records it.
        log.warning("charge.installment_not_processing", extra={"installment_id": str(inst.id), "status": inst.status})
        record_event(
            s,
            res.id,
            f"charge.{target}",
            charge.amount,
            charge_id=charge.id,
            installment=inst.number,
            note="installment_not_processing",
        )
        return verdict

    # Success: close the installment, confirm the reservation, complete the plan if all are paid.
    if target == "succeeded":
        inst.status = transition(INSTALLMENT_TRANSITIONS, inst.status, "succeeded")
        inst.next_attempt_at = None
        if inst.attempts > 1:
            RECOVERED_PAYMENTS.inc()
        record_event(
            s,
            res.id,
            "charge.succeeded",
            charge.amount,
            charge_id=charge.id,
            installment=inst.number,
            attempt=inst.attempts,
            source=source,
        )
        if res.status in ("pending_payment", "at_risk"):
            res.status = "confirmed"
        siblings = (await s.execute(select(Installment.status).where(Installment.plan_id == plan.id))).scalars()
        if all(st == "succeeded" for st in siblings):
            plan.status = "completed"
        return verdict

    # declined
    inst.last_decline_code = decline_code
    record_event(
        s,
        res.id,
        "charge.declined",
        charge.amount,
        charge_id=charge.id,
        installment=inst.number,
        attempt=inst.attempts,
        decline_code=decline_code,
        source=source,
    )

    if inst.number == 1 and res.status == "pending_payment":
        # Declined at checkout: the guest is right there, so tell them now instead of retrying.
        inst.status = transition(INSTALLMENT_TRANSITIONS, inst.status, "failed")
        res.status = "payment_failed"
        plan.status = "failed"
        for other in (
            await s.execute(select(Installment).where(Installment.plan_id == plan.id, Installment.number > 1))
        ).scalars():
            other.status = "cancelled"
        return verdict

    # Later installment declined: retry, fail (needs a new card), or abandon per the retry policy.
    decision = decide_retry(
        decline_code=decline_code, attempts_so_far=inst.attempts, now=now, deadline=plan.payment_deadline
    )
    if decision.retry:
        assert decision.next_attempt_at is not None
        inst.status = transition(INSTALLMENT_TRANSITIONS, inst.status, "retrying")
        inst.next_attempt_at = decision.next_attempt_at
        notify(
            s,
            res.guest_id,
            res.id,
            "payment_failed_will_retry",
            installment=inst.number,
            next_attempt_at=decision.next_attempt_at.isoformat(),
        )
    elif decision.reason == "non_retryable":
        inst.status = transition(INSTALLMENT_TRANSITIONS, inst.status, "failed")
        res.status = "at_risk"
        notify(s, res.guest_id, res.id, "update_payment_method", installment=inst.number, decline_code=decline_code)
    else:
        inst.status = transition(INSTALLMENT_TRANSITIONS, inst.status, "abandoned")
        ABANDONED_INSTALLMENTS.labels(decision.reason).inc()
        res.status = "at_risk"
        notify(s, res.guest_id, res.id, "payment_abandoned", installment=inst.number, reason=decision.reason)
    record_event(s, res.id, f"installment.{inst.status}", inst.amount, installment=inst.number, reason=decision.reason)
    return verdict


async def recover_stuck_charges(
    sm: async_sessionmaker, processor: ProcessorClient, now: datetime | None = None, limit: int = 50
) -> int:
    """Re-drive charges stuck in `pending` (worker crashed, processor timed out)."""
    now = now or utcnow()
    cutoff = now - timedelta(seconds=get_settings().stuck_processing_after_s)
    async with sm() as s, s.begin():
        stuck = (
            (
                await s.execute(
                    select(Charge)
                    .where(Charge.status == "pending", Charge.updated_at < cutoff)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        ids = [c.id for c in stuck]
        for c in stuck:
            c.updated_at = now  # push it out of the window so another sweeper won't grab it
    # After the claim commits, re-send each stuck charge with its original key.
    for cid in ids:
        log.info("charge.recovering", extra={"charge_id": str(cid)})
        await execute_charge(sm, processor, cid, now=now)
    return len(ids)
