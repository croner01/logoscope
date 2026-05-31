"""
Dynamic host registry backed by ClickHouse.

Follows the same pattern as exec-service's runtime_history_store:
HTTP-based CH client via urllib.request, JSONEachRow format,
env var configuration, schema auto-creation.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen


_LOCK = threading.RLock()
_CLICKHOUSE_SCHEMA_READY = False
_logger = logging.getLogger("ssh-gateway.host_registry")


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


def _safe_identifier(value: str, default: str) -> str:
    normalized = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in as_str(value, default))
    normalized = normalized.strip("_")
    return normalized or default


def _clickhouse_url() -> str:
    return as_str(os.getenv("SSH_GATEWAY_HOST_REGISTRY_CH_URL"), "http://clickhouse:8123").strip().rstrip("/")


def _clickhouse_database() -> str:
    return _safe_identifier(as_str(os.getenv("SSH_GATEWAY_HOST_REGISTRY_CH_DATABASE"), "logs"), "logs")


def _host_table() -> str:
    return _safe_identifier(as_str(os.getenv("SSH_GATEWAY_HOST_REGISTRY_CH_TABLE"), "ssh_host_registry"), "ssh_host_registry")


def _clickhouse_timeout_seconds() -> float:
    raw = as_str(os.getenv("SSH_GATEWAY_HOST_REGISTRY_CH_TIMEOUT_MS"), "1200")
    try:
        parsed = int(raw)
    except Exception:
        parsed = 1200
    bounded = max(50, min(8000, parsed))
    return float(bounded) / 1000.0


def _clickhouse_fail_open() -> bool:
    return as_bool(os.getenv("SSH_GATEWAY_HOST_REGISTRY_CH_FAIL_OPEN"), False)


def _clickhouse_headers() -> Dict[str, str]:
    headers = {"Content-Type": "text/plain; charset=utf-8"}
    user = as_str(os.getenv("SSH_GATEWAY_HOST_REGISTRY_CH_USER")).strip()
    password = as_str(os.getenv("SSH_GATEWAY_HOST_REGISTRY_CH_PASSWORD")).strip()
    if user:
        headers["X-ClickHouse-User"] = user
    if password:
        headers["X-ClickHouse-Key"] = password
    return headers


def _handle_clickhouse_error(exc: Exception) -> None:
    if _clickhouse_fail_open():
        return
    raise RuntimeError(f"host registry clickhouse backend unavailable: {as_str(exc)}") from exc


def _sql_str(value: Any) -> str:
    escaped = as_str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _to_clickhouse_datetime(value: Any) -> str:
    raw = as_str(value).strip()
    if not raw:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
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


def _clickhouse_execute(sql: str) -> str:
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


def ensure_schema() -> None:
    global _CLICKHOUSE_SCHEMA_READY
    with _LOCK:
        if _CLICKHOUSE_SCHEMA_READY:
            return

    database = _clickhouse_database()
    table = _host_table()

    create_db_sql = f"CREATE DATABASE IF NOT EXISTS {database}"
    create_table_sql = f"""
        CREATE TABLE IF NOT EXISTS {database}.{table} (
            name String,
            host String,
            port UInt16 DEFAULT 22,
            user String DEFAULT 'root',
            key_file String DEFAULT '',
            private_key String DEFAULT '',
            labels_json String DEFAULT '{{}}',
            created_at DateTime64(3, 'UTC'),
            updated_at DateTime64(3, 'UTC'),
            is_deleted UInt8 DEFAULT 0
        )
        ENGINE = ReplacingMergeTree(updated_at)
        ORDER BY (name)
    """
    alter_add_private_key = f"""
        ALTER TABLE {database}.{table}
        ADD COLUMN IF NOT EXISTS private_key String DEFAULT ''
    """
    try:
        _clickhouse_execute(create_db_sql)
        _clickhouse_execute(create_table_sql)
        _clickhouse_execute(alter_add_private_key)
    except Exception as exc:
        _handle_clickhouse_error(exc)
        return
    with _LOCK:
        _CLICKHOUSE_SCHEMA_READY = True


def _encode_private_key(raw: Optional[str]) -> str:
    """Base64-encode a private key for storage. Returns empty string if no key."""
    if not raw or not as_str(raw).strip():
        return ""
    try:
        return base64.b64encode(as_str(raw).encode("utf-8")).decode("ascii")
    except Exception as exc:
        _logger.warning("Failed to encode private key: %s", exc)
        return ""


def _mask_key_preview(raw: str) -> str:
    """Return a safe preview of a key for logging (shows first 20 chars)."""
    if not raw:
        return ""
    cleaned = raw.strip().replace("\n", "\\n")
    if len(cleaned) <= 30:
        return cleaned
    return cleaned[:20] + "..." + cleaned[-10:]


def register_host(
    name: str,
    host: str,
    port: int = 22,
    user: str = "root",
    key_file: str = "",
    labels: Optional[Dict[str, str]] = None,
    private_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Register a new host or update an existing one.

    If ``private_key`` is provided, it is base64-encoded and stored
    in ClickHouse.  At execution time the gateway writes it to a temp
    file so it can be used with ``ssh -i``.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    encoded_key = _encode_private_key(private_key)
    if encoded_key and _logger.isEnabledFor(logging.DEBUG):
        _logger.debug("Storing private_key for host '%s' (%s)", name, _mask_key_preview(private_key or ""))
    # Store empty key_file when private_key is provided,
    # to avoid persisting a misleading default path.
    # When key_file is explicitly given (no private_key), use it as-is.
    resolved_key_file = as_str(key_file) if key_file else ""
    record = {
        "name": name,
        "host": host,
        "port": max(1, min(65535, int(port))),
        "user": as_str(user) or "root",
        "key_file": resolved_key_file,
        "private_key": encoded_key,
        "labels_json": json.dumps(labels if isinstance(labels, dict) else {}, ensure_ascii=False),
        "created_at": now,
        "updated_at": now,
        "is_deleted": 0,
    }

    sql = (
        f"INSERT INTO {_clickhouse_database()}.{_host_table()} FORMAT JSONEachRow\n"
        f"{json.dumps(record, ensure_ascii=False)}\n"
    )
    try:
        _clickhouse_execute(sql)
    except Exception as exc:
        _handle_clickhouse_error(exc)

    return record


def unregister_host(name: str) -> bool:
    """Soft-delete a host by name. Returns True if any row was affected."""
    if not as_str(name).strip():
        return False
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    sql = (
        f"INSERT INTO {_clickhouse_database()}.{_host_table()} FORMAT JSONEachRow\n"
        f"{json.dumps({'name': name, 'updated_at': now, 'is_deleted': 1}, ensure_ascii=False)}\n"
    )
    try:
        _clickhouse_execute(sql)
    except Exception as exc:
        _handle_clickhouse_error(exc)
        return False
    return True


def get_host(name: str) -> Optional[Dict[str, Any]]:
    """Get a single host by name. Returns None if not found or soft-deleted."""
    if not as_str(name).strip():
        return None
    sql = (
        f"SELECT name, host, port, user, key_file, labels_json, created_at, updated_at "
        f"FROM {_clickhouse_database()}.{_host_table()} FINAL "
        f"WHERE name = {_sql_str(name)} AND is_deleted = 0 "
        "ORDER BY updated_at DESC LIMIT 1 FORMAT JSONEachRow"
    )
    rows = _clickhouse_query_json_each_row(sql)
    if not rows:
        return None
    return _normalize_host_row(rows[0])


def list_hosts(include_deleted: bool = False) -> List[Dict[str, Any]]:
    """List all registered hosts."""
    condition = "" if include_deleted else "WHERE is_deleted = 0"
    sql = (
        f"SELECT name, host, port, user, key_file, labels_json, created_at, updated_at "
        f"FROM {_clickhouse_database()}.{_host_table()} FINAL "
        f"{condition} "
        "ORDER BY name ASC FORMAT JSONEachRow"
    )
    rows = _clickhouse_query_json_each_row(sql)
    return [_normalize_host_row(r) for r in rows]


def _normalize_host_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a host row from ClickHouse JSONEachRow.

    Note: ``private_key`` is included for internal use (``_execute_ssh``
    writes it to a temp file).  The API router strips it from responses.
    """
    labels_raw = as_str(row.get("labels_json", "{}"))
    try:
        labels = json.loads(labels_raw)
    except Exception:
        labels = {}
    pk_b64 = as_str(row.get("private_key", ""))
    raw_key_file = row.get("key_file")
    return {
        "name": as_str(row.get("name")),
        "host": as_str(row.get("host")),
        "port": int(row.get("port", 22)),
        "user": as_str(row.get("user"), "root"),
        "key_file": as_str(raw_key_file) if raw_key_file is not None else "",
        "private_key_b64": pk_b64,
        "has_private_key": bool(pk_b64),
        "labels": labels if isinstance(labels, dict) else {},
        "created_at": as_str(row.get("created_at")),
        "updated_at": as_str(row.get("updated_at")),
    }
