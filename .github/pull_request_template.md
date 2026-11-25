## What and why
<!-- One paragraph. Link the ticket (docs/tickets/SP-XXX). -->

## How I tested it
- [ ] Unit tests
- [ ] Integration / chaos tests
- [ ] Ran it locally against MockPay (paste the curl or the demo step)

## Payments checklist
- [ ] Every new processor call carries an idempotency key, and a retry reuses it
- [ ] A timeout is treated as "unknown", never as "failed"
- [ ] Status changes go through `state_machine.transition` (no raw `x.status = ...` for money states)
- [ ] No DB transaction is held open across a network call
- [ ] Money is integer minor units; no floats
- [ ] Migration is backward compatible with the currently deployed code (expand → migrate → contract)
- [ ] New failure modes have a metric, and an alert or runbook if someone must act

## Rollout / rollback
<!-- Feature flag? Can it be rolled back without a data fix? -->
