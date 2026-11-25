"""MockPay: a simulated payment processor with failure injection.

It behaves like a real processor in the ways that matter for reliability work:
- Idempotency keys: the same key returns the same stored response, a different body is an error.
- Card outcomes are driven by the payment-method token (see PAYMENT_METHODS).
- Webhooks are signed and delivered asynchronously, with retries, and can be dropped,
  duplicated, delayed or reordered.
- Every event is also listed at GET /v1/events, so the client can backfill what it missed.
- A daily settlement CSV can be generated with injected discrepancies.
- A "timeout" fault processes the charge and THEN hangs, which is the nasty real-world case:
  the money moved, but the client never heard about it.

State is in memory. Restarting MockPay clears it (POST /admin/reset does the same).
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
import os
import random
import secrets
from collections import Counter
from datetime import UTC, date, datetime

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from mockpay.signing import SIGNATURE_HEADER, sign

log = logging.getLogger("mockpay")

# token -> decline code (None = success). "pm_card_soft_then_ok" is handled specially.
PAYMENT_METHODS: dict[str, str | None] = {
    "pm_card_ok": None,
    "pm_card_insufficient_funds": "insufficient_funds",
    "pm_card_closed": "card_closed",
    "pm_card_expired": "expired_card",
    "pm_card_stolen": "stolen_card",
    "pm_card_do_not_honor": "do_not_honor",
    "pm_card_processing_error": "processing_error",
}
SOFT_THEN_OK = "pm_card_soft_then_ok"  # declines insufficient_funds twice, then succeeds


# Fault-injection settings, changed via POST /admin/faults.
class Faults(BaseModel):
    seed: int | None = None
    error_rate: float = 0.0  # HTTP 500 before anything is processed
    timeout_rate: float = 0.0  # processes the request, then hangs
    timeout_delay_s: float = 30.0
    decline_rate: float = 0.0  # random insufficient_funds on otherwise-good cards
    webhook_drop_rate: float = 0.0
    webhook_duplicate_rate: float = 0.0
    webhook_reorder: bool = False
    webhook_max_delay_ms: int = 2000
    webhook_delay_ms: int = 0
    settlement_rounding: bool = False  # +/-1 minor unit on some lines
    settlement_amount_mismatch: bool = False  # a real amount difference on one line
    settlement_drop_rate: float = 0.0  # lines missing from the file
    settlement_extra: bool = False  # a phantom line the client never saw
    settlement_status_flip: bool = False  # one line marked "reversed"


# Request body for POST /v1/charges.
class ChargeIn(BaseModel):
    amount: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    payment_method: str
    metadata: dict = Field(default_factory=dict)


# Request body for POST /v1/refunds.
class RefundIn(BaseModel):
    charge: str
    amount: int = Field(gt=0)
    metadata: dict = Field(default_factory=dict)


# Current time in UTC.
def _now() -> datetime:
    return datetime.now(UTC)


# Random processor-style id, e.g. "ch_3f9a...".
def _id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


# Hash of a request body, to detect an idempotency key reused with different parameters.
def _fingerprint(body: BaseModel) -> str:
    return hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()


# All in-memory processor state: charges, refunds, idempotency cache, events, and faults.
class MockPayState:
    def __init__(self, webhook_url: str, webhook_secret: str, webhook_client: httpx.AsyncClient | None):
        self.webhook_url = webhook_url
        self.webhook_secret = webhook_secret
        self.webhook_client = webhook_client
        self.webhook_retry_backoff_s = float(os.getenv("MOCKPAY_WEBHOOK_RETRY_BACKOFF_S", "1.0"))
        self.reset()

    # Clear everything back to a fresh processor.
    def reset(self) -> None:
        self.faults = Faults()
        self.rng = random.Random()
        self.charges: dict[str, dict] = {}
        self.refunds: dict[str, dict] = {}
        self.idem: dict[str, tuple[str, int, dict]] = {}  # key -> (fingerprint, status, body)
        self.events: list[dict] = []
        self.soft_attempts: Counter[str] = Counter()
        self.tasks: set[asyncio.Task] = set()
        self.delivery_log: list[dict] = []

    # Replace the active faults and reseed the RNG so runs are reproducible.
    def set_faults(self, faults: Faults) -> None:
        self.faults = faults
        self.rng = random.Random(faults.seed)

    # Random draw that is True with probability `rate`.
    def roll(self, rate: float) -> bool:
        return rate > 0 and self.rng.random() < rate

    # ---- events & webhooks -------------------------------------------------------------
    # Record an event (for /v1/events) and schedule its webhook delivery.
    def emit(self, type_: str, obj: dict) -> dict:
        event = {
            "id": _id("evt"),
            "type": type_,
            "created": _now().isoformat(),
            "data": {"object": obj},
        }
        self.events.append(event)
        self._schedule_delivery(event)
        return event

    # Apply webhook faults (drop, duplicate, delay/reorder), then deliver in the background.
    def _schedule_delivery(self, event: dict) -> None:
        f = self.faults
        if self.roll(f.webhook_drop_rate):
            self.delivery_log.append({"event_id": event["id"], "result": "dropped"})
            return
        copies = 2 if self.roll(f.webhook_duplicate_rate) else 1
        for _ in range(copies):
            delay_ms = f.webhook_delay_ms
            if f.webhook_reorder:
                delay_ms += self.rng.randint(0, f.webhook_max_delay_ms)
            task = asyncio.get_event_loop().create_task(self._deliver(event, delay_ms / 1000))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    # POST one signed webhook, retrying up to 3 times with exponential backoff.
    async def _deliver(self, event: dict, delay_s: float) -> None:
        if delay_s:
            await asyncio.sleep(delay_s)
        body = json.dumps(event).encode()
        client = self.webhook_client or httpx.AsyncClient(timeout=5.0)
        try:
            for attempt in range(3):
                try:
                    resp = await client.post(
                        self.webhook_url,
                        content=body,
                        headers={
                            "Content-Type": "application/json",
                            SIGNATURE_HEADER: sign(self.webhook_secret, body),
                        },
                    )
                    if resp.status_code < 300:
                        self.delivery_log.append({"event_id": event["id"], "result": "delivered"})
                        return
                except httpx.HTTPError as e:
                    log.warning("webhook delivery error %s: %s", event["id"], e)
                await asyncio.sleep(self.webhook_retry_backoff_s * (2**attempt))
            self.delivery_log.append({"event_id": event["id"], "result": "gave_up"})
        finally:
            if self.webhook_client is None:
                await client.aclose()

    async def drain(self) -> None:
        """Wait for in-flight webhook deliveries (used by tests and demos)."""
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)


# Build the MockPay FastAPI app and its routes around one MockPayState.
def create_app(
    webhook_url: str | None = None,
    webhook_secret: str | None = None,
    webhook_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    app = FastAPI(title="MockPay", version="1.0", description="Simulated payment processor")
    state = MockPayState(
        webhook_url=webhook_url or os.getenv("MOCKPAY_WEBHOOK_URL", "http://localhost:8000/webhooks/mockpay"),
        webhook_secret=webhook_secret or os.getenv("MOCKPAY_WEBHOOK_SECRET", "whsec_dev_secret"),
        webhook_client=webhook_client,
    )
    app.state.mockpay = state
    api_keys = set(os.getenv("MOCKPAY_API_KEYS", "sk_test_staypay").split(","))

    # Reject requests without a valid Bearer API key.
    def auth(authorization: str | None) -> None:
        if not authorization or authorization.removeprefix("Bearer ") not in api_keys:
            raise HTTPException(401, {"error": {"type": "authentication_error"}})

    # Require an Idempotency-Key; replay the stored response, or 400 if the body differs.
    def idem_lookup(key: str | None, fp: str) -> JSONResponse | None:
        if not key:
            raise HTTPException(400, {"error": {"type": "invalid_request", "message": "Idempotency-Key required"}})
        if key in state.idem:
            stored_fp, code, body = state.idem[key]
            if stored_fp != fp:
                raise HTTPException(
                    400,
                    {"error": {"type": "idempotency_error", "message": "key reused with different parameters"}},
                )
            return JSONResponse(body, status_code=code, headers={"Idempotent-Replayed": "true"})
        return None

    # Timeout fault: sleep long enough that the client gives up waiting.
    async def maybe_hang() -> None:
        if state.roll(state.faults.timeout_rate):
            await asyncio.sleep(state.faults.timeout_delay_s)

    # Liveness check.
    @app.get("/health")
    async def health():
        return {"ok": True}

    # Create a charge: replay a known key, else decide success or decline from the card token and faults.
    @app.post("/v1/charges")
    async def create_charge(
        body: ChargeIn,
        idempotency_key: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        auth(authorization)
        fp = _fingerprint(body)
        if (replay := idem_lookup(idempotency_key, fp)) is not None:
            await maybe_hang()
            return replay
        if state.roll(state.faults.error_rate):
            raise HTTPException(500, {"error": {"type": "api_error"}})

        # Pick the outcome: soft-then-ok card, the token's fixed result, or a random decline fault.
        decline: str | None
        if body.payment_method == SOFT_THEN_OK:
            state.soft_attempts[body.payment_method] += 1
            decline = "insufficient_funds" if state.soft_attempts[body.payment_method] <= 2 else None
        else:
            decline = PAYMENT_METHODS.get(body.payment_method)
        if decline is None and state.roll(state.faults.decline_rate):
            decline = "insufficient_funds"

        charge = {
            "id": _id("ch"),
            "object": "charge",
            "amount": body.amount,
            "currency": body.currency.lower(),
            "payment_method": body.payment_method,
            "status": "failed" if decline else "succeeded",
            "decline_code": decline,
            "amount_refunded": 0,
            "refunded": False,
            "disputed": False,
            "idempotency_key": idempotency_key,
            "metadata": body.metadata,
            "created": _now().isoformat(),
        }
        state.charges[charge["id"]] = charge
        if decline:
            code, resp = 402, {"error": {"type": "card_error", "code": decline, "charge": charge}}
            state.emit("charge.failed", dict(charge))
        else:
            code, resp = 200, charge
            state.emit("charge.succeeded", dict(charge))
        # Cache the response so the same key replays it.
        state.idem[idempotency_key] = (fp, code, resp)
        await maybe_hang()  # money moved; now maybe the client never hears about it
        return JSONResponse(resp, status_code=code)

    # Fetch one charge by id.
    @app.get("/v1/charges/{charge_id}")
    async def get_charge(charge_id: str, authorization: str | None = Header(default=None)):
        auth(authorization)
        if charge_id not in state.charges:
            raise HTTPException(404, {"error": {"type": "not_found"}})
        return state.charges[charge_id]

    # Refund part or all of a succeeded charge, with the same idempotency and faults as charges.
    @app.post("/v1/refunds")
    async def create_refund(
        body: RefundIn,
        idempotency_key: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        auth(authorization)
        fp = _fingerprint(body)
        if (replay := idem_lookup(idempotency_key, fp)) is not None:
            await maybe_hang()
            return replay
        if state.roll(state.faults.error_rate):
            raise HTTPException(500, {"error": {"type": "api_error"}})
        charge = state.charges.get(body.charge)
        if charge is None or charge["status"] != "succeeded":
            raise HTTPException(400, {"error": {"type": "invalid_request", "message": "charge not refundable"}})
        if body.amount > charge["amount"] - charge["amount_refunded"]:
            raise HTTPException(400, {"error": {"type": "invalid_request", "message": "amount exceeds refundable"}})

        refund = {
            "id": _id("re"),
            "object": "refund",
            "charge": charge["id"],
            "amount": body.amount,
            "currency": charge["currency"],
            "status": "succeeded",
            "idempotency_key": idempotency_key,
            "metadata": body.metadata,
            "created": _now().isoformat(),
        }
        state.refunds[refund["id"]] = refund
        charge["amount_refunded"] += body.amount
        charge["refunded"] = charge["amount_refunded"] == charge["amount"]
        state.idem[idempotency_key] = (fp, 200, refund)
        state.emit("charge.refunded", {**charge, "refund": refund})
        await maybe_hang()
        return refund

    # List events created after a timestamp; the client's backfill reads this.
    @app.get("/v1/events")
    async def list_events(
        created_after: datetime | None = None,
        limit: int = 100,
        authorization: str | None = Header(default=None),
    ):
        auth(authorization)
        evts = [
            e for e in state.events if created_after is None or datetime.fromisoformat(e["created"]) > created_after
        ]
        return {"data": evts[:limit], "has_more": len(evts) > limit}

    # Build the day's settlement CSV from succeeded charges and refunds.
    @app.get("/v1/settlements/{day}", response_class=PlainTextResponse)
    async def settlement(day: date, authorization: str | None = Header(default=None)):
        auth(authorization)
        f = state.faults
        rows: list[dict] = []
        for ch in state.charges.values():
            if ch["status"] == "succeeded" and datetime.fromisoformat(ch["created"]).date() == day:
                rows.append(
                    {
                        "processor_txn_id": ch["id"],
                        "type": "charge",
                        "amount": ch["amount"],
                        "currency": ch["currency"],
                        "status": "settled",
                    }
                )
        for re in state.refunds.values():
            if datetime.fromisoformat(re["created"]).date() == day:
                rows.append(
                    {
                        "processor_txn_id": re["id"],
                        "type": "refund",
                        "amount": re["amount"],
                        "currency": re["currency"],
                        "status": "settled",
                    }
                )

        # Inject settlement discrepancies (drop, rounding, amount, status flip, phantom line) for reconciliation.
        rng = random.Random(f.seed)
        rows = [r for r in rows if not (f.settlement_drop_rate and rng.random() < f.settlement_drop_rate)]
        if f.settlement_rounding and rows:
            rows[0]["amount"] += 1
        if f.settlement_amount_mismatch and len(rows) > 1:
            rows[1]["amount"] += 500
        if f.settlement_status_flip and len(rows) > 2:
            rows[2]["status"] = "reversed"
        if f.settlement_extra:
            rows.append(
                {
                    "processor_txn_id": _id("ch"),
                    "type": "charge",
                    "amount": 1234,
                    "currency": "usd",
                    "status": "settled",
                }
            )

        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=["processor_txn_id", "type", "amount", "currency", "status"])
        w.writeheader()
        w.writerows(rows)
        return buf.getvalue()

    # ---- admin -------------------------------------------------------------------------
    # Show the current faults.
    @app.get("/admin/faults")
    async def get_faults():
        return state.faults

    # Merge the posted fields into the current faults.
    @app.post("/admin/faults")
    async def set_faults(request: Request):
        patch = await request.json()
        state.set_faults(Faults(**{**state.faults.model_dump(), **patch}))
        return state.faults

    # Wipe all MockPay state.
    @app.post("/admin/reset")
    async def reset():
        state.reset()
        return {"ok": True}

    # Simulate a chargeback on a charge and emit charge.dispute.created.
    @app.post("/admin/charges/{charge_id}/dispute")
    async def dispute(charge_id: str):
        ch = state.charges.get(charge_id)
        if ch is None:
            raise HTTPException(404)
        ch["disputed"] = True
        state.emit("charge.dispute.created", dict(ch))
        return ch

    # List charges, optionally filtered by idempotency key.
    @app.get("/admin/charges")
    async def admin_charges(idempotency_key: str | None = None):
        rows = [c for c in state.charges.values() if idempotency_key in (None, c["idempotency_key"])]
        return {"count": len(rows), "data": rows}

    # Show the webhook delivery log (delivered, dropped, gave_up).
    @app.get("/admin/deliveries")
    async def deliveries():
        return {"deliveries": state.delivery_log, "events": len(state.events)}

    return app


# Module-level app for `uvicorn mockpay.app:app`.
app = create_app()
