"""Build an installment schedule for a reservation."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta


@dataclass(frozen=True)
class PlannedInstallment:
    number: int
    amount: int
    due_at: datetime


def payment_deadline(check_in: date, days_before: int) -> datetime:
    return datetime.combine(check_in, time(0, 0), tzinfo=UTC) - timedelta(days=days_before)


def build_schedule(total: int, n: int, now: datetime, deadline: datetime) -> list[PlannedInstallment]:
    """Split `total` into n installments. #1 is due now; the rest are spread evenly before the
    deadline. Rounding remainder goes on installment #1, so the sum is always exactly `total`."""
    if n < 1:
        raise ValueError("need at least one installment")
    if n > 1 and deadline <= now:
        raise ValueError("check-in too soon for an installment plan")
    base, remainder = divmod(total, n)
    step = (deadline - now) / n if n > 1 else timedelta(0)
    return [
        PlannedInstallment(number=i + 1, amount=base + (remainder if i == 0 else 0), due_at=now + step * i)
        for i in range(n)
    ]
