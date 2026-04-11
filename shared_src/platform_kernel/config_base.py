"""Shared base configuration class for service configs."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional


class BaseServiceConfig:
    """Base configuration with optional component blocks."""

    def __init__(
        self,
        *,
        app_name: str,
        default_port: int,
        enable_clickhouse: bool = True,
        enable_neo4j: bool = True,
        warn_if_empty_neo4j_password: bool = True,
    ) -> None:
        self._enable_clickhouse = enable_clickhouse
        self._enable_neo4j = enable_neo4j

        self.app_name = os.getenv("APP_NAME", app_name)
        self.app_version = os.getenv("APP_VERSION", "1.0.0")
        self.host = os.getenv("HOST", "0.0.0.0")
        self.port = self.parse_int_env("PORT", default_port, min_value=1)
        self.debug = self.parse_bool_env("DEBUG", False)

        if self._enable_clickhouse:
            self.clickhouse_host = os.getenv("CLICKHOUSE_HOST", "clickhouse")
            self.clickhouse_port = self.parse_port(os.getenv("CLICKHOUSE_PORT", "9000"))
            self.clickhouse_database = os.getenv("CLICKHOUSE_DATABASE", "logs")
            self.clickhouse_user = os.getenv("CLICKHOUSE_USER", "default")
            self.clickhouse_password = os.getenv("CLICKHOUSE_PASSWORD", "")

        if self._enable_neo4j:
            self.neo4j_host = os.getenv("NEO4J_HOST", "neo4j")
            self.neo4j_port = self.parse_port(os.getenv("NEO4J_PORT", "7687"))
            self.neo4j_user = os.getenv("NEO4J_USER", "neo4j")
            self.neo4j_password = os.getenv("NEO4J_PASSWORD", "")
            self.neo4j_database = os.getenv("NEO4J_DATABASE", "neo4j")
            if warn_if_empty_neo4j_password and not self.neo4j_password:
                logging.getLogger(__name__).warning(
                    "NEO4J_PASSWORD not set. Neo4j connection may fail if authentication is required."
                )

        self.log_level = os.getenv("LOG_LEVEL", "INFO").upper()

    @staticmethod
    def parse_port(raw: str) -> int:
        """Parse port from plain value or URL-like value."""
        value = str(raw or "").strip()
        if "://" in value:
            value = value.split(":")[-1]
        return int(value)

    @staticmethod
    def parse_bool_env(name: str, default: bool) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def parse_int_env(name: str, default: int, min_value: Optional[int] = None) -> int:
        raw = os.getenv(name)
        if raw is None:
            value = default
        else:
            try:
                value = int(str(raw).strip())
            except (TypeError, ValueError):
                value = default
        if min_value is not None:
            value = max(value, min_value)
        return value

    def get_clickhouse_config(self) -> Dict[str, Any]:
        if not self._enable_clickhouse:
            return {}
        return {
            "host": self.clickhouse_host,
            "port": self.clickhouse_port,
            "database": self.clickhouse_database,
            "user": self.clickhouse_user,
            "password": self.clickhouse_password,
        }

    def get_neo4j_config(self) -> Dict[str, Any]:
        if not self._enable_neo4j:
            return {}
        return {
            "host": self.neo4j_host,
            "port": self.neo4j_port,
            "user": self.neo4j_user,
            "password": self.neo4j_password,
            "database": self.neo4j_database,
        }

    def get_storage_config(self) -> Dict[str, Any]:
        config: Dict[str, Any] = {}
        if self._enable_clickhouse:
            config["clickhouse"] = self.get_clickhouse_config()
        if self._enable_neo4j:
            config["neo4j"] = self.get_neo4j_config()
        return config
