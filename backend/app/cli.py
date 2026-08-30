"""nls-admin -- operational command line.

install.bash drives the database and admin bootstrap through this rather than
through raw SQL, so there is exactly one implementation of "create the schema"
and it is the same one the upgrade path uses.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from .auth.passwords import generate_password, hash_password, password_problems
from .config import get_config
from .database.clickhouse import ClickHouseService, build_client, render_schema
from .database.meta import MetadataStore
from .logging_setup import setup_logging

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "database" / "schema" / "clickhouse.sql"


def cmd_init_db(args) -> int:
    cfg = get_config()
    schema = Path(args.schema) if args.schema else SCHEMA_PATH
    if not schema.is_file():
        print(f"error: schema file not found at {schema}", file=sys.stderr)
        return 1

    meta = MetadataStore(cfg.paths.metadata_db)
    hot_days = int(meta.get_setting("hot_days", str(cfg.retention.hot_days)))
    retention = int(meta.get_setting("retention_months", str(cfg.retention.retention_months)))

    # Connect without a database first: it may not exist yet.
    client = build_client(cfg, database="default")
    for statement in render_schema(schema, cfg.clickhouse.database, hot_days, retention):
        client.command(statement)
    client.close()

    service = ClickHouseService(cfg)
    stats = service.storage_stats()
    print(f"database {cfg.clickhouse.database}.{cfg.clickhouse.table} ready "
          f"({stats['rows']:,} rows)")
    service.close()
    return 0


def cmd_create_admin(args) -> int:
    cfg = get_config()
    meta = MetadataStore(cfg.paths.metadata_db)
    if meta.get_user(args.username) and not args.force:
        print(f"user {args.username!r} already exists; nothing changed")
        return 0

    if args.password:
        password = args.password
    elif args.random:
        password = generate_password()
    else:
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Confirm: "):
            print("error: passwords do not match", file=sys.stderr)
            return 1

    problems = password_problems(password)
    if problems and not args.random:
        print("error: " + " ".join(problems), file=sys.stderr)
        return 1

    if meta.get_user(args.username):
        meta.set_password(args.username, hash_password(password))
    else:
        meta.create_user(args.username, hash_password(password), must_change=args.random)

    if args.random:
        print(password)   # install.bash captures exactly this line
    else:
        print(f"user {args.username!r} ready")
    return 0


def cmd_add_router(args) -> int:
    cfg = get_config()
    meta = MetadataStore(cfg.paths.metadata_db)
    try:
        created = meta.add_router(args.name, args.ip, args.description or "", not args.disabled)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"router {created['name']} ({created['ip_address']}) added")
    return 0


def cmd_list_routers(args) -> int:
    cfg = get_config()
    rows = MetadataStore(cfg.paths.metadata_db).list_routers()
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No routers configured. Add one before any logs will be accepted.")
        return 0
    print(f"{'NAME':<24}{'IP':<18}{'STATUS':<10}DESCRIPTION")
    for r in rows:
        status = "enabled" if r["enabled"] else "disabled"
        print(f"{r['name']:<24}{r['ip_address']:<18}{status:<10}{r['description']}")
    return 0


def cmd_status(args) -> int:
    cfg = get_config()
    service = ClickHouseService(cfg)
    ok = service.ping()
    print(f"clickhouse : {'reachable' if ok else 'UNREACHABLE'}")
    if ok:
        stats = service.storage_stats()
        print(f"rows       : {stats['rows']:,}")
        print(f"on disk    : {stats['compressed_bytes'] / 1073741824:.2f} GiB")
        print(f"compression: {stats['compression_ratio']}x "
              f"({stats['bytes_per_row']} bytes/row)")
        print(f"partitions : {', '.join(p['partition'] for p in service.partitions()) or 'none'}")
    service.close()
    return 0 if ok else 1


def cmd_apply_retention(args) -> int:
    cfg = get_config()
    meta = MetadataStore(cfg.paths.metadata_db)
    hot = args.hot_days or int(meta.get_setting("hot_days", "30"))
    months = args.retention_months or int(meta.get_setting("retention_months", "12"))
    service = ClickHouseService(cfg)
    service.apply_retention(hot, months)
    meta.set_setting("hot_days", str(hot))
    meta.set_setting("retention_months", str(months))
    print(f"retention: recompress after {hot} days, delete after {months} months")
    service.close()
    return 0


def cmd_archive(args) -> int:
    from .services.maintenance import run as run_maintenance
    return run_maintenance()


def cmd_serve(args) -> int:
    import uvicorn
    cfg = get_config()
    uvicorn.run(
        "app.api.app:get_app",
        factory=True,
        host=args.host or cfg.server.bind,
        port=args.port or cfg.server.port,
        workers=args.workers or cfg.server.workers,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        access_log=False,
        log_level=cfg.log_level.lower(),
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nls-admin", description="Network Log Server admin")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-db", help="create the ClickHouse database and table")
    p.add_argument("--schema", help="path to clickhouse.sql")
    p.set_defaults(func=cmd_init_db)

    p = sub.add_parser("create-admin", help="create or reset an administrator")
    p.add_argument("username")
    p.add_argument("--password")
    p.add_argument("--random", action="store_true", help="generate and print a password")
    p.add_argument("--force", action="store_true", help="reset the password if the user exists")
    p.set_defaults(func=cmd_create_admin)

    p = sub.add_parser("add-router", help="authorise a router")
    p.add_argument("name")
    p.add_argument("ip")
    p.add_argument("--description")
    p.add_argument("--disabled", action="store_true")
    p.set_defaults(func=cmd_add_router)

    p = sub.add_parser("list-routers")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list_routers)

    p = sub.add_parser("status", help="database and storage summary")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("apply-retention")
    p.add_argument("--hot-days", type=int)
    p.add_argument("--retention-months", type=int)
    p.set_defaults(func=cmd_apply_retention)

    p = sub.add_parser("archive", help="run the monthly archive now")
    p.set_defaults(func=cmd_archive)

    p = sub.add_parser("serve", help="run the API (development; use systemd in production)")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--workers", type=int)
    p.set_defaults(func=cmd_serve)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        cfg = get_config()
        setup_logging(cfg, "admin")
    except Exception as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        sys.exit(2)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
