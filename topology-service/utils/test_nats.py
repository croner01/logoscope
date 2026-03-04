"""NATS test adapter (shared implementation)."""

import asyncio
import os
import sys

_SHARED_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "shared_src"))
if os.path.isdir(_SHARED_SRC_DIR) and _SHARED_SRC_DIR not in sys.path:
    sys.path.append(_SHARED_SRC_DIR)

from shared_src.utils.test_nats import main, test_nats_connection

__all__ = ["test_nats_connection", "main"]


if __name__ == "__main__":
    asyncio.run(main())
