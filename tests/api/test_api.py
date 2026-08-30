"""API tests.

ClickHouse is stubbed with a recorder so we can assert the exact SQL and the
exact bound parameters the search layer produces -- that is how we prove the
search form cannot be turned into a query injection.
"""

from __future__ import annotations

import os
import socket
import sys
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

from app.api.app import create_app
from app.auth.passwords import hash_password
from app.config import Config
from app.database.meta import MetadataStore

REDIS_PORT = 6399
ADMIN = "admin"
SECRET = "TestPassword123!"


def redis_available() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", REDIS_PORT), timeout=1):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not redis_available(), reason="no Redis on 127.0.0.1:6399")


class RecordingResult:
    def __init__(self, columns, rows, summary=None):
        self.column_names = columns
        self.result_rows = rows
        self.summary = summary or {}


class FakeClickHouse:
    """Stand-in for ClickHouseService that records what it was asked."""

    def __init__(self):
        self.queries = []
        self.inserted = []

    class _Client:
        def __init__(self, outer):
            self.outer = outer

        def query(self, sql, parameters=None, settings=None):
            self.outer.queries.append((sql, parameters or {}))
            if "count()" in sql:
                return RecordingResult(["count()"], [(7,)])
            return RecordingResult(["timestamp"], [(datetime(2026, 8, 30),)])

        def command(self, *a, **k):
            return None

        def close(self):
            return None

    @property
    def client(self):
        return self._Client(self)

    def search(self, q):
        from app.database.clickhouse import ClickHouseService, SearchResult
        # Reuse the real WHERE builder so the test exercises production code.
        cfg = Config(secret_key="x" * 48)
        real = ClickHouseService(cfg, client=self._Client(self))
        return real.search(q)

    def count(self, q):
        from app.database.clickhouse import ClickHouseService
        cfg = Config(secret_key="x" * 48)
        real = ClickHouseService(cfg, client=self._Client(self))
        return real.count(q)

    def ping(self):
        return True

    def close(self):
        pass

    def storage_stats(self):
        return {"rows": 0, "compressed_bytes": 0, "uncompressed_bytes": 0,
                "compression_ratio": 0.0, "bytes_per_row": 0.0, "parts": 0,
                "oldest_event": None, "newest_event": None}

    def partitions(self):
        return []


@pytest.fixture
def client(tmp_path):
    # db 15 is scratch space for the test suite; flushed before every test so
    # login-throttle counters cannot leak between cases.
    import redis as sync_redis
    sync_redis.Redis(port=REDIS_PORT, db=15).flushdb()

    cfg = Config(
        secret_key="k" * 48,
        redis={"port": REDIS_PORT, "db": 15, "queue_key": "nls:apitest:queue"},
        paths={
            "data_dir": str(tmp_path), "log_dir": str(tmp_path),
            "metadata_db": str(tmp_path / "meta.sqlite3"),
            "logo_dir": str(tmp_path / "branding"),
        },
    )
    meta = MetadataStore(cfg.paths.metadata_db)
    meta.create_user(ADMIN, hash_password(SECRET), must_change=False)

    app = create_app(cfg)
    with TestClient(app) as c:
        app.state.clickhouse = FakeClickHouse()
        c.fake_ch = app.state.clickhouse
        yield c


def login(c):
    r = c.post("/api/auth/login", json={"username": ADMIN, "password": SECRET})
    assert r.status_code == 200, r.text
    return r


# -- authentication --------------------------------------------------------

def test_protected_endpoints_reject_anonymous(client):
    assert client.get("/api/routers").status_code == 401
    assert client.get("/api/system/status").status_code == 401
    assert client.get("/api/auth/me").status_code == 401
    assert client.post("/api/search", json={}).status_code == 401


def test_login_sets_httponly_strict_cookie(client):
    r = login(client)
    cookie = r.headers.get("set-cookie", "")
    assert "HttpOnly" in cookie
    assert "samesite=strict" in cookie.lower()


def test_wrong_password_rejected(client):
    r = client.post("/api/auth/login", json={"username": ADMIN, "password": "wrong"})
    assert r.status_code == 401
    assert "Incorrect username or password" in r.json()["detail"]


def test_unknown_user_gives_identical_message(client):
    r = client.post("/api/auth/login", json={"username": "ghost", "password": "wrong"})
    assert r.status_code == 401
    assert "Incorrect username or password" in r.json()["detail"]


def test_login_lockout_after_repeated_failures(client):
    for _ in range(6):
        client.post("/api/auth/login", json={"username": ADMIN, "password": "wrong"})
    r = client.post("/api/auth/login", json={"username": ADMIN, "password": SECRET})
    assert r.status_code == 429


def test_logout_clears_the_session(client):
    login(client)
    assert client.get("/api/auth/me").status_code == 200
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401


def test_cross_origin_mutation_is_refused(client):
    login(client)
    r = client.post("/api/routers",
                    json={"name": "evil", "ip_address": "10.0.0.1"},
                    headers={"Origin": "https://attacker.example"})
    assert r.status_code == 403


# -- routers ---------------------------------------------------------------

def test_router_lifecycle(client):
    login(client)
    r = client.post("/api/routers", json={"name": "LHE-MX204", "ip_address": "10.10.10.1",
                                          "description": "Lahore core"})
    assert r.status_code == 201
    router_id = r.json()["id"]

    assert len(client.get("/api/routers").json()) == 1

    r = client.put(f"/api/routers/{router_id}", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] == 0

    assert client.delete(f"/api/routers/{router_id}").status_code == 200
    assert client.get("/api/routers").json() == []


def test_duplicate_router_ip_is_rejected(client):
    login(client)
    client.post("/api/routers", json={"name": "a", "ip_address": "10.10.10.1"})
    r = client.post("/api/routers", json={"name": "b", "ip_address": "10.10.10.1"})
    assert r.status_code == 409


@pytest.mark.parametrize("bad", ["10.10.10.999", "not-an-ip", "10.10.10.0/24",
                                 "2001:db8::1", "10.10.10.1; DROP TABLE"])
def test_invalid_router_addresses_rejected(client, bad):
    login(client)
    r = client.post("/api/routers", json={"name": "x", "ip_address": bad})
    assert r.status_code in (400, 422), bad


def test_disabling_a_router_persists(client):
    login(client)
    rid = client.post("/api/routers",
                      json={"name": "r1", "ip_address": "10.10.10.7"}).json()["id"]
    client.put(f"/api/routers/{rid}", json={"enabled": False})
    assert client.get("/api/routers").json()[0]["enabled"] == 0


# -- search / injection ----------------------------------------------------

def test_search_binds_every_value_as_a_parameter(client):
    login(client)
    r = client.post("/api/search", json={
        "public_ip": "103.125.177.119", "public_port": 35420,
        "protocol": "tcp", "subscriber_id": "P2-musa",
        "time_from": "2026-08-30T00:00:00", "time_to": "2026-08-30T23:59:59",
    })
    assert r.status_code == 200
    sql, params = client.fake_ch.queries[-1]

    # No user value is ever spliced into the SQL text.
    for value in ("103.125.177.119", "35420", "P2-musa", "TCP"):
        assert value not in sql
    assert params["public_ip"] == "103.125.177.119"
    assert params["protocol"] == "TCP"
    assert params["subscriber_id"] == "P2-musa"
    assert "{public_ip:IPv4}" in sql


def test_injection_attempts_are_rejected_or_bound(client):
    login(client)
    payloads = [
        {"public_ip": "1.1.1.1' OR 1=1 --"},
        {"subscriber_id": "'; DROP TABLE nat_logs; --"},
        {"protocol": "TCP') UNION ALL SELECT * FROM system.users --"},
        {"order_by": "timestamp; DROP TABLE nat_logs"},
        {"order_by": "(SELECT password FROM system.users)"},
    ]
    for payload in payloads:
        r = client.post("/api/search", json=payload)
        if r.status_code == 200:
            sql, params = client.fake_ch.queries[-1]
            assert "DROP" not in sql.upper()
            assert "UNION" not in sql.upper()
            assert "system.users" not in sql
        else:
            assert r.status_code in (400, 422), payload


def test_order_by_is_restricted_to_an_allow_list(client):
    login(client)
    r = client.post("/api/search", json={"order_by": "password"})
    assert r.status_code == 422


def test_time_window_is_capped(client):
    login(client)
    r = client.post("/api/search", json={"time_from": "2000-01-01T00:00:00",
                                         "time_to": "2026-08-30T00:00:00"})
    assert r.status_code == 400
    assert "Narrow the range" in r.json()["detail"]


def test_search_defaults_to_the_last_24_hours(client):
    login(client)
    assert client.post("/api/search", json={}).status_code == 200
    _, params = client.fake_ch.queries[-1]
    delta = params["time_to"] - params["time_from"]
    assert 23 <= delta.total_seconds() / 3600 <= 25


def test_partial_subscriber_search_uses_a_bound_needle(client):
    login(client)
    client.post("/api/search", json={"subscriber_id": "musa", "subscriber_partial": True})
    sql, params = client.fake_ch.queries[-1]
    assert "positionCaseInsensitive(subscriber_id, {subscriber_id:String})" in sql
    assert params["subscriber_id"] == "musa"


def test_count_is_a_separate_call(client):
    login(client)
    r = client.post("/api/search/count", json={"public_ip": "1.2.3.4"})
    assert r.status_code == 200 and r.json()["count"] == 7


# -- branding --------------------------------------------------------------

def test_branding_is_readable_before_login(client):
    r = client.get("/api/settings/branding")
    assert r.status_code == 200 and "company_name" in r.json()


def test_company_name_round_trips(client):
    login(client)
    assert client.put("/api/settings/branding",
                      json={"company_name": "Zoom Net (Pvt) Ltd"}).status_code == 200
    assert client.get("/api/settings/branding").json()["company_name"] == "Zoom Net (Pvt) Ltd"


def test_logo_upload_rejects_non_images(client):
    login(client)
    r = client.post("/api/settings/logo",
                    files={"file": ("evil.svg", b"<svg onload=alert(1)></svg>", "image/svg+xml")})
    assert r.status_code == 400


def test_logo_upload_reencodes_a_real_image(client):
    from io import BytesIO
    from PIL import Image

    login(client)
    buf = BytesIO()
    Image.new("RGB", (300, 120), (10, 60, 120)).save(buf, format="JPEG")
    r = client.post("/api/settings/logo",
                    files={"file": ("logo.jpg", buf.getvalue(), "image/jpeg")})
    assert r.status_code == 200
    url = r.json()["logo_url"]
    served = client.get(url)
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/png"   # re-encoded, not passed through


@pytest.mark.parametrize("name", ["../../../etc/passwd", "logo-x.png/../../etc/shadow",
                                  "notalogo.png", "logo-abc.txt"])
def test_logo_path_traversal_blocked(client, name):
    assert client.get(f"/api/settings/logo/{name}").status_code == 404


# -- security headers ------------------------------------------------------

def test_security_headers_present(client):
    r = client.get("/api/settings/branding")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["Cache-Control"] == "no-store"


def test_health_endpoint_is_anonymous(client):
    r = client.get("/api/health")
    assert r.status_code in (200, 503)
