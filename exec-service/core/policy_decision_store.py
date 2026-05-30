"""
Policy decision store for audit/replay.

Backend modes:
1. `memory` (default for local unit tests)
2. `sqlite` via `EXEC_POLICY_DECISION_STORE_BACKEND=sqlite` (local/dev fallback)
3. `clickhouse` via `EXEC_POLICY_DECISION_STORE_BACKEND=clickhouse` (recommended for k8s)
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


_MAX_DECISIONS = 5000
_LOCK = threading.RLock()
_ORDER: List[str] = []
_STORE: Dict[str, Dict[str, Any]] = {}

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS policy_decisions (
    decision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    command_run_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    record_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_policy_decisions_run_created
    ON policy_decisions (run_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_policy_decisions_action_created
    ON policy_decisions (action_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_policy_decisions_result_created
    ON policy_decisions (result, created_at DESC);
"""

_CLICKHOUSE_SCHEMA_READY = False


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    return as_str(os.getenv("EXEC_POLICY_DECISION_STORE_BACKEND"), "memory").strip().lower()


def _sqlite_backend_selected() -> bool:
    return _backend() == "sqlite"


def _sqlite_allowed() -> bool:
    return as_bool(os.getenv("EXEC_POLICY_DECISION_SQLITE_ENABLED"), True)


def _assert_sqlite_backend_allowed() -> None:
    if _sqlite_backend_selected() and not _sqlite_allowed():
        raise RuntimeError("policy decision sqlite backend is disabled by EXEC_POLICY_DECISION_SQLITE_ENABLED=false")


def _sqlite_enabled() -> bool:
    return _sqlite_backend_selected() and _sqlite_allowed()


def _clickhouse_enabled() -> bool:
    return _backend() == "clickhouse"


def _sqlite_fail_open() -> bool:
    return as_bool(os.getenv("EXEC_POLICY_DECISION_SQLITE_FAIL_OPEN"), False)


def _clickhouse_fail_open() -> bool:
    return as_bool(os.getenv("EXEC_POLICY_DECISION_CH_FAIL_OPEN"), False)


def _backend_fail_open() -> bool:
    if _clickhouse_enabled():
        return _clickhouse_fail_open()
    if _sqlite_enabled():
        return _sqlite_fail_open()
    return False


def _sqlite_path() -> str:
    raw = as_str(os.getenv("EXEC_POLICY_DECISION_SQLITE_PATH"), "/tmp/exec-policy-decisions.sqlite3").strip()
    return raw or "/tmp/exec-policy-decisions.sqlite3"


def _safe_identifier(value: str, default: str) -> str:
    normalized = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in as_str(value, default))
    normalized = normalized.strip("_")
    return normalized or default


def _clickhouse_url() -> str:
    return as_str(os.getenv("EXEC_POLICY_DECISION_CH_URL"), "http://clickhouse:8123").strip().rstrip("/")


def _clickhouse_database() -> str:
    return _safe_identifier(
        as_str(os.getenv("EXEC_POLICY_DECISION_CH_DATABASE"), "logs"),
        "logs",
    )


def _clickhouse_table() -> str:
    return _safe_identifier(
        as_str(os.getenv("EXEC_POLICY_DECISION_CH_TABLE"), "exec_policy_decisions"),
        "exec_policy_decisions",
    )


def _clickhouse_timeout_seconds() -> float:
    timeout_ms = max(50, min(8000, int(as_str(os.getenv("EXEC_POLICY_DECISION_CH_TIMEOUT_MS"), "1200"))))
    return float(timeout_ms) / 1000.0


def _clickhouse_headers() -> Dict[str, str]:
    headers = {"Content-Type": "text/plain; charset=utf-8"}
    user = as_str(os.getenv("EXEC_POLICY_DECISION_CH_USER")).strip()
    password = as_str(os.getenv("EXEC_POLICY_DECISION_CH_PASSWORD")).strip()
    if user:
        headers["X-ClickHouse-User"] = user
    if password:
        headers["X-ClickHouse-Key"] = password
    return headers


def _sqlite_connect() -> sqlite3.Connection:
    path = _sqlite_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    connection = sqlite3.connect(path, timeout=2.0)
    connection.row_factory = sqlite3.Row
    connection.executescript(_SQLITE_SCHEMA)
    return connection


def _handle_sqlite_error(exc: Exception) -> None:
    if _sqlite_fail_open():
        return
    raise RuntimeError(f"policy decision sqlite backend unavailable: {as_str(exc)}") from exc


def _handle_clickhouse_error(exc: Exception) -> None:
    if _clickhouse_fail_open():
        return
    raise RuntimeError(f"policy decision clickhouse backend unavailable: {as_str(exc)}") from exc


def _sql_str(value: Any) -> str:
    escaped = as_str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _clickhouse_execute(sql: str) -> str:
    if not _clickhouse_enabled():
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
    if not _clickhouse_enabled():
        return
    with _LOCK:
        if _CLICKHOUSE_SCHEMA_READY:
            return
    database = _clickhouse_database()
    table = _clickhouse_table()
    create_db_sql = f"CREATE DATABASE IF NOT EXISTS {database}"
    create_table_sql = f"""
        CREATE TABLE IF NOT EXISTS {database}.{table} (
            decision_id String,
            run_id String,
            command_run_id String,
            action_id String,
            result LowCardinality(String),
            created_at DateTime64(3, 'UTC'),
            updated_at DateTime64(3, 'UTC'),
            record_json String
        )
        ENGINE = ReplacingMergeTree(updated_at)
        PARTITION BY toYYYYMM(created_at)
        ORDER BY (run_id, created_at, decision_id)
    """
    try:
        _clickhouse_execute(create_db_sql)
        _clickhouse_execute(create_table_sql)
    except Exception as exc:
        _handle_clickhouse_error(exc)
        return
    with _LOCK:
        _CLICKHOUSE_SCHEMA_READY = True


def _persist_record_to_clickhouse(record: Dict[str, Any]) -> None:
    if not _clickhouse_enabled():
        return
    _ensure_clickhouse_schema()
    payload = dict(record if isinstance(record, dict) else {})
    row = {
        "decision_id": as_str(payload.get("decision_id")),
        "run_id": as_str(payload.get("run_id")),
        "command_run_id": as_str(payload.get("command_run_id")),
        "action_id": as_str(payload.get("action_id")),
        "result": as_str(payload.get("result")),
        "created_at": _to_clickhouse_datetime(payload.get("created_at")),
        "updated_at": _to_clickhouse_datetime(payload.get("updated_at")),
        "record_json": _canonical_json(payload),
    }
    database = _clickhouse_database()
    table = _clickhouse_table()
    sql = f"INSERT INTO {database}.{table} FORMAT JSONEachRow\n{json.dumps(row, ensure_ascii=False)}\n"
    try:
        _clickhouse_execute(sql)
    except Exception as exc:
        _handle_clickhouse_error(exc)


def _load_record_from_clickhouse(decision_id: str) -> Optional[Dict[str, Any]]:
    if not _clickhouse_enabled():
        return None
    _ensure_clickhouse_schema()
    database = _clickhouse_database()
    table = _clickhouse_table()
    sql = (
        f"SELECT record_json FROM {database}.{table} FINAL "
        f"WHERE decision_id = {_sql_str(decision_id)} "
        "ORDER BY updated_at DESC LIMIT 1 FORMAT JSONEachRow"
    )
    rows = _clickhouse_query_json_each_row(sql)
    if not rows:
        return None
    raw_json = as_str((rows[0] if isinstance(rows[0], dict) else {}).get("record_json"))
    try:
        payload = json.loads(raw_json)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _list_records_from_clickhouse(
    *,
    limit: int,
    run_id: str,
    action_id: str,
    result: str,
) -> List[Dict[str, Any]]:
    if not _clickhouse_enabled():
        return []
    _ensure_clickhouse_schema()
    database = _clickhouse_database()
    table = _clickhouse_table()
    filters: List[str] = []
    if as_str(run_id).strip():
        filters.append(f"run_id = {_sql_str(run_id)}")
    if as_str(action_id).strip():
        filters.append(f"action_id = {_sql_str(action_id)}")
    if as_str(result).strip():
        filters.append(f"lower(result) = lower({_sql_str(result)})")
    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
    safe_limit = max(1, min(int(limit or 100), 1000))
    sql = (
        f"SELECT record_json FROM {database}.{table} FINAL "
        f"{where_clause} "
        "ORDER BY created_at DESC "
        f"LIMIT {safe_limit} FORMAT JSONEachRow"
    )
    rows = _clickhouse_query_json_each_row(sql)
    resolved_rows: List[Dict[str, Any]] = []
    for item in rows:
        raw_json = as_str((item if isinstance(item, dict) else {}).get("record_json"))
        if not raw_json:
            continue
        try:
            payload = json.loads(raw_json)
        except Exception:
            continue
        if isinstance(payload, dict):
            resolved_rows.append(payload)
    return resolved_rows


def _clear_records_from_clickhouse() -> None:
    if not _clickhouse_enabled():
        return
    _ensure_clickhouse_schema()
    database = _clickhouse_database()
    table = _clickhouse_table()
    sql = f"TRUNCATE TABLE {database}.{table}"
    try:
        _clickhouse_execute(sql)
    except Exception as exc:
        _handle_clickhouse_error(exc)


def _cache_record(record: Dict[str, Any]) -> None:
    safe_record = record if isinstance(record, dict) else {}
    decision_id = as_str(safe_record.get("decision_id"))
    if not decision_id:
        return
    _STORE[decision_id] = dict(safe_record)
    if decision_id in _ORDER:
        _ORDER.remove(decision_id)
    _ORDER.append(decision_id)
    if len(_ORDER) > _MAX_DECISIONS:
        stale = _ORDER[:-_MAX_DECISIONS]
        _ORDER[:] = _ORDER[-_MAX_DECISIONS:]
        for item in stale:
            _STORE.pop(item, None)


def clear_policy_decision_cache() -> None:
    with _LOCK:
        _ORDER.clear()
        _STORE.clear()


def build_decision_id() -> str:
    return f"dec-{uuid.uuid4().hex[:12]}"


def _canonical_json(payload: Dict[str, Any]) -> str:
    safe_payload = payload if isinstance(payload, dict) else {}
    return json.dumps(safe_payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def build_input_hash(payload: Dict[str, Any]) -> str:
    encoded = _canonical_json(payload).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _persist_record_to_sqlite(record: Dict[str, Any]) -> None:
    if not _sqlite_enabled():
        return
    payload = dict(record if isinstance(record, dict) else {})
    try:
        with _sqlite_connect() as conn:
            conn.execute(
                """
                INSERT INTO policy_decisions (
                    decision_id, run_id, command_run_id, action_id, result, created_at, updated_at, record_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(decision_id) DO UPDATE SET
                    run_id=excluded.run_id,
                    command_run_id=excluded.command_run_id,
                    action_id=excluded.action_id,
                    result=excluded.result,
                    updated_at=excluded.updated_at,
                    record_json=excluded.record_json
                """,
                (
                    as_str(payload.get("decision_id")),
                    as_str(payload.get("run_id")),
                    as_str(payload.get("command_run_id")),
                    as_str(payload.get("action_id")),
                    as_str(payload.get("result")),
                    as_str(payload.get("created_at")),
                    as_str(payload.get("updated_at")),
                    _canonical_json(payload),
                ),
            )
    except Exception as exc:
        _handle_sqlite_error(exc)


def _load_record_from_sqlite(decision_id: str) -> Optional[Dict[str, Any]]:
    if not _sqlite_enabled():
        return None
    try:
        with _sqlite_connect() as conn:
            row = conn.execute(
                "SELECT record_json FROM policy_decisions WHERE decision_id = ?",
                (as_str(decision_id),),
            ).fetchone()
    except Exception as exc:
        _handle_sqlite_error(exc)
        return None
    if row is None:
        return None
    raw_json = as_str(row["record_json"])
    try:
        payload = json.loads(raw_json)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _list_records_from_sqlite(
    *,
    limit: int,
    run_id: str,
    action_id: str,
    result: str,
) -> List[Dict[str, Any]]:
    if not _sqlite_enabled():
        return []
    filters: List[str] = []
    values: List[Any] = []
    if as_str(run_id).strip():
        filters.append("run_id = ?")
        values.append(as_str(run_id))
    if as_str(action_id).strip():
        filters.append("action_id = ?")
        values.append(as_str(action_id))
    if as_str(result).strip():
        filters.append("LOWER(result) = ?")
        values.append(as_str(result).strip().lower())
    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
    sql = (
        "SELECT record_json FROM policy_decisions "
        f"{where_clause} "
        "ORDER BY created_at DESC "
        "LIMIT ?"
    )
    values.append(int(limit))
    try:
        with _sqlite_connect() as conn:
            rows = conn.execute(sql, tuple(values)).fetchall()
    except Exception as exc:
        _handle_sqlite_error(exc)
        return []
    resolved_rows: List[Dict[str, Any]] = []
    for row in rows:
        raw_json = as_str(row["record_json"])
        try:
            payload = json.loads(raw_json)
        except Exception:
            continue
        if isinstance(payload, dict):
            resolved_rows.append(payload)
    return resolved_rows


def clear_policy_decisions() -> None:
    _assert_sqlite_backend_allowed()
    clear_policy_decision_cache()
    if _sqlite_enabled():
        try:
            with _sqlite_connect() as conn:
                conn.execute("DELETE FROM policy_decisions")
        except Exception as exc:
            _handle_sqlite_error(exc)
        return
    if _clickhouse_enabled():
        _clear_records_from_clickhouse()


def record_policy_decision(
    *,
    session_id: str,
    message_id: str,
    action_id: str,
    run_id: str,
    command_run_id: str,
    command: str,
    purpose: str,
    command_type: str,
    risk_level: str,
    command_family: str,
    approval_policy: str,
    target_kind: str,
    target_identity: str,
    executor_type: str,
    executor_profile: str,
    dispatch_backend: str,
    dispatch_mode: str,
    dispatch_reason: str,
    dispatch_ready: bool,
    dispatch_degraded: bool,
    whitelist_match: bool,
    whitelist_reason: str,
    status: str,
    result: str,
    reason: str,
    policy_engine: str,
    policy_package: str,
    input_payload: Dict[str, Any],
    policy_mode: str = "local",
    decision_source: str = "local",
    local_result: str = "",
    local_reason: str = "",
    opa_available: bool = False,
    opa_result: str = "",
    opa_reason: str = "",
    opa_package: str = "",
) -> Dict[str, Any]:
    _assert_sqlite_backend_allowed()
    now_iso = utc_now_iso()
    decision_id = build_decision_id()
    record = {
        "decision_id": decision_id,
        "run_id": as_str(run_id),
        "command_run_id": as_str(command_run_id),
        "session_id": as_str(session_id),
        "message_id": as_str(message_id),
        "action_id": as_str(action_id),
        "command": as_str(command),
        "purpose": as_str(purpose),
        "command_type": as_str(command_type, "unknown"),
        "risk_level": as_str(risk_level, "high"),
        "command_family": as_str(command_family, "unknown"),
        "approval_policy": as_str(approval_policy, "deny"),
        "target_kind": as_str(target_kind, "runtime_node"),
        "target_identity": as_str(target_identity, "runtime:local"),
        "executor_type": as_str(executor_type, "local_process"),
        "executor_profile": as_str(executor_profile, "local-default"),
        "dispatch_backend": as_str(dispatch_backend),
        "dispatch_mode": as_str(dispatch_mode),
        "dispatch_reason": as_str(dispatch_reason),
        "dispatch_ready": as_bool(dispatch_ready, False),
        "dispatch_degraded": as_bool(dispatch_degraded, False),
        "whitelist_match": as_bool(whitelist_match, False),
        "whitelist_reason": as_str(whitelist_reason),
        "status": as_str(status, "permission_required"),
        "result": as_str(result, "deny"),
        "reason": as_str(reason),
        "engine": as_str(policy_engine, "python-inline"),
        "package": as_str(policy_package, "runtime.command.v1"),
        "mode": as_str(policy_mode, "local"),
        "source": as_str(decision_source, "local"),
        "local_result": as_str(local_result),
        "local_reason": as_str(local_reason),
        "opa_available": as_bool(opa_available, False),
        "opa_result": as_str(opa_result),
        "opa_reason": as_str(opa_reason),
        "opa_package": as_str(opa_package),
        "input_hash": build_input_hash(input_payload),
        "input_payload": input_payload if isinstance(input_payload, dict) else {},
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    with _LOCK:
        _cache_record(record)
    if _sqlite_enabled():
        _persist_record_to_sqlite(record)
    elif _clickhouse_enabled():
        _persist_record_to_clickhouse(record)
    return dict(record)


def bind_decision_to_run(decision_id: str, *, run_id: str, command_run_id: str = "") -> Optional[Dict[str, Any]]:
    _assert_sqlite_backend_allowed()
    safe_decision_id = as_str(decision_id)
    if not safe_decision_id:
        return None
    with _LOCK:
        record = _STORE.get(safe_decision_id)
    if not isinstance(record, dict):
        if _sqlite_enabled():
            record = _load_record_from_sqlite(safe_decision_id)
        elif _clickhouse_enabled():
            record = _load_record_from_clickhouse(safe_decision_id)
        if not isinstance(record, dict):
            return None
    record["run_id"] = as_str(run_id)
    if as_str(command_run_id):
        record["command_run_id"] = as_str(command_run_id)
    record["updated_at"] = utc_now_iso()
    with _LOCK:
        _cache_record(record)
    if _sqlite_enabled():
        _persist_record_to_sqlite(record)
    elif _clickhouse_enabled():
        _persist_record_to_clickhouse(record)
    return dict(record)


def get_policy_decision(decision_id: str) -> Optional[Dict[str, Any]]:
    _assert_sqlite_backend_allowed()
    safe_decision_id = as_str(decision_id)
    if not safe_decision_id:
        return None
    with _LOCK:
        record = _STORE.get(safe_decision_id)
    if isinstance(record, dict):
        return dict(record)
    if _sqlite_enabled():
        record = _load_record_from_sqlite(safe_decision_id)
    elif _clickhouse_enabled():
        record = _load_record_from_clickhouse(safe_decision_id)
    else:
        record = None
    if not isinstance(record, dict):
        return None
    with _LOCK:
        _cache_record(record)
    return dict(record)


def list_policy_decisions(
    *,
    limit: int = 100,
    run_id: str = "",
    action_id: str = "",
    result: str = "",
) -> List[Dict[str, Any]]:
    _assert_sqlite_backend_allowed()
    safe_limit = max(1, min(int(limit or 100), 1000))
    safe_run_id = as_str(run_id).strip()
    safe_action_id = as_str(action_id).strip()
    safe_result = as_str(result).strip().lower()
    backend_rows: List[Dict[str, Any]] = []
    if _sqlite_enabled():
        backend_rows = _list_records_from_sqlite(
            limit=safe_limit,
            run_id=safe_run_id,
            action_id=safe_action_id,
            result=safe_result,
        )
    elif _clickhouse_enabled():
        backend_rows = _list_records_from_clickhouse(
            limit=safe_limit,
            run_id=safe_run_id,
            action_id=safe_action_id,
            result=safe_result,
        )
    if backend_rows:
        with _LOCK:
            for row in backend_rows:
                _cache_record(row)
        return [dict(item) for item in backend_rows]
    with _LOCK:
        decision_ids = list(reversed(_ORDER))
    rows: List[Dict[str, Any]] = []
    with _LOCK:
        for decision_id in decision_ids:
            record = _STORE.get(decision_id)
            if not isinstance(record, dict):
                continue
            if safe_run_id and as_str(record.get("run_id")) != safe_run_id:
                continue
            if safe_action_id and as_str(record.get("action_id")) != safe_action_id:
                continue
            if safe_result and as_str(record.get("result")).strip().lower() != safe_result:
                continue
            rows.append(dict(record))
            if len(rows) >= safe_limit:
                break
    return rows
