# Runbook: webhook inbox backlog / dead letters / signature failures

## Backlog (`WebhookInboxBacklog`)
1. Is the inbox worker alive? Check pod/container, logs (`worker.error`), metrics port.
2. Is it stuck on one event? `select event_id, attempts, last_error from webhook_inbox where processed_at is null order by attempts desc limit 10;`
   A poison event raising on every try is retried with backoff and dead-lettered after
   `STAYPAY_WEBHOOK_MAX_DEFERS` attempts. It does not block other events (each runs in a savepoint).
3. Is the DB slow? Lock waits: `select * from pg_stat_activity where wait_event_type = 'Lock';`
4. Scale the inbox worker. Replicas are safe (`SKIP LOCKED`).

No events are lost while the worker is down: the endpoint only stores them.

## Dead letters (`WebhookDeadLetters`)
`select * from webhook_inbox where result = 'dead';` Read `last_error`, fix the bug, then replay
(SP-114; until then: `update webhook_inbox set processed_at = null, result = null, attempts = 0, next_attempt_at = null where event_id = '...'`, reviewed by a second person).

## Signature failures (`WebhookSignatureFailures`)
- Started right after a deploy or secret rotation → config. Make our secret match the processor's.
- Random event ids / unknown source IPs → possible attack. The 400s are already protecting you; tell security.
- After fixing: the processor may have **given up** on some events. Run backfill.
