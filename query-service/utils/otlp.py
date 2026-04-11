"""OTLP utils adapter (shared implementation)."""

import os
import sys

_SHARED_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "shared_src"))
if os.path.isdir(_SHARED_SRC_DIR) and _SHARED_SRC_DIR not in sys.path:
    sys.path.append(_SHARED_SRC_DIR)

from shared_src.utils import otlp as _shared_otlp

parse_otlp_attributes = _shared_otlp.parse_otlp_attributes

__all__ = ["parse_otlp_attributes"]
