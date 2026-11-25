# ADR 0001: Idempotency keys with recovery points ("atomic phases")

**Status:** accepted

## Context
Mobile clients retry. Guests double-tap. Pods die mid-request. Any of these can produce two
reservations or two charges for one intent.

## Decision
- Client sends `Idempotency-Key` (one per checkout attempt, reused on every retry).
- The first request wins an `INSERT` on the key's primary key and becomes the *owner*. Other
  requests poll for the stored response (409 after a timeout).
- The owner works in phases, each its own short DB transaction, and records a recovery point:
  `started → reservation_created → finished`. No transaction is held open during the processor call.
- An owner that dies leaves `in_progress`. After `IDEMPOTENCY_STALE_AFTER_S` a retry takes over
  and resumes from the recovery point. On a handled exception, the lock is released immediately.
- The processor call uses its own deterministic key (`plan:installment:attempt`), so a resumed
  request can never double-charge either.
- Same key with a different body → 422.

## Consequences
- Exactly one reservation and one charge per key, even under 50 concurrent duplicates (chaos test).
- Stored responses grow the table (SP-104).
- Keys are global today; collisions between guests cause 422s (SP-105).

## Alternatives considered
- *Redis lock only:* lost on Redis failover, and doesn't store the response.
- *One big transaction around everything, including the processor call:* holds row locks for
  seconds, exhausts the pool under processor slowness, and a rollback can't undo a real charge.
