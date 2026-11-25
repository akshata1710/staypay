# Drill answers (spoilers)

## INC-01: charged but pending
**What happened:** every processor call timed out *after* the processor charged the card
(`timeout_rate=1`), **and** every webhook was dropped (`webhook_drop_rate=1`), **and** the sweeper
was down. Checkout returned 202, the charge rows stayed `pending`, and nothing told us the outcome.

**Prove it:**
```sql
select c.id, c.idempotency_key, c.status, c.updated_at, r.guest_id
from charges c join reservations r on r.id = c.reservation_id
where c.status = 'pending' and c.updated_at < now() - interval '5 minutes';
```
At the processor: `curl "localhost:8100/admin/charges?idempotency_key=<key>"` shows `succeeded`.
Counting gives the full blast radius, not just the five who complained.

**Mitigate:** re-drive with the *same* keys, which is safe because the processor replays the stored result:
`docker compose -f deploy/docker-compose.yml start worker-sweeper` (or run `python -m app.workers.run sweeper --once`), and
`python -m app.workers.run backfill --once` to pull the missing events.
**Never** create a new charge or change the idempotency key: that's how you double-charge.

**Why it didn't self-heal:** both safety nets (webhooks and the sweeper) were down at once.
**Follow-ups:** alert on "pending charges older than 10 min" (it would have fired before support
did); alert when a worker has no heartbeat; SP-115.

## INC-02: inbox backlog
**What happened:** `worker-inbox` was stopped. The endpoint kept returning 200 and storing events
(`select count(*) from webhook_inbox where processed_at is null`), so **no data was lost**. That's
the point of store-then-process. Guests whose checkout timed out stay "pending" until the worker
runs again.

**Fix:** start the worker. Events are applied in `received_at` order, but order doesn't matter for
correctness: each event goes through the state machine; stale ones are ignored and early ones deferred.
**Follow-ups:** a liveness alert per worker (metrics port down / no ticks), and a dashboard
panel for inbox depth by `type`.

## INC-03: reconciliation
| Reason | Meaning | Action |
|---|---|---|
| `CURRENCY_ROUNDING` (diff 1) | FX/rounding at the processor | Harmless. Book to the rounding account; track the total trend |
| `AMOUNT_MISMATCH` (diff 500) | Processor settled a different amount than we charged | **Money at risk.** Pull the processor's charge object, compare to our ledger row, open a case with the processor |
| `STATUS_MISMATCH` (reversed) | Processor reversed a charge we think succeeded | **Money at risk.** Likely a chargeback/reversal we never got an event for → run backfill, check disputes |
| `MISSING_IN_LEDGER` | Processor settled something we have no record of | **Investigate first.** Our write was lost, or another system shares the account. Never "fix" by inserting a ledger row without understanding it |

**Reply to finance (example):** "Of the 4 items, 1 is a 1-cent rounding difference and can be
booked as usual. 3 need investigation: one charge settled $5.00 more than we charged, one was
reversed by the processor, and one settlement line has no matching record on our side. We've
opened cases for all three and will update you by 3pm."

## INC-04: "charged twice"
**Idempotency did not fail.** The two requests had *different* keys:
```sql
select r.id, r.created_at, k.key, k.request_hash
from reservations r join idempotency_keys k on k.reservation_id = r.id
where r.guest_id = 'guest_maria' order by r.created_at;
```
Same `request_hash`, different keys, one second apart: the **client** generates a new key per tap
instead of one per checkout attempt.

**For the guest now:** cancel the duplicate reservation (full refund via grace period) and reply to the guest.
**Real fix:** client creates the key when the checkout screen opens and reuses it for every retry.
Server-side safety net (optional): warn/hold when the same guest+listing+dates+amount arrives
twice within 60s with different keys. That's a product decision, so involve your PM.

## INC-05: slow checkout
**What happened:** 30% of processor calls hang. Checkout waits `processor_timeout_s (10s) ×
processor_max_attempts (2)` ≈ 20s before giving up with 202. Guests *are* charged (the processor processed it);
webhooks settle them a moment later, but the 20s spinner makes them retry and abandon.

**Mitigate:** lower the checkout timeout (`STAYPAY_PROCESSOR_TIMEOUT_S=4`,
`STAYPAY_PROCESSOR_MAX_ATTEMPTS=1`) and restart the API. Risk: more 202s ("pending") for slow-but-fine calls.
That's acceptable because the webhook or sweeper completes them, and it's still the truth.
**Real fix:** SP-115 (separate timeouts for interactive vs background paths, circuit breaker).

## INC-06: signature failures
**What happened:** MockPay's signing secret was rotated but StayPay still has the old one. Every
webhook is rejected with 400 → `webhook_signature_failures_total` climbs. It's a misconfiguration
rather than an attack: the failures started exactly at the deploy, come from the processor's IPs, and are for real event ids.

**Impact:** anything that depended on a webhook (timeouts in this drill) stays pending. After 3
failed attempts MockPay **gives up**, so fixing the secret alone does not recover those events.

**Fix:** give StayPay the processor's new secret, *without* restarting MockPay (its event history
is what you'll backfill from):
```bash
STAYPAY_SECRET_OVERRIDE=whsec_rotated docker compose -f deploy/docker-compose.yml up -d --no-deps api worker-backfill
docker compose -f deploy/docker-compose.yml run --rm worker-backfill python -m app.workers.run backfill --once
```
Backfill pulls the events MockPay gave up on; the inbox worker applies them. Check
`select count(*) from charges where status='pending'` is back to 0. To return to defaults afterwards:
`bash scripts/drill.sh reset --secret`.
**Follow-up:** SP-113 (accept two secrets during rotation) and a rotation runbook.
