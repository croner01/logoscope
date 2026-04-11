"""Semantic engine deduplication adapter (shared implementation)."""

import os
import sys
from typing import Any, Dict, Optional

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_SHARED_SRC_DIR = os.path.join(_PROJECT_ROOT, "shared_src")
for _path in (_PROJECT_ROOT, _SHARED_SRC_DIR):
    if os.path.isdir(_path) and _path not in sys.path:
        sys.path.append(_path)

from shared_src.storage import deduplication as _shared_dedup

DataDeduplicator = _shared_dedup.DataDeduplicator
_sanitize_interval = _shared_dedup._sanitize_interval
_sanitize_limit = _shared_dedup._sanitize_limit

_deduplicator: Optional[DataDeduplicator] = None


def get_deduplicator(storage_adapter) -> DataDeduplicator:
    """Return cached deduplicator instance for backward compatibility."""
    global _deduplicator
    if _deduplicator is None:
        _deduplicator = DataDeduplicator(storage_adapter)
    return _deduplicator


def save_event_with_deduplication(storage_adapter, event: Dict[str, Any]) -> bool:
    """Check duplication before save while preserving historical API."""
    deduplicator = get_deduplicator(storage_adapter)
    is_duplicate, reason = deduplicator.is_duplicate_event(event)
    if is_duplicate:
        _shared_dedup.logger.debug("Skipping duplicate event: %s", reason)
        return True
    return storage_adapter.save_event(event)


__all__ = [
    "DataDeduplicator",
    "_sanitize_interval",
    "_sanitize_limit",
    "_deduplicator",
    "get_deduplicator",
    "save_event_with_deduplication",
]
