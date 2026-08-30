# API

Interactive reference: `http://<server>/api/docs` (OpenAPI, requires no auth to
view; the endpoints themselves still do).

Authentication is a signed JWT in an `HttpOnly`, `SameSite=Strict` cookie set by
`/api/auth/login`. Mutating requests also get an `Origin` check.

## Authentication

| Method | Path | Description |
|---|---|---|
| POST | `/api/auth/login` | `{username, password}` → sets session cookie |
| POST | `/api/auth/logout` | Clears the cookie |
| GET | `/api/auth/me` | Current user |
| POST | `/api/auth/password` | `{current_password, new_password}` |

Login is rate limited per username+IP: 5 failures, then a 5-minute lockout,
plus a 10 requests/minute limit at nginx.

## Search

**POST `/api/search`**

```json
{
  "time_from": "2026-08-30T18:00:00",
  "time_to":   "2026-08-30T19:00:00",
  "public_ip": "103.125.177.119",
  "public_port": 35420,
  "private_ip": null, "private_port": null,
  "dest_ip": null, "dest_port": null,
  "protocol": "TCP",
  "router_ip": null,
  "subscriber_id": "P2-musa",
  "subscriber_partial": false,
  "limit": 100, "offset": 0,
  "order_by": "timestamp", "descending": true
}
```

Response:

```json
{
  "rows": [{
    "timestamp": "2026-08-30 18:33:17",
    "router_ip": "10.10.10.1",
    "subscriber_id": "P2-musa",
    "private_ip": "100.68.180.230", "private_port": 35420,
    "public_ip": "103.125.177.119", "public_port": 35420,
    "dest_ip": "99.124.164.160",    "dest_port": 22,
    "protocol": "TCP",
    "session_start_time": "2026-08-30 18:33:17",
    "session_end_time": null
  }],
  "has_more": false, "elapsed_ms": 12, "rows_scanned": 8192,
  "limit": 100, "offset": 0
}
```

Notes:

- A time range is **required**. Omitting it defaults to the last 24 hours; it
  cannot exceed 400 days.
- `order_by` is restricted to an allow-list of real column names.
- Every value is bound as a ClickHouse parameter (`{name:IPv4}` etc). No user
  input is ever formatted into SQL text.
- `has_more` comes from fetching one extra row, so paging costs nothing extra.

**POST `/api/search/count`** — same body, returns `{"count": N}`. Separate
because counting every match over a wide range is expensive.

## Routers

| Method | Path | Description |
|---|---|---|
| GET | `/api/routers` | List all |
| POST | `/api/routers` | `{name, ip_address, description, enabled}` |
| PUT | `/api/routers/{id}` | Partial update |
| DELETE | `/api/routers/{id}` | Remove |

Every mutation republishes the Redis allow-list, so a disabled router stops
being accepted immediately.

## Settings

| Method | Path | Description |
|---|---|---|
| GET | `/api/settings/branding` | **No auth** — the login page needs it |
| PUT | `/api/settings/branding` | `{company_name}` |
| POST | `/api/settings/logo` | multipart upload, ≤2 MB, re-encoded to PNG |
| DELETE | `/api/settings/logo` | Remove |
| GET/PUT | `/api/settings/retention` | `{hot_days, retention_months}` |

## System

| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | **No auth.** Liveness only. 200 or 503 |
| GET | `/api/system/status` | Full counters, queue, storage, host |
| GET | `/api/system/partitions` | Per-month rows and size |
| GET | `/api/system/archives` | Archive run history |
| POST | `/api/system/metrics/reset` | Zero the counters |

`/api/health` is deliberately minimal — it reports component reachability and
nothing else, so it is safe to expose to a monitoring system.

## Scripting example

```bash
COOKIE=$(mktemp)
curl -s -c "$COOKIE" -X POST http://server/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"..."}'

curl -s -b "$COOKIE" -X POST http://server/api/search \
  -H 'Content-Type: application/json' \
  -d '{"public_ip":"103.125.177.119","time_from":"2026-08-30T18:00:00",
       "time_to":"2026-08-30T19:00:00"}' | jq '.rows[]'
```
