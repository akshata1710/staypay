"""Background workers. One process per worker type; scale by running more replicas.

    python -m app.workers.run charge        # charge due installments (also retries)
    python -m app.workers.run sweeper       # re-drive stuck charges and refunds
    python -m app.workers.run inbox         # process stored webhook events
    python -m app.workers.run backfill      # pull missed events from the processor
    python -m app.workers.run cleanup       # delete expired idempotency keys
    python -m app.workers.run reconcile --date 2026-11-23
    add --once to run a single iteration (handy for debugging and cron jobs)

No queue system is needed: Postgres `SELECT ... FOR UPDATE SKIP LOCKED` lets many replicas share
the work without ever claiming the same row twice.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable
from datetime import date, timedelta

from prometheus_client import start_http_server

from app.clock import utcnow
from app.config import get_settings
from app.db.session import dispose_engine, get_sessionmaker
from app.domain import charging, cleanup, reconciliation, refunds, webhooks
from app.observability.logging import setup_logging
from app.processor.client import get_processor

log = logging.getLogger("worker")


async def charge_once() -> int:
    sm, proc = get_sessionmaker(), get_processor()
    ids = await charging.claim_due_installments(sm)
    sem = asyncio.Semaphore(10)

    async def run(cid):
        async with sem:
            await charging.execute_charge(sm, proc, cid)

    await asyncio.gather(*(run(c) for c in ids))
    return len(ids)


async def sweeper_once() -> int:
    sm, proc = get_sessionmaker(), get_processor()
    return await charging.recover_stuck_charges(sm, proc) + await refunds.recover_stuck_refunds(sm, proc)


async def inbox_once() -> int:
    return sum((await webhooks.process_inbox(get_sessionmaker())).values())


async def backfill_once() -> int:
    return await webhooks.backfill(get_sessionmaker(), get_processor(), since=utcnow() - timedelta(hours=48))


async def cleanup_once() -> int:
    return await cleanup.cleanup_idempotency_keys(get_sessionmaker())


WORKERS: dict[str, tuple[Callable[[], Awaitable[int]], float]] = {
    "charge": (charge_once, 5.0),
    "sweeper": (sweeper_once, 60.0),
    "inbox": (inbox_once, 1.0),
    "backfill": (backfill_once, 24 * 3600.0),
    "cleanup": (cleanup_once, 3600.0),
}


async def loop(name: str, once: bool) -> None:
    fn, interval = WORKERS[name]
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    log.info("worker.start", extra={"worker": name, "interval_s": interval})
    while not stop.is_set():
        n = 0
        try:
            n = await fn()
            if n:
                log.info("worker.tick", extra={"worker": name, "items": n})
        except Exception:  # keep the loop alive; the error is logged and alerted on
            log.exception("worker.error", extra={"worker": name})
        if once:
            break
        # Busy worker: go again right away. Idle worker: sleep (but wake up on shutdown).
        if not n:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass
    log.info("worker.stop", extra={"worker": name})


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("worker", choices=[*WORKERS, "reconcile"])
    p.add_argument("--once", action="store_true")
    p.add_argument("--date", type=date.fromisoformat, help="settlement date for reconcile (default yesterday)")
    p.add_argument("--metrics-port", type=int, default=0)
    args = p.parse_args()
    setup_logging(get_settings().log_level)
    if args.metrics_port:
        start_http_server(args.metrics_port)
    try:
        if args.worker == "reconcile":
            day = args.date or (utcnow().date() - timedelta(days=1))
            report = await reconciliation.reconcile(get_sessionmaker(), get_processor(), day)
            log.info("reconcile.report", extra={k: v for k, v in report.items() if k != "mismatches"})
        else:
            await loop(args.worker, args.once)
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
