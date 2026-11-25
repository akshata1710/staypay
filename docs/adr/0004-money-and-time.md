# ADR 0004: Money as integer minor units; time as UTC; "now" is a parameter

**Status:** accepted

- **Money** is `BIGINT` minor units (cents) everywhere: DB, Python, API. Floats can't represent
  0.10 exactly, so sums drift. Splits put the rounding remainder on one installment so the total
  is always exact. Formatting for humans happens only at the edges (and must respect currency
  exponents: SP-109).
- **Time** is stored as `timestamptz`, computed in UTC. Check-in is a date; policy math uses
  15:00 UTC on that date (a documented simplification. Real systems use the listing's timezone).
- **"now"** is passed into domain functions (`now=`) instead of read inside them, so tests can
  fast-forward days (retry schedules, deadlines, policy tiers) without sleeping or mocking clocks.
