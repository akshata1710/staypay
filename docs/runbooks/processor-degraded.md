# Runbook: processor degraded (errors, timeouts, slow checkout)

1. Confirm it's them, not us: `processor_errors_total` by `kind` (timeout / 5xx / transport), the
   processor's status page, and whether only one endpoint is affected.
2. Guest impact: checkouts return 202 ("pending") on timeouts; that's correct behaviour, as
   webhooks/sweeper finish them. Watch `charges_total{result="unknown"}` and stuck-pending count.
3. Latency: checkout waits up to `PROCESSOR_TIMEOUT_S × PROCESSOR_MAX_ATTEMPTS`. If p95 is bad,
   lower them for the API (not the workers) and restart the API. Trade-off: more "pending" answers.
4. 4xx `rejected` errors are **our** bug (bad request, key reused with a different body). Page the team; don't retry them.
5. After recovery: run backfill once and check the stuck-pending query is back to 0.

Never "fix" a timeout by retrying with a new idempotency key.
