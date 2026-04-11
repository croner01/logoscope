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
        self.queue_type = str(os.getenv("QUEUE_TYPE", "kafka") or "kafka").strip().lower()
        self.kafka_brokers = os.getenv("KAFKA_BROKERS", "kafka:9092")
        self.kafka_group_id = os.getenv("KAFKA_GROUP_ID", "log-workers")
        self.kafka_client_id = os.getenv("KAFKA_CLIENT_ID", self.app_name)
        self.kafka_auto_offset_reset = os.getenv("KAFKA_AUTO_OFFSET_RESET", "latest")
        self.kafka_poll_timeout_ms = self.parse_int_env("KAFKA_POLL_TIMEOUT_MS", 1000, min_value=100)
        self.kafka_max_poll_interval_ms = self.parse_int_env("KAFKA_MAX_POLL_INTERVAL_MS", 300000, min_value=1000)
        self.kafka_session_timeout_ms = self.parse_int_env("KAFKA_SESSION_TIMEOUT_MS", 45000, min_value=6000)
        self.kafka_heartbeat_interval_ms = self.parse_int_env("KAFKA_HEARTBEAT_INTERVAL_MS", 3000, min_value=1000)
        self.kafka_group_per_stream = self.parse_bool_env("KAFKA_GROUP_PER_STREAM", True)
        self.kafka_callback_offload = self.parse_bool_env("KAFKA_CALLBACK_OFFLOAD", True)
        self.kafka_flush_offload = self.parse_bool_env("KAFKA_FLUSH_OFFLOAD", True)
        self.kafka_commit_error_as_warning = self.parse_bool_env("KAFKA_COMMIT_ERROR_AS_WARNING", True)
        self.kafka_max_batch_size = self.parse_int_env("KAFKA_MAX_BATCH_SIZE", 200, min_value=1)
        self.kafka_max_retry_attempts = self.parse_int_env("KAFKA_MAX_RETRY_ATTEMPTS", 3, min_value=1)
        self.kafka_retry_delay_seconds = self.parse_int_env("KAFKA_RETRY_DELAY_SECONDS", 2, min_value=0)

        # Keep lowercase log level compatibility with existing startup command.
        self.log_level = os.getenv("LOG_LEVEL", "info")

    def get_clickhouse_config(self) -> Dict[str, Any]:
        return super().get_clickhouse_config()

    def get_neo4j_config(self) -> Dict[str, Any]:
        return super().get_neo4j_config()

    def get_storage_config(self) -> Dict[str, Any]:
        return super().get_storage_config()


config = Config()
