"""Authentication endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from ..auth import (LoginThrottle, hash_password, needs_rehash,
                    password_problems, tokens, verify_password)
from .deps import CurrentUser, SameOrigin, client_ip, get_cfg, get_meta

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


@router.post("/login")
async def login(payload: LoginRequest, request: Request, response: Response,
                _: None = SameOrigin):
    cfg = get_cfg(request)
    meta = get_meta(request)
    ip = client_ip(request)
    throttle = LoginThrottle(request.app.state.redis, cfg.auth.max_failed_logins,
                             cfg.auth.lockout_seconds)

    allowed, remaining = await throttle.check(payload.username, ip)
    if not allowed:
        minutes = max(1, remaining // 60)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Too many failed attempts. Try again in {minutes} minute(s).",
        )

    user = meta.get_user(payload.username)
    # Verify against a dummy hash when the user does not exist so that the
    # response time does not reveal which usernames are real.
    stored = user["password_hash"] if user else request.app.state.dummy_hash
    ok = verify_password(stored, payload.password)

    if not user or not ok:
        await throttle.record_failure(payload.username, ip)
        log.warning("failed login for %r from %s", payload.username[:64], ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect username or password.")

    await throttle.clear(payload.username, ip)
    if needs_rehash(stored):
        meta.set_password(user["username"], hash_password(payload.password))
    meta.touch_login(user["username"])

    token = tokens.issue(cfg.secret_key, user["username"], cfg.auth.session_hours)
    response.set_cookie(
        cfg.auth.cookie_name, token,
        max_age=cfg.auth.session_hours * 3600,
        httponly=True, samesite="strict", secure=cfg.auth.cookie_secure, path="/",
    )
    log.info("login: %s from %s", user["username"], ip)
    return {"username": user["username"], "must_change_password": bool(user["must_change"])}


@router.post("/logout")
async def logout(request: Request, response: Response):
    cfg = get_cfg(request)
    response.delete_cookie(cfg.auth.cookie_name, path="/")
    return {"status": "signed out"}


@router.get("/me")
async def me(request: Request, username: str = CurrentUser):
    user = get_meta(request).get_user(username)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Your account no longer exists.")
    return {
        "username": user["username"],
        "must_change_password": bool(user["must_change"]),
        "last_login": user["last_login"],
    }


@router.post("/password")
async def change_password(payload: PasswordChange, request: Request,
                          username: str = CurrentUser, _: None = SameOrigin):
    meta = get_meta(request)
    user = meta.get_user(username)
    if not user or not verify_password(user["password_hash"], payload.current_password):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Current password is incorrect.")
    problems = password_problems(payload.new_password)
    if problems:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, " ".join(problems))
    meta.set_password(username, hash_password(payload.new_password))
    log.info("password changed for %s", username)
    return {"status": "Password changed"}
