"""Helpers that write to the append-only ledger history and the notification outbox."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Notification, PaymentEvent


def record_event(
    session: AsyncSession, reservation_id: uuid.UUID, type_: str, amount: int | None = None, **payload
) -> None:
    session.add(
        PaymentEvent(
            reservation_id=reservation_id,
            type=type_,
            amount=amount,
            payload={k: str(v) if isinstance(v, uuid.UUID) else v for k, v in payload.items()},
        )
    )


def notify(session: AsyncSession, guest_id: str, reservation_id: uuid.UUID, kind: str, **payload) -> None:
    session.add(Notification(guest_id=guest_id, reservation_id=reservation_id, kind=kind, payload=payload))
