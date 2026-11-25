# Backlog

Realistic tickets for this codebase, the kind an SDE-1 gets in their first months. Several are
**real bugs or gaps in this code**, left in on purpose. Each ticket says what "done" means.
Write the test first.

Labels: `good-first-issue` · `medium` · `hard` · `bug` · `feature` · `ops` · `design`

| ID | Title | Labels |
|---|---|---|
| [SP-101](#sp-101) | Let guests update their card and reschedule failed installments | feature, medium |
| [SP-102](#sp-102) | Backfill can skip events that share a timestamp | bug, medium |
| [SP-103](#sp-103) | Reconciliation false positives around midnight UTC | bug, hard |
| [SP-104](#sp-104) | `idempotency_keys` grows forever | ops, good-first-issue |
| [SP-105](#sp-105) | Idempotency keys collide across guests | bug, medium |
| [SP-106](#sp-106) | Notifications are written but never sent | feature, medium |
| [SP-107](#sp-107) | Should `do_not_honor` be retried? | design, medium |
| [SP-108](#sp-108) | Cancelling during an in-flight charge returns 409 | feature, hard |
| [SP-109](#sp-109) | Refund explanation shows wrong amounts for JPY | bug, good-first-issue |
| [SP-110](#sp-110) | Measure and publish the history API latency numbers | ops, good-first-issue |
| [SP-111](#sp-111) | Support agents need a manual partial-refund endpoint | feature, medium |
| [SP-112](#sp-112) | We can't tell when rate limiting is silently off | ops, good-first-issue |
| [SP-113](#sp-113) | Webhook secret rotation without downtime | feature, medium |
| [SP-114](#sp-114) | Replay dead-lettered webhook events | ops, medium |
| [SP-115](#sp-115) | Checkout p95 can reach 20s when the processor hangs | bug, hard |

---

### SP-101
**Let guests update their card and reschedule failed installments** · feature, medium

A hard decline puts an installment in `failed` and sends `update_payment_method`, but there's no
way to actually update it. Guests whose card expired lose their trip.

- `PUT /reservations/{id}/payment-method` with `{ "payment_method": "pm_..." }`
- Every `failed` or `abandoned` installment goes back to `scheduled` with `next_attempt_at = now`
  (both transitions already exist in `INSTALLMENT_TRANSITIONS`). `attempts` keeps counting.
- Reservation `at_risk` → `pending_payment` until the charge lands.
- Reject if the reservation is cancelled, or if any installment is `processing` (409).
- A `payment_method.updated` payment event.

**Done when:** a test goes closed card → failed → update card → charge worker → succeeded, and
the processor saw a *new* idempotency key for the new attempt.

### SP-102
**Backfill can skip events that share a timestamp** · bug, medium

`webhooks.backfill()` pages with `created_after=<last event's created>`, and MockPay filters with
`>`. If page 1 ends in the middle of several events with the same `created`, the rest are skipped.

- Reproduce in a test: create >100 events, force identical timestamps on the boundary.
- Fix with a tie-breaker cursor (`starting_after=<event_id>`, which real processors offer). You
  need to change MockPay's `/v1/events` and the client.

**Done when:** the test fails before the fix and passes after; no duplicates, no gaps.

### SP-103
**Reconciliation false positives around midnight UTC** · bug, hard

The ledger side uses *our* `charges.created_at` to choose the day; the settlement file uses the
*processor's* timestamp. A charge we insert at 23:59:59.900 and the processor creates at
00:00:00.100 shows up as `MISSING_IN_SETTLEMENT` today and `MISSING_IN_LEDGER` tomorrow.

- Store the processor's `created` on the charge/refund row (migration!) and reconcile on that.
- Or: compare against a window (day ± 5 min) and only report items missing from both adjacent days.
- Write down which you chose and why (a short ADR in `docs/adr/`).

**Done when:** a test with a charge straddling midnight produces zero mismatches for both days.

### SP-104
**`idempotency_keys` grows forever** · ops, good-first-issue

Every checkout adds a row with the full response JSON. Nothing deletes them.

- Add an index on `created_at` (Alembic migration: `make migration m="..."`).
- Add a `cleanup` worker: delete `completed` keys older than 24h, in batches of 1000 (never
  one giant `DELETE`: it locks and bloats). Leave `in_progress` keys alone.
- Add a metric `idempotency_keys_deleted_total`.

**Done when:** test proves old completed keys go, recent ones and in-progress ones stay.

### SP-105
**Idempotency keys collide across guests** · bug, medium

`idempotency_keys.key` is global. If two different guests' apps happen to send the same key
(a client bug that uses a timestamp as the key, say), the second guest gets a 422
`idempotency_key_reused` for a perfectly valid request.

- Scope keys by guest: primary key `(guest_id, key)`. That needs a migration with a safe rollout
  plan (expand → backfill → switch → contract). Describe it in the PR.

**Done when:** two guests with the same key both get 201 and two reservations; same guest + same key still replays.

### SP-106
**Notifications are written but never sent** · feature, medium

`notifications` is an outbox, but nothing drains it.

- Add `sent_at` and `attempts` columns. Add a `notifier` worker that claims unsent rows with
  `SKIP LOCKED`, "sends" (log it, or POST to a fake endpoint), and marks them sent.
- Make it at-least-once and say in the PR why exactly-once isn't possible here. How should the
  receiving side de-duplicate?

### SP-107
**Should `do_not_honor` be retried?** · design, medium

Product says many `do_not_honor` declines succeed on a later try. Risk says retrying hard declines
gets merchants flagged by card networks.

- Add a `decline_code` label to `charges_total` (careful: label cardinality, so map unknown codes to `other`).
- Use MockPay with `pm_card_do_not_honor` and a `decline_rate` to produce data and write a
  one-page proposal: what you'd measure in production, a safe experiment (e.g. 1 retry max, 10%
  of traffic behind a flag), and the rollback signal.

**Done when:** proposal reviewed; no code change to the policy until it's agreed.

### SP-108
**Cancelling during an in-flight charge returns 409** · feature, hard

`cancel_reservation` refuses while an installment is `processing`. Fine for a second, bad if the
processor is slow and the guest keeps getting errors.

- Introduce `cancel_requested` on the reservation. Cancel returns 202. When the in-flight charge
  resolves (API path, webhook or sweeper), finish the cancellation and the refund.
- Think through every interleaving. Write them as tests first.

### SP-109
**Refund explanation shows wrong amounts for JPY** · bug, good-first-issue

`refunds._money()` divides by 100. JPY has no minor unit, so ¥12,000 shows as "120.00 JPY".

- Add a small currency table (`USD: 2, EUR: 2, JPY: 0, KWD: 3`) and use it everywhere money is
  formatted. Unknown currency → fail loudly in tests.

**Done when:** parametrized test for USD, JPY, KWD passes.

### SP-110
**Measure and publish the history API latency numbers** · ops, good-first-issue

The README has a "Metrics" section with placeholders. Fill it with numbers you measured:

```bash
make stack
python scripts/seed_history.py --events 1000      # prints a reservation id
# before: temporarily change history.get_page to use OFFSET and no cache; after: current code
hey -n 2000 -c 20 "http://localhost:8000/reservations/<id>/payments?limit=50"   # or locust
```
Record hardware, data size, and p50/p95/p99. Never publish a number you didn't measure.

### SP-111
**Support agents need a manual partial-refund endpoint** · feature, medium

Manual review cases (`manual_review_queue`) have no way to be resolved.

- `POST /admin/reservations/{id}/refunds` `{amount, reason, agent_id}` → idempotent (`Idempotency-Key`
  header required), writes `refund_audit` with `decided_by=agent_id`, resolves the open review item.
- Never refund more than what's refundable across charges.

### SP-112
**We can't tell when rate limiting is silently off** · ops, good-first-issue

`cache.rate_limited()` fails open when Redis is down. That's the right call, but it's invisible.

- Add `redis_errors_total{op}` and an alert if it's non-zero for 5 minutes.
- Add a test using a Redis URL that points nowhere.

### SP-113
**Webhook secret rotation without downtime** · feature, medium

Rotating `STAYPAY_WEBHOOK_SECRET` breaks every webhook until MockPay is updated too.

- Accept a comma-separated list of secrets; verify against each. Document the rotation steps in a runbook.

### SP-114
**Replay dead-lettered webhook events** · ops, medium

Events with `result='dead'` need a way back once the bug is fixed.

- `POST /admin/webhooks/replay` `{event_ids: [...]}` or `{since: ...}`: resets `processed_at`, `result`,
  `attempts`. Audit who did it.

### SP-115
**Checkout p95 can reach 20s when the processor hangs** · bug, hard

`processor_timeout_s=10` × `processor_max_attempts=2` means a hanging processor holds a guest's
checkout for ~20s (see incident drill INC-05).

- Lower the checkout-path timeout (e.g. 4s, 1 attempt) and keep the long timeout for background
  workers. The charge just ends `pending` → 202 → webhook/sweeper finishes it.
- Stretch: a simple circuit breaker. After N timeouts in M seconds, stop calling for 30s and
  return 503 with `Retry-After`.
