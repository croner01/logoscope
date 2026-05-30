"""Hybrid topology enhancement adapter (shared implementation)."""

import os
import sys

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_SHARED_SRC_DIR = os.path.join(_PROJECT_ROOT, "shared_src")
for _path in (_PROJECT_ROOT, _SHARED_SRC_DIR):
    if os.path.isdir(_path) and _path not in sys.path:
        sys.path.append(_path)

from shared_src.graph import hybrid_topology_enhanced as _shared_enhanced

EnhancedTopologyMixin = _shared_enhanced.EnhancedTopologyMixin
apply_enhancements_to_builder = _shared_enhanced.apply_enhancements_to_builder

__all__ = ["EnhancedTopologyMixin", "apply_enhancements_to_builder"]
