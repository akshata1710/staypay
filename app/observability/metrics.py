"""Prometheus metrics. Names follow <thing>_<unit>_total / _seconds conventions."""

from prometheus_client import Counter, Gauge, Histogram

HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route", "status"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)

CHECKOUTS = Counter("checkout_requests_total", "Checkout requests by outcome", ["outcome"])
IDEMPOTENT_REPLAYS = Counter("idempotent_replays_total", "Requests answered from a stored response")

CHARGES = Counter("charges_total", "Charge attempts by result", ["result"])
PROCESSOR_ERRORS = Counter("processor_errors_total", "Processor calls that failed", ["kind"])
RECOVERED_PAYMENTS = Counter("recovered_payments_total", "Installments that succeeded after a retry")
ABANDONED_INSTALLMENTS = Counter("abandoned_installments_total", "Installments given up on", ["reason"])

WEBHOOKS_RECEIVED = Counter("webhook_events_received_total", "Webhook events stored", ["source", "duplicate"])
WEBHOOKS_PROCESSED = Counter("webhook_events_processed_total", "Inbox events processed", ["result"])
WEBHOOK_SIGNATURE_FAILURES = Counter("webhook_signature_failures_total", "Rejected webhook signatures")
INBOX_OLDEST_UNPROCESSED = Gauge(
    "webhook_inbox_oldest_unprocessed_seconds", "Age of the oldest unprocessed inbox event"
)
BACKFILL_INSERTED = Counter("backfill_events_inserted_total", "Events recovered by backfill")

REFUNDS = Counter("refunds_total", "Refund decisions by route", ["route"])
RECON_MISMATCHES = Counter("reconciliation_mismatches_total", "Reconciliation mismatches", ["reason"])
