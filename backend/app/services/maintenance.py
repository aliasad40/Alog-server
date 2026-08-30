"""Scheduled maintenance: monthly archiving and queue hygiene.

Run from a systemd timer, not as a daemon: it does a bounded amount of work
and exits, so a hung archive cannot silently stop happening.

Archiving exports a closed month to a compressed ClickHouse Native file. Native
format is used rather than CSV because it round-trips types exactly and is
what `clickhouse-client` can restore from directly, and because it is already
compact before zstd touches it.

Ingestion is never paused. Reading a partition takes no locks that block
inserts, and the current month is never archived -- only months that can no
longer receive new rows.
"""

from __future__ import annotations

import gzip
import logging
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import List, Optional, Tuple

from ..config import Config, get_config
from ..database.clickhouse import ClickHouseService
from ..database.meta import MetadataStore
from ..logging_setup import setup_logging

log = logging.getLogger(__name__)


def closed_partitions(service: ClickHouseService, meta: MetadataStore) -> List[str]:
    """Months that exist, are finished, and have not been archived yet."""
    today = date.today()
    current = f"{today.year}{today.month:02d}"
    done = set(meta.archived_partitions())
    return [
        p["partition"] for p in service.partitions()
        if p["partition"] < current and p["partition"] not in done
    ]


def archive_partition(cfg: Config, service: ClickHouseService, meta: MetadataStore,
                      partition: str) -> Tuple[bool, str]:
    archive_dir = Path(cfg.retention.archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archive_dir / f"{cfg.clickhouse.table}-{partition}.native.gz"
    run_id = meta.start_archive(partition)

    try:
        table = f"{cfg.clickhouse.database}.{cfg.clickhouse.table}"
        expected = service.client.query(
            f"SELECT count() FROM {table} WHERE toYYYYMM(timestamp) = {{p:UInt32}}",
            parameters={"p": int(partition)},
        ).result_rows[0][0]

        if expected == 0:
            meta.finish_archive(run_id, "skipped", message="Partition is empty")
            return True, "empty"

        tmp = target.with_suffix(".tmp")
        # Stream straight from the driver to a gzip file; a month of NAT logs
        # will not fit in memory and must never be materialised there.
        with gzip.open(tmp, "wb", compresslevel=6) as fh:
            with service.client.raw_stream(
                f"SELECT * FROM {table} WHERE toYYYYMM(timestamp) = {{p:UInt32}} FORMAT Native",
                parameters={"p": int(partition)},
            ) as stream:
                for chunk in stream:
                    fh.write(chunk)
        tmp.replace(target)
        size = target.stat().st_size
        os.chmod(target, 0o640)

        if size < 64:
            raise RuntimeError(f"archive file is implausibly small ({size} bytes)")

        meta.finish_archive(run_id, "success", rows=int(expected), size=size, path=str(target))
        log.info("archived partition %s: %d rows -> %s (%.1f MiB)",
                 partition, expected, target, size / 1048576)

        if cfg.retention.drop_after_archive:
            service.client.command(f"ALTER TABLE {table} DROP PARTITION {{p:String}}",
                                   parameters={"p": partition})
            log.warning("dropped partition %s from ClickHouse after archiving", partition)
        return True, "archived"

    except Exception as exc:
        log.error("archive of %s failed: %s", partition, exc)
        meta.finish_archive(run_id, "failed", message=str(exc))
        try:
            target.with_suffix(".tmp").unlink(missing_ok=True)
        except OSError:
            pass
        return False, str(exc)


def prune_archives(cfg: Config) -> int:
    """Delete exported files past the retention window."""
    archive_dir = Path(cfg.retention.archive_dir)
    if not archive_dir.is_dir():
        return 0
    today = date.today()
    months = today.year * 12 + today.month - cfg.retention.retention_months
    cutoff = f"{months // 12}{months % 12 + 1:02d}"
    removed = 0
    for path in archive_dir.glob(f"{cfg.clickhouse.table}-*.native.gz"):
        stem = path.name.split("-")[-1].split(".")[0]
        if stem.isdigit() and stem < cutoff:
            path.unlink(missing_ok=True)
            log.info("pruned expired archive %s", path.name)
            removed += 1
    return removed


def recover_orphaned_claims(cfg: Config) -> int:
    """Return in-flight batches belonging to dead workers to the queue.

    A worker that is SIGKILLed leaves its claim behind. On restart it
    recovers its own, but if that worker never comes back (scaled down,
    host replaced) the records would sit there forever.
    """
    import asyncio

    from ..queue.redis_queue import LogQueue, build_client
    from ..services.worker import INFLIGHT_PREFIX

    async def _run() -> int:
        client = build_client(cfg)
        try:
            queue = LogQueue(client, cfg)
            live = _live_worker_ids()
            recovered = 0
            async for key in client.scan_iter(match=f"{INFLIGHT_PREFIX}*", count=100):
                name = key.decode() if isinstance(key, bytes) else key
                worker_id = name[len(INFLIGHT_PREFIX):]
                if worker_id in live:
                    continue
                n = await queue.release(name)
                if n:
                    log.warning("recovered %d records from dead worker %s", n, worker_id)
                    recovered += n
            return recovered
        finally:
            await client.aclose()

    return asyncio.run(_run())


def _live_worker_ids() -> set:
    """Worker ids are hostname-pid; a pid that no longer exists is dead."""
    import socket

    host = socket.gethostname()
    live = set()
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            live.add(f"{host}-{entry}")
    return live


def run(cfg: Optional[Config] = None) -> int:
    cfg = cfg or get_config()
    setup_logging(cfg, "maintenance")
    meta = MetadataStore(cfg.paths.metadata_db)
    service = ClickHouseService(cfg)

    if not service.ping():
        log.error("ClickHouse unreachable; skipping this maintenance run")
        return 1

    try:
        recover_orphaned_claims(cfg)
    except Exception as exc:
        log.warning("orphan recovery failed: %s", exc)

    failures = 0
    if cfg.retention.archive_enabled:
        for partition in closed_partitions(service, meta):
            ok, _ = archive_partition(cfg, service, meta, partition)
            failures += 0 if ok else 1
        try:
            prune_archives(cfg)
        except Exception as exc:
            log.warning("archive pruning failed: %s", exc)
    else:
        log.info("archiving is disabled in configuration")

    service.close()
    return 1 if failures else 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
