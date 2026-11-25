# SQL cookbook for on-call

Read-only queries. Run them in SQLTools ("StayPay local") or `psql postgresql://staypay:staypay@localhost:5432/staypay`.
**Never** `UPDATE` money tables by hand in production. Write a script, get it reviewed, keep an audit trail.

```sql
-- Everything about one reservation, in order
select occurred_at, type, amount, payload from payment_events
where reservation_id = :rid order by occurred_at, id;

-- Charges stuck pending (the "charged but pending" signal)
select id, reservation_id, idempotency_key, amount, updated_at, now() - updated_at as age
from charges where status = 'pending' order by updated_at limit 50;

-- Installments currently retrying, and when they'll run
select i.id, i.number, i.attempts, i.last_decline_code, i.next_attempt_at, r.guest_id
from installments i join payment_plans p on p.id = i.plan_id join reservations r on r.id = p.reservation_id
where i.status = 'retrying' order by i.next_attempt_at;

-- Decline mix in the last 24h
select decline_code, count(*) from charges
where status = 'declined' and created_at > now() - interval '24 hours'
group by 1 order by 2 desc;

-- Webhook inbox health
select coalesce(result, 'unprocessed') as state, type, count(*), min(received_at) as oldest
from webhook_inbox group by 1, 2 order by 1, 2;

-- Events that keep being deferred (too_early / errors)
select event_id, type, attempts, next_attempt_at, last_error from webhook_inbox
where processed_at is null and attempts > 3 order by attempts desc;

-- Possible client double-submits: same guest + listing + amount, different keys, within 2 minutes
select a.guest_id, a.id as first, b.id as second, b.created_at - a.created_at as gap
from reservations a join reservations b
  on a.guest_id = b.guest_id and a.listing_id = b.listing_id and a.total_amount = b.total_amount
 and a.check_in = b.check_in and b.created_at > a.created_at and b.created_at < a.created_at + interval '2 minutes';

-- Idempotency keys stuck in progress (crashed owners)
select key, recovery_point, reservation_id, locked_at from idempotency_keys
where status = 'in_progress' and locked_at < now() - interval '1 minute';

-- Refund decisions with explanations (what support sees)
select created_at, policy, decided_by, result->>'total_refund' as refund, result->>'explanation' as why
from refund_audit where reservation_id = :rid order by created_at;

-- Open manual reviews
select * from manual_review_queue where status = 'open' order by created_at;

-- Latest reconciliation summary
select r.settlement_date, m.reason, count(*) from reconciliation_runs r
join reconciliation_mismatches m on m.run_id = r.id
where r.id = (select id from reconciliation_runs order by created_at desc limit 1)
group by 1, 2;

-- Is the history query using the index? (look for "Index Scan using ix_payment_events_res_time_id")
explain analyze select * from payment_events where reservation_id = :rid
order by occurred_at, id limit 51;
```
