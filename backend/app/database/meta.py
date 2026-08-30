"""Application metadata: users, routers, settings, archive history.

SQLite, not ClickHouse. These tables hold a few dozen rows, are mutated
interactively and need transactions and unique constraints -- everything
ClickHouse is bad at. Keeping them out of ClickHouse also means the log
database is strictly append-only, which is what makes it fast.
"""

from __future__ import annotations

import ipaddress
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 1,
    must_change    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    last_login    TEXT
);

CREATE TABLE IF NOT EXISTS routers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    ip_address  TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_routers_enabled ON routers(enabled);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS archive_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    partition    TEXT NOT NULL,
    status       TEXT NOT NULL,
    rows         INTEGER NOT NULL DEFAULT 0,
    bytes        INTEGER NOT NULL DEFAULT 0,
    path         TEXT NOT NULL DEFAULT '',
    message      TEXT NOT NULL DEFAULT '',
    started_at   TEXT NOT NULL,
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_archive_partition ON archive_runs(partition);
"""

DEFAULT_SETTINGS = {
    "company_name": "Network Log Server",
    "logo_filename": "",
    "hot_days": "30",
    "retention_months": "12",
}

_local = threading.local()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MetadataStore:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = getattr(_local, "conn", None)
        if conn is None or getattr(_local, "path", None) != self.path:
            conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            _local.conn = conn
            _local.path = self.path
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            for key, value in DEFAULT_SETTINGS.items():
                conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))
            conn.commit()

    # -- users -------------------------------------------------------------
    def create_user(self, username: str, password_hash: str, must_change: bool = True) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO users(username, password_hash, is_admin, must_change, created_at) "
                "VALUES (?, ?, 1, ?, ?)",
                (username, password_hash, int(must_change), _now()),
            )
            conn.commit()
            return int(cur.lastrowid)

    def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            return dict(row) if row else None

    def user_count(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT count(*) FROM users").fetchone()[0])

    def set_password(self, username: str, password_hash: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ?, must_change = 0 WHERE username = ?",
                (password_hash, username),
            )
            conn.commit()

    def touch_login(self, username: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE users SET last_login = ? WHERE username = ?", (_now(), username))
            conn.commit()

    # -- routers -----------------------------------------------------------
    @staticmethod
    def normalise_ip(value: str) -> str:
        """Reject anything that is not a plain IPv4 host address."""
        addr = ipaddress.ip_address(value.strip())
        if addr.version != 4:
            raise ValueError("only IPv4 router addresses are supported")
        return str(addr)

    def list_routers(self) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM routers ORDER BY name").fetchall()
            return [dict(r) for r in rows]

    def enabled_router_ips(self) -> List[str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT ip_address FROM routers WHERE enabled = 1").fetchall()
            return [r["ip_address"] for r in rows]

    def add_router(self, name: str, ip_address: str, description: str = "", enabled: bool = True) -> Dict[str, Any]:
        ip_address = self.normalise_ip(ip_address)
        now = _now()
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO routers(name, ip_address, description, enabled, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name.strip(), ip_address, description.strip(), int(enabled), now, now),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM routers WHERE id = ?", (cur.lastrowid,)).fetchone()
            return dict(row)

    def update_router(self, router_id: int, **fields) -> Optional[Dict[str, Any]]:
        allowed = {"name", "ip_address", "description", "enabled"}
        sets, values = [], []
        for key, value in fields.items():
            if key not in allowed or value is None:
                continue
            if key == "ip_address":
                value = self.normalise_ip(value)
            if key == "enabled":
                value = int(bool(value))
            if key in ("name", "description"):
                value = str(value).strip()
            sets.append(f"{key} = ?")
            values.append(value)
        if not sets:
            return self.get_router(router_id)
        sets.append("updated_at = ?")
        values.extend([_now(), router_id])
        with self.connect() as conn:
            conn.execute(f"UPDATE routers SET {', '.join(sets)} WHERE id = ?", values)
            conn.commit()
        return self.get_router(router_id)

    def get_router(self, router_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM routers WHERE id = ?", (router_id,)).fetchone()
            return dict(row) if row else None

    def delete_router(self, router_id: int) -> bool:
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM routers WHERE id = ?", (router_id,))
            conn.commit()
            return cur.rowcount > 0

    # -- settings ----------------------------------------------------------
    def get_setting(self, key: str, default: str = "") -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def all_settings(self) -> Dict[str, str]:
        with self.connect() as conn:
            return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
            conn.commit()

    # -- archive history ---------------------------------------------------
    def start_archive(self, partition: str) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO archive_runs(partition, status, started_at) VALUES (?, 'running', ?)",
                (partition, _now()),
            )
            conn.commit()
            return int(cur.lastrowid)

    def finish_archive(self, run_id: int, status: str, rows: int = 0, size: int = 0,
                       path: str = "", message: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE archive_runs SET status = ?, rows = ?, bytes = ?, path = ?, "
                "message = ?, finished_at = ? WHERE id = ?",
                (status, rows, size, path, message[:2000], _now(), run_id),
            )
            conn.commit()

    def archive_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM archive_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def archived_partitions(self) -> List[str]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT partition FROM archive_runs WHERE status = 'success'"
            ).fetchall()
            return [r["partition"] for r in rows]
