"""Ingest Service configuration module."""

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
    """Ingest Service config."""

    def __init__(self) -> None:
        super().__init__(
            app_name="ingest-service",
            default_port=8080,
            enable_clickhouse=False,
            enable_neo4j=False,
            enable_redis=True,
            warn_if_empty_neo4j_password=False,
        )

        # Keep previous defaults for stream names and processing behavior.
        self.redis_stream = os.getenv("REDIS_STREAM", "logs.raw")
        self.redis_stream_logs = os.getenv("REDIS_STREAM_LOGS", self.redis_stream)
        self.redis_stream_metrics = os.getenv("REDIS_STREAM_METRICS", "metrics.raw")
        self.redis_stream_traces = os.getenv("REDIS_STREAM_TRACES", "traces.raw")

        self.batch_size = self.parse_int_env("BATCH_SIZE", 100, min_value=1)
        self.batch_timeout = self.parse_int_env("BATCH_TIMEOUT", 5, min_value=1)
        self.memory_queue_max_size = self.parse_int_env("MEMORY_QUEUE_MAX_SIZE", 1000, min_value=1)

        # Keep lowercase log level compatibility with existing startup command.
        self.log_level = os.getenv("LOG_LEVEL", "info")

    def get_redis_config(self) -> Dict[str, Any]:
        return super().get_redis_config()


config = Config()
