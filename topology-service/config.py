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
        self.TOPOLOGY_BUILD_PROCESS_ISOLATION_ENABLED = self.parse_bool_env(
            "TOPOLOGY_BUILD_PROCESS_ISOLATION_ENABLED",
            True,
        )
        self.TOPOLOGY_BUILD_PROCESS_TIMEOUT_SECONDS = self.parse_int_env(
            "TOPOLOGY_BUILD_PROCESS_TIMEOUT_SECONDS",
            45,
            min_value=3,
        )
        self.TOPOLOGY_BUILD_PROCESS_FALLBACK_LOCAL_ON_ERROR = self.parse_bool_env(
            "TOPOLOGY_BUILD_PROCESS_FALLBACK_LOCAL_ON_ERROR",
            True,
        )
        self.TOPOLOGY_BUILD_PROCESS_MAX_CONCURRENCY = self.parse_int_env(
            "TOPOLOGY_BUILD_PROCESS_MAX_CONCURRENCY",
            2,
            min_value=1,
        )
        self.TOPOLOGY_BUILD_PROCESS_MAX_QUEUE_SIZE = self.parse_int_env(
            "TOPOLOGY_BUILD_PROCESS_MAX_QUEUE_SIZE",
            64,
            min_value=1,
        )
        self.TOPOLOGY_BUILD_PROCESS_ACQUIRE_TIMEOUT_SECONDS = self.parse_int_env(
            "TOPOLOGY_BUILD_PROCESS_ACQUIRE_TIMEOUT_SECONDS",
            2,
            min_value=1,
        )
        process_python = os.getenv("TOPOLOGY_BUILD_PROCESS_PYTHON", "")
        process_python = process_python.strip()
        self.TOPOLOGY_BUILD_PROCESS_PYTHON = process_python or None

    def get_clickhouse_config(self) -> Dict[str, Any]:
        return super().get_clickhouse_config()

    def get_neo4j_config(self) -> Dict[str, Any]:
        return super().get_neo4j_config()

    def get_storage_config(self) -> Dict[str, Any]:
        return super().get_storage_config()


config = Config()
settings = config
