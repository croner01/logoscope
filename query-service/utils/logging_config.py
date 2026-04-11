"""Query service logging config compatibility wrapper."""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
from types import ModuleType


logger = logging.getLogger(__name__)


def _shared_lib_candidates() -> list[str]:
    query_service_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return [
        os.getenv("LOGOSCOPE_SHARED_LIB", ""),
        os.path.abspath(os.path.join(query_service_dir, "..", "shared_src")),
        "/app/shared_lib",
        "/app/shared_src",
    ]


def _load_shared_logging_config() -> ModuleType:
    try:
        return importlib.import_module("shared_src.utils.logging_config")
    except Exception as exc:
        logger.warning(
            "Failed to import shared_src.utils.logging_config directly, trying fallback paths: %s",
            exc,
        )

    for candidate in _shared_lib_candidates():
        if not candidate:
            continue
        module_file = os.path.join(candidate, "utils", "logging_config.py")
        if not os.path.isfile(module_file):
            continue
        if os.path.abspath(module_file) == os.path.abspath(__file__):
            continue
        spec = importlib.util.spec_from_file_location(
            "_logoscope_shared_logging_config",
            module_file,
        )
        if not spec or not spec.loader:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    raise ImportError("Unable to load shared_src.utils.logging_config from known locations")


_shared_logging_config = _load_shared_logging_config()

# Re-export all public/private attributes for backward compatibility with tests
# and existing `import utils.logging_config as logging_config` call sites.
for _name in dir(_shared_logging_config):
    if _name.startswith("__"):
        continue
    globals()[_name] = getattr(_shared_logging_config, _name)

__all__ = getattr(
    _shared_logging_config,
    "__all__",
    [name for name in globals() if not name.startswith("__")],
)
