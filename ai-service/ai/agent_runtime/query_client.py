"""
Query-service HTTP client for local log lookups.

Routes simple ClickHouse log queries to query-service /api/v1/logs
instead of going through kubectl exec into ClickHouse pods.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any, Dict, List, Optional

import requests


class QueryServiceClientError(RuntimeError):
    """Raised when query-service returns an invalid or failed response."""


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


_SIMPLE_SELECT_RE = re.compile(
    r"^\s*SELECT\s+(?!.*\bGROUP\s+BY\b)(?!.*\bJOIN\b)(?!.*\bUNION\b)(?!.*\bHAVING\b)"
    r"(?!.*\bOVER\s*\()"
    r".*?\bFROM\s+logs\.events\b",
    re.IGNORECASE | re.DOTALL,
)

_CONDITION_RE = re.compile(
    r"(?:WHERE|AND)\s+(\w+)\s*=\s*'([^']*)'",
    re.IGNORECASE,
)

_LIMIT_RE = re.compile(r"LIMIT\s+(\d+)", re.IGNORECASE)
_ORDER_BY_RE = re.compile(r"ORDER\s+BY\s+(\w+)\s*(DESC|ASC)?", re.IGNORECASE)


class QueryServiceClient:
    """Calls query-service /api/v1/logs for local log lookups.

    Translates simple ClickHouse SELECT queries from ``kubectl_clickhouse_query``
    command_specs into query-service filter parameters, avoiding the overhead
    of kubectl exec into ClickHouse pods.
    """

    def __init__(self, base_url: str | None = None):
        self._base_url = (base_url or os.getenv("QUERY_SERVICE_BASE_URL", "http://query-service:8092")).rstrip("/")

    # ── public API ──────────────────────────────────────────────────────────

    def translate_clickhouse_spec(self, command_spec: Dict[str, Any]) -> Dict[str, Any] | None:
        """Try to translate a kubectl_clickhouse_query spec to query-service params.

        Returns None if the SQL is too complex for query-service (needs remote).
        """
        args = command_spec.get("args") if isinstance(command_spec.get("args"), dict) else {}
        query = _as_str(args.get("query") or command_spec.get("query"))
        if not query:
            return None

        if not _SIMPLE_SELECT_RE.search(query):
            return None

        params: Dict[str, Any] = {}

        # Extract WHERE conditions
        for match in _CONDITION_RE.finditer(query):
            col = match.group(1).lower()
            val = match.group(2)
            if col == "service_name":
                params["service_name"] = val
            elif col == "namespace":
                params["namespace"] = val
            elif col == "pod_name":
                params["pod_name"] = val
            elif col == "trace_id":
                params["trace_id"] = val
            elif col == "level" or col == "level_norm":
                params["level"] = val.upper()
            elif col == "container_name":
                params["container_name"] = val

        # Extract LIMIT
        limit_match = _LIMIT_RE.search(query)
        if limit_match:
            params["limit"] = int(limit_match.group(1))

        # Extract ORDER BY for cursor direction
        order_match = _ORDER_BY_RE.search(query)
        if order_match:
            params["order_by"] = order_match.group(1)
            if order_match.group(2):
                params["order_dir"] = order_match.group(2).upper()

        return params if len(params) > 1 else None  # need more than just limit

    async def query_logs(
        self,
        *,
        service_name: str | None = None,
        namespace: str | None = None,
        pod_name: str | None = None,
        trace_id: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        level: str | None = None,
        search: str | None = None,
        container_name: str | None = None,
        limit: int = 200,
        timeout_seconds: int = 30,
    ) -> Dict[str, Any]:
        """Query logs via query-service /api/v1/logs.

        Returns a dict matching exec-service's command result shape so callers
        don't care which channel was used.
        """
        params = self._build_log_params(
            service_name=service_name,
            namespace=namespace,
            pod_name=pod_name,
            trace_id=trace_id,
            start_time=start_time,
            end_time=end_time,
            level=level,
            search=search,
            container_name=container_name,
            limit=limit,
        )
        started_at = time.monotonic()
        result = await self._request("GET", "/api/v1/logs", params=params, timeout_seconds=timeout_seconds)
        duration_ms = int((time.monotonic() - started_at) * 1000)

        events = result.get("events") if isinstance(result, dict) else []
        if not isinstance(events, list):
            events = []
        total_count = _as_int(result.get("total") or result.get("count") or len(events))

        return self._to_command_result(
            events=events,
            total_count=total_count,
            duration_ms=duration_ms,
        )

    # ── internal helpers ────────────────────────────────────────────────────

    def _build_log_params(
        self,
        *,
        service_name: str | None = None,
        namespace: str | None = None,
        pod_name: str | None = None,
        trace_id: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        level: str | None = None,
        search: str | None = None,
        container_name: str | None = None,
        limit: int = 200,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": max(1, min(int(limit or 200), 1000))}
        for key, val in (
            ("service_name", service_name),
            ("namespace", namespace),
            ("pod_name", pod_name),
            ("trace_id", trace_id),
            ("start_time", start_time),
            ("end_time", end_time),
            ("container_name", container_name),
        ):
            text = _as_str(val).strip()
            if text:
                params[key] = text
        level_text = _as_str(level).strip().upper()
        if level_text in {"TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"}:
            params["level"] = level_text
        search_text = _as_str(search).strip()
        if search_text:
            params["search"] = search_text
        return params

    def _to_command_result(
        self,
        *,
        events: List[Dict[str, Any]],
        total_count: int,
        duration_ms: int,
    ) -> Dict[str, Any]:
        return {
            "status": "completed",
            "exit_code": 0,
            "stdout": json.dumps({"events": events, "total_count": total_count}, ensure_ascii=False),
            "stderr": "",
            "duration_ms": duration_ms,
            "total_count": total_count,
            "output_truncated": len(events) < total_count,
            "command_type": "query",
            "risk_level": "low",
            "command_family": "clickhouse",
            "executor_type": "query_service",
            "executor_profile": "query-service-readonly",
            "target_kind": "clickhouse_cluster",
            "target_identity": "database:logs",
            "timed_out": False,
            "error_code": "",
            "error_detail": "",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Dict[str, Any] | None = None,
        timeout_seconds: int = 30,
    ) -> Dict[str, Any]:
        endpoint = f"{self._base_url}{path}"

        def _do_request() -> Dict[str, Any]:
            try:
                response = requests.request(
                    method=method.upper(),
                    url=endpoint,
                    params=params if isinstance(params, dict) else None,
                    timeout=(3, max(3, int(timeout_seconds))),
                )
            except Exception as exc:
                raise QueryServiceClientError(f"query-service unavailable: {exc}") from exc

            if int(response.status_code) >= 400:
                raise QueryServiceClientError(
                    f"query-service request failed status={response.status_code} path={path}"
                )
            try:
                body = response.json()
            except Exception:
                return {}
            return body if isinstance(body, dict) else {}

        if os.environ.get("PYTEST_CURRENT_TEST") is not None:
            return _do_request()
        return await asyncio.to_thread(_do_request)


__all__ = [
    "QueryServiceClient",
    "QueryServiceClientError",
]
