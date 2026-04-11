"""
Subprocess worker entrypoint for hybrid topology build.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, Dict

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from graph.hybrid_topology import get_hybrid_topology_builder
from storage.adapter import StorageAdapter

logger = logging.getLogger(__name__)


def _emit_response(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, default=str), flush=True)


def main() -> int:
    storage = None
    try:
        raw_input = sys.stdin.read()
        request = json.loads(raw_input or "{}")
        if not isinstance(request, dict):
            raise ValueError("request payload must be JSON object")

        storage_config = request.get("storage_config") or {}
        build_kwargs = request.get("build_kwargs") or {}
        if not isinstance(storage_config, dict):
            raise ValueError("storage_config must be object")
        if not isinstance(build_kwargs, dict):
            raise ValueError("build_kwargs must be object")

        storage = StorageAdapter(config=storage_config)
        builder = get_hybrid_topology_builder(storage)
        topology = builder.build_topology(**build_kwargs)
        _emit_response({"ok": True, "result": topology})
        return 0
    except Exception as exc:
        _emit_response({
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        })
        return 1
    finally:
        if storage is not None:
            try:
                storage.close()
            except Exception as exc:
                logger.warning("Failed to close storage in topology build worker: %s", exc)


if __name__ == "__main__":
    raise SystemExit(main())
