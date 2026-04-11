"""
In-memory audit store.
"""

from typing import Any, Dict, List

from core.runtime_history_store import (
    clickhouse_enabled as runtime_history_clickhouse_enabled,
    list_audit_records,
    persist_audit_record,
)


AUDIT_LOGS: List[Dict[str, Any]] = []


def append_audit(record: Dict[str, Any]) -> None:
    if not isinstance(record, dict):
        return
    AUDIT_LOGS.append(record)
    if len(AUDIT_LOGS) > 2000:
        del AUDIT_LOGS[:-2000]
    persist_audit_record(record)


def list_audits(limit: int = 100, *, run_id: str = "") -> List[Dict[str, Any]]:
    safe_limit = max(1, min(int(limit or 100), 1000))
    if runtime_history_clickhouse_enabled():
        rows = list_audit_records(limit=safe_limit, run_id=str(run_id or ""))
        if rows:
            return rows
    if run_id:
        target = str(run_id or "")
        return [item for item in AUDIT_LOGS if str((item if isinstance(item, dict) else {}).get("run_id")) == target][
            -safe_limit:
        ]
    return AUDIT_LOGS[-safe_limit:]


def audit_retention_note() -> str:
    if runtime_history_clickhouse_enabled():
        return "clickhouse runtime audit log"
    return "in-memory audit log"
