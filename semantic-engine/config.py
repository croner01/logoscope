"""Semantic Engine configuration module."""

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
    """Semantic Engine config."""

    def __init__(self) -> None:
        super().__init__(app_name="semantic-engine", default_port=8080)
        self.process_batch_size = self.parse_int_env("PROCESS_BATCH_SIZE", 10, min_value=1)
        self.process_timeout = self.parse_int_env("PROCESS_TIMEOUT", 30, min_value=1)

        self.use_queue = self.parse_bool_env("USE_QUEUE", False)
        self.queue_type = os.getenv("QUEUE_TYPE", "redis")
        self.nats_servers = os.getenv("NATS_SERVERS", "nats:4222")

        # Keep lowercase log level compatibility with existing startup command.
        self.log_level = os.getenv("LOG_LEVEL", "info")

    def get_clickhouse_config(self) -> Dict[str, Any]:
        return super().get_clickhouse_config()

    def get_neo4j_config(self) -> Dict[str, Any]:
        return super().get_neo4j_config()

    def get_redis_config(self) -> Dict[str, Any]:
        return super().get_redis_config()

    def get_storage_config(self) -> Dict[str, Any]:
        return super().get_storage_config()


config = Config()
