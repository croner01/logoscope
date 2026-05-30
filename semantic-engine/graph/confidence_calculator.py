"""Semantic engine confidence calculator adapter (shared implementation)."""

import os
import sys

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_SHARED_SRC_DIR = os.path.join(_PROJECT_ROOT, "shared_src")
for _path in (_PROJECT_ROOT, _SHARED_SRC_DIR):
    if os.path.isdir(_path) and _path not in sys.path:
        sys.path.append(_path)

from shared_src.graph import confidence_calculator as _shared_confidence

ConfidenceCalculator = _shared_confidence.ConfidenceCalculator
get_confidence_calculator = _shared_confidence.get_confidence_calculator

__all__ = ["ConfidenceCalculator", "get_confidence_calculator"]
