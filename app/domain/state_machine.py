"""Allowed state transitions. Any code that changes a status goes through these checks.

Why: webhooks arrive late, twice, or out of order. If every handler just wrote
`charge.status = event.status`, a late `charge.succeeded` could overwrite `refunded`.
With explicit transitions, a stale event is detected and ignored instead.
"""

from enum import StrEnum


# Raised when code attempts a status change the transition table does not allow.
class InvalidTransition(Exception):
    pass


# Allowed next states for an installment (an empty set means terminal).
INSTALLMENT_TRANSITIONS: dict[str, set[str]] = {
    "scheduled": {"processing", "cancelled"},
    "processing": {"succeeded", "failed", "retrying", "abandoned"},
    "retrying": {"processing", "cancelled", "abandoned"},
    "failed": {"scheduled", "cancelled"},  # guest adds a new card -> reschedule
    "succeeded": set(),
    "abandoned": {"scheduled", "cancelled"},
    "cancelled": set(),
}

# Allowed next states for a charge; refund states only move forward.
CHARGE_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"succeeded", "declined"},
    "succeeded": {"partially_refunded", "refunded"},
    "partially_refunded": {"partially_refunded", "refunded"},
    "declined": set(),
    "refunded": set(),
}

# How "far along" a charge is. An event whose target rank is <= current rank is stale.
CHARGE_RANK = {"pending": 0, "declined": 1, "succeeded": 1, "partially_refunded": 2, "refunded": 3}


# What to do with an incoming charge event, compared to the charge's current state.
class EventVerdict(StrEnum):
    APPLY = "apply"
    STALE = "stale"  # we're already at or past this state: ignore
    TOO_EARLY = "too_early"  # event is ahead of us (e.g. refund before success): retry later


# True if `current -> target` is listed in the given transition table.
def can_transition(table: dict[str, set[str]], current: str, target: str) -> bool:
    return target in table.get(current, set())


# Validate a status change and return the new status, or raise InvalidTransition.
def transition(table: dict[str, set[str]], current: str, target: str) -> str:
    if not can_transition(table, current, target):
        raise InvalidTransition(f"{current} -> {target}")
    return target


# Classify a charge event: apply it, ignore it as stale, or defer it as too early.
def judge_charge_event(current: str, target: str) -> EventVerdict:
    if can_transition(CHARGE_TRANSITIONS, current, target):
        return EventVerdict.APPLY
    if CHARGE_RANK.get(target, 0) <= CHARGE_RANK.get(current, 0):
        return EventVerdict.STALE
    return EventVerdict.TOO_EARLY
