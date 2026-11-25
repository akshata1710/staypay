# Runbook: payments stuck in `pending`

**Symptom:** guests charged at the processor but reservation shows "Payment pending"; or the
count of `charges.status='pending'` older than 10 min is above 0.

**Why it happens:** the processor call timed out (outcome unknown) and the result hasn't arrived
by webhook yet. Normally the webhook or the sweeper resolves it within ~5 minutes.

## Check
1. How many and how old? (SQL cookbook: "Charges stuck pending")
2. Is the sweeper running? `docker compose -f deploy/docker-compose.yml ps worker-sweeper` / pod status / its metrics port.
3. Are webhooks arriving? `rate(webhook_events_received_total[5m])` > 0? Inbox lag?
4. Pick one stuck charge and ask the processor directly with its idempotency key (MockPay:
   `/admin/charges?idempotency_key=...`; real processors have a "retrieve by idempotency key" or search).

## Fix
- Sweeper down → start it. It re-sends the **same** idempotency key; the processor replays the result.
- Webhooks missing → run backfill once: `python -m app.workers.run backfill --once`.
- Do it now (don't wait for the 5-minute window): `python -m app.workers.run sweeper --once`.

## Never
- Create a new charge for the guest, or change a charge's idempotency key.
- Mark a charge `succeeded`/`declined` by hand without the processor's answer.
