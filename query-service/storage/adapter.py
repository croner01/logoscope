"""Query service storage adapter (shared implementation)."""

import os
import sys

_SHARED_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "shared_src"))
if os.path.isdir(_SHARED_SRC_DIR) and _SHARED_SRC_DIR not in sys.path:
    sys.path.append(_SHARED_SRC_DIR)

from logoscope_storage import adapter as _shared_adapter

StorageAdapter = _shared_adapter.StorageAdapter

_sanitize_interval = _shared_adapter._sanitize_interval
_sanitize_limit = _shared_adapter._sanitize_limit
_escape_sql_literal = _shared_adapter._escape_sql_literal
_read_int_env = _shared_adapter._read_int_env
_read_float_env = _shared_adapter._read_float_env
_compact_sql = _shared_adapter._compact_sql
_is_aggregation_query = _shared_adapter._is_aggregation_query

# Backward-compatible module-level knobs for tests/feature flags.
SLOW_QUERY_THRESHOLD_MS = _shared_adapter.SLOW_QUERY_THRESHOLD_MS
AGG_QUERY_LOG_SAMPLE_RATE = _shared_adapter.AGG_QUERY_LOG_SAMPLE_RATE
QUERY_LOG_MAX_CHARS = _shared_adapter.QUERY_LOG_MAX_CHARS
random = _shared_adapter.random


def _clip_sql(sql: str, max_chars: int = None) -> str:
    """Clip SQL text with module-level default for backward compatibility."""
    effective_max = QUERY_LOG_MAX_CHARS if max_chars is None else max_chars
    compacted = _compact_sql(sql)
    if len(compacted) <= effective_max:
        return compacted
    return f"{compacted[:effective_max]} ...[truncated]"


def _should_log_query_info(sql: str) -> bool:
    """Sampling helper kept patchable at module scope for regression tests."""
    if not _is_aggregation_query(sql):
        return True
    return random.random() < AGG_QUERY_LOG_SAMPLE_RATE


__all__ = [
    "StorageAdapter",
    "_sanitize_interval",
    "_sanitize_limit",
    "_escape_sql_literal",
    "_read_int_env",
    "_read_float_env",
    "_compact_sql",
    "_clip_sql",
    "_is_aggregation_query",
    "_should_log_query_info",
    "SLOW_QUERY_THRESHOLD_MS",
    "AGG_QUERY_LOG_SAMPLE_RATE",
    "QUERY_LOG_MAX_CHARS",
    "random",
]
