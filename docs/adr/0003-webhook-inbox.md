# ADR 0003: Store-then-process webhooks with state-aware handling and backfill

**Status:** accepted

## Context
Processing webhooks inline made the endpoint slow under load; the processor timed out, retried a
few times, then gave up, which left charged guests "pending". Events also arrive duplicated and out of order.

## Decision
1. Endpoint: verify HMAC (signed timestamp, 5-minute tolerance) → `INSERT ... ON CONFLICT DO NOTHING`
   into `webhook_inbox` → 200. Nothing else.
2. Inbox worker applies each event through the state machine:
   - valid transition → apply
   - target state at or behind current (`CHARGE_RANK`) → `ignored_stale`
   - target state ahead but not reachable yet (refund before success) → defer with backoff;
     dead-letter after N tries
3. Nightly backfill lists the processor's events and inserts any the inbox is missing.
4. The API path and the webhook path share `apply_charge_outcome`, so whichever arrives first wins
   and the other is a no-op.

## Consequences
- The endpoint is fast and never loses an accepted event.
- Correctness doesn't depend on delivery order or count (chaos tests cover drop, dup, reorder).
- Charges can be matched by our idempotency key even if the API call timed out before we learned the processor id.
