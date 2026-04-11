"""Timestamp utils adapter (shared implementation)."""

import os
import sys

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if os.path.isdir(_PROJECT_ROOT) and _PROJECT_ROOT not in sys.path:
    sys.path.append(_PROJECT_ROOT)

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
