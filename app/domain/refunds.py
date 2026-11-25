"""Refund engine: cancellation policy rules + idempotent refunds + audit log.

compute_refund() is a pure function (no DB, no clock) so every rule can be unit tested with a
table of cases. cancel_reservation() wires it to the database and the processor.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.clock import utcnow
from app.db.models import (
    Charge,
    Installment,
    ManualReview,
    PaymentPlan,
    Refund,
    RefundAudit,
    Reservation,
)
from app.domain.events import record_event
from app.domain.money import format_money
from app.domain.state_machine import CHARGE_TRANSITIONS, transition
from app.observability.metrics import REFUNDS
from app.processor.client import Outcome, ProcessorClient

log = logging.getLogger(__name__)
POLICY_FILE = Path(__file__).resolve().parents[2] / "policies" / "cancellation.yaml"


# Load policies/cancellation.yaml once and cache it.
@lru_cache
def load_policies() -> dict:
    return yaml.safe_load(POLICY_FILE.read_text())


# Plain snapshot of a charge, so compute_refund needs no database.
@dataclass(frozen=True)
class PaidCharge:
    charge_id: str
    amount: int
    amount_refunded: int
    created_at: datetime
    disputed: bool = False


# Refund calculation result: route, percentage, rule, per-charge amounts, and an explanation.
@dataclass
class RefundDecision:
    route: str  # auto | manual_review
    refund_pct: int | None
    rule: str
    total_refund: int
    allocations: list[dict] = field(default_factory=list)  # [{charge_id, amount}]
    explanation: str = ""


def check_in_datetime(check_in: date) -> datetime:
    """Check-in is treated as 15:00 UTC on the check-in date."""
    return datetime.combine(check_in, time(15, 0), tzinfo=UTC)


# Pure policy calculation: how much to refund for this cancellation, and from which charges.
def compute_refund(
    *,
    policy_name: str,
    total_amount: int,
    currency: str,
    check_in: date,
    booked_at: datetime,
    cancelled_at: datetime,
    charges: list[PaidCharge],
) -> RefundDecision:
    policies = load_policies()
    if policy_name not in policies["policies"]:
        raise ValueError(f"unknown policy {policy_name}")
    ci = check_in_datetime(check_in)

    # Disputed charges and cancellations after check-in always go to a person.
    if any(c.disputed for c in charges):
        return RefundDecision(
            "manual_review",
            None,
            "disputed_charge",
            0,
            explanation="A charge on this reservation is disputed; a person must decide.",
        )
    if cancelled_at >= ci:
        return RefundDecision(
            "manual_review",
            None,
            "after_check_in",
            0,
            explanation="Cancelled after check-in (partial stay); a person must decide.",
        )

    # The grace period beats policy tiers; otherwise use the first tier whose threshold is met.
    hours_before = (ci - cancelled_at).total_seconds() / 3600
    grace = policies["grace_period"]
    if cancelled_at - booked_at <= timedelta(hours=grace["hours_after_booking"]) and ci - cancelled_at >= timedelta(
        days=grace["min_days_before_checkin"]
    ):
        pct, rule = 100, "grace_period"
    else:
        tier = next(
            t for t in policies["policies"][policy_name]["tiers"] if hours_before >= t["min_hours_before_checkin"]
        )
        pct, rule = tier["refund_pct"], f"{policy_name}>={tier['min_hours_before_checkin']}h"

    # Refund = what the guest paid minus what they still owe under the policy (never negative).
    paid = sum(c.amount - c.amount_refunded for c in charges)
    owed = total_amount - (total_amount * pct) // 100  # what the guest keeps owing
    total_refund = max(0, paid - owed)

    # Spread the refund across charges, newest first.
    allocations, remaining = [], total_refund
    for c in sorted(charges, key=lambda c: c.created_at, reverse=True):  # newest charge first
        if remaining == 0:
            break
        take = min(remaining, c.amount - c.amount_refunded)
        if take > 0:
            allocations.append({"charge_id": c.charge_id, "amount": take})
            remaining -= take

    # Human-readable explanation, stored in the audit and shown to support.
    explanation = (
        f"{policy_name.capitalize()} policy, cancelled {hours_before:.1f}h before check-in "
        f"(rule {rule}): {pct}% of the {format_money(total_amount, currency)} total is refundable. "
        f"Guest paid {format_money(paid, currency)} and keeps owing {format_money(owed, currency)}, "
        f"so the refund is {format_money(total_refund, currency)}."
    )
    if paid < owed:
        explanation += " Unpaid installments are cancelled; the shortfall is not collected."
    return RefundDecision("auto", pct, rule, total_refund, allocations, explanation)


class PaymentInFlight(Exception):
    """A charge is mid-flight; cancelling now could miss it. Client should retry shortly."""


# Cancel a reservation: stop unpaid installments, decide the refund, audit it, then send refunds.
async def cancel_reservation(
    sm: async_sessionmaker,
    processor: ProcessorClient,
    reservation_id: uuid.UUID,
    cancelled_at: datetime | None = None,
    decided_by: str = "system",
) -> dict:
    cancelled_at = cancelled_at or utcnow()
    async with sm() as s, s.begin():
        res = (
            await s.execute(select(Reservation).where(Reservation.id == reservation_id).with_for_update())
        ).scalar_one_or_none()
        if res is None:
            raise LookupError("reservation not found")
        if res.status in ("cancelled", "cancellation_review"):
            return await _summary(sm, reservation_id, already=True)  # idempotent

        plan = (await s.execute(select(PaymentPlan).where(PaymentPlan.reservation_id == res.id))).scalar_one()
        insts = (
            (await s.execute(select(Installment).where(Installment.plan_id == plan.id).with_for_update()))
            .scalars()
            .all()
        )
        # Refuse while a charge is in flight; otherwise cancel every unpaid installment.
        if any(i.status == "processing" for i in insts):
            raise PaymentInFlight()
        for i in insts:
            if i.status in ("scheduled", "retrying", "failed", "abandoned"):
                i.status = "cancelled"
        plan.status = "cancelled" if plan.status != "completed" else plan.status

        charges = (
            (
                await s.execute(
                    select(Charge).where(
                        Charge.reservation_id == res.id, Charge.status.in_(("succeeded", "partially_refunded"))
                    )
                )
            )
            .scalars()
            .all()
        )
        # Decide the refund from the charges that actually took money.
        decision = compute_refund(
            policy_name=res.cancellation_policy,
            total_amount=res.total_amount,
            currency=res.currency,
            check_in=res.check_in,
            booked_at=res.created_at,
            cancelled_at=cancelled_at,
            charges=[PaidCharge(str(c.id), c.amount, c.amount_refunded, c.created_at, c.disputed) for c in charges],
        )
        # Bump the version so this cancellation's refund keys differ from earlier ones, then write the audit.
        res.refund_version += 1
        res.cancelled_at = cancelled_at
        s.add(
            RefundAudit(
                reservation_id=res.id,
                policy=res.cancellation_policy,
                decided_by=decided_by,
                inputs={
                    "cancelled_at": cancelled_at.isoformat(),
                    "check_in": res.check_in.isoformat(),
                    "booked_at": res.created_at.isoformat(),
                    "total_amount": res.total_amount,
                    "charges": [
                        {"id": str(c.id), "amount": c.amount, "refunded": c.amount_refunded, "disputed": c.disputed}
                        for c in charges
                    ],
                },
                result=asdict(decision),
            )
        )
        REFUNDS.labels(decision.route).inc()

        # Manual route queues a review; auto route cancels and creates one pending Refund per allocation.
        if decision.route == "manual_review":
            res.status = "cancellation_review"
            s.add(
                ManualReview(reservation_id=res.id, reason=decision.rule, details={"explanation": decision.explanation})
            )
            record_event(s, res.id, "cancellation.manual_review", None, reason=decision.rule)
        else:
            res.status = "cancelled"
            record_event(s, res.id, "reservation.cancelled", decision.total_refund, explanation=decision.explanation)
            for a in decision.allocations:
                s.add(
                    Refund(
                        reservation_id=res.id,
                        charge_id=uuid.UUID(a["charge_id"]),
                        amount=a["amount"],
                        idempotency_key=f"refund:{res.id}:v{res.refund_version}:{a['charge_id']}",
                    )
                )

    async with sm() as s:
        # After the commit, send every pending refund to the processor.
        pending = (
            (
                await s.execute(
                    select(Refund.id).where(Refund.reservation_id == reservation_id, Refund.status == "pending")
                )
            )
            .scalars()
            .all()
        )
    for rid in pending:
        await execute_refund(sm, processor, rid)
    return await _summary(sm, reservation_id, decision=decision)


# Send one pending refund to the processor and record the outcome (same phased pattern as charges).
async def execute_refund(sm: async_sessionmaker, processor: ProcessorClient, refund_id: uuid.UUID) -> str:
    async with sm() as s:
        refund = await s.get(Refund, refund_id)
        if refund.status != "pending":
            return refund.status
        charge = await s.get(Charge, refund.charge_id)
        key, amount, txn = refund.idempotency_key, refund.amount, charge.processor_txn_id

    # Call the processor with no DB transaction open; an unknown outcome stays pending.
    result = await processor.refund(
        key=key, processor_charge_id=txn, amount=amount, metadata={"refund_id": str(refund_id)}
    )
    if result.outcome is Outcome.UNKNOWN:
        log.warning("refund.outcome_unknown", extra={"refund_id": str(refund_id), "err": result.error})
        return "pending"

    async with sm() as s, s.begin():
        # Lock order: charge, then refund. The webhook handler uses the same order (no deadlocks).
        charge_id = (await s.get(Refund, refund_id)).charge_id
        charge = (await s.execute(select(Charge).where(Charge.id == charge_id).with_for_update())).scalar_one()
        refund = (await s.execute(select(Refund).where(Refund.id == refund_id).with_for_update())).scalar_one()
        if refund.status != "pending":
            return refund.status  # the webhook already applied it
        # Processor refused: mark the refund failed and hand it to a person.
        if result.outcome is not Outcome.SUCCEEDED:
            refund.status = "failed"
            s.add(
                ManualReview(
                    reservation_id=refund.reservation_id,
                    reason="refund_rejected",
                    details={"refund_id": str(refund_id), "error": result.error},
                )
            )
            log.error("refund.rejected", extra={"refund_id": str(refund_id), "err": result.error})
            return "failed"
        # Success: mark the refund done and move the charge to partially_refunded or refunded.
        refund.status, refund.processor_refund_id = "succeeded", result.processor_id
        charge.amount_refunded += refund.amount
        target = "refunded" if charge.amount_refunded >= charge.amount else "partially_refunded"
        charge.status = transition(CHARGE_TRANSITIONS, charge.status, target)
        record_event(
            s,
            refund.reservation_id,
            "refund.succeeded",
            refund.amount,
            charge_id=charge.id,
            processor_refund_id=result.processor_id,
            source="api",
        )
        return "succeeded"


# Sweeper: retry refunds still pending after `older_than_s` seconds.
async def recover_stuck_refunds(sm: async_sessionmaker, processor: ProcessorClient, older_than_s: int = 300) -> int:
    cutoff = utcnow() - timedelta(seconds=older_than_s)
    async with sm() as s:
        ids = (
            (await s.execute(select(Refund.id).where(Refund.status == "pending", Refund.created_at < cutoff)))
            .scalars()
            .all()
        )
    for rid in ids:
        await execute_refund(sm, processor, rid)
    return len(ids)


# Build the cancel API response from the reservation, its refunds, and the latest decision.
async def _summary(
    sm: async_sessionmaker, reservation_id: uuid.UUID, decision: RefundDecision | None = None, already: bool = False
) -> dict:
    async with sm() as s:
        res = await s.get(Reservation, reservation_id)
        refunds = (await s.execute(select(Refund).where(Refund.reservation_id == reservation_id))).scalars().all()
        if decision is None:
            audit = (
                await s.execute(
                    select(RefundAudit)
                    .where(RefundAudit.reservation_id == reservation_id)
                    .order_by(RefundAudit.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            d = audit.result if audit else {}
        else:
            d = asdict(decision)
    return {
        "reservation_id": str(reservation_id),
        "status": res.status,
        "already_cancelled": already,
        "decision": d,
        "refunds": [
            {"id": str(r.id), "charge_id": str(r.charge_id), "amount": r.amount, "status": r.status} for r in refunds
        ],
    }
