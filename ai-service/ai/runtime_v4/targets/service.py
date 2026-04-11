"""
Runtime v4 target/capability registry service.

This service keeps an in-memory cache and optionally persists/reloads registry
state from ClickHouse when storage adapter is attached.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional, Tuple

from ai.runtime_v4.targets.models import TargetChangeRecord, TargetRecord


logger = logging.getLogger(__name__)


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip()
    else:
        text = str(value).strip()
    return text or default


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    normalized = _as_str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _to_datetime(value: Any) -> datetime:
    text = _as_str(value)
    if not text:
        return datetime.now(timezone.utc)
    normalized = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except Exception:
        return datetime.now(timezone.utc)


def _normalize_capabilities(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    normalized: List[str] = []
    seen = set()
    for item in value:
        capability = _as_str(item).lower()
        if not capability or capability in seen:
            continue
        seen.add(capability)
        normalized.append(capability)
    return normalized


def _normalize_profiles(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    normalized: List[str] = []
    seen = set()
    for item in value:
        profile = _as_str(item).lower()
        if not profile or profile in seen:
            continue
        seen.add(profile)
        normalized.append(profile)
    return normalized


def _normalize_csv_tokens(value: Any) -> List[str]:
    raw = _as_str(value)
    if not raw:
        return []
    tokens: List[str] = []
    seen = set()
    for item in raw.split(","):
        token = _as_str(item).lower()
        if not token or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def _normalize_target_metadata(
    value: Any,
    *,
    target_kind: str = "",
    target_identity: str = "",
) -> Dict[str, Any]:
    metadata = dict(value) if isinstance(value, dict) else {}
    safe_kind = _as_str(target_kind).lower()
    safe_identity = _as_str(target_identity)

    cluster_id = _as_str(metadata.get("cluster_id"))
    namespace = _as_str(metadata.get("namespace"))
    node_name = _as_str(metadata.get("node_name"))
    risk_tier = _as_str(metadata.get("risk_tier")).lower()
    profiles = _normalize_profiles(metadata.get("preferred_executor_profiles"))

    if safe_identity.startswith("namespace:") and not namespace:
        namespace = _as_str(safe_identity.split(":", 1)[1])
    if safe_identity.startswith("host:") and not node_name:
        node_name = _as_str(safe_identity.split(":", 1)[1])

    if cluster_id:
        metadata["cluster_id"] = cluster_id
    elif "cluster_id" in metadata:
        metadata.pop("cluster_id", None)

    if namespace:
        metadata["namespace"] = namespace
    elif "namespace" in metadata:
        metadata.pop("namespace", None)

    if node_name:
        metadata["node_name"] = node_name
    elif "node_name" in metadata:
        metadata.pop("node_name", None)

    if risk_tier in {"low", "medium", "high", "critical"}:
        metadata["risk_tier"] = risk_tier
    elif "risk_tier" in metadata:
        metadata.pop("risk_tier", None)

    if profiles:
        metadata["preferred_executor_profiles"] = profiles
    elif "preferred_executor_profiles" in metadata:
        metadata.pop("preferred_executor_profiles", None)

    if safe_kind == "host_node" and node_name and not metadata.get("execution_scope"):
        metadata["execution_scope"] = "node"
    return metadata


def _required_metadata_keys(target_kind: str) -> List[str]:
    safe_kind = _as_str(target_kind).lower()
    if safe_kind == "host_node":
        return ["cluster_id", "node_name", "preferred_executor_profiles", "risk_tier"]
    if safe_kind in {"k8s_cluster", "clickhouse_cluster", "openstack_project"}:
        return ["cluster_id", "preferred_executor_profiles", "risk_tier"]
    return ["preferred_executor_profiles", "risk_tier"]


def _metadata_value_missing(key: str, value: Any) -> bool:
    safe_key = _as_str(key).lower()
    if safe_key == "preferred_executor_profiles":
        return not (isinstance(value, list) and len(value) > 0)
    text = _as_str(value).lower()
    if not text:
        return True
    if text in {"unknown", "n/a", "na", "none", "null", "unset"}:
        return True
    return text.endswith(":unknown")


def _build_metadata_contract(
    *,
    target_kind: str,
    target_identity: str,
    metadata: Dict[str, Any],
) -> Dict[str, Any]:
    safe_kind = _as_str(target_kind).lower()
    normalized_metadata = _normalize_target_metadata(
        metadata,
        target_kind=safe_kind,
        target_identity=target_identity,
    )
    required_keys = _required_metadata_keys(safe_kind)
    missing_required_keys = [
        key for key in required_keys
        if _metadata_value_missing(key, normalized_metadata.get(key))
    ]
    execution_scope = {
        "cluster_id": _as_str(normalized_metadata.get("cluster_id")),
        "namespace": _as_str(normalized_metadata.get("namespace")),
        "node_name": _as_str(normalized_metadata.get("node_name")),
        "target_kind": safe_kind,
        "target_identity": _as_str(target_identity),
    }
    return {
        "required_keys": required_keys,
        "missing_required_keys": missing_required_keys,
        "metadata": normalized_metadata,
        "execution_scope": execution_scope,
    }


class RuntimeV4TargetRegistry:
    """Target/capability registry with replayable change log."""

    def __init__(self, storage_adapter: Any = None) -> None:
        self._lock = threading.RLock()
        self.storage = storage_adapter
        self._targets: Dict[str, TargetRecord] = {}
        self._changes: List[TargetChangeRecord] = []
        self._next_seq = 1

        default_database = (
            getattr(storage_adapter, "ch_database", "")
            or (getattr(storage_adapter, "config", {}) or {}).get("clickhouse", {}).get("database", "logs")
            or "logs"
        )
        self.target_table = os.getenv("AI_RUNTIME_V4_TARGET_TABLE", f"{default_database}.ai_runtime_v4_targets")
        self.target_change_table = os.getenv(
            "AI_RUNTIME_V4_TARGET_CHANGE_TABLE",
            f"{default_database}.ai_runtime_v4_target_changes",
        )
        self.target_latest_view = os.getenv(
            "AI_RUNTIME_V4_TARGET_LATEST_VIEW",
            f"{default_database}.v_ai_runtime_v4_targets_latest",
        )
        self._target_read_source_cache: Optional[Tuple[str, bool]] = None
        if self._is_clickhouse_available():
            self._ensure_clickhouse_tables()
            self._sync_next_seq_from_clickhouse()

    def attach_storage(self, storage_adapter: Any) -> None:
        with self._lock:
            self.storage = storage_adapter
            default_database = (
                getattr(storage_adapter, "ch_database", "")
                or (getattr(storage_adapter, "config", {}) or {}).get("clickhouse", {}).get("database", "logs")
                or "logs"
            )
            self.target_table = os.getenv("AI_RUNTIME_V4_TARGET_TABLE", f"{default_database}.ai_runtime_v4_targets")
            self.target_change_table = os.getenv(
                "AI_RUNTIME_V4_TARGET_CHANGE_TABLE",
                f"{default_database}.ai_runtime_v4_target_changes",
            )
            self.target_latest_view = os.getenv(
                "AI_RUNTIME_V4_TARGET_LATEST_VIEW",
                f"{default_database}.v_ai_runtime_v4_targets_latest",
            )
            self._target_read_source_cache = None
            if self._is_clickhouse_available():
                self._ensure_clickhouse_tables()
                self._sync_next_seq_from_clickhouse()

    def clear(self) -> None:
        with self._lock:
            self._targets.clear()
            self._changes.clear()
            self._next_seq = 1

    def _is_clickhouse_available(self) -> bool:
        return bool(self.storage and getattr(self.storage, "ch_client", None))

    @staticmethod
    def _split_table_name(table_name: str) -> Tuple[str, str]:
        normalized = _as_str(table_name)
        if "." in normalized:
            db_name, tbl_name = normalized.split(".", 1)
            return db_name, tbl_name
        return "default", normalized

    def _table_exists(self, table_name: str) -> bool:
        if not self._is_clickhouse_available():
            return False
        db_name, tbl_name = self._split_table_name(table_name)
        try:
            rows = self.storage.ch_client.execute(
                """
                SELECT count()
                FROM system.tables
                WHERE database = %(database)s
                  AND name = %(name)s
                """,
                {"database": db_name, "name": tbl_name},
            )
            return bool(rows and rows[0] and int(rows[0][0]) > 0)
        except Exception:
            return False

    def _get_target_read_source(self) -> Tuple[str, bool]:
        cached = self._target_read_source_cache
        if cached is not None:
            return cached
        if self._table_exists(self.target_latest_view):
            self._target_read_source_cache = (self.target_latest_view, False)
        else:
            self._target_read_source_cache = (self.target_table, True)
        return self._target_read_source_cache

    def _ensure_clickhouse_tables(self) -> None:
        if not self._is_clickhouse_available():
            return
        create_target_sql = f"""
        CREATE TABLE IF NOT EXISTS {self.target_table} (
            target_id String,
            target_kind String,
            target_identity String,
            display_name String,
            description String,
            capabilities_json String,
            credential_scope_json String,
            metadata_json String,
            status String,
            version UInt32,
            updated_by String,
            created_at DateTime64(3, 'UTC'),
            updated_at DateTime64(3, 'UTC')
        )
        ENGINE = ReplacingMergeTree(updated_at)
        PARTITION BY toYYYYMM(created_at)
        ORDER BY (target_id)
        SETTINGS index_granularity = 8192
        """
        create_change_sql = f"""
        CREATE TABLE IF NOT EXISTS {self.target_change_table} (
            seq UInt64,
            change_id String,
            change_type String,
            target_id String,
            target_kind String,
            run_id String,
            action_id String,
            reason String,
            before_json String,
            after_json String,
            created_at DateTime64(3, 'UTC'),
            updated_by String
        )
        ENGINE = MergeTree()
        PARTITION BY toYYYYMM(created_at)
        ORDER BY (seq, created_at, change_id)
        SETTINGS index_granularity = 8192
        """
        create_target_latest_view_sql = f"""
        CREATE VIEW IF NOT EXISTS {self.target_latest_view} AS
        SELECT
            target_id,
            argMax(target_kind, _updated_at) AS target_kind,
            argMax(target_identity, _updated_at) AS target_identity,
            argMax(display_name, _updated_at) AS display_name,
            argMax(description, _updated_at) AS description,
            argMax(capabilities_json, _updated_at) AS capabilities_json,
            argMax(credential_scope_json, _updated_at) AS credential_scope_json,
            argMax(metadata_json, _updated_at) AS metadata_json,
            argMax(status, _updated_at) AS status,
            argMax(version, _updated_at) AS version,
            argMax(updated_by, _updated_at) AS updated_by,
            min(created_at) AS created_at,
            max(_updated_at) AS updated_at
        FROM
        (
            SELECT *, updated_at AS _updated_at
            FROM {self.target_table}
        )
        GROUP BY target_id
        """
        try:
            self.storage.ch_client.execute(create_target_sql)
            self.storage.ch_client.execute(create_change_sql)
            self.storage.ch_client.execute(f"DROP VIEW IF EXISTS {self.target_latest_view}")
            self.storage.ch_client.execute(create_target_latest_view_sql)
        except Exception as exc:
            logger.warning("failed to ensure target registry clickhouse tables: %s", exc)

    def _sync_next_seq_from_clickhouse(self) -> None:
        if not self._is_clickhouse_available():
            return
        try:
            rows = self.storage.ch_client.execute(f"SELECT max(seq) FROM {self.target_change_table}")
        except Exception:
            return
        max_seq = int(rows[0][0]) if rows and rows[0] and rows[0][0] is not None else 0
        self._next_seq = max(self._next_seq, max_seq + 1)

    def _next_change_seq(self) -> int:
        self._sync_next_seq_from_clickhouse()
        current = self._next_seq
        self._next_seq += 1
        return current

    def _insert_target_row(self, record: TargetRecord) -> None:
        if not self._is_clickhouse_available():
            return
        sql = f"""
        INSERT INTO {self.target_table} (
            target_id, target_kind, target_identity, display_name, description,
            capabilities_json, credential_scope_json, metadata_json,
            status, version, updated_by, created_at, updated_at
        ) VALUES
        """
        row = {
            "target_id": record.target_id,
            "target_kind": record.target_kind,
            "target_identity": record.target_identity,
            "display_name": record.display_name,
            "description": record.description,
            "capabilities_json": json.dumps(record.capabilities, ensure_ascii=False),
            "credential_scope_json": json.dumps(record.credential_scope, ensure_ascii=False),
            "metadata_json": json.dumps(record.metadata, ensure_ascii=False),
            "status": record.status,
            "version": int(record.version),
            "updated_by": record.updated_by,
            "created_at": _to_datetime(record.created_at),
            "updated_at": _to_datetime(record.updated_at),
        }
        try:
            self.storage.ch_client.execute(sql, [row])
        except Exception as exc:
            logger.warning("failed to insert target row: %s", exc)

    def _insert_change_row(self, record: TargetChangeRecord) -> None:
        if not self._is_clickhouse_available():
            return
        sql = f"""
        INSERT INTO {self.target_change_table} (
            seq, change_id, change_type, target_id, target_kind,
            run_id, action_id, reason, before_json, after_json, created_at, updated_by
        ) VALUES
        """
        row = {
            "seq": int(record.seq),
            "change_id": record.change_id,
            "change_type": record.change_type,
            "target_id": record.target_id,
            "target_kind": record.target_kind,
            "run_id": record.run_id,
            "action_id": record.action_id,
            "reason": record.reason,
            "before_json": json.dumps(record.before, ensure_ascii=False),
            "after_json": json.dumps(record.after, ensure_ascii=False),
            "created_at": _to_datetime(record.created_at),
            "updated_by": record.updated_by,
        }
        try:
            self.storage.ch_client.execute(sql, [row])
        except Exception as exc:
            logger.warning("failed to insert target change row: %s", exc)

    @staticmethod
    def _target_from_row(row: List[Any]) -> TargetRecord:
        try:
            capabilities = json.loads(_as_str(row[5], "[]") or "[]")
        except Exception:
            capabilities = []
        try:
            credential_scope = json.loads(_as_str(row[6], "{}") or "{}")
        except Exception:
            credential_scope = {}
        try:
            metadata = json.loads(_as_str(row[7], "{}") or "{}")
        except Exception:
            metadata = {}
        return TargetRecord(
            target_id=_as_str(row[0]),
            target_kind=_as_str(row[1], "unknown"),
            target_identity=_as_str(row[2], "unknown"),
            display_name=_as_str(row[3]),
            description=_as_str(row[4]),
            capabilities=_normalize_capabilities(capabilities),
            credential_scope=credential_scope if isinstance(credential_scope, dict) else {},
            metadata=metadata if isinstance(metadata, dict) else {},
            status=_as_str(row[8], "active"),
            version=max(1, int(row[9] or 1)),
            updated_by=_as_str(row[10], "system"),
            created_at=_as_str(row[11]),
            updated_at=_as_str(row[12]),
        )

    @staticmethod
    def _change_from_row(row: List[Any]) -> TargetChangeRecord:
        try:
            before = json.loads(_as_str(row[8], "{}") or "{}")
        except Exception:
            before = {}
        try:
            after = json.loads(_as_str(row[9], "{}") or "{}")
        except Exception:
            after = {}
        return TargetChangeRecord(
            seq=int(row[0] or 0),
            change_id=_as_str(row[1]),
            change_type=_as_str(row[2]),
            target_id=_as_str(row[3]),
            target_kind=_as_str(row[4], "unknown"),
            run_id=_as_str(row[5]),
            action_id=_as_str(row[6]),
            reason=_as_str(row[7]),
            before=before if isinstance(before, dict) else {},
            after=after if isinstance(after, dict) else {},
            created_at=_as_str(row[10]),
            updated_by=_as_str(row[11], "system"),
        )

    def _load_target_from_clickhouse(self, target_id: str) -> Optional[TargetRecord]:
        if not self._is_clickhouse_available():
            return None
        source, need_final = self._get_target_read_source()
        final_clause = " FINAL" if need_final else ""
        sql = f"""
        SELECT
            target_id, target_kind, target_identity, display_name, description,
            capabilities_json, credential_scope_json, metadata_json,
            status, version, updated_by, created_at, updated_at
        FROM {source}{final_clause}
        WHERE target_id = %(target_id)s
        LIMIT 1
        """
        try:
            rows = self.storage.ch_client.execute(sql, {"target_id": _as_str(target_id)})
        except Exception as exc:
            logger.warning("failed to load target from clickhouse: %s", exc)
            return None
        if not rows:
            return None
        return self._target_from_row(rows[0])

    def _list_targets_from_clickhouse(self) -> List[TargetRecord]:
        if not self._is_clickhouse_available():
            return []
        source, need_final = self._get_target_read_source()
        final_clause = " FINAL" if need_final else ""
        sql = f"""
        SELECT
            target_id, target_kind, target_identity, display_name, description,
            capabilities_json, credential_scope_json, metadata_json,
            status, version, updated_by, created_at, updated_at
        FROM {source}{final_clause}
        ORDER BY updated_at DESC
        LIMIT 5000
        """
        try:
            rows = self.storage.ch_client.execute(sql)
        except Exception as exc:
            logger.warning("failed to list targets from clickhouse: %s", exc)
            return []
        return [self._target_from_row(row) for row in rows]

    def _list_changes_from_clickhouse(self, *, target_id: str, after_seq: int, limit: int) -> List[TargetChangeRecord]:
        if not self._is_clickhouse_available():
            return []
        sql = f"""
        SELECT
            seq, change_id, change_type, target_id, target_kind,
            run_id, action_id, reason, before_json, after_json, created_at, updated_by
        FROM {self.target_change_table}
        WHERE seq > %(after_seq)s
          AND (%(target_id)s = '' OR target_id = %(target_id)s)
        ORDER BY seq ASC
        LIMIT %(limit)s
        """
        try:
            rows = self.storage.ch_client.execute(
                sql,
                {"after_seq": int(after_seq), "target_id": _as_str(target_id), "limit": int(limit)},
            )
        except Exception as exc:
            logger.warning("failed to list target changes from clickhouse: %s", exc)
            return []
        return [self._change_from_row(row) for row in rows]

    def upsert_target(
        self,
        *,
        target_id: str,
        target_kind: str,
        target_identity: str,
        display_name: str,
        description: str,
        capabilities: List[str],
        credential_scope: Dict[str, Any],
        metadata: Dict[str, Any],
        updated_by: str,
        reason: str = "",
        run_id: str = "",
        action_id: str = "",
    ) -> Dict[str, Any]:
        safe_target_id = _as_str(target_id)
        if not safe_target_id:
            raise ValueError("target_id is required")
        safe_target_kind = _as_str(target_kind, "unknown")
        safe_target_identity = _as_str(target_identity, "unknown")
        safe_updated_by = _as_str(updated_by, "system")
        safe_capabilities = _normalize_capabilities(capabilities)
        safe_credential_scope = credential_scope if isinstance(credential_scope, dict) else {}
        safe_metadata = _normalize_target_metadata(
            metadata,
            target_kind=safe_target_kind,
            target_identity=safe_target_identity,
        )

        with self._lock:
            existing = self._targets.get(safe_target_id)
            if existing is None:
                existing = self._load_target_from_clickhouse(safe_target_id)
                if existing is not None:
                    self._targets[safe_target_id] = existing

            before = existing.to_dict() if existing is not None else {}
            if existing is None:
                next_record = TargetRecord(
                    target_id=safe_target_id,
                    target_kind=safe_target_kind,
                    target_identity=safe_target_identity,
                    display_name=_as_str(display_name),
                    description=_as_str(description),
                    capabilities=safe_capabilities,
                    credential_scope=dict(safe_credential_scope),
                    metadata=dict(safe_metadata),
                    status="active",
                    version=1,
                    updated_by=safe_updated_by,
                )
                change_type = "target_registered"
            else:
                next_record = replace(
                    existing,
                    target_kind=safe_target_kind,
                    target_identity=safe_target_identity,
                    display_name=_as_str(display_name),
                    description=_as_str(description),
                    capabilities=safe_capabilities,
                    credential_scope=dict(safe_credential_scope),
                    metadata=dict(safe_metadata),
                    status="active",
                    version=int(existing.version) + 1,
                    updated_by=safe_updated_by,
                    updated_at=_utc_now_iso(),
                )
                change_type = "target_updated"

            self._targets[safe_target_id] = next_record
            self._insert_target_row(next_record)
            change = self._append_change(
                change_type=change_type,
                target_id=safe_target_id,
                target_kind=next_record.target_kind,
                run_id=run_id,
                action_id=action_id,
                reason=reason,
                before=before,
                after=next_record.to_dict(),
                updated_by=safe_updated_by,
            )
            return {"target": next_record.to_dict(), "change": change.to_dict()}

    def get_target(self, target_id: str) -> Optional[Dict[str, Any]]:
        safe_target_id = _as_str(target_id)
        with self._lock:
            record = self._targets.get(safe_target_id)
            if record is None:
                loaded = self._load_target_from_clickhouse(safe_target_id)
                if loaded is not None:
                    self._targets[safe_target_id] = loaded
                    record = loaded
            return record.to_dict() if record is not None else None

    def list_targets(
        self,
        *,
        status: str = "",
        target_kind: str = "",
        capability: str = "",
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        safe_status = _as_str(status).lower()
        safe_kind = _as_str(target_kind).lower()
        safe_capability = _as_str(capability).lower()
        safe_limit = max(1, min(int(limit or 200), 5000))

        with self._lock:
            from_ch = self._list_targets_from_clickhouse()
            if from_ch:
                for item in from_ch:
                    self._targets[item.target_id] = item
            source = from_ch if from_ch else list(self._targets.values())
            result: List[Dict[str, Any]] = []
            for record in source:
                if safe_status and _as_str(record.status).lower() != safe_status:
                    continue
                if safe_kind and _as_str(record.target_kind).lower() != safe_kind:
                    continue
                if safe_capability and safe_capability not in set(_normalize_capabilities(record.capabilities)):
                    continue
                result.append(record.to_dict())
            result.sort(key=lambda item: _as_str(item.get("updated_at")), reverse=True)
            return result[:safe_limit]

    def find_target_by_identity(
        self,
        *,
        target_kind: str,
        target_identity: str,
    ) -> Optional[Dict[str, Any]]:
        candidates = self.list_targets_by_identity(
            target_kind=target_kind,
            target_identity=target_identity,
        )
        if not candidates:
            return None
        return candidates[0]

    def list_targets_by_identity(
        self,
        *,
        target_kind: str,
        target_identity: str,
    ) -> List[Dict[str, Any]]:
        safe_kind = _as_str(target_kind).lower()
        safe_identity = _as_str(target_identity).lower()
        if not safe_identity:
            return []
        with self._lock:
            from_ch = self._list_targets_from_clickhouse()
            if from_ch:
                for item in from_ch:
                    self._targets[item.target_id] = item
            source = from_ch if from_ch else list(self._targets.values())
            candidates: List[TargetRecord] = []
            for record in source:
                if safe_kind and _as_str(record.target_kind).lower() != safe_kind:
                    continue
                if _as_str(record.target_identity).lower() != safe_identity:
                    continue
                candidates.append(record)
            candidates.sort(
                key=lambda record: (
                    1 if _as_str(record.status).lower() == "active" else 0,
                    _as_str(record.updated_at),
                    int(record.version),
                ),
                reverse=True,
            )
            return [item.to_dict() for item in candidates]

    def deactivate_target(
        self,
        target_id: str,
        *,
        updated_by: str = "system",
        reason: str = "",
        run_id: str = "",
        action_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        safe_target_id = _as_str(target_id)
        safe_updated_by = _as_str(updated_by, "system")
        with self._lock:
            existing = self._targets.get(safe_target_id)
            if existing is None:
                existing = self._load_target_from_clickhouse(safe_target_id)
                if existing is not None:
                    self._targets[safe_target_id] = existing
            if existing is None:
                return None
            before = existing.to_dict()
            next_record = replace(
                existing,
                status="inactive",
                version=int(existing.version) + 1,
                updated_by=safe_updated_by,
                updated_at=_utc_now_iso(),
            )
            self._targets[safe_target_id] = next_record
            self._insert_target_row(next_record)
            change = self._append_change(
                change_type="target_deactivated",
                target_id=safe_target_id,
                target_kind=next_record.target_kind,
                run_id=run_id,
                action_id=action_id,
                reason=reason,
                before=before,
                after=next_record.to_dict(),
                updated_by=safe_updated_by,
            )
            return {"target": next_record.to_dict(), "change": change.to_dict()}

    def resolve_target(
        self,
        *,
        target_id: str,
        required_capabilities: List[str],
        run_id: str = "",
        action_id: str = "",
        reason: str = "",
    ) -> Dict[str, Any]:
        safe_target_id = _as_str(target_id)
        required = _normalize_capabilities(required_capabilities)
        target = self.get_target(safe_target_id)
        if target is None:
            return {
                "target_id": safe_target_id,
                "registered": False,
                "status": "unknown",
                "result": "manual_required",
                "reason": _as_str(reason, "target not registered"),
                "missing_capabilities": required,
                "matched_capabilities": [],
                "run_id": _as_str(run_id),
                "action_id": _as_str(action_id),
                "metadata_contract": {
                    "required_keys": _required_metadata_keys("unknown"),
                    "missing_required_keys": _required_metadata_keys("unknown"),
                    "metadata": {},
                    "execution_scope": {
                        "cluster_id": "",
                        "namespace": "",
                        "node_name": "",
                        "target_kind": "unknown",
                        "target_identity": "",
                    },
                },
                "resolved_target_context": {},
            }

        metadata_contract = _build_metadata_contract(
            target_kind=_as_str(target.get("target_kind")),
            target_identity=_as_str(target.get("target_identity")),
            metadata=target.get("metadata") if isinstance(target.get("metadata"), dict) else {},
        )
        resolved_target_context = {
            "target_id": _as_str(target.get("target_id")),
            "target_kind": _as_str(target.get("target_kind")),
            "target_identity": _as_str(target.get("target_identity")),
            "metadata": dict(metadata_contract.get("metadata") or {}),
            "execution_scope": dict(metadata_contract.get("execution_scope") or {}),
        }

        if _as_str(target.get("status")).lower() != "active":
            return {
                "target_id": safe_target_id,
                "registered": True,
                "status": _as_str(target.get("status"), "inactive"),
                "result": "manual_required",
                "reason": _as_str(reason, "target is not active"),
                "missing_capabilities": required,
                "matched_capabilities": [],
                "target": target,
                "run_id": _as_str(run_id),
                "action_id": _as_str(action_id),
                "metadata_contract": metadata_contract,
                "resolved_target_context": resolved_target_context,
            }

        target_capabilities = _normalize_capabilities(target.get("capabilities"))
        target_capability_set = set(target_capabilities)
        missing = [item for item in required if item not in target_capability_set]
        matched = [item for item in required if item in target_capability_set]
        if missing:
            return {
                "target_id": safe_target_id,
                "registered": True,
                "status": "active",
                "result": "manual_required",
                "reason": _as_str(reason, "target capability mismatch"),
                "missing_capabilities": missing,
                "matched_capabilities": matched,
                "target": target,
                "run_id": _as_str(run_id),
                "action_id": _as_str(action_id),
                "metadata_contract": metadata_contract,
                "resolved_target_context": resolved_target_context,
            }

        missing_required_keys = list(metadata_contract.get("missing_required_keys") or [])
        if missing_required_keys:
            missing_label = ", ".join(missing_required_keys)
            metadata_reason = f"target metadata missing required fields: {missing_label}"
            safe_reason = _as_str(reason)
            return {
                "target_id": safe_target_id,
                "registered": True,
                "status": "active",
                "result": "manual_required",
                "reason": f"{safe_reason}; {metadata_reason}" if safe_reason else metadata_reason,
                "missing_capabilities": [],
                "matched_capabilities": matched if matched else target_capabilities,
                "target": target,
                "run_id": _as_str(run_id),
                "action_id": _as_str(action_id),
                "metadata_contract": metadata_contract,
                "resolved_target_context": resolved_target_context,
            }

        return {
            "target_id": safe_target_id,
            "registered": True,
            "status": "active",
            "result": "allow",
            "reason": _as_str(reason, "target registered and capabilities matched"),
            "missing_capabilities": [],
            "matched_capabilities": matched if matched else target_capabilities,
            "target": target,
            "run_id": _as_str(run_id),
            "action_id": _as_str(action_id),
            "metadata_contract": metadata_contract,
            "resolved_target_context": resolved_target_context,
        }

    def resolve_target_by_identity(
        self,
        *,
        target_kind: str,
        target_identity: str,
        required_capabilities: List[str],
        run_id: str = "",
        action_id: str = "",
        reason: str = "",
    ) -> Dict[str, Any]:
        safe_kind = _as_str(target_kind, "unknown")
        safe_identity = _as_str(target_identity)
        required = _normalize_capabilities(required_capabilities)
        if not safe_identity:
            return {
                "target_id": "",
                "target_kind": safe_kind,
                "target_identity": safe_identity,
                "registered": False,
                "status": "unknown",
                "result": "manual_required",
                "reason": _as_str(reason, "target identity is required"),
                "missing_capabilities": required,
                "matched_capabilities": [],
                "run_id": _as_str(run_id),
                "action_id": _as_str(action_id),
                "metadata_contract": {
                    "required_keys": _required_metadata_keys(safe_kind),
                    "missing_required_keys": _required_metadata_keys(safe_kind),
                    "metadata": {},
                    "execution_scope": {
                        "cluster_id": "",
                        "namespace": "",
                        "node_name": "",
                        "target_kind": safe_kind,
                        "target_identity": safe_identity,
                    },
                },
                "resolved_target_context": {},
            }

        matched_targets = self.list_targets_by_identity(
            target_kind=safe_kind,
            target_identity=safe_identity,
        )
        if not matched_targets:
            return {
                "target_id": "",
                "target_kind": safe_kind,
                "target_identity": safe_identity,
                "registered": False,
                "status": "unknown",
                "result": "manual_required",
                "reason": _as_str(reason, "target not registered"),
                "missing_capabilities": required,
                "matched_capabilities": [],
                "run_id": _as_str(run_id),
                "action_id": _as_str(action_id),
                "metadata_contract": {
                    "required_keys": _required_metadata_keys(safe_kind),
                    "missing_required_keys": _required_metadata_keys(safe_kind),
                    "metadata": {},
                    "execution_scope": {
                        "cluster_id": "",
                        "namespace": "",
                        "node_name": "",
                        "target_kind": safe_kind,
                        "target_identity": safe_identity,
                    },
                },
                "resolved_target_context": {},
            }

        # Ambiguity should be evaluated on active targets first.
        # Historical inactive rows for the same identity are expected in
        # append-only storage and should not block dispatch.
        active_targets = [
            item for item in matched_targets
            if _as_str(item.get("status"), "active").lower() == "active"
        ]
        effective_targets = active_targets if active_targets else matched_targets
        target_ids = [_as_str(item.get("target_id")) for item in effective_targets if _as_str(item.get("target_id"))]
        unique_ids = sorted(set(target_ids))
        if len(unique_ids) > 1:
            return {
                "target_id": "",
                "target_kind": safe_kind,
                "target_identity": safe_identity,
                "registered": True,
                "status": "ambiguous",
                "result": "manual_required",
                "reason": _as_str(reason, "target identity matched multiple targets"),
                "missing_capabilities": required,
                "matched_capabilities": [],
                "ambiguous_targets": unique_ids,
                "run_id": _as_str(run_id),
                "action_id": _as_str(action_id),
                "metadata_contract": {
                    "required_keys": _required_metadata_keys(safe_kind),
                    "missing_required_keys": _required_metadata_keys(safe_kind),
                    "metadata": {},
                    "execution_scope": {
                        "cluster_id": "",
                        "namespace": "",
                        "node_name": "",
                        "target_kind": safe_kind,
                        "target_identity": safe_identity,
                    },
                },
                "resolved_target_context": {},
            }
        matched = effective_targets[0]

        resolved = self.resolve_target(
            target_id=_as_str(matched.get("target_id")),
            required_capabilities=required,
            run_id=run_id,
            action_id=action_id,
            reason=reason,
        )
        resolved["target_kind"] = _as_str(matched.get("target_kind"), safe_kind)
        resolved["target_identity"] = _as_str(matched.get("target_identity"), safe_identity)
        return resolved

    def list_changes(self, *, target_id: str = "", after_seq: int = 0, limit: int = 200) -> List[Dict[str, Any]]:
        safe_target_id = _as_str(target_id)
        safe_after = max(0, int(after_seq or 0))
        safe_limit = max(1, min(int(limit or 200), 5000))
        with self._lock:
            from_ch = self._list_changes_from_clickhouse(
                target_id=safe_target_id,
                after_seq=safe_after,
                limit=safe_limit,
            )
            if from_ch:
                return [item.to_dict() for item in from_ch]
            rows: List[Dict[str, Any]] = []
            for item in self._changes:
                if int(item.seq) <= safe_after:
                    continue
                if safe_target_id and _as_str(item.target_id) != safe_target_id:
                    continue
                rows.append(item.to_dict())
                if len(rows) >= safe_limit:
                    break
            return rows

    def _append_change(
        self,
        *,
        change_type: str,
        target_id: str,
        target_kind: str,
        run_id: str,
        action_id: str,
        reason: str,
        before: Dict[str, Any],
        after: Dict[str, Any],
        updated_by: str,
    ) -> TargetChangeRecord:
        record = TargetChangeRecord(
            seq=self._next_change_seq(),
            change_id=TargetChangeRecord.build_change_id(),
            change_type=_as_str(change_type, "target_updated"),
            target_id=_as_str(target_id),
            target_kind=_as_str(target_kind, "unknown"),
            run_id=_as_str(run_id),
            action_id=_as_str(action_id),
            reason=_as_str(reason),
            before=dict(before),
            after=dict(after),
            updated_by=_as_str(updated_by, "system"),
        )
        self._changes.append(record)
        self._insert_change_row(record)
        return record


_target_registry: Optional[RuntimeV4TargetRegistry] = None


def get_runtime_v4_target_registry() -> RuntimeV4TargetRegistry:
    global _target_registry
    if _target_registry is None:
        _target_registry = RuntimeV4TargetRegistry()
    return _target_registry


def set_runtime_v4_target_storage(storage_adapter: Any) -> RuntimeV4TargetRegistry:
    registry = get_runtime_v4_target_registry()
    registry.attach_storage(storage_adapter)
    return registry


def _auto_seed_enabled() -> bool:
    default_enabled = os.getenv("PYTEST_CURRENT_TEST") is None
    return _as_bool(os.getenv("AI_RUNTIME_V4_TARGET_AUTO_SEED_ENABLED"), default_enabled)


def _default_namespace() -> str:
    return _as_str(
        os.getenv("AI_RUNTIME_V4_TARGET_DEFAULT_NAMESPACE"),
        _as_str(os.getenv("NAMESPACE"), "islap"),
    )


def _default_cluster_id() -> str:
    return _as_str(
        os.getenv("AI_RUNTIME_V4_TARGET_DEFAULT_CLUSTER_ID"),
        _as_str(os.getenv("K8S_CLUSTER_ID"), "cluster-local"),
    )


def _default_risk_tier() -> str:
    candidate = _as_str(os.getenv("AI_RUNTIME_V4_TARGET_DEFAULT_RISK_TIER"), "high").lower()
    if candidate in {"low", "medium", "high", "critical"}:
        return candidate
    return "high"


def _default_profiles(target_kind: str) -> List[str]:
    safe_kind = _as_str(target_kind).lower()
    env_key_map = {
        "k8s_cluster": "AI_RUNTIME_V4_TARGET_DEFAULT_K8S_PROFILES",
        "clickhouse_cluster": "AI_RUNTIME_V4_TARGET_DEFAULT_CLICKHOUSE_PROFILES",
        "openstack_project": "AI_RUNTIME_V4_TARGET_DEFAULT_OPENSTACK_PROFILES",
        "host_node": "AI_RUNTIME_V4_TARGET_DEFAULT_HOST_PROFILES",
        "http_endpoint": "AI_RUNTIME_V4_TARGET_DEFAULT_HTTP_PROFILES",
    }
    defaults_map = {
        "k8s_cluster": ["toolbox-k8s-readonly", "toolbox-k8s-mutating"],
        "clickhouse_cluster": ["toolbox-k8s-readonly", "toolbox-k8s-mutating"],
        "openstack_project": ["toolbox-openstack-readonly", "toolbox-openstack-mutating"],
        "host_node": ["toolbox-node-readonly", "toolbox-node-mutating"],
        "http_endpoint": ["toolbox-http-readonly", "toolbox-http-mutating"],
    }
    from_env = _normalize_csv_tokens(os.getenv(env_key_map.get(safe_kind, "")))
    if from_env:
        return from_env
    return list(defaults_map.get(safe_kind, ["busybox-readonly", "busybox-mutating"]))


def _default_capabilities(target_kind: str) -> List[str]:
    safe_kind = _as_str(target_kind).lower()
    mapping = {
        "k8s_cluster": ["read_logs", "restart_workload", "helm_read", "helm_mutation"],
        "clickhouse_cluster": ["run_query", "clickhouse_mutation"],
        "openstack_project": ["read_cloud", "openstack_mutation"],
        "host_node": ["read_host_state", "host_mutation"],
        "http_endpoint": ["http_read", "http_mutation"],
    }
    return list(mapping.get(safe_kind, []))


def _stable_target_id(target_kind: str, target_identity: str) -> str:
    raw = f"{_as_str(target_kind).lower()}-{_as_str(target_identity).lower()}"
    normalized = "".join(ch if ch.isalnum() else "-" for ch in raw).strip("-")
    compact = "-".join(part for part in normalized.split("-") if part)
    compact = compact[:64] if compact else "runtime-target"
    return f"auto-{compact}"


def _target_specs_for_bootstrap() -> List[Dict[str, Any]]:
    namespace = _default_namespace()
    clickhouse_db = _as_str(
        os.getenv("AI_RUNTIME_V4_TARGET_DEFAULT_CLICKHOUSE_DATABASE"),
        _as_str(os.getenv("CLICKHOUSE_DATABASE"), "logs"),
    )
    cluster_id = _default_cluster_id()
    risk_tier = _default_risk_tier()
    specs: List[Dict[str, Any]] = []
    if namespace:
        specs.append(
            {
                "target_kind": "k8s_cluster",
                "target_identity": f"namespace:{namespace}",
                "display_name": f"{namespace} namespace",
                "description": "auto-seeded kubernetes diagnosis target",
                "credential_scope": {"namespace": namespace},
                "metadata": {
                    "cluster_id": cluster_id,
                    "namespace": namespace,
                    "risk_tier": risk_tier,
                    "preferred_executor_profiles": _default_profiles("k8s_cluster"),
                },
                "capabilities": _default_capabilities("k8s_cluster"),
            }
        )
    if clickhouse_db:
        specs.append(
            {
                "target_kind": "clickhouse_cluster",
                "target_identity": f"database:{clickhouse_db}",
                "display_name": f"clickhouse {clickhouse_db}",
                "description": "auto-seeded clickhouse diagnosis target",
                "credential_scope": {"database": clickhouse_db},
                "metadata": {
                    "cluster_id": cluster_id,
                    "risk_tier": risk_tier,
                    "preferred_executor_profiles": _default_profiles("clickhouse_cluster"),
                },
                "capabilities": _default_capabilities("clickhouse_cluster"),
            }
        )
    return specs


def _merge_missing_metadata(existing: Dict[str, Any], defaults: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(existing if isinstance(existing, dict) else {})
    for key, value in (defaults if isinstance(defaults, dict) else {}).items():
        if key == "preferred_executor_profiles":
            current_profiles = _normalize_profiles(merged.get(key))
            if current_profiles:
                continue
            merged[key] = _normalize_profiles(value)
            continue
        if _metadata_value_missing(key, merged.get(key)):
            merged[key] = value
    return _normalize_target_metadata(merged)


def ensure_runtime_v4_default_targets(
    registry: Optional[RuntimeV4TargetRegistry] = None,
) -> Dict[str, Any]:
    """
    Ensure minimal runtime targets exist for readonly auto-exec.

    Behavior:
    - Auto-create default targets when missing.
    - Auto-heal missing required metadata/capabilities on existing targets.
    """

    enabled = _auto_seed_enabled()
    if not enabled:
        return {"enabled": False, "created": [], "updated": [], "skipped": []}

    safe_registry = registry or get_runtime_v4_target_registry()
    updated_by = _as_str(os.getenv("AI_RUNTIME_V4_TARGET_AUTO_SEED_UPDATED_BY"), "runtime-auto-seed")
    reason = _as_str(
        os.getenv("AI_RUNTIME_V4_TARGET_AUTO_SEED_REASON"),
        "runtime bootstrap ensure default targets",
    )

    created: List[str] = []
    updated: List[str] = []
    skipped: List[str] = []

    for spec in _target_specs_for_bootstrap():
        target_kind = _as_str(spec.get("target_kind"), "unknown")
        target_identity = _as_str(spec.get("target_identity"))
        if not target_identity:
            continue
        desired_capabilities = _normalize_capabilities(spec.get("capabilities"))
        desired_metadata = _normalize_target_metadata(
            spec.get("metadata") if isinstance(spec.get("metadata"), dict) else {},
            target_kind=target_kind,
            target_identity=target_identity,
        )

        existing = safe_registry.find_target_by_identity(
            target_kind=target_kind,
            target_identity=target_identity,
        )
        if existing is None:
            target_id = _stable_target_id(target_kind, target_identity)
            safe_registry.upsert_target(
                target_id=target_id,
                target_kind=target_kind,
                target_identity=target_identity,
                display_name=_as_str(spec.get("display_name"), target_identity),
                description=_as_str(spec.get("description")),
                capabilities=desired_capabilities,
                credential_scope=spec.get("credential_scope") if isinstance(spec.get("credential_scope"), dict) else {},
                metadata=desired_metadata,
                updated_by=updated_by,
                reason=reason,
            )
            created.append(target_identity)
            continue

        existing_target_id = _as_str(existing.get("target_id"))
        existing_caps = _normalize_capabilities(existing.get("capabilities"))
        existing_cap_set = set(existing_caps)
        missing_caps = [item for item in desired_capabilities if item not in existing_cap_set]
        merged_caps = _normalize_capabilities(existing_caps + missing_caps)
        existing_metadata = existing.get("metadata") if isinstance(existing.get("metadata"), dict) else {}
        normalized_existing_metadata = _normalize_target_metadata(
            existing_metadata,
            target_kind=target_kind,
            target_identity=target_identity,
        )
        merged_metadata = _merge_missing_metadata(existing_metadata, desired_metadata)
        metadata_changed = merged_metadata != normalized_existing_metadata
        if not missing_caps and not metadata_changed:
            skipped.append(target_identity)
            continue

        safe_registry.upsert_target(
            target_id=existing_target_id or _stable_target_id(target_kind, target_identity),
            target_kind=target_kind,
            target_identity=target_identity,
            display_name=_as_str(existing.get("display_name"), _as_str(spec.get("display_name"), target_identity)),
            description=_as_str(existing.get("description"), _as_str(spec.get("description"))),
            capabilities=merged_caps,
            credential_scope=existing.get("credential_scope") if isinstance(existing.get("credential_scope"), dict) else {},
            metadata=merged_metadata,
            updated_by=updated_by,
            reason=reason,
        )
        updated.append(target_identity)

    return {
        "enabled": True,
        "created": created,
        "updated": updated,
        "skipped": skipped,
    }
