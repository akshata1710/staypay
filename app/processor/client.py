"""Client for the payment processor (MockPay).

Rules this client enforces:
1. Every mutating call carries an idempotency key, so any retry (ours, the worker's, or a
   human re-running a job) is safe.
2. A timeout is NOT a failure. The money may have moved. We return UNKNOWN and let the
   webhook or the stuck-charge sweeper find out what happened.
3. Transient errors are retried a small number of times with the SAME key.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum

import httpx

from app.config import get_settings
from app.observability.metrics import PROCESSOR_ERRORS

log = logging.getLogger(__name__)


class Outcome(StrEnum):
    SUCCEEDED = "succeeded"
    DECLINED = "declined"
    UNKNOWN = "unknown"  # timeout / 5xx after retries: state at the processor is unknown
    REJECTED = "rejected"  # 4xx request error (bad request, key reuse): a bug on our side


@dataclass
class ProcessorResult:
    outcome: Outcome
    processor_id: str | None = None
    decline_code: str | None = None
    raw: dict | None = None
    error: str | None = None


class ProcessorClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float | None = None,
        max_attempts: int | None = None,
    ):
        s = get_settings()
        self.timeout_s = timeout_s if timeout_s is not None else s.processor_timeout_s
        self.max_attempts = max_attempts or s.processor_max_attempts
        self._client = httpx.AsyncClient(
            base_url=base_url or s.mockpay_url,
            headers={"Authorization": f"Bearer {api_key or s.mockpay_api_key}"},
            transport=transport,
            timeout=self.timeout_s,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, path: str, key: str, json: dict) -> ProcessorResult:
        last_err = "unknown"
        for attempt in range(1, self.max_attempts + 1):
            try:
                # wait_for enforces the timeout even on in-process (ASGI) transports used in tests
                resp = await asyncio.wait_for(
                    self._client.post(path, json=json, headers={"Idempotency-Key": key}),
                    timeout=self.timeout_s,
                )
            except (TimeoutError, httpx.TimeoutException):
                PROCESSOR_ERRORS.labels("timeout").inc()
                last_err = "timeout"
            except httpx.TransportError as e:
                PROCESSOR_ERRORS.labels("transport").inc()
                last_err = f"transport: {e}"
            else:
                if resp.status_code == 200:
                    body = resp.json()
                    return ProcessorResult(Outcome.SUCCEEDED, processor_id=body["id"], raw=body)
                if resp.status_code == 402:
                    err = resp.json()["detail"]["error"] if "detail" in resp.json() else resp.json()["error"]
                    ch = err.get("charge") or {}
                    return ProcessorResult(
                        Outcome.DECLINED, processor_id=ch.get("id"), decline_code=err.get("code"), raw=ch
                    )
                if 400 <= resp.status_code < 500:
                    PROCESSOR_ERRORS.labels("rejected").inc()
                    return ProcessorResult(Outcome.REJECTED, error=resp.text)
                PROCESSOR_ERRORS.labels("5xx").inc()
                last_err = f"http {resp.status_code}"
            log.warning("processor.retry", extra={"path": path, "key": key, "attempt": attempt, "err": last_err})
            if attempt < self.max_attempts:
                await asyncio.sleep(0.05 * attempt)
        return ProcessorResult(Outcome.UNKNOWN, error=last_err)

    async def charge(
        self, *, key: str, amount: int, currency: str, payment_method: str, metadata: dict | None = None
    ) -> ProcessorResult:
        return await self._post(
            "/v1/charges",
            key,
            {
                "amount": amount,
                "currency": currency,
                "payment_method": payment_method,
                "metadata": metadata or {},
            },
        )

    async def refund(
        self, *, key: str, processor_charge_id: str, amount: int, metadata: dict | None = None
    ) -> ProcessorResult:
        return await self._post(
            "/v1/refunds",
            key,
            {
                "charge": processor_charge_id,
                "amount": amount,
                "metadata": metadata or {},
            },
        )

    async def list_events(self, created_after: str | None, limit: int = 100) -> dict:
        params: dict = {"limit": limit}
        if created_after:
            params["created_after"] = created_after
        resp = await self._client.get("/v1/events", params=params)
        resp.raise_for_status()
        return resp.json()

    async def settlement_csv(self, day: str) -> str:
        resp = await self._client.get(f"/v1/settlements/{day}")
        resp.raise_for_status()
        return resp.text


_default: ProcessorClient | None = None


def get_processor() -> ProcessorClient:
    global _default
    if _default is None:
        _default = ProcessorClient()
    return _default


def set_processor(client: ProcessorClient | None) -> None:
    """Override the process-wide client (tests, or pointing at another processor)."""
    global _default
    _default = client
