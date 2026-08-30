"""Authorised router management.

Every mutation refreshes the Redis allow-list immediately so a router that is
disabled in the UI stops being accepted within one refresh interval of the
receivers -- and usually instantly, because the set is rewritten here.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..services.receiver import ROUTER_SET_KEY
from .deps import CurrentUser, SameOrigin, get_meta

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/routers", tags=["routers"])


class RouterIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    ip_address: str = Field(min_length=7, max_length=15)
    description: str = Field("", max_length=256)
    enabled: bool = True


class RouterPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=64)
    ip_address: Optional[str] = Field(None, min_length=7, max_length=15)
    description: Optional[str] = Field(None, max_length=256)
    enabled: Optional[bool] = None


async def sync_allow_list(request: Request) -> None:
    """Rewrite the Redis allow-list from the metadata store."""
    ips = get_meta(request).enabled_router_ips()
    try:
        redis = request.app.state.redis
        pipe = redis.pipeline(transaction=True)
        pipe.delete(ROUTER_SET_KEY)
        if ips:
            pipe.sadd(ROUTER_SET_KEY, *ips)
        await pipe.execute()
    except Exception as exc:
        # Receivers fall back to reading SQLite directly, so this is not fatal.
        log.error("could not publish router allow-list to Redis: %s", exc)


@router.get("")
async def list_routers(request: Request, _user: str = CurrentUser) -> List[dict]:
    return get_meta(request).list_routers()


@router.post("", status_code=status.HTTP_201_CREATED)
async def add_router(payload: RouterIn, request: Request,
                     _user: str = CurrentUser, _: None = SameOrigin):
    meta = get_meta(request)
    try:
        created = meta.add_router(payload.name, payload.ip_address,
                                  payload.description, payload.enabled)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "A router with that name or IP address already exists.")
    await sync_allow_list(request)
    log.info("router added: %s (%s)", created["name"], created["ip_address"])
    return created


@router.put("/{router_id}")
async def update_router(router_id: int, payload: RouterPatch, request: Request,
                        _user: str = CurrentUser, _: None = SameOrigin):
    meta = get_meta(request)
    if meta.get_router(router_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That router no longer exists.")
    try:
        updated = meta.update_router(router_id, **payload.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "A router with that name or IP address already exists.")
    await sync_allow_list(request)
    return updated


@router.delete("/{router_id}")
async def delete_router(router_id: int, request: Request,
                        _user: str = CurrentUser, _: None = SameOrigin):
    if not get_meta(request).delete_router(router_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That router no longer exists.")
    await sync_allow_list(request)
    log.info("router deleted: id=%s", router_id)
    return {"status": "Router removed"}


async def sync_allow_list_startup(app) -> None:
    """Publish the allow-list once at API startup so a fresh boot does not
    wait for a receiver refresh cycle."""
    ips = app.state.meta.enabled_router_ips()
    try:
        pipe = app.state.redis.pipeline(transaction=True)
        pipe.delete(ROUTER_SET_KEY)
        if ips:
            pipe.sadd(ROUTER_SET_KEY, *ips)
        await pipe.execute()
        log.info("published %d authorised routers", len(ips))
    except Exception as exc:
        log.warning("could not publish router allow-list at startup: %s", exc)
