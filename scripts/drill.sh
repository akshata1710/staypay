#!/usr/bin/env bash
# Incident drills. Start the full Docker stack first (make stack), then:
#   bash scripts/drill.sh inc01     # sets up the incident; you get "paged"
#   bash scripts/drill.sh reset     # clears all MockPay faults
# Read docs/incidents/DRILLS.md for the page text. Answers: docs/incidents/ANSWERS.md (don't peek).
set -euo pipefail
API=${API:-http://localhost:8000}
MP=${MP:-http://localhost:8100}
COMPOSE="docker compose -f deploy/docker-compose.yml"
CHECKIN=$(date -d "+45 days" +%F 2>/dev/null || date -v+45d +%F)

faults() { curl -s -X POST "$MP/admin/faults" -H 'content-type: application/json' -d "$1" >/dev/null; }
checkout() { # $1 guest  $2 key  $3 payment method
  curl -s -o /dev/null -X POST "$API/checkout" -H 'content-type: application/json' -H "Idempotency-Key: $2" \
    -d "{\"guest_id\":\"$1\",\"listing_id\":\"lst_88\",\"check_in\":\"$CHECKIN\",\"total_amount\":87500,
         \"currency\":\"USD\",\"installments\":1,\"cancellation_policy\":\"moderate\",\"payment_method\":\"${3:-pm_card_ok}\"}"
}
traffic() { for i in $(seq 1 "$1"); do checkout "guest_$RANDOM" "drill-$(date +%s%N)-$i" & done; wait; }
page() { printf "\n\033[1;31m📟 PAGE: %s\033[0m\n" "$*"; }

case "${1:-}" in
  reset)
    faults '{"error_rate":0,"timeout_rate":0,"decline_rate":0,"webhook_drop_rate":0,"webhook_duplicate_rate":0,
             "webhook_reorder":false,"webhook_delay_ms":0,"settlement_rounding":false,"settlement_amount_mismatch":false,
             "settlement_drop_rate":0,"settlement_extra":false,"settlement_status_flip":false}'
    $COMPOSE start worker-inbox worker-sweeper >/dev/null 2>&1 || true
    if [ "${2:-}" = "--secret" ]; then  # back to the default secret on both sides (recreates MockPay: clears its state)
      $COMPOSE up -d --no-deps mockpay api worker-inbox worker-sweeper worker-charge worker-backfill >/dev/null
    fi
    echo "faults cleared, workers started" ;;
  inc01)
    $COMPOSE stop worker-sweeper >/dev/null 2>&1 || true
    faults '{"timeout_rate":1.0,"timeout_delay_s":30,"webhook_drop_rate":1.0}'
    echo "generating traffic (takes ~20s)..."; traffic 5
    faults '{"timeout_rate":0,"webhook_drop_rate":0}'
    page "Support: 5 guests say their card was charged \$875 but the trip still says 'Payment pending'." ;;
  inc02)
    $COMPOSE stop worker-inbox >/dev/null 2>&1 || true
    faults '{"timeout_rate":1.0,"timeout_delay_s":30}'
    echo "generating traffic (takes ~20s)..."; traffic 8
    faults '{"timeout_rate":0}'
    page "Alert WebhookInboxBacklog: webhook events unprocessed for more than 5 minutes." ;;
  inc03)
    traffic 10
    faults '{"settlement_rounding":true,"settlement_amount_mismatch":true,"settlement_status_flip":true,"settlement_extra":true}'
    curl -s -X POST "$API/admin/reconciliation/$(date -u +%F)/run" >/dev/null
    page "Finance: 'Yesterday's reconciliation has 4 mismatches, can you tell us which ones are real money?'" ;;
  inc04)
    for i in 1 2; do checkout "guest_maria" "client-generated-$(date +%s%N)"; sleep 1; done
    page "Escalation: guest_maria was charged twice for the same booking. Twitter thread is growing." ;;
  inc05)
    faults '{"timeout_rate":0.3,"timeout_delay_s":30,"seed":5}'
    echo "generating traffic (takes ~30s)..."; traffic 10
    page "Alert CheckoutLatencyHigh: checkout p95 above 2s for 10 minutes (it's ~20s)." ;;
  inc06)
    echo "restarting MockPay with a rotated secret the API doesn't know..."
    MOCKPAY_SECRET_OVERRIDE=whsec_rotated $COMPOSE up -d --no-deps mockpay >/dev/null
    sleep 3; faults '{"timeout_rate":1.0,"timeout_delay_s":30}'; traffic 3; faults '{"timeout_rate":0}'
    page "Alert WebhookSignatureFailures: >20 webhook signature failures in 10 minutes." ;;
  *) echo "usage: $0 reset|inc01|inc02|inc03|inc04|inc05|inc06"; exit 1 ;;
esac
