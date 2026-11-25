"""FastAPI application entrypoint: `uvicorn app.main:app --reload`."""

import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response

from app.api.routes import router
from app.cache import close_redis
from app.config import get_settings
from app.db.session import dispose_engine
from app.observability.logging import request_id_var, setup_logging
from app.observability.metrics import HTTP_LATENCY


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(get_settings().log_level)
    yield
    await close_redis()
    await dispose_engine()


app = FastAPI(
    title="StayPay",
    version="1.0.0",
    description="Reliable payments for a short-term rental booking product. Money is in minor units (cents).",
    lifespan=lifespan,
)


@app.middleware("http")
async def observe(request: Request, call_next):
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    token = request_id_var.set(rid)
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = rid
        return response
    finally:
        route = request.scope.get("route")
        HTTP_LATENCY.labels(request.method, getattr(route, "path", "unmatched"), str(status)).observe(
            time.perf_counter() - start
        )
        request_id_var.reset(token)


@app.get("/metrics", include_in_schema=False)
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


app.include_router(router)
