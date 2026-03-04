"""Semantic engine confidence calculator adapter (shared implementation)."""

import os
import sys

_SHARED_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "shared_src"))
if os.path.isdir(_SHARED_SRC_DIR) and _SHARED_SRC_DIR not in sys.path:
    sys.path.append(_SHARED_SRC_DIR)

from shared_src.graph import confidence_calculator as _shared_confidence

ConfidenceCalculator = _shared_confidence.ConfidenceCalculator
get_confidence_calculator = _shared_confidence.get_confidence_calculator

__all__ = ["ConfidenceCalculator", "get_confidence_calculator"]
