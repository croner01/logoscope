"""Pytest bootstrap for query-service tests."""

import os
import sys

_QUERY_SERVICE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _QUERY_SERVICE_ROOT not in sys.path:
    sys.path.insert(0, _QUERY_SERVICE_ROOT)

