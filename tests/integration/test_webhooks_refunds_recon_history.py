import json
from datetime import UTC, datetime, timedelta

import pytest

from app.clock import utcnow
from app.config import get_settings
from app.domain.refunds import check_in_datetime
from mockpay.signing import SIGNATURE_HEADER, sign

pytestmark = pytest.mark.integration


# ---- webhooks -----------------------------------------------------------------------------
async def test_webhook_rejects_bad_signature(h):
    body = json.dumps({"id": "evt_x", "type": "charge.succeeded", "data": {"object": {}}}).encode()
    r = await h.api.post("/webhooks/mockpay", content=body, headers={SIGNATURE_HEADER: sign("wrong", body)})
    assert r.status_code == 400
    assert await h.scalar("select count(*) from webhook_inbox") == 0


async def test_webhook_is_stored_once_even_if_delivered_twice(h):
    body = json.dumps({"id": "evt_dup", "type": "charge.succeeded", "data": {"object": {"id": "ch_zzz"}}}).encode()
    hdr = {SIGNATURE_HEADER: sign(get_settings().webhook_secret, body)}
    for _ in range(3):
        assert (await h.api.post("/webhooks/mockpay", content=body, headers=hdr)).status_code == 200
    assert await h.scalar("select count(*) from webhook_inbox") == 1


async def test_webhooks_for_api_confirmed_charges_are_ignored_as_stale(h):
    await h.checkout()
    counts = await h.settle_webhooks()
    assert counts == {"ignored_stale": 1}


# ---- refunds ------------------------------------------------------------------------------
async def test_cancel_full_refund_end_to_end(h):
    r = await h.checkout(policy="flexible", total=80_000, days_out=30)
    res_id = r.json()["reservation_id"]
    c = await h.api.post(f"/reservations/{res_id}/cancel", json={})
    body = c.json()
    assert c.status_code == 200, c.text
    assert body["status"] == "cancelled"
    assert body["decision"]["total_refund"] == 80_000
    assert [x["status"] for x in body["refunds"]] == ["succeeded"]
    assert await h.scalar("select status from charges") == "refunded"
    # Idempotent: cancelling again changes nothing.
    again = (await h.api.post(f"/reservations/{res_id}/cancel", json={})).json()
    assert again["already_cancelled"] is True
    assert len(h.mockpay.refunds) == 1
    # Late webhooks (charge.succeeded, charge.refunded) must not change the final state.
    await h.settle_webhooks()
    assert await h.scalar("select status from charges") == "refunded"
    assert await h.scalar("select amount_refunded from charges") == 80_000
    assert await h.scalar("select count(*) from refund_audit") == 1


async def test_late_cancel_moderate_refunds_half(h):
    r = await h.checkout(policy="moderate", total=100_000, days_out=30)
    res = r.json()
    ci = check_in_datetime(datetime.fromisoformat(res["plan"]["payment_deadline"]).date() + timedelta(days=3))
    c = await h.api.post(
        f"/reservations/{res['reservation_id']}/cancel", json={"cancelled_at": (ci - timedelta(hours=48)).isoformat()}
    )
    assert c.json()["decision"]["total_refund"] == 50_000
    assert await h.scalar("select status from charges") == "partially_refunded"


async def test_disputed_charge_routes_to_manual_review(h):
    r = await h.checkout(policy="flexible")
    res_id = r.json()["reservation_id"]
    txn = r.json()["plan"]["installments"][0]["charges"][0]["processor_txn_id"]
    await h.mockpay_admin.post(f"/admin/charges/{txn}/dispute")
    await h.settle_webhooks()
    c = (await h.api.post(f"/reservations/{res_id}/cancel", json={})).json()
    assert c["status"] == "cancellation_review"
    assert c["refunds"] == []
    assert await h.scalar("select reason from manual_review_queue") == "disputed_charge"


async def test_cancel_unknown_reservation_404(h):
    r = await h.api.post("/reservations/00000000-0000-0000-0000-000000000000/cancel", json={})
    assert r.status_code == 404


# ---- reconciliation -----------------------------------------------------------------------
async def test_reconciliation_clean_day_has_no_mismatches(h):
    for _ in range(3):
        await h.checkout()
    today = utcnow().date().isoformat()
    report = (await h.api.post(f"/admin/reconciliation/{today}/run")).json()
    assert (report["matched"], report["mismatched"]) == (3, 0)


async def test_reconciliation_flags_every_injected_mismatch(h):
    for _ in range(4):
        await h.checkout()
    await h.faults(
        settlement_rounding=True, settlement_amount_mismatch=True, settlement_status_flip=True, settlement_extra=True
    )
    today = utcnow().date().isoformat()
    await h.api.post(f"/admin/reconciliation/{today}/run")
    report = (await h.api.get(f"/admin/reconciliation/{today}")).json()
    assert report["by_reason"] == {
        "CURRENCY_ROUNDING": 1,
        "AMOUNT_MISMATCH": 1,
        "STATUS_MISMATCH": 1,
        "MISSING_IN_LEDGER": 1,
    }
    assert report["matched"] == 1


async def test_reconciliation_detects_missing_in_settlement(h):
    for _ in range(5):
        await h.checkout()
    await h.faults(settlement_drop_rate=1.0, seed=1)
    today = utcnow().date().isoformat()
    report = (await h.api.post(f"/admin/reconciliation/{today}/run")).json()
    assert {m["reason"] for m in report["mismatches"]} == {"MISSING_IN_SETTLEMENT"}


# ---- payment history ----------------------------------------------------------------------
async def test_history_pagination_walks_every_event_once_and_caches_old_pages(h):
    from app.db.models import PaymentEvent

    r = await h.checkout()
    res_id = r.json()["reservation_id"]
    old = datetime.now(UTC) - timedelta(days=1)
    async with h.sm() as s, s.begin():
        for i in range(120):
            s.add(
                PaymentEvent(
                    reservation_id=res_id,
                    type="test.event",
                    amount=i,
                    occurred_at=old + timedelta(seconds=i),
                    payload={},
                )
            )
    seen, cursor, pages = [], None, 0
    while True:
        params = {"limit": 50, **({"cursor": cursor} if cursor else {})}
        page = (await h.api.get(f"/reservations/{res_id}/payments", params=params)).json()
        seen += [e["id"] for e in page["data"]]
        pages += 1
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]
    total = await h.scalar("select count(*) from payment_events")
    assert len(seen) == len(set(seen)) == total
    assert pages == 3
    first = await h.api.get(f"/reservations/{res_id}/payments", params={"limit": 50})
    assert first.headers["X-Cache"] == "hit"  # full, old page was cached
    last = await h.api.get(f"/reservations/{res_id}/payments", params={"limit": 50, "cursor": cursor})
    assert last.headers["X-Cache"] == "miss"  # last page is still growing: never cached


async def test_history_bad_cursor_is_400(h):
    r = await h.checkout()
    res_id = r.json()["reservation_id"]
    resp = await h.api.get(f"/reservations/{res_id}/payments", params={"cursor": "%%%"})
    assert resp.status_code == 400


async def test_metrics_endpoint(h):
    await h.checkout()
    text = (await h.api.get("/metrics")).text
    assert "checkout_requests_total" in text and "charges_total" in text
