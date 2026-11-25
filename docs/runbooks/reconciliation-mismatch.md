# Runbook: reconciliation mismatches

Report: `GET /admin/reconciliation/{date}` (re-run with `POST .../run`; each run is stored).

| Reason | First check | Usual cause |
|---|---|---|
| `CURRENCY_ROUNDING` | diff is ±1 minor unit | FX rounding. No action; track the total |
| `AMOUNT_MISMATCH` | processor's charge object vs our `charges.amount` | partial capture, fee netted, our bug |
| `STATUS_MISMATCH` | events for that charge (reversal/dispute?) | missed webhook → run backfill |
| `MISSING_IN_SETTLEMENT` | did it settle the next day? | timing around midnight (SP-103), or processor delay |
| `MISSING_IN_LEDGER` | search our logs for the id; is another system on this account? | lost write, test traffic, other integration |

Escalate to finance with: count per reason, total money at risk (exclude rounding), and an ETA.
Do not insert or edit ledger rows to make the report clean.
