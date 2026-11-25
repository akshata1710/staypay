"""One place to get 'now', so tests can reason about time explicitly."""

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)
