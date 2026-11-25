# Postmortem: INC-04 guest charged twice for one booking

Blameless. Describe systems and decisions, not people.

*This is an onboarding drill. The incident was created on purpose; times are from the drill run.*

| | |
|---|---|
| Date | 2026-11-25 |
| Duration (detect → mitigate → resolve) | Duplicate created 03:16:52 → refund issued 03:17:09 → resolved once the client fix ships (open) |
| Severity | SEV3 (one guest, money returned, no system fault) |
| Author | Akshata |
| Guests affected | 1 (guest_maria), counted with the duplicate-booking query in `docs/SQL_COOKBOOK.md`. The query also matched one pair from internal test traffic (guest_42), which is not a guest. |
| Money affected | 875.00 USD charged twice; 875.00 USD refunded through the refund path. Nothing unrecovered. |

## Summary
A guest tapped "Confirm and pay" twice and the client sent two checkout requests with two different
Idempotency-Keys, so the server correctly treated them as two bookings and charged twice. We refunded the
duplicate in full and kept the original booking. Server-side idempotency worked as designed; no rollback was needed.

## Impact
- **Guest:** two bookings and two 875.00 USD charges for one trip. The duplicate is refunded; it can take
  5–10 business days to appear on the card statement.
- **Host:** saw a second booking for the same dates until it was cancelled.
- **Support:** fielded the complaint and a public social-media thread.
- **Finance:** two charges and one refund in today's settlement; all three match the ledger.

## Timeline (all times UTC)
| Time | Event |
|---|---|
| 03:16:49 | First booking and charge (`client-gen-001`) |
| 03:16:52 | Second booking and charge for the same trip (`client-gen-002`): first symptom |
| — | Detected by support from a guest complaint (no alert exists for duplicate bookings) |
| — | Acknowledged in the support thread with a 15-minute update promise |
| — | Confirmed with SQL: two reservations, two charges, two keys, identical `request_hash` |
| 03:17:09 | Mitigated: duplicate reservation cancelled; 875.00 USD refunded (grace-period rule, 100%) |
| open | Resolved when the client reuses one key per checkout attempt |

## Root cause
1. Why two charges? Two reservations were created.
2. Why two reservations? Two checkout requests arrived with different Idempotency-Keys.
3. Why different keys? The client generates a new key on each tap of "Confirm and pay".
4. Why? The key is created inside the button handler instead of once when the checkout screen opens.

Evidence: both `idempotency_keys` rows have the same `request_hash`
(`d3b8e600…41d0d`) and were created 3 seconds apart. The server promises one charge per key and kept that promise.

## What went well
- One SQL query (identical hashes, different keys) settled "is idempotency broken?" within minutes and
  stopped the call to roll back the last deploy.
- The refund went through the normal cancel path, so the processor, the ledger and the audit log agree.
- The original booking was kept, so the guest still has the trip.

## What went wrong
- We learned about it from a guest and social media, not from monitoring.
- Nothing on the server notices the same guest, listing, dates and amount arriving under two keys seconds apart.

## Where we got lucky
- Only one guest was affected. A client change that mints a key per tap could hit every guest who double-taps
  on a slow connection.
- The guest was inside the 48-hour grace period, so the policy gave a full refund automatically. Outside it,
  the moderate policy could have refunded only 50% and needed a manual decision.

## Action items
| Action | Type (prevent / detect / mitigate) | Owner | Ticket | Due |
|---|---|---|---|---|
| Client reuses one Idempotency-Key per checkout attempt, created when the screen opens and kept on every retry | Prevent | Mobile/web client team | TBD | TBD |
| Alert when the duplicate-booking query finds matches | Detect | Akshata | TBD | TBD |
| Decide whether to add a server-side duplicate guard (warn or block same guest, listing, dates and amount under different keys within 60s); some guests book twice on purpose | Prevent | PM + Akshata | TBD | TBD |
