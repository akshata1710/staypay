"""Read models returned by the API."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Charge, Installment, PaymentPlan, Reservation


async def plan_view(s: AsyncSession, reservation_id: uuid.UUID) -> dict | None:
    res = await s.get(Reservation, reservation_id)
    if res is None:
        return None
    plan = (await s.execute(select(PaymentPlan).where(PaymentPlan.reservation_id == reservation_id))).scalar_one()
    insts = (
        (await s.execute(select(Installment).where(Installment.plan_id == plan.id).order_by(Installment.number)))
        .scalars()
        .all()
    )
    charges = (
        (await s.execute(select(Charge).where(Charge.reservation_id == reservation_id).order_by(Charge.created_at)))
        .scalars()
        .all()
    )
    by_inst: dict[uuid.UUID, list[Charge]] = {}
    for c in charges:
        by_inst.setdefault(c.installment_id, []).append(c)
    return {
        "reservation_id": str(res.id),
        "reservation_status": res.status,
        "total_amount": res.total_amount,
        "currency": res.currency,
        "plan": {
            "id": str(plan.id),
            "status": plan.status,
            "payment_deadline": plan.payment_deadline.isoformat(),
            "installments": [
                {
                    "number": i.number,
                    "amount": i.amount,
                    "due_at": i.due_at.isoformat(),
                    "status": i.status,
                    "attempts": i.attempts,
                    "next_attempt_at": i.next_attempt_at.isoformat() if i.next_attempt_at else None,
                    "last_decline_code": i.last_decline_code,
                    "charges": [
                        {
                            "id": str(c.id),
                            "status": c.status,
                            "amount": c.amount,
                            "processor_txn_id": c.processor_txn_id,
                            "decline_code": c.decline_code,
                            "amount_refunded": c.amount_refunded,
                        }
                        for c in by_inst.get(i.id, [])
                    ],
                }
                for i in insts
            ],
        },
    }
