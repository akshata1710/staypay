"""HTTP routes. Handlers stay thin: validate input, call a domain function, shape the response."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime

from fastapi import APIRouter, Body, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.cache import cache_get, cache_set, rate_limited
from app.config import get_settings
from app.db.session import get_sessionmaker
from app.domain import checkout as checkout_domain
from app.domain import history, payment_methods, reconciliation, refunds, webhooks
from app.domain.views import plan_view
from app.observability.metrics import WEBHOOK_SIGNATURE_FAILURES
from app.processor.client import get_processor
from mockpay.signing import SIGNATURE_HEADER, SignatureError, verify

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health", tags=["ops"])
async def health():
    return {"ok": True}


@router.post(
    "/checkout",
    tags=["checkout"],
    status_code=201,
    responses={
        202: {"description": "Charge outcome pending"},
        402: {"description": "Card declined"},
        409: {"description": "Same key still in progress"},
        422: {"description": "Key reused"},
    },
)
async def post_checkout(
    req: checkout_domain.CheckoutRequest, idempotency_key: str = Header(..., min_length=8, max_length=200)
):
    if await rate_limited(f"checkout:{req.guest_id}", idempotency_key, get_settings().checkout_rate_limit_per_min):
        raise HTTPException(429, "Too many checkout attempts; slow down.")
    result = await checkout_domain.checkout(get_sessionmaker(), get_processor(), idempotency_key, req)
    headers = {"Idempotent-Replayed": "true"} if result.replayed else {}
    return JSONResponse(result.body, status_code=result.status_code, headers=headers)


class CancelRequest(BaseModel):
    reason: str | None = None
    cancelled_at: datetime | None = None  # honoured only outside prod, for demos and tests


@router.post("/reservations/{reservation_id}/cancel", tags=["refunds"])
async def cancel(reservation_id: uuid.UUID, body: CancelRequest = Body(default_factory=CancelRequest)):
    at = body.cancelled_at if get_settings().env != "prod" else None
    try:
        return await refunds.cancel_reservation(
            get_sessionmaker(), get_processor(), reservation_id, cancelled_at=at, decided_by="guest"
        )
    except LookupError:
        raise HTTPException(404, "reservation not found") from None
    except refunds.PaymentInFlight:
        raise HTTPException(409, "A payment is being processed for this reservation. Retry in a minute.") from None


class PaymentMethodRequest(BaseModel):
    payment_method: str = Field(min_length=1, max_length=64)


@router.put(
    "/reservations/{reservation_id}/payment-method",
    tags=["plans"],
    responses={409: {"description": "Reservation closed, or a payment is in flight"}},
)
async def put_payment_method(reservation_id: uuid.UUID, body: PaymentMethodRequest):
    try:
        return await payment_methods.update_payment_method(get_sessionmaker(), reservation_id, body.payment_method)
    except LookupError:
        raise HTTPException(404, "reservation not found") from None
    except payment_methods.PaymentMethodConflict as e:
        raise HTTPException(409, f"Can't update the card: {e}.") from None


@router.get("/reservations/{reservation_id}/plan", tags=["plans"])
async def get_plan(reservation_id: uuid.UUID):
    async with get_sessionmaker()() as s:
        view = await plan_view(s, reservation_id)
    if view is None:
        raise HTTPException(404, "reservation not found")
    return view


@router.get("/reservations/{reservation_id}/payments", tags=["history"])
async def get_payments(
    reservation_id: uuid.UUID, response: Response, cursor: str | None = None, limit: int = Query(50, ge=1, le=200)
):
    settings = get_settings()
    cache_key = f"hist:{reservation_id}:{cursor or 'first'}:{limit}"
    if (cached := await cache_get(cache_key)) is not None:
        response.headers["X-Cache"] = "hit"
        return cached
    try:
        async with get_sessionmaker()() as s:
            page = await history.get_page(s, reservation_id, cursor, limit)
    except history.BadCursor:
        raise HTTPException(400, "invalid cursor") from None
    cacheable = history.page_is_immutable(page, settings.history_immutable_after_s)
    page.pop("_last_occurred_at")
    if cacheable:
        await cache_set(cache_key, page, settings.history_cache_ttl_s)
    response.headers["X-Cache"] = "miss"
    return page


@router.post("/webhooks/mockpay", tags=["webhooks"])
async def mockpay_webhook(request: Request):
    raw = await request.body()
    settings = get_settings()
    try:
        verify(settings.webhook_secret, raw, request.headers.get(SIGNATURE_HEADER), settings.webhook_tolerance_s)
    except SignatureError as e:
        WEBHOOK_SIGNATURE_FAILURES.inc()
        log.warning("webhook.bad_signature", extra={"err": str(e)})
        raise HTTPException(400, "invalid signature") from None
    event = json.loads(raw)
    await webhooks.ingest(get_sessionmaker(), event)
    return {"received": True}


@router.get("/admin/reconciliation/{day}", tags=["admin"])
async def reconciliation_report(day: date):
    report = await reconciliation.latest_report(get_sessionmaker(), day)
    if report is None:
        raise HTTPException(404, "no reconciliation run for that date")
    return report


@router.post("/admin/reconciliation/{day}/run", tags=["admin"])
async def run_reconciliation(day: date):
    return await reconciliation.reconcile(get_sessionmaker(), get_processor(), day)
