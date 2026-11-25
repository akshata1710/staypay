"""Decline-aware retry policy, driven by policies/decline_codes.yaml."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

import yaml

log = logging.getLogger(__name__)
POLICY_DIR = Path(__file__).resolve().parents[2] / "policies"


@dataclass(frozen=True)
class DeclinePolicy:
    retryable: frozenset[str]
    non_retryable: frozenset[str]
    delays_hours: tuple[int, ...]

    def is_retryable(self, code: str | None) -> bool:
        if code in self.retryable:
            return True
        if code not in self.non_retryable:
            log.warning("decline_code.unknown", extra={"decline_code": code})
        return False


@lru_cache
def load_decline_policy(path: Path | None = None) -> DeclinePolicy:
    data = yaml.safe_load((path or POLICY_DIR / "decline_codes.yaml").read_text())
    return DeclinePolicy(
        retryable=frozenset(data["retryable"]),
        non_retryable=frozenset(data["non_retryable"]),
        delays_hours=tuple(data["retry_schedule"]["delays_hours"]),
    )


@dataclass(frozen=True)
class RetryDecision:
    retry: bool
    next_attempt_at: datetime | None
    reason: str  # retry_scheduled | non_retryable | attempts_exhausted | past_deadline


def decide_retry(
    *,
    decline_code: str | None,
    attempts_so_far: int,
    now: datetime,
    deadline: datetime,
    policy: DeclinePolicy | None = None,
) -> RetryDecision:
    """attempts_so_far counts the attempt that just failed (1 after the first failure)."""
    policy = policy or load_decline_policy()
    if not policy.is_retryable(decline_code):
        return RetryDecision(False, None, "non_retryable")
    if attempts_so_far > len(policy.delays_hours):
        return RetryDecision(False, None, "attempts_exhausted")
    nxt = now + timedelta(hours=policy.delays_hours[attempts_so_far - 1])
    if nxt >= deadline:
        return RetryDecision(False, None, "past_deadline")
    return RetryDecision(True, nxt, "retry_scheduled")
