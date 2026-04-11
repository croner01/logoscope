"""
Runtime history persistence for exec-service.

Backend modes:
1. `memory` (default for local unit tests)
2. `clickhouse` via `EXEC_RUNTIME_HISTORY_STORE_BACKEND=clickhouse` (recommended for k8s)
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import threading
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen


_LOCK = threading.RLock()
_CLICKHOUSE_SCHEMA_READY = False


def as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    normalized = as_str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _to_clickhouse_datetime(value: Any) -> str:
    raw = as_str(value).strip()
    if not raw:
        return "1970-01-01 00:00:00.000"
    candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(candidate)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            parsed = parsed.astimezone(timezone.utc)
        return parsed.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    except Exception:
        normalized = raw.replace("T", " ").replace("Z", "")
        if len(normalized) >= 6 and normalized[-6] in {"+", "-"} and normalized[-3] == ":":
            normalized = normalized[:-6]
        if "." in normalized:
            head, frac = normalized.split(".", 1)
            digits = "".join(ch for ch in frac if ch.isdigit())
            return f"{head}.{digits[:3].ljust(3, '0')}"
        return f"{normalized}.000"


def _backend() -> str:
    return as_str(os.getenv("EXEC_RUNTIME_HISTORY_STORE_BACKEND"), "memory").strip().lower()


def clickhouse_enabled() -> bool:
    return _backend() == "clickhouse"


def _clickhouse_fail_open() -> bool:
    return as_bool(os.getenv("EXEC_RUNTIME_HISTORY_CH_FAIL_OPEN"), False)


def _safe_identifier(value: str, default: str) -> str:
    normalized = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in as_str(value, default))
    normalized = normalized.strip("_")
    return normalized or default


def _clickhouse_url() -> str:
    return (
        as_str(os.getenv("EXEC_RUNTIME_HISTORY_CH_URL"))
        or as_str(os.getenv("EXEC_POLICY_DECISION_CH_URL"))
        or "http://clickhouse:8123"
    ).strip().rstrip("/")


def _clickhouse_database() -> str:
    raw = (
        as_str(os.getenv("EXEC_RUNTIME_HISTORY_CH_DATABASE"))
        or as_str(os.getenv("EXEC_POLICY_DECISION_CH_DATABASE"))
        or "logs"
    )
    return _safe_identifier(raw, "logs")


def _run_table() -> str:
    return _safe_identifier(
        as_str(os.getenv("EXEC_RUNTIME_HISTORY_RUN_TABLE"), "exec_command_runs"),
        "exec_command_runs",
    )


def _event_table() -> str:
    return _safe_identifier(
        as_str(os.getenv("EXEC_RUNTIME_HISTORY_EVENT_TABLE"), "exec_command_events"),
        "exec_command_events",
    )


def _audit_table() -> str:
    return _safe_identifier(
        as_str(os.getenv("EXEC_RUNTIME_HISTORY_AUDIT_TABLE"), "exec_command_audits"),
        "exec_command_audits",
    )


def _clickhouse_timeout_seconds() -> float:
    timeout_ms = as_str(os.getenv("EXEC_RUNTIME_HISTORY_CH_TIMEOUT_MS")) or as_str(
        os.getenv("EXEC_POLICY_DECISION_CH_TIMEOUT_MS"),
        "1200",
    )
    try:
        parsed = int(timeout_ms)
    except Exception:
        parsed = 1200
    bounded = max(50, min(8000, parsed))
    return float(bounded) / 1000.0


def _clickhouse_headers() -> Dict[str, str]:
    headers = {"Content-Type": "text/plain; charset=utf-8"}
    user = as_str(os.getenv("EXEC_RUNTIME_HISTORY_CH_USER") or os.getenv("EXEC_POLICY_DECISION_CH_USER")).strip()
    password = as_str(os.getenv("EXEC_RUNTIME_HISTORY_CH_PASSWORD") or os.getenv("EXEC_POLICY_DECISION_CH_PASSWORD")).strip()
    if user:
        headers["X-ClickHouse-User"] = user
    if password:
        headers["X-ClickHouse-Key"] = password
    return headers


def _handle_clickhouse_error(exc: Exception) -> None:
    if _clickhouse_fail_open():
        return
    raise RuntimeError(f"runtime history clickhouse backend unavailable: {as_str(exc)}") from exc


def _sql_str(value: Any) -> str:
    escaped = as_str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _clickhouse_execute(sql: str) -> str:
    if not clickhouse_enabled():
        return ""
    base_url = _clickhouse_url()
    params = {"database": _clickhouse_database()}
    url = f"{base_url}/?{urlencode(params)}"
    request = Request(
        url=url,
        data=as_str(sql).encode("utf-8"),
        headers=_clickhouse_headers(),
        method="POST",
    )
    with urlopen(request, timeout=_clickhouse_timeout_seconds()) as response:
        return response.read().decode("utf-8", errors="replace")


def _clickhouse_query_json_each_row(sql: str) -> List[Dict[str, Any]]:
    try:
        raw = _clickhouse_execute(sql)
    except Exception as exc:
        _handle_clickhouse_error(exc)
        return []
    rows: List[Dict[str, Any]] = []
    for line in raw.splitlines():
        payload = as_str(line).strip()
        if not payload:
            continue
        try:
            parsed = json.loads(payload)
        except Exception:
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


def _ensure_clickhouse_schema() -> None:
    global _CLICKHOUSE_SCHEMA_READY
    if not clickhouse_enabled():
        return
    with _LOCK:
        if _CLICKHOUSE_SCHEMA_READY:
            return
    database = _clickhouse_database()
    run_table = _run_table()
    event_table = _event_table()
    audit_table = _audit_table()

    create_db_sql = f"CREATE DATABASE IF NOT EXISTS {database}"
    create_run_sql = f"""
        CREATE TABLE IF NOT EXISTS {database}.{run_table} (
            run_id String,
            status LowCardinality(String),
            created_at DateTime64(3, 'UTC'),
            updated_at DateTime64(3, 'UTC'),
            record_json String
        )
        ENGINE = ReplacingMergeTree(updated_at)
        PARTITION BY toYYYYMM(created_at)
        ORDER BY (run_id, updated_at)
    """
    create_event_sql = f"""
        CREATE TABLE IF NOT EXISTS {database}.{event_table} (
            run_id String,
            event_id String,
            seq UInt64,
            event_type LowCardinality(String),
            created_at DateTime64(3, 'UTC'),
            record_json String
        )
        ENGINE = MergeTree()
        PARTITION BY toYYYYMM(created_at)
        ORDER BY (run_id, seq, created_at, event_id)
    """
    create_audit_sql = f"""
        CREATE TABLE IF NOT EXISTS {database}.{audit_table} (
            run_id String,
            audit_id String,
            created_at DateTime64(3, 'UTC'),
            record_json String
        )
        ENGINE = MergeTree()
        PARTITION BY toYYYYMM(created_at)
        ORDER BY (run_id, created_at, audit_id)
    """
    try:
        _clickhouse_execute(create_db_sql)
        _clickhouse_execute(create_run_sql)
        _clickhouse_execute(create_event_sql)
        _clickhouse_execute(create_audit_sql)
    except Exception as exc:
        _handle_clickhouse_error(exc)
        return
    with _LOCK:
        _CLICKHOUSE_SCHEMA_READY = True


def _canonical_json(payload: Dict[str, Any]) -> str:
    safe_payload = payload if isinstance(payload, dict) else {}
    return json.dumps(safe_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def persist_run_record(record: Dict[str, Any]) -> None:
    if not clickhouse_enabled():
        return
    _ensure_clickhouse_schema()
    payload = dict(record if isinstance(record, dict) else {})
    row = {
        "run_id": as_str(payload.get("run_id")),
        "status": as_str(payload.get("status")),
        "created_at": _to_clickhouse_datetime(payload.get("created_at")),
        "updated_at": _to_clickhouse_datetime(payload.get("updated_at") or payload.get("created_at")),
        "record_json": _canonical_json(payload),
    }
    sql = (
        f"INSERT INTO {_clickhouse_database()}.{_run_table()} FORMAT JSONEachRow\n"
        f"{json.dumps(row, ensure_ascii=False)}\n"
    )
    try:
        _clickhouse_execute(sql)
    except Exception as exc:
        _handle_clickhouse_error(exc)


def load_run_record(run_id: str) -> Optional[Dict[str, Any]]:
    if not clickhouse_enabled():
        return None
    _ensure_clickhouse_schema()
    sql = (
        f"SELECT record_json FROM {_clickhouse_database()}.{_run_table()} FINAL "
        f"WHERE run_id = {_sql_str(run_id)} "
        "ORDER BY updated_at DESC LIMIT 1 FORMAT JSONEachRow"
    )
    rows = _clickhouse_query_json_each_row(sql)
    if not rows:
        return None
    raw_json = as_str(rows[0].get("record_json"))
    if not raw_json:
        return None
    try:
        payload = json.loads(raw_json)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def list_run_records(limit: int = 100) -> List[Dict[str, Any]]:
    if not clickhouse_enabled():
        return []
    _ensure_clickhouse_schema()
    safe_limit = max(1, min(int(limit or 100), 1000))
    sql = (
        f"SELECT record_json FROM {_clickhouse_database()}.{_run_table()} FINAL "
        "ORDER BY updated_at DESC "
        f"LIMIT {safe_limit} FORMAT JSONEachRow"
    )
    rows = _clickhouse_query_json_each_row(sql)
    resolved_rows: List[Dict[str, Any]] = []
    for item in rows:
        raw_json = as_str(item.get("record_json"))
        if not raw_json:
            continue
        try:
            payload = json.loads(raw_json)
        except Exception:
            continue
        if isinstance(payload, dict):
            resolved_rows.append(payload)
    return resolved_rows


def persist_event_record(record: Dict[str, Any]) -> None:
    if not clickhouse_enabled():
        return
    _ensure_clickhouse_schema()
    payload = dict(record if isinstance(record, dict) else {})
    row = {
        "run_id": as_str(payload.get("run_id")),
        "event_id": as_str(payload.get("event_id")),
        "seq": max(0, int(payload.get("seq") or 0)),
        "event_type": as_str(payload.get("event_type")),
        "created_at": _to_clickhouse_datetime(payload.get("created_at")),
        "record_json": _canonical_json(payload),
    }
    sql = (
        f"INSERT INTO {_clickhouse_database()}.{_event_table()} FORMAT JSONEachRow\n"
        f"{json.dumps(row, ensure_ascii=False)}\n"
    )
    try:
        _clickhouse_execute(sql)
    except Exception as exc:
        _handle_clickhouse_error(exc)


def list_event_records(
    *,
    run_id: str,
    after_seq: int = 0,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    if not clickhouse_enabled():
        return []
    _ensure_clickhouse_schema()
    safe_after = max(0, int(after_seq or 0))
    safe_limit = max(1, min(int(limit or 500), 5000))
    sql = (
        f"SELECT record_json FROM {_clickhouse_database()}.{_event_table()} "
        f"WHERE run_id = {_sql_str(run_id)} AND seq > {safe_after} "
        "ORDER BY seq ASC "
        f"LIMIT {safe_limit} FORMAT JSONEachRow"
    )
    rows = _clickhouse_query_json_each_row(sql)
    resolved_rows: List[Dict[str, Any]] = []
    for item in rows:
        raw_json = as_str(item.get("record_json"))
        if not raw_json:
            continue
        try:
            payload = json.loads(raw_json)
        except Exception:
            continue
        if isinstance(payload, dict):
            resolved_rows.append(payload)
    return resolved_rows


def persist_audit_record(record: Dict[str, Any]) -> None:
    if not clickhouse_enabled():
        return
    _ensure_clickhouse_schema()
    payload = dict(record if isinstance(record, dict) else {})
    row = {
        "run_id": as_str(payload.get("run_id")),
        "audit_id": as_str(payload.get("audit_id"), f"audit-{uuid.uuid4().hex[:12]}"),
        "created_at": _to_clickhouse_datetime(payload.get("updated_at") or payload.get("created_at")),
        "record_json": _canonical_json(payload),
    }
    sql = (
        f"INSERT INTO {_clickhouse_database()}.{_audit_table()} FORMAT JSONEachRow\n"
        f"{json.dumps(row, ensure_ascii=False)}\n"
    )
    try:
        _clickhouse_execute(sql)
    except Exception as exc:
        _handle_clickhouse_error(exc)


def list_audit_records(*, limit: int = 100, run_id: str = "") -> List[Dict[str, Any]]:
    if not clickhouse_enabled():
        return []
    _ensure_clickhouse_schema()
    safe_limit = max(1, min(int(limit or 100), 5000))
    filters: List[str] = []
    if as_str(run_id).strip():
        filters.append(f"run_id = {_sql_str(run_id)}")
    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
    sql = (
        f"SELECT record_json FROM {_clickhouse_database()}.{_audit_table()} "
        f"{where_clause} "
        "ORDER BY created_at DESC "
        f"LIMIT {safe_limit} FORMAT JSONEachRow"
    )
    rows = _clickhouse_query_json_each_row(sql)
    resolved_rows: List[Dict[str, Any]] = []
    for item in rows:
        raw_json = as_str(item.get("record_json"))
        if not raw_json:
            continue
        try:
            payload = json.loads(raw_json)
        except Exception:
            continue
        if isinstance(payload, dict):
            resolved_rows.append(payload)
    return resolved_rows
