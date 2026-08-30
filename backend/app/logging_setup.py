"""Structured logging for all services.

Journald captures stdout/stderr from systemd units, so we log to stdout and
let the journal handle rotation. A file handler is added as well because ISP
operations teams usually want a path they can tail without knowing journalctl.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from .config import Config

FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(cfg: Config, component: str) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, cfg.log_level))
    root.handlers.clear()

    formatter = logging.Formatter(FORMAT, DATEFMT)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    try:
        log_dir = Path(cfg.paths.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / f"{component}.log", maxBytes=32 * 1024 * 1024, backupCount=5
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as exc:
        root.warning("file logging disabled (%s)", exc)

    # These are chatty at DEBUG and tell us nothing we want.
    for noisy in ("urllib3", "clickhouse_connect", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
