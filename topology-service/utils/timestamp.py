"""Timestamp utils adapter (shared implementation)."""

import os
import sys

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_SHARED_SRC_DIR = os.path.join(_PROJECT_ROOT, "shared_src")
for _path in (_PROJECT_ROOT, _SHARED_SRC_DIR):
    if os.path.isdir(_path) and _path not in sys.path:
        sys.path.append(_path)

from shared_src.utils import timestamp as _shared_timestamp

unix_nano_to_rfc3339 = _shared_timestamp.unix_nano_to_rfc3339
rfc3339_to_datetime64 = _shared_timestamp.rfc3339_to_datetime64
datetime64_to_rfc3339 = _shared_timestamp.datetime64_to_rfc3339
parse_any_timestamp = _shared_timestamp.parse_any_timestamp
validate_rfc3339 = _shared_timestamp.validate_rfc3339

__all__ = [
    "unix_nano_to_rfc3339",
    "rfc3339_to_datetime64",
    "datetime64_to_rfc3339",
    "parse_any_timestamp",
    "validate_rfc3339",
]
