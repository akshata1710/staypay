"""Webhook ingestion: store-then-process.

The HTTP handler does as little as possible (verify signature, INSERT into the inbox, 200), so it
is fast and never times out under load. The inbox worker does the real work later:
- duplicates are dropped by the inbox primary key (event_id)
- each event is applied only if it's a valid state transition (stale events are ignored,
  events that arrive "too early" are deferred and retried)
- a nightly backfill pulls the processor's event list and inserts anything we never received
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clock import utcnow
from app.config import get_settings
from app.db.models import Charge, Refund, WebhookInbox
from app.domain.charging import apply_charge_outcome
from app.domain.events import record_event
from app.domain.state_machine import CHARGE_TRANSITIONS, EventVerdict, judge_charge_event, transition
from app.observability.metrics import (
    BACKFILL_INSERTED,
    INBOX_OLDEST_UNPROCESSED,
    WEBHOOKS_PROCESSED,
    WEBHOOKS_RECEIVED,
)
from app.processor.client import ProcessorClient

log = logging.getLogger(__name__)


async def ingest(sm: async_sessionmaker, event: dict, source: str = "webhook") -> bool:
    """Store an event. Returns False if we already had it."""
    async with sm() as s, s.begin():
        inserted = (
            await s.execute(
                insert(WebhookInbox)
                .values(event_id=event["id"], type=event["type"], payload=event, source=source)
                .on_conflict_do_nothing(index_elements=["event_id"])
                .returning(WebhookInbox.event_id)
            )
        ).scalar_one_or_none()
    WEBHOOKS_RECEIVED.labels(source, str(inserted is None).lower()).inc()
    return inserted is not None


# Find and lock our charge for a processor object, by processor id or our idempotency key.
async def _find_charge(s: AsyncSession, obj: dict) -> Charge | None:
    conds = [Charge.processor_txn_id == obj["id"]]
    if obj.get("idempotency_key"):
        # Matching on our own key covers the case where the API call timed out and we never
        # learned the processor's charge id.
        conds.append(Charge.idempotency_key == obj["idempotency_key"])
    return (await s.execute(select(Charge).where(or_(*conds)).with_for_update())).scalars().first()


async def handle_event(s: AsyncSession, event: dict, now: datetime) -> str:
    """Returns: applied | ignored_stale | ignored_unknown | too_early."""
    type_, obj = event["type"], event["data"]["object"]

    # Charge result: apply it through the same state-machine path the API uses.
    if type_ in ("charge.succeeded", "charge.failed"):
        charge = await _find_charge(s, obj)
        if charge is None:
            return "ignored_unknown"
        target = "succeeded" if type_ == "charge.succeeded" else "declined"
        verdict = await apply_charge_outcome(
            s, charge, target, obj["id"], obj.get("decline_code"), now=now, source="webhook"
        )
        return _verdict_result(verdict)

    # Refund (possibly made outside our API): advance the charge and mark our refund row succeeded.
    if type_ == "charge.refunded":
        charge = await _find_charge(s, obj)
        if charge is None:
            return "ignored_unknown"
        refund_obj = obj.get("refund") or {}
        target = "refunded" if obj.get("refunded") else "partially_refunded"
        verdict = judge_charge_event(charge.status, target)
        if verdict is EventVerdict.APPLY and obj["amount_refunded"] <= charge.amount_refunded:
            verdict = EventVerdict.STALE  # we already counted this refund (API path got there first)
        if verdict is not EventVerdict.APPLY:
            return _verdict_result(verdict)
        charge.status = transition(CHARGE_TRANSITIONS, charge.status, target)
        charge.amount_refunded = obj["amount_refunded"]
        refund = (
            (
                await s.execute(
                    select(Refund)
                    .where(
                        or_(
                            Refund.processor_refund_id == refund_obj.get("id"),
                            Refund.idempotency_key == refund_obj.get("idempotency_key"),
                        )
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .first()
        )
        if refund is not None and refund.status == "pending":
            refund.status = "succeeded"
            refund.processor_refund_id = refund_obj.get("id")
        record_event(
            s,
            charge.reservation_id,
            "refund.succeeded",
            refund_obj.get("amount"),
            charge_id=charge.id,
            processor_refund_id=refund_obj.get("id"),
            source="webhook",
        )
        return "applied"

    # Dispute: flag the charge once so a later cancellation goes to manual review.
    if type_ == "charge.dispute.created":
        charge = await _find_charge(s, obj)
        if charge is None:
            return "ignored_unknown"
        if charge.disputed:
            return "ignored_stale"
        charge.disputed = True
        record_event(s, charge.reservation_id, "charge.disputed", charge.amount, charge_id=charge.id)
        return "applied"

    return "ignored_unknown"


# Map a state-machine verdict to the inbox result string.
def _verdict_result(v: EventVerdict) -> str:
    return {EventVerdict.APPLY: "applied", EventVerdict.STALE: "ignored_stale", EventVerdict.TOO_EARLY: "too_early"}[v]


# Inbox worker: lock a batch of unprocessed events, apply each, and defer or dead-letter failures.
async def process_inbox(sm: async_sessionmaker, now: datetime | None = None, limit: int = 50) -> dict[str, int]:
    now = now or utcnow()
    max_defers = get_settings().webhook_max_defers
    counts: dict[str, int] = {}
    async with sm() as s, s.begin():
        rows = (
            (
                await s.execute(
                    select(WebhookInbox)
                    .where(
                        WebhookInbox.processed_at.is_(None),
                        or_(WebhookInbox.next_attempt_at.is_(None), WebhookInbox.next_attempt_at <= now),
                    )
                    .order_by(WebhookInbox.received_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            try:
                async with s.begin_nested():  # one bad event must not roll back the batch
                    result = await handle_event(s, row.payload, now)
            except Exception as e:  # noqa: BLE001
                log.exception("webhook.handler_error", extra={"event_id": row.event_id})
                result, row.last_error = "error", repr(e)[:2000]
            row.attempts += 1
            # Too early or crashed: back off exponentially (max 300s) until max_defers, then dead-letter.
            if result in ("too_early", "error"):
                if row.attempts >= max_defers:
                    row.processed_at, row.result = now, "dead"
                    log.error("webhook.dead_letter", extra={"event_id": row.event_id, "type": row.type})
                else:
                    row.next_attempt_at = now + timedelta(seconds=min(2**row.attempts, 300))
            else:
                row.processed_at, row.result = now, result
            final = row.result or result
            counts[final] = counts.get(final, 0) + 1
            WEBHOOKS_PROCESSED.labels(final).inc()
    await update_inbox_lag(sm)
    return counts


# Publish the age of the oldest unprocessed event as a metric (inbox lag).
async def update_inbox_lag(sm: async_sessionmaker) -> float:
    async with sm() as s:
        oldest = (
            await s.execute(select(func.min(WebhookInbox.received_at)).where(WebhookInbox.processed_at.is_(None)))
        ).scalar()
    lag = (utcnow() - oldest).total_seconds() if oldest else 0.0
    INBOX_OLDEST_UNPROCESSED.set(lag)
    return lag


async def backfill(sm: async_sessionmaker, processor: ProcessorClient, since: datetime) -> int:
    """Pull the processor's event list and insert any event the inbox is missing."""
    inserted, cursor = 0, since.isoformat()
    while True:
        page = await processor.list_events(created_after=cursor, limit=100)
        for evt in page["data"]:
            if await ingest(sm, evt, source="backfill"):
                inserted += 1
        if not page["has_more"] or not page["data"]:
            break
        cursor = page["data"][-1]["created"]
    BACKFILL_INSERTED.inc(inserted)
    log.info("webhook.backfill_done", extra={"inserted": inserted, "since": since.isoformat()})
    return inserted
