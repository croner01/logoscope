"""Topology Service configuration module."""

from __future__ import annotations

import os
import sys
from typing import Any, Dict

_SHARED_LIB_CANDIDATES = (
    os.getenv("LOGOSCOPE_SHARED_LIB", ""),
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "shared_src")),
    "/app/shared_lib",
)
for _candidate in _SHARED_LIB_CANDIDATES:
    if _candidate and os.path.isdir(_candidate) and _candidate not in sys.path:
        sys.path.append(_candidate)

from platform_kernel.config_base import BaseServiceConfig


class Config(BaseServiceConfig):
    """Topology Service config."""

    def __init__(self) -> None:
        super().__init__(app_name="topology-service", default_port=8003)
        self.TOPOLOGY_SERVICE_PORT = self.port
        self.DEBUG = self.debug
        self.OTEL_ENABLED = self.parse_bool_env("OTEL_ENABLED", False)
        self.SERVICE_NODE_SYNC_ENABLED = self.parse_bool_env("SERVICE_NODE_SYNC_ENABLED", True)
        self.SERVICE_NODE_SYNC_INTERVAL_SECONDS = self.parse_int_env(
            "SERVICE_NODE_SYNC_INTERVAL_SECONDS",
            300,
            min_value=1,
        )

    def get_clickhouse_config(self) -> Dict[str, Any]:
        return super().get_clickhouse_config()

    def get_neo4j_config(self) -> Dict[str, Any]:
        return super().get_neo4j_config()

    def get_storage_config(self) -> Dict[str, Any]:
        return super().get_storage_config()


config = Config()
settings = config
