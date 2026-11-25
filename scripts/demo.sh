#!/usr/bin/env bash
# Walk through the failure scenarios against a RUNNING stack (make stack, or VS Code "Full stack").
# Needs: curl, jq.
set -euo pipefail
API=${API:-http://localhost:8000}
MP=${MP:-http://localhost:8100}
CHECKIN=$(date -d "+60 days" +%F 2>/dev/null || date -v+60d +%F)

step() { printf "\n\033[1;36m== %s ==\033[0m\n" "$*"; }
faults() { curl -s -X POST "$MP/admin/faults" -H 'content-type: application/json' -d "$1" >/dev/null; }
checkout() { # $1 key, $2 payment method, $3 installments
  curl -s -w '\nHTTP %{http_code}\n' -X POST "$API/checkout" -H 'content-type: application/json' \
    -H "Idempotency-Key: $1" -d "{\"guest_id\":\"demo_$RANDOM\",\"listing_id\":\"l1\",\"check_in\":\"$CHECKIN\",
    \"total_amount\":120000,\"currency\":\"USD\",\"installments\":${3:-1},\"cancellation_policy\":\"moderate\",
    \"payment_method\":\"$2\"}"
}

curl -s -X POST "$MP/admin/reset" >/dev/null

step "1. Happy path: book now, pay in 3 installments"
OUT=$(checkout "demo-$(date +%s)-a" pm_card_ok 3); echo "$OUT" | head -1 | jq '{reservation_id, reservation_status, plan: [.plan.installments[] | {number, amount, status}]}'
RES=$(echo "$OUT" | head -1 | jq -r .reservation_id)

step "2. Double tap: same Idempotency-Key 5 times in parallel"
KEY="demo-double-$(date +%s)"
BODY="{\"guest_id\":\"double\",\"listing_id\":\"l1\",\"check_in\":\"$CHECKIN\",\"total_amount\":5000,\"currency\":\"USD\",\"payment_method\":\"pm_card_ok\"}"
for i in 1 2 3 4 5; do
  curl -s -o /dev/null -w "request $i -> HTTP %{http_code}\n" -X POST "$API/checkout" -H 'content-type: application/json' -H "Idempotency-Key: $KEY" -d "$BODY" &
done; wait
PKEY=$(curl -s "$MP/admin/charges" | jq -r '[.data[] | select(.amount==5000)] | length')
echo "Charges of 5000 at the processor: $PKEY (expected 1)"

step "3. Hard decline at checkout: fail fast, no retry"
checkout "demo-$(date +%s)-c" pm_card_closed 1 | head -1 | jq '{reservation_status, error}'

step "4. Processor timeout: money moves, we don't hear back -> 202, webhook settles it"
faults '{"timeout_rate":1.0,"timeout_delay_s":15}'
echo "(waits for STAYPAY_PROCESSOR_TIMEOUT_S, default 10s x attempts...)"
OUT=$(checkout "demo-$(date +%s)-d" pm_card_ok 1); echo "$OUT" | tail -1
RES2=$(echo "$OUT" | head -1 | jq -r .reservation_id)
faults '{"timeout_rate":0}'
sleep 3
curl -s "$API/reservations/$RES2/plan" | jq '{reservation_status, charge: .plan.installments[0].charges[0].status}'

step "5. Drop 100% of webhooks, then backfill"
faults '{"webhook_drop_rate":1.0}'
checkout "demo-$(date +%s)-e" pm_card_ok 1 >/dev/null
echo "Deliveries: $(curl -s "$MP/admin/deliveries" | jq '[.deliveries[].result] | group_by(.) | map({(.[0]): length}) | add')"
echo "Run: python -m app.workers.run backfill --once   (or docker compose ... run --rm worker-backfill ... --once)"
faults '{"webhook_drop_rate":0}'

step "6. Cancel and refund (moderate policy)"
curl -s -X POST "$API/reservations/$RES/cancel" -H 'content-type: application/json' -d '{}' | jq '{status, decision: .decision.explanation, refunds}'

step "7. Payment history (cursor pagination)"
curl -s "$API/reservations/$RES/payments?limit=3" | jq '{types: [.data[].type], next_cursor, has_more}'

step "8. Reconciliation with injected settlement errors"
faults '{"settlement_rounding":true,"settlement_extra":true}'
curl -s -X POST "$API/admin/reconciliation/$(date -u +%F)/run" | jq '{matched, mismatched, reasons: [.mismatches[].reason]}'
faults '{"settlement_rounding":false,"settlement_extra":false}'

printf "\nDone. Grafana: http://localhost:3000  Prometheus: http://localhost:9090  API docs: %s/docs\n" "$API"
