"""Redis helpers. Redis is an optimisation, never the source of truth: if it's down we fail OPEN
(serve from Postgres, skip rate limiting) and log, rather than failing payments."""

from __future__ import annotations

import json
import logging

import redis.asyncio as redis

from app.config import get_settings

log = logging.getLogger(__name__)
_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(get_settings().redis_url, socket_timeout=0.5, socket_connect_timeout=0.5)
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None


async def cache_get(key: str) -> dict | None:
    try:
        raw = await get_redis().get(key)
        return json.loads(raw) if raw else None
    except redis.RedisError as e:
        log.warning("cache.get_failed", extra={"err": str(e)})
        return None


async def cache_set(key: str, value: dict, ttl_s: int) -> None:
    try:
        await get_redis().set(key, json.dumps(value, default=str), ex=ttl_s)
    except redis.RedisError as e:
        log.warning("cache.set_failed", extra={"err": str(e)})


async def rate_limited(bucket: str, member: str, limit: int, window_s: int = 60) -> bool:
    """Fixed-window limit on DISTINCT members (e.g. idempotency keys) per bucket.

    Counting distinct keys, not requests, matters: a client retrying the same Idempotency-Key
    after a timeout is doing the right thing and must never be told 429 for it."""
    try:
        r = get_redis()
        key = f"rl:{bucket}"
        async with r.pipeline(transaction=True) as p:
            p.sadd(key, member)
            p.expire(key, window_s, nx=True)
            p.scard(key)
            _, _, count = await p.execute()
        if count <= limit:
            return False
        await r.srem(key, member)  # don't let a rejected key take a slot
        return True
    except redis.RedisError as e:
        log.warning("ratelimit.redis_down", extra={"err": str(e)})
        return False
