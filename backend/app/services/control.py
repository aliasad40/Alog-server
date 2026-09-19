"""Controlled systemctl access for the web interface.

Security model
--------------
This module lets an authenticated administrator restart services from a
browser. That is a genuine privilege boundary, so it is built to be boring:

* **Unit names are an allow-list, not a parameter.** The HTTP layer passes a
  key; anything not in `UNITS` is rejected before a process is spawned. A
  user-supplied string never reaches the command line.
* **No shell, ever.** `subprocess.run` receives an argument list. There is no
  `shell=True` anywhere in this file, so quoting and metacharacters are
  meaningless.
* **sudo is scoped to exact commands.** install.bash writes a sudoers file
  containing one line per (action, unit) pair with the full argument vector
  spelled out and no wildcards. Even if this module were compromised, the
  `netlog` user cannot run any other command as root.
* **Reads need no privilege at all.** `is-active` and `show` work unprivileged,
  so the status page — the part that gets polled constantly — never touches
  sudo.

What this deliberately does not offer: arbitrary units, `daemon-reload`,
`enable`/`disable`, or anything that edits unit files. Those are deployment
operations and belong in a shell session, not a web form.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

SYSTEMCTL = shutil.which("systemctl") or "/usr/bin/systemctl"
SUDO = shutil.which("sudo") or "/usr/bin/sudo"

ACTIONS = ("restart", "start", "stop")


@dataclass(frozen=True)
class Unit:
    key: str
    unit: str
    label: str
    description: str
    # Consequence of restarting, shown in the UI before the operator commits.
    impact: str
    # Restarting this cuts the connection carrying the request itself.
    self_hosting: bool = False


UNITS: Dict[str, Unit] = {
    u.key: u for u in [
        Unit("receiver", "network-log-server-receiver", "Log receiver",
             "Accepts syslog on UDP/TCP 514, authorises routers, parses logs.",
             "Logs arriving during the restart are lost — routers do not retry UDP. "
             "Buffered records not yet pushed to Redis are also lost (at most a few hundred ms)."),
        Unit("worker", "network-log-server-worker", "Database writer",
             "Drains the Redis queue into ClickHouse in batches.",
             "Safe. The in-flight batch is returned to the queue and re-inserted "
             "on start. Nothing is lost; the queue grows briefly."),
        Unit("api", "network-log-server-api", "Web interface",
             "Serves this interface and the REST API.",
             "This page will disconnect for a few seconds. Ingestion is unaffected. "
             "Reload the page afterwards.",
             self_hosting=True),
        Unit("redis", "redis-server", "Redis queue",
             "Buffers parsed logs between the receiver and the database.",
             "Queued logs are restored from the append-only file, so at most one "
             "second of buffer is lost. The receiver drops logs until Redis is back."),
        Unit("clickhouse", "clickhouse-server", "ClickHouse database",
             "Stores and searches the log data.",
             "Searches fail until it is back. Ingestion is NOT lost — logs queue in "
             "Redis and drain when it returns. Restarting a busy instance can take "
             "a while."),
        Unit("nginx", "nginx", "Web server",
             "Reverse proxy in front of the web interface.",
             "This page will disconnect briefly. Ingestion is unaffected.",
             self_hosting=True),
    ]
}


@dataclass
class UnitStatus:
    key: str
    unit: str
    label: str
    description: str
    impact: str
    self_hosting: bool
    active: str          # active | inactive | failed | activating | unknown
    enabled: str         # enabled | disabled | static | unknown
    since: str
    memory_bytes: int
    pid: int


def _run(args: List[str], timeout: int = 20) -> subprocess.CompletedProcess:
    """Run a command with no shell. Never raises on a non-zero exit."""
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", "timed out")
    except FileNotFoundError:
        return subprocess.CompletedProcess(args, 127, "", "systemctl not found")


def _show(unit: str) -> Dict[str, str]:
    """Read unit properties. Needs no privilege."""
    proc = _run([SYSTEMCTL, "show", unit, "--no-page",
                 "--property=ActiveState,UnitFileState,ActiveEnterTimestamp,"
                 "MemoryCurrent,MainPID,SubState"])
    props: Dict[str, str] = {}
    for line in proc.stdout.splitlines():
        key, _, value = line.partition("=")
        if key:
            props[key] = value
    return props


def status(key: str) -> Optional[UnitStatus]:
    unit = UNITS.get(key)
    if unit is None:
        return None
    props = _show(unit.unit)

    memory = props.get("MemoryCurrent", "")
    try:
        # systemd reports the sentinel [not set] as 2^64-1 for units with no
        # accounting; treat anything implausible as unknown rather than
        # rendering 16 exabytes in the UI.
        memory_bytes = int(memory)
        if memory_bytes > (1 << 50):
            memory_bytes = 0
    except (TypeError, ValueError):
        memory_bytes = 0

    try:
        pid = int(props.get("MainPID", "0"))
    except (TypeError, ValueError):
        pid = 0

    return UnitStatus(
        key=unit.key,
        unit=unit.unit,
        label=unit.label,
        description=unit.description,
        impact=unit.impact,
        self_hosting=unit.self_hosting,
        active=props.get("ActiveState") or "unknown",
        enabled=props.get("UnitFileState") or "unknown",
        since=props.get("ActiveEnterTimestamp") or "",
        memory_bytes=memory_bytes,
        pid=pid,
    )


def status_all() -> List[dict]:
    return [vars(s) for s in (status(key) for key in UNITS) if s is not None]


def control(key: str, action: str) -> dict:
    """Apply an action to an allow-listed unit.

    `--no-block` is used for every action so the call returns before systemd
    finishes. That is what makes restarting the API or nginx possible at all:
    the reply is already on its way out before the process serving it dies.
    The exact argument vector here must match the sudoers file install.bash
    writes, or sudo refuses it.
    """
    unit = UNITS.get(key)
    if unit is None:
        raise ValueError(f"Unknown service: {key!r}")
    if action not in ACTIONS:
        raise ValueError(f"Unsupported action: {action!r}")

    args = [SUDO, "-n", SYSTEMCTL, "--no-block", action, unit.unit]
    log.warning("service control: %s %s", action, unit.unit)
    proc = _run(args)

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        if "a password is required" in detail.lower() or "sudo:" in detail.lower():
            detail = ("The sudoers rule for this command is missing. "
                      "Re-run install.bash and choose Repair.")
        log.error("service control failed: %s %s -> %s", action, unit.unit, detail)
        return {"ok": False, "detail": detail or "systemctl refused the request."}

    # --no-block returns immediately, so give systemd a moment before reporting
    # back. Anything slower than this shows up on the next status poll.
    time.sleep(1.5 if action != "stop" else 0.5)
    current = status(key)
    return {
        "ok": True,
        "detail": f"{unit.label}: {action} requested.",
        "status": vars(current) if current else None,
    }


def sudoers_lines(user: str = "netlog") -> List[str]:
    """The exact sudoers entries this module needs.

    Generated rather than hand-written so the file and the code can never
    drift apart. install.bash writes these through `visudo -c`.
    """
    lines = [
        "# Generated by network-log-server install.bash. Do not edit by hand.",
        "# Allows the web interface to restart its own services and their",
        "# dependencies -- and nothing else. Each line is a complete argument",
        "# vector; there are deliberately no wildcards.",
    ]
    for unit in UNITS.values():
        for action in ACTIONS:
            lines.append(
                f"{user} ALL=(root) NOPASSWD: {SYSTEMCTL} --no-block {action} {unit.unit}"
            )
    return lines
