# StayPay: Reliable Payments for a Booking Platform

StayPay is a backend payments platform for a short-term rental booking product. It handles book-now-pay-later installment plans and the failure cases that make payments hard: duplicate requests, failed charges, lost webhooks, refunds and reconciliation.

It runs against **MockPay**, a simulated payment processor included in this repo, which can be configured to time out, decline, deliver webhooks late or out of order, and settle with small differences. That makes every failure path reproducible in tests.

> Built independently to demonstrate payment-reliability patterns I've worked with professionally. All code, data and designs here are my own; nothing is taken from any employer.

**New here?** Start with [`docs/ONBOARDING.md`](docs/ONBOARDING.md): VS Code setup, a reading order, a [ticket backlog](docs/tickets/BACKLOG.md), and [incident drills](docs/incidents/DRILLS.md) that page you with realistic symptoms.

---

## What it does

| Module | Problem it solves | Key technique |
| --- | --- | --- |
| **Checkout & idempotency** | Double taps and client retries create duplicate charges | Idempotency keys + unique constraint + recovery points + key passed to processor |
| **Installment plans** | Book now, pay in installments before the trip | Payment-plan state machine + scheduled charge workers |
| **Decline-aware retry** | Retrying hard declines wastes calls; bad timing cancels saveable trips | Decline-code classification + backoff schedule + guest notification |
| **Webhook ingestion** | Charged guests stuck in "pending" when webhooks time out or arrive out of order | Store-then-process inbox, dedupe by event ID, state-aware handling, nightly backfill |
| **Refund engine** | Agents calculating refunds by hand from cancellation policies | Policy rules engine + idempotent refunds + audit log |
| **Reconciliation** | Finance matching processor settlements in spreadsheets | Daily job matching settlement files to the ledger, mismatch report |
| **Payment history API** | Slow, timing-out history for reservations with many events | Unified event view, one indexed query, cursor pagination, Redis cache |

---

## Architecture

```
            ┌──────────────┐        ┌──────────────────────┐
 Client ───▶│ API (FastAPI)│──────▶ │  PostgreSQL (ledger, │
            │  checkout,   │        │  plans, events, inbox)│
            │  refunds,    │        └──────────▲───────────┘
            │  history     │                   │
            └──────┬───────┘                   │
                   │ charge / refund           │
                   ▼                           │
            ┌──────────────┐   webhooks   ┌────┴─────────────┐
            │   MockPay    │─────────────▶│ Webhook endpoint │
            │ (simulated   │              │ verify → store → 200
            │  processor)  │◀──backfill───│ Inbox worker     │
            └──────┬───────┘              └──────────────────┘
                   │ settlement file
                   ▼
            ┌───────────────┐    ┌──────────────┐     ┌────────┐
            │ Reconciliation│    │ Charge/retry │     │ Redis  │
            │ job (daily)   │    │ + sweeper    │     │ cache, │
            └───────────────┘    └──────────────┘     │ limits │
                                                      └────────┘
```

- **API:** FastAPI, async, Pydantic models
- **Database:** PostgreSQL 16 (ledger is the source of truth), SQLAlchemy 2 async + Alembic
- **Cache and rate limiting:** Redis (fails open: Redis down never blocks a payment)
- **Workers:** charge scheduler/retry, stuck-charge sweeper, webhook inbox processor, backfill, reconciliation (plain Python workers with `SELECT … FOR UPDATE SKIP LOCKED`, no extra queue needed)
- **Infra:** Docker Compose locally; Kubernetes manifests (Kustomize) and GitHub Actions CI/CD for deploys
- **Observability:** Prometheus metrics + alert rules, a provisioned Grafana dashboard, structured JSON logs with request IDs

Design decisions are written up as ADRs in [`docs/adr/`](docs/adr/).

---

## Module details

### 1. Checkout and idempotency

**Problem:** On a slow connection the guest taps "Confirm and pay" twice, or the app retries after a timeout. Two requests reach the backend and two charges are created.

**Design**
- The client sends an `Idempotency-Key` header, one per checkout attempt.
- The first request inserts the key into `idempotency_keys` (primary key) with status `in_progress`.
- A duplicate request waits for the first to finish and receives the stored response (`Idempotent-Replayed: true`). It never creates a new charge. Same key with a different body → `422`.
- The owner records a recovery point after each phase (`started → reservation_created → finished`). If it crashes, a retry takes over and resumes. It does not create a second reservation.
- The processor call carries its own deterministic key, so a retry at the processor layer can't double-charge either.
- A processor timeout returns `202` ("pending"), never "failed": the money may have moved.

**Proven by:** a test that fires 50 concurrent identical requests and asserts exactly one charge exists, plus a crash-mid-checkout test.

### 2. Installment plans (book now, pay later)

- A reservation gets a `payment_plan` with N installments (max 4) and due dates before the payment deadline (check-in − 3 days). The rounding remainder goes on installment 1 so the sum is exact.
- Each installment moves through: `scheduled → processing → succeeded | failed | retrying → … | abandoned`, enforced by `app/domain/state_machine.py`.
- The charge worker claims due installments with `FOR UPDATE SKIP LOCKED`, so two workers never take the same one.
- Processor idempotency key: `plan_id:installment_number:a<attempt>`. A retry after a decline is a new attempt (reusing the key would just replay the decline); a crash mid-attempt reuses the same key.

### 3. Decline-aware retry

- [`policies/decline_codes.yaml`](policies/decline_codes.yaml) classifies codes as `retryable` or `non_retryable` (unknown → non-retryable, and logged).
- Retryable: backoff of 12h, 24h, 48h, 72h, stopping before the payment deadline.
- Non-retryable: no retry; the guest gets an "update your payment method" notification.
- Declined at checkout: fail fast with `402`, because the guest is right there.
- Metrics: `recovered_payments_total` vs. `abandoned_installments_total{reason}`.

### 4. Webhook ingestion

- The endpoint only verifies the HMAC signature (signed timestamp, 5-minute tolerance), inserts the raw event into `webhook_inbox` (unique on `event_id`) and returns `200`.
- The inbox worker applies an event only if it's a valid transition: stale events are ignored (a late `charge.succeeded` after `charge.refunded` doesn't regress state), early events are deferred with backoff, and poison events are dead-lettered.
- Charges are matched by processor id **or** our idempotency key (covers timeouts where we never learned the processor id).
- A nightly backfill lists recent events from MockPay's API and inserts any the inbox is missing.
- Alert: events unprocessed for more than 5 minutes.

### 5. Refund engine

- Cancellation policies (flexible, moderate, strict, plus a booking grace period) live in [`policies/cancellation.yaml`](policies/cancellation.yaml).
- `compute_refund(...)` is a pure function: it returns the refund per charge already made, newest first, with an explanation string for support.
- Refunds go to MockPay with idempotency key `refund:<reservation_id>:v<version>:<charge_id>`, and every decision is written to `refund_audit`.
- Disputed charges and cancellations after check-in go to `manual_review_queue` instead of auto-refunding.
- Test suite: a table of real-world-style cases (cancel 1 hour before a policy deadline, cancel after first installment, grace period, partial refunds…).

### 6. Reconciliation

- MockPay produces a daily settlement CSV.
- The job matches each line to the ledger by processor transaction id and compares amount, currency and status.
- Outputs `reconciliation_runs` and `reconciliation_mismatches` with a reason code: `AMOUNT_MISMATCH`, `CURRENCY_ROUNDING`, `MISSING_IN_LEDGER`, `MISSING_IN_SETTLEMENT`, `STATUS_MISMATCH`.
- MockPay can inject each mismatch type, so the report can be demoed end to end.

### 7. Payment history API

- `payment_events` is a single append-only table (charges, installments, refunds, failures) with an index on `(reservation_id, occurred_at, id)`.
- `GET /reservations/{id}/payments?cursor=…&limit=50` uses keyset (cursor) pagination.
- Only full pages whose newest row is older than 60s are cached in Redis (immutable); the last page and in-flight payments are always read fresh. Response header `X-Cache: hit|miss`.

---

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/checkout` | Create reservation and first charge (requires `Idempotency-Key`) |
| `POST` | `/reservations/{id}/cancel` | Cancel and trigger refund computation |
| `GET` | `/reservations/{id}/payments` | Paginated payment history |
| `GET` | `/reservations/{id}/plan` | Installment plan and status |
| `POST` | `/webhooks/mockpay` | Webhook receiver (HMAC-verified) |
| `POST` | `/admin/reconciliation/{date}/run` | Run reconciliation for a day |
| `GET` | `/admin/reconciliation/{date}` | Mismatch report for a day |
| `GET` | `/metrics` | Prometheus metrics |
| `GET` | `/health` | Liveness/readiness |

OpenAPI docs are served at `/docs`. Try every endpoint from VS Code with [`requests.http`](requests.http).

**MockPay payment methods:** `pm_card_ok`, `pm_card_insufficient_funds`, `pm_card_closed`, `pm_card_expired`, `pm_card_stolen`, `pm_card_do_not_honor`, `pm_card_processing_error`, `pm_card_soft_then_ok` (declines twice, then succeeds).

---

## Data model (core tables)

```
reservations(id, guest_id, listing_id, check_in, total_amount, currency, status, cancellation_policy, refund_version)
payment_plans(id, reservation_id, installments, payment_deadline, status)
installments(id, plan_id, number, amount, due_at, status, attempts, next_attempt_at, last_decline_code)
charges(id, installment_id, reservation_id, idempotency_key UNIQUE, processor_txn_id UNIQUE, amount, status, amount_refunded, disputed)
refunds(id, reservation_id, charge_id, amount, idempotency_key UNIQUE, processor_refund_id, status)
payment_events(id, reservation_id, type, amount, occurred_at, payload)
idempotency_keys(key PRIMARY KEY, request_hash, status, recovery_point, reservation_id, response_code, response)
webhook_inbox(event_id PRIMARY KEY, type, payload, source, received_at, processed_at, result, attempts, next_attempt_at)
refund_audit(id, reservation_id, policy, inputs, result, decided_by, created_at)
manual_review_queue(id, reservation_id, reason, details, status)
notifications(id, guest_id, reservation_id, kind, payload)
reconciliation_runs(id, settlement_date, matched, mismatched, created_at)
reconciliation_mismatches(id, run_id, processor_txn_id, reason, details)
```
All money is integer minor units (cents).

---

## Project structure

```
staypay/
├── app/
│   ├── api/              # FastAPI routes
│   ├── domain/           # checkout, charging, plans, retry policy, state machine, webhooks, refunds, reconciliation, history
│   ├── db/               # SQLAlchemy models, Alembic migrations
│   ├── processor/        # processor client (timeouts = unknown, retries reuse keys)
│   ├── workers/          # charge, sweeper, inbox, backfill, reconcile
│   └── observability/    # metrics, JSON logging
├── mockpay/              # simulated processor + failure injection + webhook signing
├── policies/             # cancellation policies, decline codes (YAML)
├── tests/
│   ├── unit/             # pure rules: policies, retry, state machine, signatures, cursors
│   ├── integration/      # Postgres + Redis, StayPay and MockPay wired in-process
│   └── chaos/            # duplicate requests, crashes, dropped/out-of-order webhooks
├── loadtest/             # Locust scenarios
├── scripts/              # demo.sh, drill.sh (incident drills), seed_history.py
├── deploy/
│   ├── docker-compose.yml
│   ├── k8s/              # Kustomize: API, workers, CronJobs, HPA, PDB, migrate job
│   ├── prometheus/       # scrape config + alert rules
│   └── grafana/          # provisioned datasource + dashboard
├── docs/                 # onboarding, ADRs, runbooks, tickets, incident drills, SQL cookbook
├── .vscode/              # launch configs, tasks, settings, recommended extensions
├── .devcontainer/        # one-click dev environment
└── .github/              # CI (lint, test, chaos, build, deploy) + PR template
```

---

## Running locally

### In VS Code (recommended)
1. Open the folder → install the recommended extensions.
2. Either **Dev Containers: Reopen in Container**, or run `make install && make up && make migrate` and select `.venv` as the interpreter.
3. Run and Debug → **Full stack (API + MockPay + workers)** → F5.
4. Open `requests.http` and send requests. API docs at http://localhost:8000/docs.

### Everything in Docker
```bash
git clone https://github.com/akshata1710/staypay.git
cd staypay
docker compose -f deploy/docker-compose.yml up --build
# API:        http://localhost:8000/docs
# MockPay:    http://localhost:8100/docs
# Grafana:    http://localhost:3000   (admin / admin)
# Prometheus: http://localhost:9090
make demo   # walks through every failure scenario
```

Run tests (needs Postgres + Redis: `make up`):

```bash
make test          # unit + integration
make chaos         # duplicate requests, crashes, webhook loss, out-of-order delivery
make loadtest      # Locust against checkout and history endpoints
make lint          # ruff + mypy
```

Turn on failure injection in MockPay:

```bash
curl -X POST localhost:8100/admin/faults \
  -H 'content-type: application/json' \
  -d '{"webhook_drop_rate":0.2,"webhook_reorder":true,"timeout_rate":0.05,"settlement_rounding":true}'
```
All faults: `error_rate`, `timeout_rate`, `timeout_delay_s`, `decline_rate`, `webhook_drop_rate`, `webhook_duplicate_rate`, `webhook_reorder`, `webhook_max_delay_ms`, `webhook_delay_ms`, `settlement_rounding`, `settlement_amount_mismatch`, `settlement_drop_rate`, `settlement_extra`, `settlement_status_flip`, `seed`.

---

## Reliability tests that matter

| Scenario | Expected result | Test |
| --- | --- | --- |
| 50 concurrent checkouts with the same idempotency key | Exactly 1 charge | `test_50_concurrent_checkouts_same_key_exactly_one_charge` |
| Checkout crashes midway, client retries | 1 reservation, 1 charge | `test_checkout_crash_midway_retry_resumes_without_second_reservation` |
| Worker killed mid-charge | Charge completes exactly once after restart | `test_worker_killed_after_claim_…`, `test_worker_killed_after_processor_call_…` |
| Processor times out after charging | 202, then webhook confirms | `test_processor_timeout_at_checkout_then_webhook_confirms` |
| 20% of webhooks dropped | Backfill restores every missing event; no reservation stuck pending | `test_20_percent_webhooks_dropped_backfill_restores_everything` |
| Webhooks delivered out of order / duplicated | Final payment state is correct | `test_out_of_order_webhooks_…`, `test_random_reorder_and_duplicates_converge` |
| Hard decline on an installment | No retry; guest notification sent | `test_hard_decline_on_later_installment_notifies_guest_without_retry` |
| Settlement file with rounding differences | Flagged as `CURRENCY_ROUNDING` in the report | `test_reconciliation_flags_every_injected_mismatch` |

---

## Metrics to publish in this README once built

Fill these in from your own load tests and test runs, so every number is one you measured (see ticket SP-110):

- Checkout p95 latency at N requests per second
- Payment history p95 latency for a reservation with 1,000 events, before and after cursor pagination and caching (`scripts/seed_history.py`)
- Duplicate charges under concurrent retry test: 0
- Reservations stuck pending after webhook-loss chaos test: 0
- Payments recovered by decline-aware retry vs. fixed-schedule retry, on the same simulated decline mix

---

## Tech stack

Python 3.12 · FastAPI · PostgreSQL 16 · SQLAlchemy 2 · Alembic · Redis · Docker · Kubernetes · GitHub Actions · Prometheus · Grafana · Pytest · Locust
