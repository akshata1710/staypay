# Incident drills

Each drill reproduces a real class of payments incident against your local stack.

```bash
make stack                       # terminal 1: full stack in Docker
bash scripts/drill.sh inc01      # terminal 2: you get paged
bash scripts/drill.sh reset      # when you're done
```

Rules: mitigate first (stop the bleeding), then root-cause. Write down every step and timestamp; you need them for the postmortem. Answers are in [`ANSWERS.md`](ANSWERS.md). Read them only after you've written your own conclusion.

Tools you have: `docker compose -f deploy/docker-compose.yml logs -f <service>`, Grafana (http://localhost:3000),
Prometheus (http://localhost:9090/alerts), `/metrics`, SQLTools / `psql` with [`../SQL_COOKBOOK.md`](../SQL_COOKBOOK.md),
MockPay's `/admin/charges` and `/admin/deliveries`, and the runbooks in [`../runbooks/`](../runbooks/).

---

## INC-01: "I was charged but my trip says pending"
> 📟 Support: 5 guests say their card was charged $875 but the trip still says "Payment pending".

Questions to answer:
1. Were they actually charged? How do you prove it from *our* data and from the processor's?
2. How many guests are affected? (Not just the 5 who complained.)
3. What's the fastest safe mitigation? What must you **not** do? (Hint: "just charge them again".)
4. Why didn't the system heal itself?

## INC-02: Webhook inbox backlog
> 📟 Alert `WebhookInboxBacklog`: webhook events unprocessed for more than 5 minutes.

1. Is the endpoint receiving webhooks? Are they stored? Are they processed?
2. Which guests are affected right now, and what do they see?
3. Is any data lost? What happens once you fix it: in what order are the events applied, and is that safe?

## INC-03: Finance can't close the books
> 📟 Finance: "Yesterday's reconciliation has 4 mismatches. Which ones are real money?"

1. Classify each mismatch: harmless / needs investigation / money at risk.
2. For each, what would you check at the processor and in our ledger?
3. Draft the reply to finance (3–5 sentences, no jargon).

## INC-04: "Charged twice"
> 📟 Escalation: guest_maria was charged twice for the same booking. Twitter thread is growing.

1. Did idempotency fail? Prove it either way with SQL.
2. What do you do for the guest right now?
3. What's the real fix, and whose code is it in?

## INC-05: Checkout is slow
> 📟 Alert `CheckoutLatencyHigh`: checkout p95 above 2s for 10 minutes (it's around 20s).

1. Where is the time going? Use the latency histogram and the logs (`processor.retry`).
2. Are guests being charged? Are they being told the right thing?
3. What config change mitigates it right now, and what's the risk of that change?

## INC-06: Signature failures after a "routine" change
> 📟 Alert `WebhookSignatureFailures`: more than 20 webhook signature failures in 10 minutes.

1. Is this an attack or a misconfiguration? How can you tell?
2. What's the impact while it lasts? What happens to the events MockPay gave up on?
3. Fix it, then make sure nothing is left behind.
