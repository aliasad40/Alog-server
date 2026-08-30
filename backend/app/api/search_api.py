"""Log search.

The time window is mandatory and capped. That is not a UI nicety: without it
every query becomes a scan of the whole retention period, and on a table with
tens of billions of rows one careless search would evict the page cache for
everyone. The UI always sends a range; the API enforces it.
"""

from __future__ import annotations

import ipaddress
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, field_validator

from ..database.clickhouse import EXACT_FILTERS, SORTABLE, SearchQuery
from .deps import CurrentUser, SameOrigin, get_cfg, get_ch

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/search", tags=["search"])

MAX_WINDOW_DAYS = 400


class SearchRequest(BaseModel):
    time_from: Optional[datetime] = None
    time_to: Optional[datetime] = None
    router_ip: Optional[str] = None
    private_ip: Optional[str] = None
    public_ip: Optional[str] = None
    dest_ip: Optional[str] = None
    private_port: Optional[int] = Field(None, ge=0, le=65535)
    public_port: Optional[int] = Field(None, ge=0, le=65535)
    dest_port: Optional[int] = Field(None, ge=0, le=65535)
    protocol: Optional[str] = Field(None, max_length=16)
    subscriber_id: Optional[str] = Field(None, max_length=128)
    subscriber_partial: bool = False
    limit: int = Field(100, ge=1, le=10000)
    offset: int = Field(0, ge=0, le=1_000_000)
    order_by: str = "timestamp"
    descending: bool = True

    @field_validator("router_ip", "private_ip", "public_ip", "dest_ip")
    @classmethod
    def _valid_ip(cls, v):
        if v in (None, ""):
            return None
        try:
            addr = ipaddress.ip_address(v.strip())
        except ValueError:
            raise ValueError(f"{v!r} is not a valid IP address")
        if addr.version != 4:
            raise ValueError("Only IPv4 addresses are stored")
        return str(addr)

    @field_validator("order_by")
    @classmethod
    def _valid_order(cls, v):
        if v not in SORTABLE:
            raise ValueError(f"Cannot sort by {v!r}")
        return v

    @field_validator("protocol", "subscriber_id")
    @classmethod
    def _blank_to_none(cls, v):
        v = (v or "").strip()
        return v or None

    def to_query(self) -> SearchQuery:
        now = datetime.now()
        time_to = self.time_to or now
        time_from = self.time_from or (time_to - timedelta(hours=24))
        if time_from > time_to:
            raise ValueError("The start of the range is after the end.")
        if (time_to - time_from) > timedelta(days=MAX_WINDOW_DAYS):
            raise ValueError(f"Narrow the range to {MAX_WINDOW_DAYS} days or fewer.")
        exact = {}
        for name in EXACT_FILTERS:
            value = getattr(self, name, None)
            if value not in (None, ""):
                exact[name] = value.upper() if name == "protocol" else value
        return SearchQuery(
            time_from=time_from, time_to=time_to, exact=exact,
            subscriber_id=self.subscriber_id,
            subscriber_partial=self.subscriber_partial,
            limit=self.limit, offset=self.offset,
            order_by=self.order_by, descending=self.descending,
        )


def _serialise(row: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            out[key] = value.isoformat(sep=" ", timespec="seconds")
        elif value is None:
            out[key] = None
        else:
            out[key] = str(value) if "_ip" in key else value
    return out


@router.post("")
async def search(payload: SearchRequest, request: Request,
                 _user: str = CurrentUser, _: None = SameOrigin):
    try:
        query = payload.to_query()
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))

    service = get_ch(request)
    try:
        result = await run_in_threadpool(service.search, query)
    except Exception as exc:
        log.error("search failed: %s", exc)
        service.close()
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "The log database did not answer. Check ClickHouse and try again.")
    return {
        "rows": [_serialise(r) for r in result.rows],
        "has_more": result.has_more,
        "elapsed_ms": result.elapsed_ms,
        "rows_scanned": result.rows_read,
        "limit": query.limit,
        "offset": query.offset,
    }


@router.post("/count")
async def count(payload: SearchRequest, request: Request,
                _user: str = CurrentUser, _: None = SameOrigin):
    """Exact match count. Separate endpoint because it is the expensive half
    of the search; the UI only calls it when the operator asks."""
    try:
        query = payload.to_query()
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    service = get_ch(request)
    try:
        total = await run_in_threadpool(service.count, query)
    except Exception as exc:
        log.error("count failed: %s", exc)
        service.close()
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Counting timed out. Narrow the time range and try again.")
    return {"count": total}
