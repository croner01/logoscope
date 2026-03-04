"""Hybrid topology enhancement adapter (shared implementation)."""

import os
import sys

_SHARED_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "shared_src"))
if os.path.isdir(_SHARED_SRC_DIR) and _SHARED_SRC_DIR not in sys.path:
    sys.path.append(_SHARED_SRC_DIR)

from shared_src.graph import hybrid_topology_enhanced as _shared_enhanced

EnhancedTopologyMixin = _shared_enhanced.EnhancedTopologyMixin
apply_enhancements_to_builder = _shared_enhanced.apply_enhancements_to_builder

__all__ = ["EnhancedTopologyMixin", "apply_enhancements_to_builder"]
