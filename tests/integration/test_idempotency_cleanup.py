"""SP-104: old completed idempotency keys are deleted in batches; recent and in-progress keys stay."""

from datetime import timedelta

from prometheus_client import REGISTRY
from sqlalchemy import select

from app.clock import utcnow
from app.db.models import IdempotencyKey
from app.db.session import get_sessionmaker
from app.domain.cleanup import cleanup_idempotency_keys


def _deleted_total() -> float:
    return REGISTRY.get_sample_value("idempotency_keys_deleted_total") or 0.0


async def _seed(sm, now):
    rows = (
        # 5 old completed keys -> deleted (more than one batch of 2)
        [(f"old-done-{i}", "completed", now - timedelta(hours=25 + i)) for i in range(5)]
        # recent completed key -> kept (a client may still retry it)
        + [("recent-done", "completed", now - timedelta(hours=23))]
        # old in-progress key -> kept (a takeover may still resume it)
        + [("old-in-progress", "in_progress", now - timedelta(hours=48))]
    )
    async with sm() as s, s.begin():
        for key, status, created in rows:
            s.add(IdempotencyKey(key=key, request_hash="h" * 64, status=status, created_at=created, locked_at=created))


async def _remaining(sm) -> set[str]:
    async with sm() as s:
        return set((await s.execute(select(IdempotencyKey.key))).scalars())


async def test_deletes_only_old_completed_keys_in_batches(clean_db):
    sm, now = get_sessionmaker(), utcnow()
    await _seed(sm, now)
    before = _deleted_total()

    deleted = await cleanup_idempotency_keys(sm, now=now, batch_size=2)

    assert deleted == 5
    assert await _remaining(sm) == {"recent-done", "old-in-progress"}
    assert _deleted_total() - before == 5


async def test_second_run_is_a_no_op(clean_db):
    sm, now = get_sessionmaker(), utcnow()
    await _seed(sm, now)
    await cleanup_idempotency_keys(sm, now=now)
    assert await cleanup_idempotency_keys(sm, now=now) == 0
    assert await _remaining(sm) == {"recent-done", "old-in-progress"}


async def test_purged_key_is_treated_as_new(h):
    """Documents the trade-off: after purging, the same key creates a NEW reservation.
    That's why retention (24h) must be longer than any client's retry window."""
    from datetime import timedelta

    from app.clock import utcnow
    from app.db.models import IdempotencyKey
    from app.domain.maintenance import purge_idempotency_keys

    r1 = await h.checkout(key="reused-after-purge", guest="g1")
    async with h.sm() as s, s.begin():
        row = await s.get(IdempotencyKey, "reused-after-purge")
        row.created_at = utcnow() - timedelta(days=2)
    assert await purge_idempotency_keys(h.sm) == 1
    r2 = await h.checkout(key="reused-after-purge", guest="g1")
    assert r1.json()["reservation_id"] != r2.json()["reservation_id"]
