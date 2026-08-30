"""ClickHouse access layer.

Every value that reaches ClickHouse goes through server-side parameter
binding (`{name:Type}`). No user input is ever formatted into SQL text, so
the search form cannot be turned into a query injection. Column and
direction names in ORDER BY are the one thing that cannot be bound, so they
are matched against an allow-list instead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import clickhouse_connect
from clickhouse_connect.driver.client import Client

from ..config import Config

log = logging.getLogger(__name__)

COLUMNS = [
    "timestamp", "router_ip", "subscriber_id",
    "private_ip", "private_port", "public_ip", "public_port",
    "dest_ip", "dest_port", "protocol", "session_end_time",
]

SELECT_COLUMNS = COLUMNS + ["session_start_time"]

SORTABLE = {
    "timestamp", "router_ip", "subscriber_id", "private_ip", "private_port",
    "public_ip", "public_port", "dest_ip", "dest_port", "protocol",
}

# Equality filters: form field -> (column, ClickHouse parameter type)
EXACT_FILTERS = {
    "router_ip": ("router_ip", "IPv4"),
    "private_ip": ("private_ip", "IPv4"),
    "public_ip": ("public_ip", "IPv4"),
    "dest_ip": ("dest_ip", "IPv4"),
    "private_port": ("private_port", "UInt16"),
    "public_port": ("public_port", "UInt16"),
    "dest_port": ("dest_port", "UInt16"),
    "protocol": ("protocol", "String"),
}


def build_client(cfg: Config, database: Optional[str] = None) -> Client:
    return clickhouse_connect.get_client(
        host=cfg.clickhouse.host,
        port=cfg.clickhouse.port,
        username=cfg.clickhouse.user,
        password=cfg.clickhouse.password,
        database=database if database is not None else cfg.clickhouse.database,
        secure=cfg.clickhouse.secure,
        connect_timeout=10,
        send_receive_timeout=cfg.clickhouse.insert_timeout_seconds,
        compress="lz4",
    )


@dataclass
class SearchQuery:
    """One search request from the UI, already type-coerced by the API layer."""

    time_from: datetime
    time_to: datetime
    exact: Dict[str, Any] = field(default_factory=dict)
    subscriber_id: Optional[str] = None
    subscriber_partial: bool = False
    limit: int = 100
    offset: int = 0
    order_by: str = "timestamp"
    descending: bool = True


@dataclass
class SearchResult:
    rows: List[Dict[str, Any]]
    has_more: bool
    elapsed_ms: int
    rows_read: int


class ClickHouseService:
    def __init__(self, cfg: Config, client: Optional[Client] = None):
        self.cfg = cfg
        self._client = client
        self.table = f"{cfg.clickhouse.database}.{cfg.clickhouse.table}"

    @property
    def client(self) -> Client:
        if self._client is None:
            self._client = build_client(self.cfg)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None

    # -- ingest ------------------------------------------------------------
    def insert_rows(self, rows: List[list]) -> None:
        """Batch insert. `rows` is row-oriented and column-aligned to COLUMNS."""
        self.client.insert(
            table=self.cfg.clickhouse.table,
            data=rows,
            column_names=COLUMNS,
            database=self.cfg.clickhouse.database,
            settings={
                "async_insert": 0,
                "insert_deduplicate": 0,
                "max_insert_threads": 4,
            },
        )

    # -- search ------------------------------------------------------------
    def _where(self, q: SearchQuery) -> Tuple[List[str], Dict[str, Any]]:
        clauses = ["timestamp >= {time_from:DateTime}", "timestamp <= {time_to:DateTime}"]
        params: Dict[str, Any] = {"time_from": q.time_from, "time_to": q.time_to}

        for name, value in q.exact.items():
            if value in (None, ""):
                continue
            column, ch_type = EXACT_FILTERS[name]
            clauses.append(f"{column} = {{{name}:{ch_type}}}")
            params[name] = value

        if q.subscriber_id:
            if q.subscriber_partial:
                # positionCaseInsensitive avoids the LIKE-escaping footgun
                # entirely: the needle is a bound parameter, never SQL text.
                clauses.append("positionCaseInsensitive(subscriber_id, {subscriber_id:String}) > 0")
            else:
                clauses.append("subscriber_id = {subscriber_id:String}")
            params["subscriber_id"] = q.subscriber_id

        return clauses, params

    def search(self, q: SearchQuery) -> SearchResult:
        order_column = q.order_by if q.order_by in SORTABLE else "timestamp"
        direction = "DESC" if q.descending else "ASC"
        clauses, params = self._where(q)

        # Fetch one extra row to tell the UI whether another page exists,
        # without paying for a COUNT over the whole range.
        params["limit"] = min(q.limit, self.cfg.clickhouse.max_result_rows) + 1
        params["offset"] = q.offset

        sql = (
            f"SELECT {', '.join(SELECT_COLUMNS)} FROM {self.table} "
            f"WHERE {' AND '.join(clauses)} "
            f"ORDER BY {order_column} {direction} "
            f"LIMIT {{limit:UInt32}} OFFSET {{offset:UInt32}}"
        )
        result = self.client.query(
            sql,
            parameters=params,
            settings={
                "max_execution_time": self.cfg.clickhouse.max_execution_time,
                "timeout_overflow_mode": "throw",
                "readonly": 1,
            },
        )
        rows = [dict(zip(result.column_names, r)) for r in result.result_rows]
        has_more = len(rows) > q.limit
        if has_more:
            rows = rows[: q.limit]
        summary = result.summary or {}
        return SearchResult(
            rows=rows,
            has_more=has_more,
            elapsed_ms=int(float(summary.get("elapsed_ns", 0)) / 1e6),
            rows_read=int(summary.get("read_rows", 0)),
        )

    def count(self, q: SearchQuery) -> int:
        """Exact count. Separate endpoint because it is the expensive part."""
        clauses, params = self._where(q)
        sql = f"SELECT count() FROM {self.table} WHERE {' AND '.join(clauses)}"
        result = self.client.query(
            sql,
            parameters=params,
            settings={"max_execution_time": self.cfg.clickhouse.max_execution_time, "readonly": 1},
        )
        return int(result.result_rows[0][0])

    # -- housekeeping ------------------------------------------------------
    def storage_stats(self) -> Dict[str, Any]:
        sql = """
            SELECT
                sum(rows)                                    AS rows,
                sum(data_compressed_bytes)                   AS compressed,
                sum(data_uncompressed_bytes)                 AS uncompressed,
                count()                                      AS parts,
                min(min_time)                                AS oldest,
                max(max_time)                                AS newest
            FROM system.parts
            WHERE database = {db:String} AND table = {tbl:String} AND active
        """
        row = self.client.query(
            sql,
            parameters={"db": self.cfg.clickhouse.database, "tbl": self.cfg.clickhouse.table},
        ).result_rows[0]
        rows, compressed, uncompressed, parts, oldest, newest = row
        ratio = round(uncompressed / compressed, 2) if compressed else 0.0
        return {
            "rows": int(rows or 0),
            "compressed_bytes": int(compressed or 0),
            "uncompressed_bytes": int(uncompressed or 0),
            "compression_ratio": ratio,
            "bytes_per_row": round((compressed or 0) / rows, 2) if rows else 0.0,
            "parts": int(parts or 0),
            "oldest_event": oldest,
            "newest_event": newest,
        }

    def partitions(self) -> List[Dict[str, Any]]:
        sql = """
            SELECT partition,
                   sum(rows)                  AS rows,
                   sum(data_compressed_bytes) AS compressed,
                   min(min_time)              AS oldest,
                   max(max_time)              AS newest
            FROM system.parts
            WHERE database = {db:String} AND table = {tbl:String} AND active
            GROUP BY partition ORDER BY partition DESC
        """
        res = self.client.query(
            sql,
            parameters={"db": self.cfg.clickhouse.database, "tbl": self.cfg.clickhouse.table},
        )
        return [dict(zip(res.column_names, r)) for r in res.result_rows]

    def apply_retention(self, hot_days: int, retention_months: int) -> None:
        self.client.command(
            f"ALTER TABLE {self.table} MODIFY TTL "
            f"timestamp + INTERVAL {int(hot_days)} DAY RECOMPRESS CODEC(ZSTD(9)), "
            f"timestamp + INTERVAL {int(retention_months)} MONTH DELETE"
        )

    def ping(self) -> bool:
        try:
            return self.client.query("SELECT 1").result_rows[0][0] == 1
        except Exception:
            return False


def render_schema(path: Path, database: str, hot_days: int, retention_months: int) -> List[str]:
    """Load schema.sql, substitute placeholders, split into statements."""
    text = (
        path.read_text()
        .replace("{{DATABASE}}", database)
        .replace("{{HOT_DAYS}}", str(int(hot_days)))
        .replace("{{RETENTION_MONTHS}}", str(int(retention_months)))
    )
    statements = []
    for chunk in text.split(";"):
        lines = [ln for ln in chunk.splitlines() if not ln.strip().startswith("--")]
        stmt = "\n".join(lines).strip()
        if stmt:
            statements.append(stmt)
    return statements
