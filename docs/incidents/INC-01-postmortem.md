# Postmortem: INC-01 guests charged but trips stuck on "Payment pending"

Blameless. Describe systems and decisions, not people.

*This is an onboarding drill (`scripts/drill.sh inc01`), run in the dev container: no Docker, Grafana or
Prometheus, so detection was by SQL and MockPay's admin endpoints. Times are from the drill run.*

| | |
|---|---|
| Date | 2026-11-25 |
| Duration (detect → mitigate → resolve) | Charged 04:44:36 → paged 04:44:56 → mitigated 04:46:19 (~1.5 min after the page) → resolved once the action items land (open) |
| Severity | SEV2 (guests charged with no booking confirmation; would have lasted up to 24h without a person) |
| Author | Akshata |
| Guests affected | 5: every charge in `pending` (SQL cookbook "Charges stuck pending"), all matched to a `succeeded` charge at the processor. Same 5 who complained; nobody silent. |
| Money affected | 5 × 875.00 USD = 4,375.00 USD collected correctly but unconfirmed. No double charges, nothing to refund. |

## Summary
The processor charged five guests, then every API call timed out and every webhook was dropped, so our ledger
never learned the outcome and the trips stayed "Payment pending". The sweeper that normally heals this was
stopped and nothing alerted. Running backfill pulled the processor's own events and confirmed all five bookings
without charging anyone again.

## Impact
- **Guests:** charged 875.00 USD but shown "Payment pending" with no confirmation; likely to retry or contact support.
- **Hosts:** saw unconfirmed bookings for paid stays.
- **Support:** five complaints, with no way to tell guests whether they had paid.
- **Finance:** five settled charges missing from the ledger; tonight's reconciliation would have flagged
  them `MISSING_IN_LEDGER` had they not been fixed.

## Timeline (all times UTC)
| Time | Event |
|---|---|
| 04:44:36 | Five checkouts; the processor charges all five, then hangs. Our calls time out → `202`, charges `pending` |
| 04:44:36 | All five `charge.succeeded` webhooks dropped before delivery |
| 04:44:56 | Detected by support (5 complaints). No alert fired |
| 04:45 | Confirmed: 5 `pending` charges with no `processor_txn_id`; processor has 5 `succeeded` charges with the same idempotency keys; 5 dropped deliveries; 0 inbox rows |
| 04:46:11 | Ran `sweeper --once` per the runbook: did nothing (charges younger than the 300s sweeper window) |
| 04:46:18 | Mitigation: `backfill --once` inserted the 5 missing events; `inbox --once` applied them |
| 04:46:19 | Mitigated: 5 reservations `confirmed`, 0 charges `pending`; processor still shows one charge per guest |
| open | Resolved when detection and the sweeper gap are fixed (action items) |

## Root cause
1. Why "pending"? Our ledger never received the processor's answer.
2. Why not? The API call timed out *after* the charge went through, and the webhook carrying the result was dropped.
3. Why didn't the sweeper fix it? It was stopped. Even when it runs, it waits 300s before re-sending a charge.
4. Why didn't backfill fix it? It runs once a day (02:17 UTC), so a dropped webhook waits up to 24 hours.
5. Why did we hear from guests first? No alert watches for charges stuck in `pending` or for the sweeper being down.

The design is right (one charge per key; a timeout is "unknown", never "failed"). What was missing is
detection, and a recovery path that doesn't depend on a single worker being up.

## What went well
- Idempotency held: the guests were charged exactly once, and recovery charged no one again.
- Each charge carried our own idempotency key, so our rows and the processor's matched one-to-one.
- Checkout answered `202` (unknown), never "payment failed", so no guest was told to pay again.
- Backfill recovers from the processor's own event list without any new charge call.
- The 9 older webhooks that the inbox applied at the same time were all correctly `ignored_stale`.

## What went wrong
- No alert: support found it, not monitoring.
- The runbook says "don't wait for the 5-minute window: `sweeper --once`", but the sweeper applies the same
  300s window, so during the first five minutes that step does nothing.
- Both self-healing paths failed together: the sweeper was down and backfill is daily.

## Where we got lucky
- Only five guests: the faults were on for ~20 seconds. A longer processor outage plus a broken webhook
  endpoint would affect everyone checking out.
- No one ran "just charge them again", which would have double-charged five guests (the runbook's "Never" list helped).

## Action items
| Action | Type (prevent / detect / mitigate) | Owner | Ticket | Due |
|---|---|---|---|---|
| Alert when any charge is `pending` for more than 10 minutes (expose a gauge for the oldest pending charge, like `webhook_inbox_oldest_unprocessed_seconds`) | Detect | Akshata | TBD | TBD |
| Alert when the sweeper hasn't completed a run in 5 minutes (heartbeat metric) | Detect | Akshata | TBD | TBD |
| Fix the runbook: `sweeper --once` only touches charges older than `STAYPAY_STUCK_PROCESSING_AFTER_S`; document backfill as the first step when webhooks are missing, or add a `--min-age` override for the sweeper | Mitigate | Akshata | TBD | TBD |
| Run backfill hourly instead of daily (it dedupes, so it's cheap and safe) | Mitigate | Akshata | TBD | TBD |
| Shorter processor timeout on the checkout path: each guest waited ~20s before the `202` | Mitigate | Akshata | SP-115 | TBD |
