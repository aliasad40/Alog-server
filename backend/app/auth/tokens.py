"""Session tokens.

A signed JWT carried in an HttpOnly, SameSite=Strict cookie. HttpOnly means
a cross-site scripting bug cannot read the token; SameSite=Strict means
another site cannot cause the browser to send it, which covers CSRF without
a separate token dance. Mutating endpoints additionally check the Origin
header (see api/deps.py) as defence in depth.
"""

from __future__ import annotations

import time
from typing import Optional

import jwt

ALGORITHM = "HS256"
ISSUER = "network-log-server"


def issue(secret: str, username: str, ttl_hours: int, session_epoch: int = 0) -> str:
    now = int(time.time())
    payload = {
        "sub": username,
        "iat": now,
        "nbf": now,
        "exp": now + ttl_hours * 3600,
        "iss": ISSUER,
        "se": session_epoch,
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def verify(secret: str, token: str) -> Optional[dict]:
    try:
        return jwt.decode(
            token, secret, algorithms=[ALGORITHM], issuer=ISSUER,
            options={"require": ["exp", "sub", "iss"]},
        )
    except jwt.PyJWTError:
        return None
