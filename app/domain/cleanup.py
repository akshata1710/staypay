"""Retention cleanup for idempotency keys (SP-104).

Every checkout stores a row with its full response, so without cleanup the table grows forever.
Completed keys only matter while a client might still retry, so they're deleted after a retention
window (default 24h). In-progress keys are never deleted: a takeover may still need to resume one.

Deletes run in small batches, each in its own short transaction. One giant DELETE would hold
locks for a long time and leave a burst of dead rows for autovacuum to clean up.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.clock import utcnow
from app.config import get_settings
from app.db.models import IdempotencyKey
from app.observability.metrics import IDEMPOTENCY_KEYS_DELETED

log = logging.getLogger(__name__)


async def cleanup_idempotency_keys(
    sm: async_sessionmaker, now: datetime | None = None, batch_size: int | None = None
) -> int:
    """Delete completed keys older than the retention window. Returns how many were deleted."""
    settings = get_settings()
    now = now or utcnow()
    batch_size = batch_size or settings.idempotency_cleanup_batch_size
    cutoff = now - timedelta(hours=settings.idempotency_key_retention_h)
    total = 0
    while True:
        async with sm() as s, s.begin():
            # Pick one batch, oldest first; SKIP LOCKED so two cleanup runs never fight over rows.
            batch = (
                select(IdempotencyKey.key)
                .where(IdempotencyKey.status == "completed", IdempotencyKey.created_at < cutoff)
                .order_by(IdempotencyKey.created_at)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
            result = await s.execute(delete(IdempotencyKey).where(IdempotencyKey.key.in_(batch.scalar_subquery())))
        deleted = result.rowcount or 0
        total += deleted
        IDEMPOTENCY_KEYS_DELETED.inc(deleted)
        if deleted < batch_size:  # a short batch means nothing old is left
            break
    if total:
        log.info("idempotency.cleanup_done", extra={"deleted": total, "cutoff": cutoff.isoformat()})
    return total
