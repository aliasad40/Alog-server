"""Schema tests.

These do not need a running ClickHouse. They assert the properties of the
schema that are easy to break by accident and expensive to discover in
production -- a dropped codec, a reordered sorting key, a column that stops
matching the insert path.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

from app.database.clickhouse import COLUMNS, SELECT_COLUMNS, SORTABLE, render_schema
from app.parser import MikroTikParser

SCHEMA = Path(__file__).resolve().parents[2] / "database" / "schema" / "clickhouse.sql"


@pytest.fixture
def sql() -> str:
    return SCHEMA.read_text()


def test_placeholders_are_substituted():
    statements = render_schema(SCHEMA, "test_db", 14, 6)
    joined = "\n".join(statements)
    assert "{{" not in joined
    assert "test_db.nat_logs" in joined
    assert "INTERVAL 14 DAY" in joined
    assert "INTERVAL 6 MONTH" in joined


def test_schema_splits_into_two_statements():
    statements = render_schema(SCHEMA, "network_logs", 30, 12)
    assert len(statements) == 2
    assert statements[0].startswith("CREATE DATABASE")
    assert statements[1].startswith("CREATE TABLE")


def test_sorting_key_leads_with_the_cgnat_lookup(sql):
    """The reverse lookup is the query this database exists for. If someone
    reorders this key, every lawful-intercept search becomes a scan."""
    assert "ORDER BY (public_ip, public_port, timestamp)" in sql


def test_partitioned_monthly(sql):
    assert "PARTITION BY toYYYYMM(timestamp)" in sql


def test_session_start_time_costs_no_storage(sql):
    """It is defined as identical to timestamp; storing it twice would waste
    real disk across billions of rows."""
    assert "session_start_time DateTime ALIAS timestamp" in sql
    # ...but it must still be selectable.
    assert "session_start_time" in SELECT_COLUMNS
    assert "session_start_time" not in COLUMNS   # never written


def test_every_column_has_a_deliberate_codec(sql):
    body = sql.split("CREATE TABLE", 1)[1]
    for column in COLUMNS:
        line = next((l for l in body.splitlines()
                     if l.strip().startswith(column + " ")), None)
        assert line is not None, f"{column} missing from schema"
        assert "CODEC(" in line, f"{column} has no codec"


def test_timestamps_use_doubledelta(sql):
    for column in ("timestamp", "session_end_time"):
        line = next(l for l in sql.splitlines() if l.strip().startswith(column + " "))
        assert "DoubleDelta" in line


def test_repeated_strings_are_lowcardinality(sql):
    for column in ("subscriber_id", "protocol"):
        line = next(l for l in sql.splitlines() if l.strip().startswith(column + " "))
        assert "LowCardinality(String)" in line


def test_ports_are_uint16_not_larger(sql):
    for column in ("private_port", "public_port", "dest_port"):
        line = next(l for l in sql.splitlines() if l.strip().startswith(column + " "))
        assert "UInt16" in line, f"{column} should be UInt16"


def test_addresses_are_ipv4_not_strings(sql):
    for column in ("router_ip", "private_ip", "public_ip", "dest_ip"):
        line = next(l for l in sql.splitlines() if l.strip().startswith(column + " "))
        assert "IPv4" in line, f"{column} should be the IPv4 type, not a String"


def test_skip_indexes_cover_the_non_sorting_key_searches(sql):
    """Searches on these columns cannot use the sorting key, so without a
    skip index each one becomes a full partition scan."""
    for column in ("subscriber_id", "private_ip", "dest_ip"):
        assert re.search(rf"INDEX\s+\w+\s+{column}\s+TYPE\s+bloom_filter", sql), \
            f"no bloom filter skip index for {column}"


def test_ttl_recompresses_then_deletes(sql):
    assert "RECOMPRESS CODEC(ZSTD(9))" in sql
    assert "MONTH DELETE" in sql


def test_ttl_drops_whole_parts(sql):
    """Row-level TTL expiry rewrites parts; part-level drops are far cheaper."""
    assert "ttl_only_drop_parts = 1" in sql


def test_parser_output_matches_the_insert_column_order():
    """The msgpack wire format, the ClickHouse column list and ParsedLog.as_row
    must stay aligned. This is the seam most likely to break silently."""
    log, reason = MikroTikParser().parse(
        "in:<pppoe-u1> proto TCP, 10.0.0.1:1->8.8.8.8:2, "
        "NAT (10.0.0.1:1->1.2.3.4:3)->8.8.8.8:2")
    assert reason == ""
    row = log.as_row(1756577597, "10.10.10.1")
    assert len(row) == len(COLUMNS)

    named = dict(zip(COLUMNS, row))
    assert named["timestamp"] == 1756577597
    assert named["router_ip"] == "10.10.10.1"
    assert named["subscriber_id"] == "u1"
    assert named["private_ip"] == "10.0.0.1"
    assert named["public_ip"] == "1.2.3.4"
    assert named["dest_ip"] == "8.8.8.8"
    assert named["protocol"] == "TCP"
    assert named["session_end_time"] is None


def test_sortable_columns_are_all_real_columns():
    """SORTABLE is spliced into ORDER BY, so a typo here would be a query
    error at best and an injection vector at worst."""
    assert SORTABLE <= set(SELECT_COLUMNS)
