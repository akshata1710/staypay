"""Create a reservation with N payment events, to measure history latency before/after.

    python scripts/seed_history.py --events 1000
Prints the reservation id. Then time:
    curl -w '%{time_total}\\n' -o /dev/null "localhost:8000/reservations/<id>/payments?limit=50"
"""

import argparse
import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta

from app.db.models import Installment, PaymentEvent, PaymentPlan, Reservation
from app.db.session import dispose_engine, get_sessionmaker


async def main(n: int) -> None:
    sm = get_sessionmaker()
    async with sm() as s, s.begin():
        res = Reservation(
            guest_id="seed",
            listing_id="seed",
            check_in=date.today() + timedelta(days=90),
            total_amount=100_000,
            currency="USD",
            status="confirmed",
            cancellation_policy="moderate",
            payment_method="pm_card_ok",
        )
        s.add(res)
        await s.flush()
        plan = PaymentPlan(
            reservation_id=res.id,
            installments=1,
            payment_deadline=datetime.now(UTC) + timedelta(days=80),
            status="completed",
        )
        s.add(plan)
        await s.flush()
        s.add(Installment(plan_id=plan.id, number=1, amount=100_000, due_at=datetime.now(UTC), status="succeeded"))
        start = datetime.now(UTC) - timedelta(days=30)
        for i in range(n):
            s.add(
                PaymentEvent(
                    reservation_id=res.id,
                    type=["charge.declined", "charge.succeeded"][i % 2],
                    amount=1000,
                    occurred_at=start + timedelta(seconds=i * 10),
                    payload={"seq": i, "trace": str(uuid.uuid4())},
                )
            )
    print(res.id)
    await dispose_engine()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--events", type=int, default=1000)
    asyncio.run(main(p.parse_args().events))
