"""System status, health and archive visibility."""

from __future__ import annotations

import logging
import os
import shutil
import time

import psutil
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool

from .. import metrics as M
from ..services.receiver import ROUTER_SET_KEY
from .deps import CurrentUser, SameOrigin, get_cfg, get_ch, get_meta, get_redis

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["system"])

_rates = M.RateTracker()


@router.get("/health")
async def health(request: Request):
    """Unauthenticated liveness probe for nginx, systemd and monitoring.
    Reports component reachability only -- no counts, no configuration."""
    redis_ok = False
    try:
        await get_redis(request).ping()
        redis_ok = True
    except Exception:
        pass
    ch_ok = await run_in_threadpool(get_ch(request).ping)
    healthy = redis_ok and ch_ok
    body = {"status": "ok" if healthy else "degraded", "redis": redis_ok, "clickhouse": ch_ok}
    if not healthy:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, body)
    return body


@router.get("/system/status")
async def system_status(request: Request, _user: str = CurrentUser):
    cfg = get_cfg(request)
    redis_client = get_redis(request)
    meta = get_meta(request)
    service = get_ch(request)

    counters = await M.read_all(redis_client)
    gauges = await M.read_gauges(redis_client)

    queue_length, redis_ok, redis_memory = -1, False, 0
    try:
        queue_length = int(await redis_client.llen(cfg.redis.queue_key))
        info = await redis_client.info("memory")
        redis_memory = int(info.get("used_memory", 0))
        redis_ok = True
    except Exception:
        pass

    inflight = 0
    try:
        async for key in redis_client.scan_iter(match="nls:inflight:*", count=50):
            inflight += int(await redis_client.llen(key))
    except Exception:
        pass

    storage, ch_ok = {}, False
    try:
        storage = await run_in_threadpool(service.storage_stats)
        ch_ok = True
    except Exception as exc:
        log.debug("storage stats unavailable: %s", exc)
        service.close()

    usage = shutil.disk_usage(cfg.paths.data_dir if os.path.isdir(cfg.paths.data_dir) else "/")
    load1, load5, load15 = os.getloadavg()

    received = counters[M.RECEIVED]
    parsed = counters[M.PARSED]
    dropped = (counters[M.DROPPED_UNKNOWN_ROUTER] + counters[M.DROPPED_BACKPRESSURE]
               + counters[M.DROPPED_REDIS_ERROR])

    try:
        authorised = int(await redis_client.scard(ROUTER_SET_KEY))
    except Exception:
        authorised = len(meta.enabled_router_ips())

    return {
        "generated_at": int(time.time()),
        "ingest": {
            "received": received,
            "parsed": parsed,
            "stored": counters[M.STORED],
            "dropped_total": dropped,
            "unknown_router": counters[M.DROPPED_UNKNOWN_ROUTER],
            "dropped_backpressure": counters[M.DROPPED_BACKPRESSURE],
            "dropped_redis_error": counters[M.DROPPED_REDIS_ERROR],
            "parser_errors": counters[M.PARSER_ERRORS],
            "bytes_received": counters[M.BYTES_RECEIVED],
            "received_per_sec": _rates.rate("received", received),
            "stored_per_sec": _rates.rate("stored", counters[M.STORED]),
            "parse_success_pct": round(parsed / received * 100, 2) if received else 0.0,
        },
        "queue": {
            "length": queue_length,
            "in_flight": inflight,
            "max_length": cfg.redis.max_queue_length,
            "redis_memory_bytes": redis_memory,
            "healthy": redis_ok,
        },
        "database": {
            "healthy": ch_ok,
            "insert_errors": counters[M.CH_INSERT_ERRORS],
            "insert_batches": counters[M.CH_INSERT_BATCHES],
            "last_insert_ms": gauges.get("last_insert_ms", 0),
            "last_insert_at": int(gauges.get("last_insert_at", 0)),
            **storage,
        },
        "host": {
            "cpu_percent": psutil.cpu_percent(interval=None),
            "memory_percent": psutil.virtual_memory().percent,
            "disk_total": usage.total,
            "disk_used": usage.used,
            "disk_free": usage.free,
            "disk_percent": round(usage.used / usage.total * 100, 1),
            "load": [round(load1, 2), round(load5, 2), round(load15, 2)],
        },
        "routers": {"authorised": authorised, "configured": len(meta.list_routers())},
    }


@router.get("/system/partitions")
async def partitions(request: Request, _user: str = CurrentUser):
    service = get_ch(request)
    try:
        rows = await run_in_threadpool(service.partitions)
    except Exception:
        service.close()
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "ClickHouse did not answer.")
    archived = set(get_meta(request).archived_partitions())
    for row in rows:
        row["archived"] = row["partition"] in archived
        for key in ("oldest", "newest"):
            if row.get(key) is not None:
                row[key] = str(row[key])
    return rows


@router.get("/system/archives")
async def archives(request: Request, _user: str = CurrentUser):
    return get_meta(request).archive_history()


@router.post("/system/metrics/reset")
async def reset_metrics(request: Request, _user: str = CurrentUser, _: None = SameOrigin):
    try:
        await get_redis(request).delete(M.METRICS_KEY)
    except Exception:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Redis did not answer.")
    return {"status": "Counters reset"}
