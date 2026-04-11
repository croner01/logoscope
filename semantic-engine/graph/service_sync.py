"""Service sync adapter (shared implementation)."""

import os
import sys

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_SHARED_SRC_DIR = os.path.join(_PROJECT_ROOT, "shared_src")
for _path in (_PROJECT_ROOT, _SHARED_SRC_DIR):
    if os.path.isdir(_path) and _path not in sys.path:
        sys.path.append(_path)

from shared_src.graph import service_sync as _shared_sync

CLICKHOUSE_DATABASE = _shared_sync.CLICKHOUSE_DATABASE
SERVICE_SOURCE_TABLES = _shared_sync.SERVICE_SOURCE_TABLES
SERVICE_NAME_COLUMN_CANDIDATES = _shared_sync.SERVICE_NAME_COLUMN_CANDIDATES
TIMESTAMP_COLUMN_CANDIDATES = _shared_sync.TIMESTAMP_COLUMN_CANDIDATES
INVALID_SERVICE_NAMES = _shared_sync.INVALID_SERVICE_NAMES
BATCH_SIZE = _shared_sync.BATCH_SIZE

_normalize_service_name = _shared_sync._normalize_service_name
_parse_timestamp = _shared_sync._parse_timestamp
_pick_first_existing = _shared_sync._pick_first_existing
_fetch_clickhouse_table_columns = _shared_sync._fetch_clickhouse_table_columns
_collect_clickhouse_service_inventory = _shared_sync._collect_clickhouse_service_inventory
_fetch_neo4j_service_ids = _shared_sync._fetch_neo4j_service_ids
_chunked = _shared_sync._chunked
_build_coverage_stats = _shared_sync._build_coverage_stats
_build_source_summary = _shared_sync._build_source_summary

sync_services_from_logs = _shared_sync.sync_services_from_logs
get_sync_status = _shared_sync.get_sync_status

__all__ = [
    "CLICKHOUSE_DATABASE",
    "SERVICE_SOURCE_TABLES",
    "SERVICE_NAME_COLUMN_CANDIDATES",
    "TIMESTAMP_COLUMN_CANDIDATES",
    "INVALID_SERVICE_NAMES",
    "BATCH_SIZE",
    "_normalize_service_name",
    "_parse_timestamp",
    "_pick_first_existing",
    "_fetch_clickhouse_table_columns",
    "_collect_clickhouse_service_inventory",
    "_fetch_neo4j_service_ids",
    "_chunked",
    "_build_coverage_stats",
    "_build_source_summary",
    "sync_services_from_logs",
    "get_sync_status",
]
