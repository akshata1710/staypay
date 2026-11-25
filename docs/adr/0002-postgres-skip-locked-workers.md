# ADR 0002: Postgres `FOR UPDATE SKIP LOCKED` instead of a message queue

**Status:** accepted

## Context
Charge scheduling, retries, webhook processing and sweeping all need "claim a row, work on it,
mark it done", safely across several worker replicas.

## Decision
Workers poll Postgres with `SELECT ... FOR UPDATE SKIP LOCKED LIMIT n`. Each worker gets rows nobody
else holds; a crashed worker's locks are released when its connection drops.

## Consequences
- One less system to run. The work queue *is* the source of truth, so a job can't be "in the
  queue but not in the DB" or the other way round.
- Polling adds up to one interval of latency (1–5s). Fine for installments due days ahead.
- Composite indexes such as `(status, next_attempt_at)` keep the polling queries cheap; revisit at ~1k claims/s.
- If we outgrow it, the inbox/outbox tables are a natural bridge to Kafka/SQS.
