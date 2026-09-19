"""Timezone handling.

The problem this solves
----------------------
ClickHouse stores `DateTime` as epoch seconds. The timezone is only metadata
for parsing and formatting, and clickhouse-connect returns timezone-aware
datetimes, which is why timestamps rendered as `2026-08-30 18:33:17+00:00`.
Stripping the `+00:00` alone would be worse than leaving it: the operator
would see a UTC clock labelled as if it were local time, and a lawful-intercept
response citing the wrong hour is a serious error.

So the rule here is: **store UTC, display in the configured zone, and interpret
operator input in that same zone.** All three have to move together, which is
why they live in one module rather than being sprinkled through the API.

`display_timezone` is an IANA name (`Asia/Karachi`, `Europe/London`) held in
the settings table, so it survives restarts and is changeable from the UI.
Fixed offsets are deliberately not supported: they get daylight saving wrong,
and a log platform that shifts by an hour twice a year is worse than useless.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

UTC = timezone.utc
DEFAULT_TIMEZONE = "UTC"

# Offered in the settings dropdown. Any valid IANA name is accepted by the API;
# this is just a shortlist so the common cases need no typing.
COMMON_TIMEZONES = [
    "UTC",
    "Asia/Karachi", "Asia/Kolkata", "Asia/Dhaka", "Asia/Dubai", "Asia/Riyadh",
    "Asia/Tehran", "Asia/Kabul", "Asia/Tashkent", "Asia/Shanghai", "Asia/Tokyo",
    "Asia/Singapore", "Asia/Jakarta", "Asia/Manila",
    "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Moscow",
    "Europe/Istanbul",
    "Africa/Cairo", "Africa/Lagos", "Africa/Nairobi", "Africa/Johannesburg",
    "America/New_York", "America/Chicago", "America/Denver",
    "America/Los_Angeles", "America/Sao_Paulo",
    "Australia/Sydney", "Pacific/Auckland",
]


def resolve(name: Optional[str]) -> ZoneInfo:
    """Return a ZoneInfo, falling back to UTC rather than raising.

    A bad value in the settings table must not take the search page down, so
    this is forgiving at read time. Validation happens at write time, in
    `is_valid`, where the operator can still be told about it.
    """
    if not name:
        return ZoneInfo(DEFAULT_TIMEZONE)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        log.warning("unknown timezone %r in settings; falling back to UTC", name)
        return ZoneInfo(DEFAULT_TIMEZONE)


def is_valid(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return False


def to_utc(value: datetime, tz: ZoneInfo) -> datetime:
    """Interpret operator input as wall-clock time in `tz` and return UTC.

    The browser sends `2026-08-30T18:00:00` with no offset — that is what the
    operator typed on their own clock. Attaching `tz` to it is what makes a
    search for "18:00 Karachi time" actually find 13:00 UTC rows.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=tz)
    return value.astimezone(UTC)


def format_display(value: Optional[datetime], tz: ZoneInfo) -> Optional[str]:
    """Render a stored timestamp on the operator's clock, with no offset suffix.

    A naive value is assumed to be UTC. That assumption is safe because the
    worker inserts UTC-aware datetimes and the column is pinned to UTC; it
    only applies to rows written before that was true.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")


def offset_label(tz: ZoneInfo, at: Optional[datetime] = None) -> str:
    """Human-readable current offset, e.g. "UTC+05:00".

    Computed at a moment in time rather than stored, because it changes with
    daylight saving.
    """
    moment = (at or datetime.now(UTC)).astimezone(tz)
    delta = moment.utcoffset()
    if delta is None:
        return "UTC"
    total = int(delta.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"UTC{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


def now_in(tz: ZoneInfo) -> datetime:
    return datetime.now(UTC).astimezone(tz)
