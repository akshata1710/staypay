"""Payment history with keyset (cursor) pagination.

Why not OFFSET? `OFFSET 5000` makes Postgres read and throw away 5000 rows, so deep pages get
slower and slower. Keyset pagination asks for "rows after (occurred_at, id)", which the composite
index (reservation_id, occurred_at, id) answers directly, at the same speed for every page.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.db.models import PaymentEvent


class BadCursor(ValueError):
    pass


def encode_cursor(occurred_at: datetime, event_id: int) -> str:
    raw = json.dumps({"t": occurred_at.isoformat(), "i": event_id}).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        d = json.loads(raw)
        return datetime.fromisoformat(d["t"]), int(d["i"])
    except Exception as e:  # noqa: BLE001
        raise BadCursor("invalid cursor") from e


async def get_page(s: AsyncSession, reservation_id: uuid.UUID, cursor: str | None, limit: int) -> dict:
    q = select(PaymentEvent).where(PaymentEvent.reservation_id == reservation_id)
    if cursor:
        t, i = decode_cursor(cursor)
        q = q.where(tuple_(PaymentEvent.occurred_at, PaymentEvent.id) > tuple_(t, i))
    rows = (await s.execute(q.order_by(PaymentEvent.occurred_at, PaymentEvent.id).limit(limit + 1))).scalars().all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "data": [
            {
                "id": r.id,
                "type": r.type,
                "amount": r.amount,
                "occurred_at": r.occurred_at.isoformat(),
                "payload": r.payload,
            }
            for r in rows
        ],
        "next_cursor": encode_cursor(rows[-1].occurred_at, rows[-1].id) if has_more else None,
        "has_more": has_more,
        "_last_occurred_at": rows[-1].occurred_at if rows else None,
    }


def page_is_immutable(page: dict, immutable_after_s: int, now: datetime | None = None) -> bool:
    """A page can be cached only if it's full (more rows follow it) and its newest row is old enough
    that no concurrent transaction could still insert a row that sorts inside it."""
    last = page.get("_last_occurred_at")
    return bool(page["has_more"] and last and (now or utcnow()) - last > timedelta(seconds=immutable_after_s))
