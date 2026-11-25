"""Daily reconciliation: does the processor's settlement file agree with our ledger?

Matching key is the processor transaction id (charge id or refund id). For each id we compare
amount, currency and status, and classify every disagreement with a reason code finance can act on.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import Charge, ReconciliationMismatch, ReconciliationRun, Refund
from app.observability.metrics import RECON_MISMATCHES
from app.processor.client import ProcessorClient

log = logging.getLogger(__name__)
ROUNDING_TOLERANCE = 1  # minor units


# One transaction line, from either our ledger or the processor's settlement file.
@dataclass(frozen=True)
class Line:
    txn_id: str
    kind: str
    amount: int
    currency: str
    status: str


# Parse the processor's settlement CSV into lines keyed by processor transaction id.
def parse_settlement(text: str) -> dict[str, Line]:
    return {
        r["processor_txn_id"]: Line(
            r["processor_txn_id"], r["type"], int(r["amount"]), r["currency"].upper(), r["status"]
        )
        for r in csv.DictReader(io.StringIO(text))
    }


# Compare ledger and settlement per transaction id; return the match count and classified mismatches.
def compare(ledger: dict[str, Line], settlement: dict[str, Line]) -> tuple[int, list[dict]]:
    matched, mismatches = 0, []
    for txn in sorted(ledger.keys() | settlement.keys()):
        ours, theirs = ledger.get(txn), settlement.get(txn)
        # Checked in order: missing on either side, currency, amount (rounding vs real), then status.
        if theirs is None:
            mismatches.append({"txn": txn, "reason": "MISSING_IN_SETTLEMENT", "details": {"ledger": ours.__dict__}})
        elif ours is None:
            mismatches.append({"txn": txn, "reason": "MISSING_IN_LEDGER", "details": {"settlement": theirs.__dict__}})
        elif ours.currency != theirs.currency:
            mismatches.append(
                {
                    "txn": txn,
                    "reason": "AMOUNT_MISMATCH",
                    "details": {"ledger_currency": ours.currency, "settlement_currency": theirs.currency},
                }
            )
        elif ours.amount != theirs.amount:
            diff = theirs.amount - ours.amount
            reason = "CURRENCY_ROUNDING" if abs(diff) <= ROUNDING_TOLERANCE else "AMOUNT_MISMATCH"
            mismatches.append(
                {
                    "txn": txn,
                    "reason": reason,
                    "details": {"ledger": ours.amount, "settlement": theirs.amount, "diff": diff},
                }
            )
        elif theirs.status != "settled":
            mismatches.append(
                {
                    "txn": txn,
                    "reason": "STATUS_MISMATCH",
                    "details": {"ledger": ours.status, "settlement": theirs.status},
                }
            )
        else:
            matched += 1
    return matched, mismatches


# Build our side for one UTC day: successful charges and succeeded refunds that have processor ids.
async def load_ledger(sm: async_sessionmaker, day: date) -> dict[str, Line]:
    start = datetime.combine(day, time(0), tzinfo=UTC)
    end = start + timedelta(days=1)
    async with sm() as s:
        charges = (
            (
                await s.execute(
                    select(Charge).where(
                        Charge.processor_txn_id.is_not(None),
                        Charge.status.in_(("succeeded", "partially_refunded", "refunded")),
                        and_(Charge.created_at >= start, Charge.created_at < end),
                    )
                )
            )
            .scalars()
            .all()
        )
        refunds = (
            await s.execute(
                select(Refund, Charge.currency)
                .join(Charge, Charge.id == Refund.charge_id)
                .where(
                    Refund.status == "succeeded",
                    Refund.processor_refund_id.is_not(None),
                    and_(Refund.created_at >= start, Refund.created_at < end),
                )
            )
        ).all()
    out = {
        c.processor_txn_id: Line(c.processor_txn_id, "charge", c.amount, c.currency.upper(), "settled") for c in charges
    }
    out.update(
        {
            r.processor_refund_id: Line(r.processor_refund_id, "refund", r.amount, cur.upper(), "settled")
            for r, cur in refunds
        }
    )
    return out


# Reconcile one day: fetch the settlement, compare, and save the run and its mismatches.
async def reconcile(sm: async_sessionmaker, processor: ProcessorClient, day: date) -> dict:
    settlement = parse_settlement(await processor.settlement_csv(day.isoformat()))
    ledger = await load_ledger(sm, day)
    matched, mismatches = compare(ledger, settlement)
    async with sm() as s, s.begin():
        run = ReconciliationRun(settlement_date=day, matched=matched, mismatched=len(mismatches))
        s.add(run)
        await s.flush()
        for m in mismatches:
            s.add(
                ReconciliationMismatch(
                    run_id=run.id, processor_txn_id=m["txn"], reason=m["reason"], details=m["details"]
                )
            )
            RECON_MISMATCHES.labels(m["reason"]).inc()
        run_id = run.id
    log.info("reconciliation.done", extra={"day": day.isoformat(), "matched": matched, "mismatched": len(mismatches)})
    return {
        "run_id": str(run_id),
        "settlement_date": day.isoformat(),
        "matched": matched,
        "mismatched": len(mismatches),
        "mismatches": mismatches,
    }


# Return the latest reconciliation run for a day, with mismatches counted by reason.
async def latest_report(sm: async_sessionmaker, day: date) -> dict | None:
    async with sm() as s:
        run = (
            await s.execute(
                select(ReconciliationRun)
                .where(ReconciliationRun.settlement_date == day)
                .order_by(ReconciliationRun.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if run is None:
            return None
        rows = (
            (
                await s.execute(
                    select(ReconciliationMismatch)
                    .where(ReconciliationMismatch.run_id == run.id)
                    .order_by(ReconciliationMismatch.reason)
                )
            )
            .scalars()
            .all()
        )
    summary: dict[str, int] = {}
    for r in rows:
        summary[r.reason] = summary.get(r.reason, 0) + 1
    return {
        "run_id": str(run.id),
        "settlement_date": day.isoformat(),
        "matched": run.matched,
        "mismatched": run.mismatched,
        "by_reason": summary,
        "mismatches": [
            {"processor_txn_id": r.processor_txn_id, "reason": r.reason, "details": r.details} for r in rows
        ],
    }
