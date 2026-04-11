"""
AI 分析会话历史存储

目标：
- 每次分析请求都生成会话记录
- 每次追问都以消息形式追加保存
- 默认写入 ClickHouse，若不可用则退化为进程内存
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

ALLOWED_SESSION_SORT_FIELDS: Dict[str, str] = {
    "updated_at": "updated_at",
    "created_at": "created_at",
    "title": "title",
    "service_name": "service_name",
    "analysis_type": "analysis_type",
}
ALLOWED_SESSION_SORT_ORDERS: Dict[str, str] = {
    "asc": "ASC",
    "desc": "DESC",
}
MESSAGE_METADATA_MAX_CHARS = max(4096, int(os.getenv("AI_HISTORY_MESSAGE_METADATA_MAX_CHARS", "65536")))
MESSAGE_METADATA_TEXT_MAX_CHARS = max(120, int(os.getenv("AI_HISTORY_MESSAGE_METADATA_TEXT_MAX_CHARS", "1200")))
MESSAGE_METADATA_LIST_MAX_ITEMS = max(1, int(os.getenv("AI_HISTORY_MESSAGE_METADATA_LIST_MAX_ITEMS", "40")))


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _to_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        return datetime.now(timezone.utc).replace(tzinfo=None)


def _to_iso(value: Any) -> str:
    if isinstance(value, datetime):
        iso_text = value.isoformat()
        if value.tzinfo is None:
            return f"{iso_text}Z"
        if iso_text.endswith("+00:00"):
            return f"{iso_text[:-6]}Z"
        return iso_text
    text = str(value or "").strip()
    if text and text.endswith("Z") and len(text) > 6 and text[-7] in {"+", "-"}:
        # 兼容历史脏数据: 2026-03-03T02:24:29.513000+00:00Z -> +00:00
        return text[:-1]
    return text


def _safe_json_loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    text = str(value).strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:
        return default


def _sanitize_session_sort_field(value: str) -> str:
    """规范化会话排序字段，回退到安全默认值。"""
    normalized = str(value or "").strip().lower()
    return ALLOWED_SESSION_SORT_FIELDS.get(normalized, ALLOWED_SESSION_SORT_FIELDS["updated_at"])


def _sanitize_session_sort_order(value: str) -> str:
    """规范化会话排序方向，回退到安全默认值。"""
    normalized = str(value or "").strip().lower()
    return ALLOWED_SESSION_SORT_ORDERS.get(normalized, ALLOWED_SESSION_SORT_ORDERS["desc"])


def _session_sort_value(session: "AISession", sort_field: str) -> Any:
    """提取会话排序值，供内存排序复用。"""
    if sort_field in {"updated_at", "created_at"}:
        return _to_datetime(getattr(session, sort_field, ""))
    return str(getattr(session, sort_field, "") or "").lower()


def _build_session_order_expr(
    *,
    sort_by: str,
    sort_order: str,
    pinned_first: bool,
) -> Tuple[str, str, str]:
    """构建会话列表 ORDER BY 子句（字段/方向白名单）。"""
    safe_sort_field = _sanitize_session_sort_field(sort_by)
    safe_sort_order = _sanitize_session_sort_order(sort_order)
    order_parts: List[str] = []
    if pinned_first:
        order_parts.append("is_pinned DESC")
    order_parts.append(f"{safe_sort_field} {safe_sort_order}")
    if not (safe_sort_field == "updated_at" and safe_sort_order == "DESC"):
        order_parts.append("updated_at DESC")
    order_parts.append("session_id DESC")
    return ", ".join(order_parts), safe_sort_field, safe_sort_order


def _sort_sessions_in_memory(
    sessions: List["AISession"],
    *,
    sort_field: str,
    sort_order: str,
    pinned_first: bool,
) -> List["AISession"]:
    """内存兜底排序，尽量与 ClickHouse ORDER BY 语义保持一致。"""
    ordered = list(sessions)
    ordered.sort(key=lambda item: str(item.session_id or ""), reverse=True)
    ordered.sort(key=lambda item: _to_datetime(item.updated_at), reverse=True)
    ordered.sort(key=lambda item: _session_sort_value(item, sort_field), reverse=(sort_order == "DESC"))
    if pinned_first:
        ordered.sort(key=lambda item: 1 if item.is_pinned else 0, reverse=True)
    return ordered


def _extract_summary_text(result: Optional[Dict[str, Any]]) -> str:
    """从分析结果中提取可检索的摘要文本。"""
    if not isinstance(result, dict):
        return ""
    summary = str(result.get("summary") or "").strip()
    if summary:
        return summary
    raw = result.get("raw")
    if isinstance(raw, dict):
        overview = raw.get("overview")
        if isinstance(overview, dict):
            description = str(overview.get("description") or "").strip()
            if description:
                return description
        fallback = str(raw.get("summary") or raw.get("description") or "").strip()
        if fallback:
            return fallback
    return ""


def _deleted_message_count_from_context(context: Any) -> int:
    """计算 context.deleted_message_ids 条数。"""
    if not isinstance(context, dict):
        return 0
    raw = context.get("deleted_message_ids")
    if not isinstance(raw, list):
        return 0
    seen = set()
    count = 0
    for item in raw:
        mid = str(item or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        count += 1
    return count


def _truncate_text(value: Any, max_chars: int = MESSAGE_METADATA_TEXT_MAX_CHARS) -> str:
    text = str(value or "")
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}\n...<truncated>..."


def _compact_action_observation(item: Any) -> Dict[str, Any]:
    obs = item if isinstance(item, dict) else {}
    return {
        "status": str(obs.get("status") or "").strip().lower(),
        "action_id": str(obs.get("action_id") or "").strip(),
        "command_run_id": str(obs.get("command_run_id") or "").strip(),
        "command": _truncate_text(obs.get("command"), max_chars=320),
        "command_type": str(obs.get("command_type") or "").strip(),
        "risk_level": str(obs.get("risk_level") or "").strip(),
        "exit_code": int(obs.get("exit_code") or 0),
        "timed_out": bool(obs.get("timed_out")),
        "auto_executed": bool(obs.get("auto_executed")),
        "output_truncated": bool(obs.get("output_truncated")),
        "message": _truncate_text(obs.get("message"), max_chars=360),
        "stdout_preview": _truncate_text(obs.get("stdout"), max_chars=280),
        "stderr_preview": _truncate_text(obs.get("stderr"), max_chars=280),
    }


def _compact_followup_action(item: Any) -> Dict[str, Any]:
    action = item if isinstance(item, dict) else {}
    return {
        "id": str(action.get("id") or "").strip(),
        "title": _truncate_text(action.get("title"), max_chars=240),
        "purpose": _truncate_text(action.get("purpose"), max_chars=280),
        "action_type": str(action.get("action_type") or "").strip(),
        "command": _truncate_text(action.get("command"), max_chars=320),
        "command_type": str(action.get("command_type") or "").strip(),
        "risk_level": str(action.get("risk_level") or "").strip(),
        "requires_confirmation": bool(action.get("requires_confirmation")),
        "requires_elevation": bool(action.get("requires_elevation")),
        "requires_write_permission": bool(action.get("requires_write_permission")),
        "executable": bool(action.get("executable")),
        "priority": int(action.get("priority") or 0),
    }


def _compact_message_metadata(metadata: Any) -> Dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    compact: Dict[str, Any] = dict(metadata)

    if isinstance(compact.get("action_observations"), list):
        compact["action_observations"] = [
            _compact_action_observation(item)
            for item in compact.get("action_observations", [])[:MESSAGE_METADATA_LIST_MAX_ITEMS]
        ]

    if isinstance(compact.get("actions"), list):
        compact["actions"] = [
            _compact_followup_action(item)
            for item in compact.get("actions", [])[:MESSAGE_METADATA_LIST_MAX_ITEMS]
        ]

    if isinstance(compact.get("thoughts"), list):
        safe_thoughts: List[Dict[str, Any]] = []
        for item in compact.get("thoughts", [])[:MESSAGE_METADATA_LIST_MAX_ITEMS]:
            payload = item if isinstance(item, dict) else {}
            safe_thoughts.append(
                {
                    "phase": str(payload.get("phase") or "").strip(),
                    "title": _truncate_text(payload.get("title"), max_chars=200),
                    "detail": _truncate_text(payload.get("detail"), max_chars=320),
                    "status": str(payload.get("status") or "").strip(),
                    "iteration": int(payload.get("iteration") or 0),
                }
            )
        compact["thoughts"] = safe_thoughts

    if isinstance(compact.get("long_term_memory_summary"), str):
        compact["long_term_memory_summary"] = _truncate_text(
            compact.get("long_term_memory_summary"),
            max_chars=800,
        )

    encoded = json.dumps(compact, ensure_ascii=False)
    if len(encoded) <= MESSAGE_METADATA_MAX_CHARS:
        return compact

    drop_order = [
        "thoughts",
        "react_iterations",
        "references",
        "context_pills",
        "subgoals",
        "reflection",
        "react_memory",
        "timeout_profile",
    ]
    for key in drop_order:
        if key in compact:
            compact.pop(key, None)
            encoded = json.dumps(compact, ensure_ascii=False)
            if len(encoded) <= MESSAGE_METADATA_MAX_CHARS:
                return compact

    compact["actions"] = [
        _compact_followup_action(item)
        for item in (compact.get("actions") or [])[:10]
    ]
    compact["action_observations"] = [
        _compact_action_observation(item)
        for item in (compact.get("action_observations") or [])[:10]
    ]
    encoded = json.dumps(compact, ensure_ascii=False)
    if len(encoded) <= MESSAGE_METADATA_MAX_CHARS:
        return compact

    react_loop = compact.get("react_loop") if isinstance(compact.get("react_loop"), dict) else {}
    return {
        "truncated": True,
        "reason": "metadata_too_large",
        "react_loop": {
            "summary": _truncate_text(react_loop.get("summary"), max_chars=320),
            "replan": react_loop.get("replan") if isinstance(react_loop.get("replan"), dict) else {},
        },
        "action_observations": [
            _compact_action_observation(item)
            for item in (compact.get("action_observations") or [])[:6]
        ],
    }


def _build_session_title(
    *,
    analysis_type: str,
    service_name: str,
    trace_id: str,
    summary_text: str,
    input_text: str,
) -> str:
    """生成默认会话标题。"""
    base = str(summary_text or "").strip()
    if not base:
        base = str(input_text or "").strip().replace("\n", " ")
    if not base:
        if trace_id:
            base = f"trace={trace_id}"
        elif service_name:
            base = f"service={service_name}"
        else:
            base = "AI analysis session"
    prefix = "Trace" if str(analysis_type or "").strip().lower() == "trace" else "Log"
    service = str(service_name or "").strip()
    if service:
        return f"{prefix}:{service} {base}"[:180]
    return f"{prefix}:{base}"[:180]


@dataclass
class AISession:
    session_id: str
    analysis_type: str
    title: str = ""
    service_name: str = ""
    input_text: str = ""
    trace_id: str = ""
    summary_text: str = ""
    context: Dict[str, Any] = field(default_factory=dict)
    result: Dict[str, Any] = field(default_factory=dict)
    analysis_method: str = ""
    llm_model: str = ""
    llm_provider: str = ""
    source: str = "ai-analysis"
    status: str = "completed"
    created_at: str = ""
    updated_at: str = ""
    is_pinned: bool = False
    is_archived: bool = False
    is_deleted: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AISessionMessage:
    session_id: str
    message_id: str
    msg_index: int
    role: str
    content: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class AISessionStore:
    """AI 会话存储（ClickHouse + 内存兜底）。"""

    def __init__(self, storage_adapter=None):
        self.storage = storage_adapter
        self._sessions: Dict[str, AISession] = {}
        self._messages: Dict[str, List[AISessionMessage]] = {}

        default_database = (
            getattr(storage_adapter, "ch_database", "")
            or (getattr(storage_adapter, "config", {}) or {}).get("clickhouse", {}).get("database", "logs")
            or "logs"
        )
        self.session_table = os.getenv("AI_HISTORY_SESSION_CH_TABLE", f"{default_database}.ai_analysis_sessions")
        self.message_table = os.getenv("AI_HISTORY_MESSAGE_CH_TABLE", f"{default_database}.ai_analysis_messages")
        self.session_latest_view = os.getenv(
            "AI_HISTORY_SESSION_LATEST_VIEW",
            f"{default_database}.v_ai_analysis_sessions_latest",
        )
        self._read_source_cache_ttl_seconds = max(
            5,
            int(os.getenv("AI_HISTORY_READ_SOURCE_CACHE_TTL_SECONDS", "30")),
        )
        self._session_read_source_cache: Optional[Tuple[str, bool]] = None
        self._session_read_source_cache_checked_at = 0.0

        if self._is_clickhouse_available():
            self._ensure_clickhouse_tables()

    def attach_storage(self, storage_adapter) -> None:
        self.storage = storage_adapter
        self._session_read_source_cache = None
        self._session_read_source_cache_checked_at = 0.0
        if self._is_clickhouse_available():
            self._ensure_clickhouse_tables()

    def _is_clickhouse_available(self) -> bool:
        return bool(self.storage and getattr(self.storage, "ch_client", None))

    @staticmethod
    def _split_table_name(table_name: str) -> Tuple[str, str]:
        normalized = str(table_name or "").strip()
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

    def _get_session_read_source(self) -> Tuple[str, bool]:
        """
        返回会话读取数据源：
        - 优先 latest 视图（无需 FINAL）
        - 回退原始表（需 FINAL 保证去重语义）
        """
        now_ts = time.time()
        cached = self._session_read_source_cache
        if cached is not None and (now_ts - self._session_read_source_cache_checked_at) < self._read_source_cache_ttl_seconds:
            return cached

        if self._table_exists(self.session_latest_view):
            self._session_read_source_cache = (self.session_latest_view, False)
        else:
            self._session_read_source_cache = (self.session_table, True)
        self._session_read_source_cache_checked_at = now_ts
        return self._session_read_source_cache

    def _ensure_clickhouse_tables(self) -> None:
        if not self._is_clickhouse_available():
            return

        create_session_sql = f"""
        CREATE TABLE IF NOT EXISTS {self.session_table} (
            session_id String,
            analysis_type String,
            title String,
            service_name String,
            input_text String,
            trace_id String,
            summary_text String,
            context_json String,
            result_json String,
            analysis_method String,
            llm_model String,
            llm_provider String,
            source String,
            status String,
            created_at DateTime64(3, 'UTC'),
            updated_at DateTime64(3, 'UTC'),
            is_pinned UInt8 DEFAULT 0,
            is_archived UInt8 DEFAULT 0,
            is_deleted UInt8 DEFAULT 0
        )
        ENGINE = ReplacingMergeTree(updated_at)
        PARTITION BY toYYYYMM(created_at)
        ORDER BY (session_id)
        SETTINGS index_granularity = 8192
        """

        create_message_sql = f"""
        CREATE TABLE IF NOT EXISTS {self.message_table} (
            session_id String,
            message_id String,
            msg_index UInt32,
            role String,
            content String,
            metadata_json String,
            created_at DateTime64(3, 'UTC')
        )
        ENGINE = MergeTree()
        PARTITION BY toYYYYMM(created_at)
        ORDER BY (session_id, msg_index, created_at, message_id)
        SETTINGS index_granularity = 8192
        """

        self.storage.ch_client.execute(create_session_sql)
        self.storage.ch_client.execute(create_message_sql)
        self._ensure_session_schema()

    def _ensure_session_schema(self) -> None:
        """兼容旧表结构，按需补齐新增列。"""
        if not self._is_clickhouse_available():
            return
        alters = [
            f"ALTER TABLE {self.session_table} ADD COLUMN IF NOT EXISTS title String DEFAULT ''",
            f"ALTER TABLE {self.session_table} ADD COLUMN IF NOT EXISTS summary_text String DEFAULT ''",
            f"ALTER TABLE {self.session_table} ADD COLUMN IF NOT EXISTS is_pinned UInt8 DEFAULT 0",
            f"ALTER TABLE {self.session_table} ADD COLUMN IF NOT EXISTS is_archived UInt8 DEFAULT 0",
        ]
        for sql in alters:
            try:
                self.storage.ch_client.execute(sql)
            except Exception as exc:
                logger.warning(f"failed to ensure ai session schema: {exc}")

    def _build_session_row(self, session: AISession) -> Dict[str, Any]:
        return {
            "session_id": session.session_id,
            "analysis_type": session.analysis_type,
            "title": session.title,
            "service_name": session.service_name,
            "input_text": session.input_text,
            "trace_id": session.trace_id,
            "summary_text": session.summary_text,
            "context_json": json.dumps(session.context or {}, ensure_ascii=False),
            "result_json": json.dumps(session.result or {}, ensure_ascii=False),
            "analysis_method": session.analysis_method,
            "llm_model": session.llm_model,
            "llm_provider": session.llm_provider,
            "source": session.source,
            "status": session.status,
            "created_at": _to_datetime(session.created_at),
            "updated_at": _to_datetime(session.updated_at),
            "is_pinned": 1 if session.is_pinned else 0,
            "is_archived": 1 if session.is_archived else 0,
            "is_deleted": 1 if session.is_deleted else 0,
        }

    def _upsert_session_to_clickhouse(self, session: AISession) -> None:
        if not self._is_clickhouse_available():
            return
        sql = f"""
        INSERT INTO {self.session_table} (
            session_id, analysis_type, title, service_name, input_text, trace_id,
            summary_text, context_json, result_json, analysis_method, llm_model, llm_provider,
            source, status, created_at, updated_at, is_pinned, is_archived, is_deleted
        ) VALUES
        """
        self.storage.ch_client.execute(sql, [self._build_session_row(session)])

    def _build_message_rows(self, messages: List[AISessionMessage]) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for msg in messages:
            compacted_metadata = _compact_message_metadata(msg.metadata)
            rows.append(
                {
                    "session_id": msg.session_id,
                    "message_id": msg.message_id,
                    "msg_index": max(0, int(msg.msg_index)),
                    "role": msg.role,
                    "content": msg.content,
                    "metadata_json": json.dumps(compacted_metadata, ensure_ascii=False),
                    "created_at": _to_datetime(msg.created_at),
                }
            )
        return rows

    def _insert_messages_to_clickhouse(self, messages: List[AISessionMessage]) -> None:
        if not self._is_clickhouse_available() or not messages:
            return
        sql = f"""
        INSERT INTO {self.message_table} (
            session_id, message_id, msg_index, role, content, metadata_json, created_at
        ) VALUES
        """
        self.storage.ch_client.execute(sql, self._build_message_rows(messages))

    def _row_to_session(self, row: Any) -> Optional[AISession]:
        if not row or len(row) < 19:
            return None
        return AISession(
            session_id=str(row[0]),
            analysis_type=str(row[1]),
            title=str(row[2] or ""),
            service_name=str(row[3] or ""),
            input_text=str(row[4] or ""),
            trace_id=str(row[5] or ""),
            summary_text=str(row[6] or ""),
            context=_safe_json_loads(row[7], {}),
            result=_safe_json_loads(row[8], {}),
            analysis_method=str(row[9] or ""),
            llm_model=str(row[10] or ""),
            llm_provider=str(row[11] or ""),
            source=str(row[12] or "ai-analysis"),
            status=str(row[13] or "completed"),
            created_at=_to_iso(row[14]),
            updated_at=_to_iso(row[15]),
            is_pinned=bool(row[16]),
            is_archived=bool(row[17]),
            is_deleted=bool(row[18]),
        )

    def _query_session_from_clickhouse(self, session_id: str) -> Optional[AISession]:
        if not self._is_clickhouse_available():
            return None
        # 点查 session_id 场景强制走基表 + FINAL，避免 latest 聚合视图在内存紧张时触发全局 AggregatingTransform。
        source_table = self.session_table
        final_clause = "FINAL"
        sql = f"""
        SELECT
            session_id, analysis_type, title, service_name, input_text, trace_id,
            summary_text, context_json, result_json, analysis_method, llm_model, llm_provider,
            source, status, created_at, updated_at, is_pinned, is_archived, is_deleted
        FROM {source_table}
        {final_clause}
        WHERE session_id = %(session_id)s
        LIMIT 1
        """
        try:
            rows = self.storage.ch_client.execute(sql, {"session_id": session_id})
        except Exception as exc:
            logger.warning("query session from clickhouse failed (session_id=%s): %s", session_id, exc)
            return None
        if not rows:
            return None
        session = self._row_to_session(rows[0])
        if not session or session.is_deleted:
            return None
        return session

    def _query_messages_from_clickhouse(
        self,
        session_id: str,
        limit: int = 500,
        *,
        include_metadata: bool = True,
    ) -> List[AISessionMessage]:
        if not self._is_clickhouse_available():
            return []
        metadata_expr = "metadata_json" if include_metadata else "'' AS metadata_json"
        sql = f"""
        SELECT
            session_id, message_id, msg_index, role, content, {metadata_expr}, created_at
        FROM {self.message_table}
        WHERE session_id = %(session_id)s
        ORDER BY msg_index ASC, created_at ASC
        LIMIT %(limit)s
        """
        rows = self.storage.ch_client.execute(sql, {"session_id": session_id, "limit": max(1, int(limit))})
        messages: List[AISessionMessage] = []
        for row in rows:
            if not row or len(row) < 7:
                continue
            messages.append(
                AISessionMessage(
                    session_id=str(row[0]),
                    message_id=str(row[1]),
                    msg_index=int(row[2]),
                    role=str(row[3]),
                    content=str(row[4]),
                    metadata=_safe_json_loads(row[5], {}),
                    created_at=_to_iso(row[6]),
                )
            )
        return messages

    def _query_recent_assistant_messages_from_clickhouse(
        self,
        session_id: str,
        limit: int = 10,
    ) -> List[AISessionMessage]:
        if not self._is_clickhouse_available():
            return []
        sql = f"""
        SELECT
            session_id, message_id, msg_index, role, content, metadata_json, created_at
        FROM (
            SELECT
                session_id, message_id, msg_index, role, content, metadata_json, created_at
            FROM {self.message_table}
            WHERE session_id = %(session_id)s
              AND role = 'assistant'
            ORDER BY msg_index DESC, created_at DESC
            LIMIT %(limit)s
        )
        ORDER BY msg_index ASC, created_at ASC
        """
        rows = self.storage.ch_client.execute(sql, {"session_id": session_id, "limit": max(1, int(limit))})
        messages: List[AISessionMessage] = []
        for row in rows:
            if not row or len(row) < 7:
                continue
            messages.append(
                AISessionMessage(
                    session_id=str(row[0]),
                    message_id=str(row[1]),
                    msg_index=int(row[2]),
                    role=str(row[3]),
                    content=str(row[4]),
                    metadata=_safe_json_loads(row[5], {}),
                    created_at=_to_iso(row[6]),
                )
            )
        return messages

    def _normalize_deleted_message_ids(self, raw: Any, max_items: int = 2000) -> List[str]:
        """规范化会话上下文中的 deleted_message_ids。"""
        normalized: List[str] = []
        seen = set()
        for item in raw if isinstance(raw, list) else []:
            mid = str(item or "").strip()
            if not mid or mid in seen:
                continue
            seen.add(mid)
            normalized.append(mid)
            if len(normalized) >= max_items:
                break
        return normalized

    def _get_deleted_message_ids(self, session_id: str) -> set[str]:
        """读取会话级别的消息删除标记集合。"""
        session = self.get_session(session_id)
        if not session or not isinstance(session.context, dict):
            return set()
        return set(self._normalize_deleted_message_ids(session.context.get("deleted_message_ids")))

    def _get_raw_messages(self, session_id: str, limit: int = 500) -> List[AISessionMessage]:
        """获取原始消息（含已删除标记消息），供索引与持久化复用。"""
        sid = (session_id or "").strip()
        if not sid:
            return []
        safe_limit = max(1, int(limit))
        if self._is_clickhouse_available():
            messages = self._query_messages_from_clickhouse(sid, limit=safe_limit)
            self._messages[sid] = messages
            return messages
        return list(self._messages.get(sid, []))[:safe_limit]

    def create_session(
        self,
        analysis_type: str,
        service_name: str,
        input_text: str,
        trace_id: str = "",
        context: Optional[Dict[str, Any]] = None,
        result: Optional[Dict[str, Any]] = None,
        analysis_method: str = "",
        llm_model: str = "",
        llm_provider: str = "",
        source: str = "ai-analysis",
        session_id: str = "",
        title: str = "",
        summary_text: str = "",
    ) -> AISession:
        now = _iso_now()
        sid = (session_id or "").strip() or f"ais-{uuid.uuid4().hex[:16]}"
        final_summary = (summary_text or "").strip() or _extract_summary_text(result)
        final_title = (title or "").strip() or _build_session_title(
            analysis_type=analysis_type,
            service_name=service_name,
            trace_id=trace_id,
            summary_text=final_summary,
            input_text=input_text,
        )
        session = AISession(
            session_id=sid,
            analysis_type=(analysis_type or "log").strip() or "log",
            title=final_title,
            service_name=(service_name or "").strip(),
            input_text=str(input_text or ""),
            trace_id=(trace_id or "").strip(),
            summary_text=final_summary,
            context=context or {},
            result=result or {},
            analysis_method=(analysis_method or "").strip(),
            llm_model=(llm_model or "").strip(),
            llm_provider=(llm_provider or "").strip(),
            source=(source or "ai-analysis").strip(),
            status="completed",
            created_at=now,
            updated_at=now,
        )
        self._sessions[sid] = session
        self._messages.setdefault(sid, [])
        self._upsert_session_to_clickhouse(session)
        return session

    def update_session(self, session_id: str, **changes: Any) -> Optional[AISession]:
        session = self.get_session(session_id)
        if not session:
            return None
        payload = session.to_dict()
        payload.update(changes)
        payload["updated_at"] = _iso_now()
        updated = AISession(**payload)
        self._sessions[session_id] = updated
        self._upsert_session_to_clickhouse(updated)
        return updated

    def get_session(self, session_id: str) -> Optional[AISession]:
        sid = (session_id or "").strip()
        if not sid:
            return None
        if self._is_clickhouse_available():
            session = self._query_session_from_clickhouse(sid)
            if session:
                self._sessions[sid] = session
                return session
            return None
        session = self._sessions.get(sid)
        if session and session.is_deleted:
            return None
        return session

    def _build_clickhouse_history_filters(
        self,
        analysis_type: str,
        service_name: str,
        include_archived: bool,
        search_query: str,
    ) -> Tuple[List[str], Dict[str, Any]]:
        """构建会话列表/计数共用的 ClickHouse 过滤条件。"""
        where_clauses = ["is_deleted = 0"]
        params: Dict[str, Any] = {}
        if analysis_type:
            where_clauses.append("analysis_type = %(analysis_type)s")
            params["analysis_type"] = analysis_type
        if service_name:
            where_clauses.append("service_name = %(service_name)s")
            params["service_name"] = service_name
        if not include_archived:
            where_clauses.append("is_archived = 0")

        search = str(search_query or "").strip()
        if search:
            params["search"] = search
            where_clauses.append(
                "("
                "positionCaseInsensitive(title, %(search)s) > 0 "
                "OR positionCaseInsensitive(summary_text, %(search)s) > 0 "
                "OR positionCaseInsensitive(input_text, %(search)s) > 0 "
                "OR positionCaseInsensitive(trace_id, %(search)s) > 0 "
                "OR positionCaseInsensitive(service_name, %(search)s) > 0 "
                "OR session_id IN ("
                f"SELECT DISTINCT session_id FROM {self.message_table} "
                "WHERE positionCaseInsensitive(content, %(search)s) > 0 "
                "LIMIT 2000"
                ")"
                ")"
            )
        return where_clauses, params

    def count_sessions(
        self,
        analysis_type: str = "",
        service_name: str = "",
        include_archived: bool = False,
        search_query: str = "",
    ) -> int:
        """统计符合过滤条件的会话总数（不受分页限制）。"""
        if self._is_clickhouse_available():
            where_clauses, params = self._build_clickhouse_history_filters(
                analysis_type=analysis_type,
                service_name=service_name,
                include_archived=include_archived,
                search_query=search_query,
            )
            source_table, use_final = self._get_session_read_source()
            final_clause = "FINAL" if use_final else ""
            sql = f"""
            SELECT count()
            FROM {source_table}
            {final_clause}
            WHERE {" AND ".join(where_clauses)}
            """
            rows = self.storage.ch_client.execute(sql, params)
            if rows and rows[0]:
                try:
                    return max(0, int(rows[0][0]))
                except Exception:
                    return 0
            return 0

        sessions = list(self._sessions.values())
        sessions = [s for s in sessions if not s.is_deleted]
        if analysis_type:
            sessions = [s for s in sessions if s.analysis_type == analysis_type]
        if service_name:
            sessions = [s for s in sessions if s.service_name == service_name]
        if not include_archived:
            sessions = [s for s in sessions if not s.is_archived]
        search = str(search_query or "").strip().lower()
        if search:
            filtered: List[AISession] = []
            for session in sessions:
                haystacks = [
                    str(session.title or "").lower(),
                    str(session.summary_text or "").lower(),
                    str(session.input_text or "").lower(),
                    str(session.trace_id or "").lower(),
                    str(session.service_name or "").lower(),
                ]
                has_message = any(search in str(msg.content or "").lower() for msg in self._messages.get(session.session_id, []))
                if any(search in item for item in haystacks) or has_message:
                    filtered.append(session)
            sessions = filtered
        return len(sessions)

    def list_sessions(
        self,
        limit: int = 20,
        offset: int = 0,
        analysis_type: str = "",
        service_name: str = "",
        include_archived: bool = False,
        search_query: str = "",
        pinned_first: bool = True,
        sort_by: str = "updated_at",
        sort_order: str = "desc",
    ) -> List[AISession]:
        safe_limit = max(1, int(limit))
        safe_offset = max(0, int(offset))
        order_expr, safe_sort_field, safe_sort_order = _build_session_order_expr(
            sort_by=sort_by,
            sort_order=sort_order,
            pinned_first=pinned_first,
        )
        if self._is_clickhouse_available():
            where_clauses, params = self._build_clickhouse_history_filters(
                analysis_type=analysis_type,
                service_name=service_name,
                include_archived=include_archived,
                search_query=search_query,
            )
            params.update({"limit": safe_limit, "offset": safe_offset})
            source_table, use_final = self._get_session_read_source()
            final_clause = "FINAL" if use_final else ""

            sql = f"""
            SELECT
                session_id, analysis_type, title, service_name, input_text, trace_id,
                summary_text, context_json, result_json, analysis_method, llm_model, llm_provider,
                source, status, created_at, updated_at, is_pinned, is_archived, is_deleted
            FROM {source_table}
            {final_clause}
            WHERE {" AND ".join(where_clauses)}
            ORDER BY {order_expr}
            LIMIT %(limit)s OFFSET %(offset)s
            """
            rows = self.storage.ch_client.execute(sql, params)
            sessions: List[AISession] = []
            for row in rows:
                session = self._row_to_session(row)
                if not session or session.is_deleted:
                    continue
                self._sessions[session.session_id] = session
                sessions.append(session)
            return sessions

        sessions = list(self._sessions.values())
        sessions = [s for s in sessions if not s.is_deleted]
        if analysis_type:
            sessions = [s for s in sessions if s.analysis_type == analysis_type]
        if service_name:
            sessions = [s for s in sessions if s.service_name == service_name]
        if not include_archived:
            sessions = [s for s in sessions if not s.is_archived]
        search = str(search_query or "").strip().lower()
        if search:
            filtered: List[AISession] = []
            for session in sessions:
                haystacks = [
                    str(session.title or "").lower(),
                    str(session.summary_text or "").lower(),
                    str(session.input_text or "").lower(),
                    str(session.trace_id or "").lower(),
                    str(session.service_name or "").lower(),
                ]
                has_message = any(search in str(msg.content or "").lower() for msg in self._messages.get(session.session_id, []))
                if any(search in item for item in haystacks) or has_message:
                    filtered.append(session)
            sessions = filtered
        sessions = _sort_sessions_in_memory(
            sessions=sessions,
            sort_field=safe_sort_field,
            sort_order=safe_sort_order,
            pinned_first=pinned_first,
        )
        return sessions[safe_offset: safe_offset + safe_limit]

    def list_sessions_with_total(
        self,
        limit: int = 20,
        offset: int = 0,
        analysis_type: str = "",
        service_name: str = "",
        include_archived: bool = False,
        search_query: str = "",
        pinned_first: bool = True,
        sort_by: str = "updated_at",
        sort_order: str = "desc",
    ) -> Tuple[List[AISession], int]:
        """一次查询返回分页会话与总数，减少重复扫描。"""
        safe_limit = max(1, int(limit))
        safe_offset = max(0, int(offset))
        order_expr, safe_sort_field, safe_sort_order = _build_session_order_expr(
            sort_by=sort_by,
            sort_order=sort_order,
            pinned_first=pinned_first,
        )
        if self._is_clickhouse_available():
            where_clauses, params = self._build_clickhouse_history_filters(
                analysis_type=analysis_type,
                service_name=service_name,
                include_archived=include_archived,
                search_query=search_query,
            )
            params.update({"limit": safe_limit, "offset": safe_offset})
            source_table, use_final = self._get_session_read_source()
            final_clause = "FINAL" if use_final else ""

            sql = f"""
            SELECT
                session_id, analysis_type, title, service_name, input_text, trace_id,
                summary_text, context_json, result_json, analysis_method, llm_model, llm_provider,
                source, status, created_at, updated_at, is_pinned, is_archived, is_deleted,
                count() OVER() AS total_count
            FROM {source_table}
            {final_clause}
            WHERE {" AND ".join(where_clauses)}
            ORDER BY {order_expr}
            LIMIT %(limit)s OFFSET %(offset)s
            """
            rows = self.storage.ch_client.execute(sql, params)
            sessions: List[AISession] = []
            total_count = 0
            for row in rows:
                if not row:
                    continue
                session = self._row_to_session(row[:19] if len(row) >= 19 else row)
                if not session or session.is_deleted:
                    continue
                self._sessions[session.session_id] = session
                sessions.append(session)
                if len(row) > 19:
                    try:
                        total_count = max(total_count, int(row[19] or 0))
                    except (TypeError, ValueError) as exc:
                        logger.warning(
                            "Failed to parse total_count from row for session %s: %s",
                            session.session_id if session else "",
                            exc,
                        )
            return sessions, total_count

        sessions = self.list_sessions(
            limit=1000000,
            offset=0,
            analysis_type=analysis_type,
            service_name=service_name,
            include_archived=include_archived,
            search_query=search_query,
            pinned_first=pinned_first,
            sort_by=safe_sort_field,
            sort_order=safe_sort_order,
        )
        total_count = len(sessions)
        return sessions[safe_offset: safe_offset + safe_limit], total_count

    def get_messages(self, session_id: str, limit: int = 500) -> List[AISessionMessage]:
        sid = (session_id or "").strip()
        if not sid:
            return []
        safe_limit = max(1, int(limit))
        raw_messages = self._get_raw_messages(sid, limit=safe_limit)
        deleted_ids = self._get_deleted_message_ids(sid)
        if not deleted_ids:
            return raw_messages[:safe_limit]
        visible = [msg for msg in raw_messages if msg.message_id not in deleted_ids]
        return visible[:safe_limit]

    def get_messages_light(self, session_id: str, limit: int = 500) -> List[AISessionMessage]:
        """读取轻量消息视图，仅返回 role/content/timestamp，不加载 metadata_json。"""
        sid = (session_id or "").strip()
        if not sid:
            return []
        safe_limit = max(1, int(limit))
        if self._is_clickhouse_available():
            try:
                raw_messages = self._query_messages_from_clickhouse(
                    sid,
                    limit=safe_limit,
                    include_metadata=False,
                )
            except Exception as exc:
                logger.warning(
                    "get_messages_light failed, fallback to empty list (session_id=%s): %s",
                    sid,
                    exc,
                )
                return []
        else:
            raw_messages = [
                AISessionMessage(
                    session_id=msg.session_id,
                    message_id=msg.message_id,
                    msg_index=msg.msg_index,
                    role=msg.role,
                    content=msg.content,
                    metadata={},
                    created_at=msg.created_at,
                )
                for msg in list(self._messages.get(sid, []))[:safe_limit]
            ]
        deleted_ids = self._get_deleted_message_ids(sid)
        if not deleted_ids:
            return raw_messages[:safe_limit]
        visible = [msg for msg in raw_messages if msg.message_id not in deleted_ids]
        return visible[:safe_limit]

    def get_recent_assistant_messages_for_react(self, session_id: str, limit: int = 10) -> List[AISessionMessage]:
        """读取 ReAct 记忆需要的最近 assistant 消息（含 metadata，数量小且可降级）。"""
        sid = (session_id or "").strip()
        if not sid:
            return []
        safe_limit = max(1, int(limit))
        if self._is_clickhouse_available():
            try:
                raw_messages = self._query_recent_assistant_messages_from_clickhouse(sid, limit=safe_limit)
            except Exception as exc:
                logger.warning(
                    "get_recent_assistant_messages_for_react failed, fallback to empty list "
                    "(session_id=%s): %s",
                    sid,
                    exc,
                )
                return []
        else:
            raw_messages = [
                msg
                for msg in self._messages.get(sid, [])
                if str(msg.role or "").strip().lower() == "assistant"
            ][-safe_limit:]
        deleted_ids = self._get_deleted_message_ids(sid)
        if not deleted_ids:
            return raw_messages[:safe_limit]
        visible = [msg for msg in raw_messages if msg.message_id not in deleted_ids]
        return visible[:safe_limit]

    def append_messages(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
    ) -> List[AISessionMessage]:
        sid = (session_id or "").strip()
        if not sid or not messages:
            return []

        existing_raw = self._get_raw_messages(sid, limit=10000)
        next_index = (max((msg.msg_index for msg in existing_raw), default=-1) + 1) if existing_raw else 0
        prepared: List[AISessionMessage] = []
        for item in messages:
            role = str(item.get("role", "")).strip().lower()
            content = str(item.get("content", "")).strip()
            if role not in {"user", "assistant", "system"} or not content:
                continue
            prepared.append(
                AISessionMessage(
                    session_id=sid,
                    message_id=str(item.get("message_id") or f"msg-{uuid.uuid4().hex[:12]}"),
                    msg_index=next_index,
                    role=role,
                    content=content,
                    metadata=item.get("metadata", {}) if isinstance(item.get("metadata"), dict) else {},
                    created_at=str(item.get("timestamp") or item.get("created_at") or _iso_now()),
                )
            )
            next_index += 1

        if not prepared:
            return []

        merged = existing_raw + prepared
        self._messages[sid] = merged
        self._insert_messages_to_clickhouse(prepared)
        self.update_session(sid)
        return prepared

    def get_message_count(self, session_id: str) -> int:
        sid = (session_id or "").strip()
        if not sid:
            return 0
        if self._is_clickhouse_available():
            sql = f"SELECT count() FROM {self.message_table} WHERE session_id = %(session_id)s"
            rows = self.storage.ch_client.execute(sql, {"session_id": sid})
            if rows and rows[0]:
                try:
                    total = int(rows[0][0])
                    deleted_ids = self._get_deleted_message_ids(sid)
                    return max(0, total - len(deleted_ids))
                except Exception:
                    return 0
            return 0
        deleted_ids = self._get_deleted_message_ids(sid)
        if not deleted_ids:
            return len(self._messages.get(sid, []))
        return len([msg for msg in self._messages.get(sid, []) if msg.message_id not in deleted_ids])

    def get_message_counts(self, session_ids: List[str]) -> Dict[str, int]:
        """批量获取会话消息数，避免 N+1 查询。"""
        clean_ids: List[str] = []
        for session_id in session_ids:
            sid = str(session_id or "").strip()
            if sid and sid not in clean_ids:
                clean_ids.append(sid)

        if not clean_ids:
            return {}

        if self._is_clickhouse_available():
            escaped = []
            for sid in clean_ids:
                escaped.append("'" + sid.replace("\\", "\\\\").replace("'", "\\'") + "'")
            in_clause = ", ".join(escaped)
            sql = (
                f"SELECT session_id, count() "
                f"FROM {self.message_table} "
                f"WHERE session_id IN ({in_clause}) "
                f"GROUP BY session_id"
            )
            rows = self.storage.ch_client.execute(sql)
            counts: Dict[str, int] = {sid: 0 for sid in clean_ids}
            for row in rows:
                if not row or len(row) < 2:
                    continue
                sid = str(row[0] or "")
                try:
                    counts[sid] = int(row[1] or 0)
                except Exception:
                    counts[sid] = 0
            deleted_counts = self._get_deleted_message_counts(clean_ids)
            for sid in clean_ids:
                counts[sid] = max(0, int(counts.get(sid, 0)) - int(deleted_counts.get(sid, 0)))
            return counts

        counts: Dict[str, int] = {}
        for sid in clean_ids:
            deleted_ids = self._get_deleted_message_ids(sid)
            if not deleted_ids:
                counts[sid] = len(self._messages.get(sid, []))
                continue
            counts[sid] = len([msg for msg in self._messages.get(sid, []) if msg.message_id not in deleted_ids])
        return counts

    def _get_deleted_message_counts(self, session_ids: List[str]) -> Dict[str, int]:
        """批量读取 deleted_message_ids 数量，避免逐会话查询。"""
        clean_ids: List[str] = []
        for session_id in session_ids:
            sid = str(session_id or "").strip()
            if sid and sid not in clean_ids:
                clean_ids.append(sid)
        if not clean_ids:
            return {}

        counts: Dict[str, int] = {}
        missing_ids: List[str] = []
        for sid in clean_ids:
            cached_session = self._sessions.get(sid)
            if isinstance(cached_session, AISession):
                counts[sid] = _deleted_message_count_from_context(cached_session.context)
            else:
                missing_ids.append(sid)

        if not missing_ids or not self._is_clickhouse_available():
            for sid in clean_ids:
                counts.setdefault(sid, 0)
            return counts

        escaped = []
        for sid in missing_ids:
            escaped.append("'" + sid.replace("\\", "\\\\").replace("'", "\\'") + "'")
        in_clause = ", ".join(escaped)

        source_table, use_final = self._get_session_read_source()
        final_clause = "FINAL" if use_final else ""
        sql = f"""
        SELECT session_id, context_json
        FROM {source_table}
        {final_clause}
        WHERE is_deleted = 0
          AND session_id IN ({in_clause})
        """
        rows = self.storage.ch_client.execute(sql)
        for row in rows:
            if not row or len(row) < 2:
                continue
            sid = str(row[0] or "").strip()
            if not sid:
                continue
            context = _safe_json_loads(row[1], {})
            counts[sid] = _deleted_message_count_from_context(context)

        for sid in clean_ids:
            counts.setdefault(sid, 0)
        return counts

    def get_message_by_id(self, session_id: str, message_id: str) -> Optional[AISessionMessage]:
        sid = (session_id or "").strip()
        mid = (message_id or "").strip()
        if not sid or not mid:
            return None
        for item in self.get_messages(sid, limit=5000):
            if item.message_id == mid:
                return item
        return None

    def delete_message(self, session_id: str, message_id: str) -> bool:
        """逻辑删除单条消息（保留原始记录，避免破坏索引与审计轨迹）。"""
        sid = (session_id or "").strip()
        mid = (message_id or "").strip()
        if not sid or not mid:
            return False

        session = self.get_session(sid)
        if not session:
            return False

        existing = self.get_message_by_id(sid, mid)
        if not existing:
            return False

        context = session.context if isinstance(session.context, dict) else {}
        updated_context = dict(context)
        deleted_ids = self._normalize_deleted_message_ids(updated_context.get("deleted_message_ids"))
        if mid not in deleted_ids:
            deleted_ids.append(mid)
        updated_context["deleted_message_ids"] = deleted_ids[-2000:]
        updated = self.update_session(sid, context=updated_context)
        return bool(updated)

    def delete_session(self, session_id: str) -> bool:
        """删除会话（ClickHouse 为软删除）。"""
        sid = (session_id or "").strip()
        if not sid:
            return False
        existing = self.get_session(sid)
        if not existing:
            return False
        deleted = self.update_session(
            sid,
            is_deleted=True,
            is_archived=True,
            status="deleted",
        )
        return bool(deleted)

    def get_session_with_messages(self, session_id: str) -> Optional[Dict[str, Any]]:
        session = self.get_session(session_id)
        if not session:
            return None
        messages = self.get_messages(session_id, limit=2000)
        return {
            "session": session.to_dict(),
            "messages": [msg.to_dict() for msg in messages],
            "message_count": len(messages),
        }


_session_store: Optional[AISessionStore] = None


def get_ai_session_store(storage_adapter=None) -> AISessionStore:
    global _session_store
    if _session_store is None:
        _session_store = AISessionStore(storage_adapter=storage_adapter)
    elif storage_adapter is not None and not _session_store.storage:
        _session_store.attach_storage(storage_adapter)
    return _session_store
