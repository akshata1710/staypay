"""SQLAlchemy models. The ledger (charges, refunds, payment_events) is the source of truth.

All money is stored as integer minor units (cents). Never use floats for money.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# JSON column type that becomes JSONB on Postgres and plain JSON elsewhere.
JsonType = JSON().with_variant(JSONB(), "postgresql")


# Declarative base class that every ORM model inherits from.
class Base(DeclarativeBase):
    pass


# Default primary-key factory: a random UUID4.
def _uuid() -> uuid.UUID:
    return uuid.uuid4()


# A guest's booking; `status` tracks its payment and cancellation lifecycle.
class Reservation(Base):
    __tablename__ = "reservations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    guest_id: Mapped[str] = mapped_column(String(64), index=True)
    listing_id: Mapped[str] = mapped_column(String(64))
    check_in: Mapped[date] = mapped_column(Date)
    total_amount: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    # pending_payment | confirmed | payment_failed | at_risk | cancelled | cancellation_review
    status: Mapped[str] = mapped_column(String(32), default="pending_payment")
    cancellation_policy: Mapped[str] = mapped_column(String(32))
    payment_method: Mapped[str] = mapped_column(String(64))
    refund_version: Mapped[int] = mapped_column(Integer, default=0)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# One plan per reservation: number of installments and the deadline to collect them.
class PaymentPlan(Base):
    __tablename__ = "payment_plans"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    reservation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reservations.id"), unique=True)
    installments: Mapped[int] = mapped_column(Integer)
    payment_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # active | completed | failed | cancelled
    status: Mapped[str] = mapped_column(String(32), default="active")


# One scheduled slice of the total; the charge worker selects rows by (status, next_attempt_at).
class Installment(Base):
    __tablename__ = "installments"
    __table_args__ = (
        UniqueConstraint("plan_id", "number", name="uq_installment_plan_number"),
        Index("ix_installments_due", "status", "next_attempt_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payment_plans.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    amount: Mapped[int] = mapped_column(BigInteger)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # scheduled | processing | succeeded | failed | retrying | abandoned | cancelled
    status: Mapped[str] = mapped_column(String(32), default="scheduled")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_decline_code: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# Ledger: one processor charge attempt for an installment; the unique idempotency_key blocks double charges.
class Charge(Base):
    __tablename__ = "charges"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    installment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("installments.id"), index=True)
    reservation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reservations.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    processor_txn_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    amount: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    # pending | succeeded | declined | partially_refunded | refunded
    status: Mapped[str] = mapped_column(String(32), default="pending")
    decline_code: Mapped[str | None] = mapped_column(String(64))
    amount_refunded: Mapped[int] = mapped_column(BigInteger, default=0)
    disputed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# Ledger: money returned against one specific charge, with its own unique idempotency key.
class Refund(Base):
    __tablename__ = "refunds"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    reservation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reservations.id"), index=True)
    charge_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("charges.id"), index=True)
    amount: Mapped[int] = mapped_column(BigInteger)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    processor_refund_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    # pending | succeeded | failed
    status: Mapped[str] = mapped_column(String(32), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PaymentEvent(Base):
    """Append-only history of everything that happened to a reservation's money."""

    __tablename__ = "payment_events"
    __table_args__ = (Index("ix_payment_events_res_time_id", "reservation_id", "occurred_at", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    reservation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    type: Mapped[str] = mapped_column(String(64))
    amount: Mapped[int | None] = mapped_column(BigInteger)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    payload: Mapped[dict] = mapped_column(JsonType, default=dict)


# Checkout Idempotency-Key: who owns the request, how far it got, and the response to replay.
class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (Index("ix_idempotency_keys_created_at", "created_at"),)  # cleanup scans by age

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    # in_progress | completed
    status: Mapped[str] = mapped_column(String(32), default="in_progress")
    # Recovery point ("atomic phases"): which phase finished, so a takeover can resume.
    recovery_point: Mapped[str] = mapped_column(String(32), default="started")
    reservation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    response_code: Mapped[int | None] = mapped_column(Integer)
    response: Mapped[dict | None] = mapped_column(JsonType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    locked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# Store-then-process inbox for processor events; the event_id primary key drops duplicates.
class WebhookInbox(Base):
    __tablename__ = "webhook_inbox"
    __table_args__ = (Index("ix_webhook_inbox_pending", "processed_at", "next_attempt_at"),)

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JsonType)
    source: Mapped[str] = mapped_column(String(16), default="webhook")  # webhook | backfill
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # applied | ignored_stale | ignored_unknown | dead
    result: Mapped[str | None] = mapped_column(String(32))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


# Audit trail of every refund decision: policy, inputs, result, and who decided.
class RefundAudit(Base):
    __tablename__ = "refund_audit"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    reservation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    policy: Mapped[str] = mapped_column(String(32))
    inputs: Mapped[dict] = mapped_column(JsonType)
    result: Mapped[dict] = mapped_column(JsonType)
    decided_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# Queue of cases a person must decide (disputes, cancels after check-in, rejected refunds).
class ManualReview(Base):
    __tablename__ = "manual_review_queue"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    reservation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    reason: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JsonType, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="open")  # open | resolved
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Notification(Base):
    """Outbox of guest notifications (a real system would have a sender drain this)."""

    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    guest_id: Mapped[str] = mapped_column(String(64), index=True)
    reservation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# One daily settlement-vs-ledger comparison, with matched and mismatched counts.
class ReconciliationRun(Base):
    __tablename__ = "reconciliation_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    settlement_date: Mapped[date] = mapped_column(Date, index=True)
    matched: Mapped[int] = mapped_column(Integer, default=0)
    mismatched: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# One disagreement found by a reconciliation run, with a reason code finance can act on.
class ReconciliationMismatch(Base):
    __tablename__ = "reconciliation_mismatches"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reconciliation_runs.id"), index=True)
    processor_txn_id: Mapped[str] = mapped_column(String(64))
    # AMOUNT_MISMATCH | CURRENCY_ROUNDING | MISSING_IN_LEDGER | MISSING_IN_SETTLEMENT | STATUS_MISMATCH
    reason: Mapped[str] = mapped_column(String(32))
    details: Mapped[dict] = mapped_column(JsonType, default=dict)
