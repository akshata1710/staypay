"""Test harness.

Unit tests need nothing. Integration and chaos tests need Postgres + Redis (`make up` or the
devcontainer starts them). Everything else runs in-process: StayPay and MockPay are both ASGI
apps wired to each other with httpx.ASGITransport, so webhooks really travel MockPay -> StayPay.
"""

from __future__ import annotations

import os

# Tests TRUNCATE every table, so they must never point at your dev database. They always use
# STAYPAY_TEST_DATABASE_URL (never STAYPAY_DATABASE_URL from .env) and refuse a non-test DB name.
TEST_DB = os.getenv("STAYPAY_TEST_DATABASE_URL", "postgresql+asyncpg://staypay:staypay@localhost:5432/staypay_test")
assert TEST_DB.rsplit("/", 1)[-1].endswith("_test"), f"refusing to run tests against {TEST_DB}"
os.environ["STAYPAY_DATABASE_URL"] = TEST_DB
os.environ["STAYPAY_REDIS_URL"] = os.getenv("STAYPAY_TEST_REDIS_URL", "redis://localhost:6379/15")
os.environ["STAYPAY_ENV"] = "test"
os.environ.setdefault("STAYPAY_PROCESSOR_TIMEOUT_S", "1.0")
os.environ.setdefault("STAYPAY_LOG_LEVEL", "WARNING")
os.environ.setdefault("MOCKPAY_WEBHOOK_RETRY_BACKOFF_S", "0.01")

import uuid  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from datetime import date, timedelta  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.cache import close_redis, get_redis  # noqa: E402
from app.clock import utcnow  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.models import Base  # noqa: E402
from app.db.session import dispose_engine, get_engine, get_sessionmaker, init_engine  # noqa: E402
from app.domain import webhooks  # noqa: E402
from app.main import app as staypay_app  # noqa: E402
from app.processor.client import ProcessorClient, set_processor  # noqa: E402
from mockpay.app import MockPayState, create_app  # noqa: E402


@pytest.fixture(scope="session")
async def database():
    init_engine(get_settings().database_url)
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    await close_redis()
    await dispose_engine()


@pytest.fixture
async def clean_db(database):
    tables = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
    async with get_engine().begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    await get_redis().flushdb()
    yield


@dataclass
class Harness:
    api: httpx.AsyncClient
    mockpay: MockPayState
    mockpay_admin: httpx.AsyncClient
    processor: ProcessorClient

    @property
    def sm(self):
        return get_sessionmaker()

    async def checkout(
        self,
        *,
        key: str | None = None,
        pm: str = "pm_card_ok",
        installments: int = 1,
        total: int = 100_000,
        policy: str = "moderate",
        days_out: int = 60,
        guest: str | None = None,
    ) -> httpx.Response:
        body = {
            "guest_id": guest or f"guest_{uuid.uuid4().hex[:8]}",
            "listing_id": "listing_1",
            "check_in": (date.today() + timedelta(days=days_out)).isoformat(),
            "total_amount": total,
            "currency": "USD",
            "installments": installments,
            "cancellation_policy": policy,
            "payment_method": pm,
        }
        return await self.api.post("/checkout", json=body, headers={"Idempotency-Key": key or uuid.uuid4().hex})

    async def faults(self, **kw) -> None:
        r = await self.mockpay_admin.post("/admin/faults", json=kw)
        r.raise_for_status()

    async def settle_webhooks(self) -> dict[str, int]:
        """Deliver every in-flight webhook, then process the inbox until it's empty."""
        await self.mockpay.drain()
        totals: dict[str, int] = {}
        for i in range(10):
            # Step the clock forward each pass so deferred ("too_early") events become due again.
            counts = await webhooks.process_inbox(self.sm, now=utcnow() + timedelta(hours=1 + i))
            if not counts:
                break
            for k, v in counts.items():
                totals[k] = totals.get(k, 0) + v
        return totals

    async def scalar(self, sql: str, **params):
        async with self.sm() as s:
            return (await s.execute(text(sql), params)).scalar()

    async def rows(self, sql: str, **params) -> list[tuple]:
        async with self.sm() as s:
            return [tuple(r) for r in (await s.execute(text(sql), params)).all()]


@pytest.fixture
async def h(clean_db):
    staypay_transport = httpx.ASGITransport(app=staypay_app)
    webhook_client = httpx.AsyncClient(transport=staypay_transport, base_url="http://staypay")
    mp_app = create_app(
        webhook_url="http://staypay/webhooks/mockpay",
        webhook_secret=get_settings().webhook_secret,
        webhook_client=webhook_client,
    )
    mp_transport = httpx.ASGITransport(app=mp_app)
    processor = ProcessorClient(base_url="http://mockpay", transport=mp_transport)
    set_processor(processor)
    api = httpx.AsyncClient(transport=staypay_transport, base_url="http://staypay", timeout=30)
    mp_admin = httpx.AsyncClient(transport=mp_transport, base_url="http://mockpay")
    harness = Harness(api=api, mockpay=mp_app.state.mockpay, mockpay_admin=mp_admin, processor=processor)
    yield harness
    await harness.mockpay.drain()
    for c in (api, mp_admin, webhook_client):
        await c.aclose()
    await processor.aclose()
    set_processor(None)
