"""Shared request-scoped dependencies."""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urlparse

from fastapi import Depends, HTTPException, Request, status

from ..auth import tokens
from ..config import Config
from ..database.clickhouse import ClickHouseService
from ..database.meta import MetadataStore

log = logging.getLogger(__name__)

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def get_cfg(request: Request) -> Config:
    return request.app.state.cfg


def get_meta(request: Request) -> MetadataStore:
    return request.app.state.meta


def get_ch(request: Request) -> ClickHouseService:
    return request.app.state.clickhouse


def get_redis(request: Request):
    return request.app.state.redis


def client_ip(request: Request) -> str:
    """Trust X-Forwarded-For only from the local reverse proxy.

    Nginx runs on the same host and is the only thing that should be talking
    to the API, so anything arriving from elsewhere gets its own address used
    regardless of what headers it claims.
    """
    peer = request.client.host if request.client else "unknown"
    if peer in ("127.0.0.1", "::1"):
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return peer


async def require_user(request: Request) -> str:
    cfg: Config = request.app.state.cfg
    token = request.cookies.get(cfg.auth.cookie_name)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in to continue.")
    claims = tokens.verify(cfg.secret_key, token)
    if claims is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Your session has expired. Sign in again.")
    return claims["sub"]


async def require_same_origin(request: Request) -> None:
    """CSRF defence in depth for state-changing requests.

    The session cookie is already SameSite=Strict; this catches the case
    where a proxy or browser quirk relaxes that.
    """
    if request.method in SAFE_METHODS:
        return
    origin = request.headers.get("origin")
    if origin is None:
        return  # non-browser client (curl, monitoring); cookie policy still applies
    host = request.headers.get("host", "")
    if urlparse(origin).netloc != host:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cross-origin request refused.")


CurrentUser = Depends(require_user)
SameOrigin = Depends(require_same_origin)
