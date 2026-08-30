"""Configuration loading and validation.

Two files, on purpose:

  /etc/network-log-server/log-server.yaml   tunables, 0644, safe to read
  /etc/network-log-server/.env              secrets, 0640 root:netlog, never in git

Everything is validated at startup with pydantic. A service that cannot build
a valid Config exits non-zero rather than starting up half-configured -- a
receiver running with a silently-wrong batch size is worse than one that
refuses to boot.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import List, Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator

DEFAULT_CONFIG_DIR = Path(os.environ.get("NLS_CONFIG_DIR", "/etc/network-log-server"))


class ReceiverConfig(BaseModel):
    udp_enabled: bool = True
    tcp_enabled: bool = True
    bind_address: str = "0.0.0.0"
    udp_port: int = Field(514, ge=1, le=65535)
    tcp_port: int = Field(514, ge=1, le=65535)
    # Kernel-load-balanced receiver processes (SO_REUSEPORT). 0 = auto (cpu/2).
    workers: int = Field(0, ge=0, le=64)
    # Socket receive buffer. The single most important tuning knob for UDP
    # syslog: too small and the kernel drops bursts before Python ever sees them.
    so_rcvbuf: int = Field(16 * 1024 * 1024, ge=65536)
    max_datagram: int = Field(4096, ge=512, le=65535)
    # Push to Redis when either threshold is hit.
    push_batch: int = Field(500, ge=1, le=50000)
    push_interval_ms: int = Field(200, ge=10, le=5000)
    # Refresh interval for the authorised-router set held in memory.
    router_refresh_seconds: int = Field(15, ge=1, le=3600)


class ParserConfig(BaseModel):
    require_nat: bool = True
    subscriber_from_interface: bool = False


class RedisConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(6379, ge=1, le=65535)
    db: int = Field(0, ge=0, le=15)
    password: Optional[str] = None
    socket_path: Optional[str] = None
    queue_key: str = "nls:queue"
    # Backpressure. When the queue exceeds this, new logs are dropped and
    # counted rather than pushing Redis into OOM and losing everything.
    max_queue_length: int = Field(5_000_000, ge=1000)


class ClickHouseConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(8123, ge=1, le=65535)
    user: str = "netlog"
    password: str = ""
    database: str = "network_logs"
    table: str = "nat_logs"
    secure: bool = False
    # Insert when either threshold is hit. Large batches are the whole point
    # of the Redis buffer; ClickHouse hates small frequent inserts.
    batch_size: int = Field(20000, ge=100, le=1_000_000)
    batch_interval_ms: int = Field(1000, ge=50, le=60000)
    insert_timeout_seconds: int = Field(60, ge=5, le=600)
    # Query guard rails for the search UI.
    max_execution_time: int = Field(30, ge=1, le=600)
    max_result_rows: int = Field(10000, ge=100, le=1_000_000)


class RetentionConfig(BaseModel):
    # Days before a partition is recompressed at a higher ZSTD level.
    hot_days: int = Field(30, ge=1, le=3650)
    # Months of total retention before rows are dropped.
    retention_months: int = Field(12, ge=1, le=120)
    archive_enabled: bool = True
    archive_dir: str = "/var/backups/network-log-server/archive"
    # Drop the partition from ClickHouse once it has been exported.
    drop_after_archive: bool = False


class AuthConfig(BaseModel):
    session_hours: int = Field(12, ge=1, le=720)
    cookie_name: str = "nls_session"
    cookie_secure: bool = False  # set true once HTTPS is configured
    max_failed_logins: int = Field(5, ge=1, le=100)
    lockout_seconds: int = Field(300, ge=10, le=86400)


class ServerConfig(BaseModel):
    bind: str = "127.0.0.1"
    port: int = Field(8088, ge=1, le=65535)
    workers: int = Field(2, ge=1, le=32)


class Paths(BaseModel):
    data_dir: str = "/var/lib/network-log-server"
    log_dir: str = "/var/log/network-log-server"
    metadata_db: str = "/var/lib/network-log-server/metadata.sqlite3"
    logo_dir: str = "/var/lib/network-log-server/branding"


class Config(BaseModel):
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    server: ServerConfig = ServerConfig()
    receiver: ReceiverConfig = ReceiverConfig()
    parser: ParserConfig = ParserConfig()
    redis: RedisConfig = RedisConfig()
    clickhouse: ClickHouseConfig = ClickHouseConfig()
    retention: RetentionConfig = RetentionConfig()
    auth: AuthConfig = AuthConfig()
    paths: Paths = Paths()
    secret_key: str = ""

    @field_validator("secret_key")
    @classmethod
    def _secret_required(cls, v: str) -> str:
        if len(v) < 32:
            raise ValueError(
                "NLS_SECRET_KEY must be at least 32 characters. "
                "install.bash generates one; do not run with a default."
            )
        return v


def _load_env_file(path: Path) -> None:
    """Minimal .env reader. Existing environment variables win."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@lru_cache(maxsize=1)
def get_config(config_dir: Optional[str] = None) -> Config:
    base = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
    _load_env_file(base / ".env")

    data = {}
    yaml_path = base / "log-server.yaml"
    if yaml_path.is_file():
        data = yaml.safe_load(yaml_path.read_text()) or {}

    # Secrets always come from the environment, never from the YAML file.
    data["secret_key"] = os.environ.get("NLS_SECRET_KEY", data.get("secret_key", ""))
    ch = data.setdefault("clickhouse", {})
    ch["password"] = os.environ.get("NLS_CLICKHOUSE_PASSWORD", ch.get("password", ""))
    rd = data.setdefault("redis", {})
    if os.environ.get("NLS_REDIS_PASSWORD"):
        rd["password"] = os.environ["NLS_REDIS_PASSWORD"]

    return Config(**data)


def reset_config_cache() -> None:
    get_config.cache_clear()
