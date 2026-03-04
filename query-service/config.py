"""Query Service configuration module."""

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
    """Query Service config."""

    def __init__(self) -> None:
        super().__init__(app_name="query-service", default_port=8002)
        self.QUERY_SERVICE_PORT = self.port
        self.DEBUG = self.debug
        self.OTEL_ENABLED = self.parse_bool_env("OTEL_ENABLED", False)

        self.log_format = os.getenv("LOG_FORMAT", "text").lower()
        self.log_output = os.getenv("LOG_OUTPUT", "stdout").lower()
        self.log_level_map = {
            "DEBUG": 10,
            "INFO": 20,
            "WARN": 30,
            "WARNING": 30,
            "ERROR": 40,
            "CRITICAL": 50,
        }
        self.log_level_int = self.log_level_map.get(self.log_level, 20)

    def get_clickhouse_config(self) -> Dict[str, Any]:
        return super().get_clickhouse_config()

    def get_neo4j_config(self) -> Dict[str, Any]:
        return super().get_neo4j_config()

    def get_storage_config(self) -> Dict[str, Any]:
        return super().get_storage_config()


config = Config()
settings = config
