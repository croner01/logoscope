"""Query service OpenTelemetry adapter (shared implementation)."""

import os
import sys

_SHARED_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "shared_src"))
if os.path.isdir(_SHARED_SRC_DIR) and _SHARED_SRC_DIR not in sys.path:
    sys.path.append(_SHARED_SRC_DIR)

from shared_src.utils.otel_init import init_otel, init_opentelemetry

__all__ = ["init_otel", "init_opentelemetry"]
