"""
相似案例推荐服务

提供基于历史数据的相似问题检索和推荐：
- 问题特征提取
- 相似度计算
- 历史案例存储
- 推荐排序

Date: 2026-02-22
"""

import os
import json
import logging
import time
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from collections import defaultdict
import re
import uuid

logger = logging.getLogger(__name__)


@dataclass
class Case:
    """案例数据结构"""
    id: str
    problem_type: str
    severity: str
    summary: str
    log_content: str
    service_name: str
    root_causes: List[str] = field(default_factory=list)
    solutions: List[Dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    resolved: bool = False
    resolution: str = ""
    resolved_at: str = ""
    updated_at: str = ""
    context: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
    similarity_features: Dict[str, Any] = field(default_factory=dict)
    llm_provider: str = ""
    llm_model: str = ""
    llm_metadata: Dict[str, Any] = field(default_factory=dict)
    source: str = "manual"
    is_deleted: bool = False
    analysis_summary: str = ""
    manual_remediation_steps: List[str] = field(default_factory=list)
    verification_result: str = ""
    verification_notes: str = ""
    knowledge_version: int = 1
    last_editor: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SimilarCaseResult:
    """相似案例结果"""
    case: Case
    similarity_score: float
    matched_features: List[str]
    relevance_reason: str


class FeatureExtractor:
    """特征提取器"""

    ERROR_PATTERNS = {
        'database': [
            r'connection.*timeout', r'database.*error', r'sql.*exception',
            r'pool.*exhausted', r'deadlock', r'query.*failed'
        ],
        'network': [
            r'connection.*refused', r'network.*unreachable', r'timeout',
            r'dns.*failed', r'socket.*error', r'connection.*reset'
        ],
        'memory': [
            r'out of memory', r'oom', r'heap.*overflow',
            r'memory.*limit', r'gc.*overhead'
        ],
        'disk': [
            r'no space left', r'disk.*full', r'i/o error',
            r'filesystem.*error', r'quota.*exceeded'
        ],
        'auth': [
            r'authentication.*failed', r'unauthorized', r'access.*denied',
            r'token.*expired', r'permission.*denied'
        ],
        'performance': [
            r'slow.*query', r'high.*latency', r'performance.*degraded',
            r'bottleneck', r'throughput.*low'
        ]
    }

    KEYWORD_WEIGHTS = {
        'error': 3.0,
        'exception': 2.5,
        'failed': 2.0,
        'timeout': 2.0,
        'error': 1.5,
        'warn': 1.0,
        'critical': 3.0,
        'fatal': 3.5,
    }

    @classmethod
    def _normalize_text(cls, value: Any) -> str:
        """标准化文本值。"""
        if value is None:
            return ""
        return str(value).strip().lower()

    @classmethod
    def _extract_context_services(cls, context: Optional[Dict[str, Any]]) -> List[str]:
        """提取上下文中出现的服务集合。"""
        if not isinstance(context, dict):
            return []

        service_keys = [
            "service_name",
            "source_service",
            "target_service",
            "upstream_service",
            "downstream_service",
            "caller_service",
            "callee_service",
        ]

        services: set[str] = set()
        for key in service_keys:
            service = cls._normalize_text(context.get(key))
            if service:
                services.add(service)

        for key in ["upstream_services", "downstream_services", "related_services"]:
            for service in context.get(key, []) if isinstance(context.get(key), list) else []:
                normalized = cls._normalize_text(service)
                if normalized:
                    services.add(normalized)

        topology = context.get("topology")
        if isinstance(topology, dict):
            for key in service_keys:
                service = cls._normalize_text(topology.get(key))
                if service:
                    services.add(service)

        return sorted(services)

    @classmethod
    def _normalize_pod_prefix(cls, pod_name: str) -> str:
        """提取 pod 前缀，减少滚动发布随机后缀干扰。"""
        pod = cls._normalize_text(pod_name)
        if not pod:
            return ""

        parts = pod.split("-")
        if len(parts) >= 3 and len(parts[-1]) >= 5 and len(parts[-2]) >= 5:
            return "-".join(parts[:-2])
        if len(parts) >= 2 and len(parts[-1]) >= 5:
            return "-".join(parts[:-1])
        return pod

    @classmethod
    def extract_features(
        cls,
        log_content: str,
        service_name: str = "",
        context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """提取日志特征"""
        context_data = context if isinstance(context, dict) else {}
        k8s_context = context_data.get("k8s") if isinstance(context_data.get("k8s"), dict) else {}

        source_service = cls._normalize_text(
            context_data.get("source_service")
            or context_data.get("caller_service")
            or context_data.get("upstream_service")
            or (context_data.get("topology", {}) or {}).get("source_service")
        )
        target_service = cls._normalize_text(
            context_data.get("target_service")
            or context_data.get("callee_service")
            or context_data.get("downstream_service")
            or (context_data.get("topology", {}) or {}).get("target_service")
        )

        namespace = cls._normalize_text(
            context_data.get("namespace")
            or context_data.get("k8s_namespace")
            or k8s_context.get("namespace")
        )
        pod_name = cls._normalize_text(
            context_data.get("pod_name")
            or k8s_context.get("pod")
        )
        trace_id = cls._normalize_text(
            context_data.get("trace_id")
            or context_data.get("traceId")
        )
        context_services = cls._extract_context_services(context_data)

        features = {
            'problem_types': [],
            'keywords': [],
            'error_codes': [],
            'service': cls._normalize_text(service_name),
            'patterns': [],
            'severity_indicators': [],
            'trace_present': bool(trace_id),
            'trace_id_prefix': trace_id[:12] if trace_id else "",
            'namespace': namespace,
            'pod_prefix': cls._normalize_pod_prefix(pod_name),
            'source_service': source_service,
            'target_service': target_service,
            'call_edge': f"{source_service}->{target_service}" if source_service and target_service else "",
            'context_services': context_services,
        }

        log_lower = log_content.lower()

        for ptype, patterns in cls.ERROR_PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, log_lower):
                    features['problem_types'].append(ptype)
                    features['patterns'].append(pattern)
                    break

        for keyword, weight in cls.KEYWORD_WEIGHTS.items():
            if keyword in log_lower:
                features['keywords'].append(keyword)
                if weight >= 2.5:
                    features['severity_indicators'].append(keyword)

        error_codes = re.findall(r'(?:error|err|e)[\s\-:]?(\d{3,6})', log_lower)
        features['error_codes'] = list(set(error_codes))

        ip_patterns = re.findall(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}', log_content)
        features['has_ip'] = len(ip_patterns) > 0

        url_patterns = re.findall(r'https?://[^\s]+', log_content)
        features['has_url'] = len(url_patterns) > 0

        features['content_length'] = len(log_content)
        features['word_count'] = len(log_content.split())

        return features

    @classmethod
    def compute_similarity(cls, features1: Dict[str, Any], features2: Dict[str, Any]) -> Tuple[float, List[str]]:
        """计算两个特征集的相似度"""
        score = 0.0
        matched_features = []

        if set(features1.get('problem_types', [])) & set(features2.get('problem_types', [])):
            score += 0.3
            matched_features.append('problem_type')

        if features1.get('service') and features1.get('service') == features2.get('service'):
            score += 0.2
            matched_features.append('service')

        common_keywords = set(features1.get('keywords', [])) & set(features2.get('keywords', []))
        if common_keywords:
            keyword_score = min(len(common_keywords) * 0.05, 0.2)
            score += keyword_score
            matched_features.append(f'keywords: {len(common_keywords)}')

        common_patterns = set(features1.get('patterns', [])) & set(features2.get('patterns', []))
        if common_patterns:
            score += 0.15
            matched_features.append('error_pattern')

        common_codes = set(features1.get('error_codes', [])) & set(features2.get('error_codes', []))
        if common_codes:
            score += 0.1
            matched_features.append('error_code')

        if features1.get('call_edge') and features1.get('call_edge') == features2.get('call_edge'):
            score += 0.2
            matched_features.append('call_edge')

        if features1.get('namespace') and features1.get('namespace') == features2.get('namespace'):
            score += 0.08
            matched_features.append('namespace')

        if features1.get('pod_prefix') and features1.get('pod_prefix') == features2.get('pod_prefix'):
            score += 0.05
            matched_features.append('pod_prefix')

        if features1.get('trace_present') and features2.get('trace_present'):
            score += 0.05
            matched_features.append('trace_context')

        common_context_services = set(features1.get('context_services', [])) & set(features2.get('context_services', []))
        if common_context_services:
            context_score = min(len(common_context_services) * 0.04, 0.12)
            score += context_score
            matched_features.append(f'context_services: {len(common_context_services)}')

        return min(score, 1.0), matched_features


class CaseStore:
    """案例存储"""

    def __init__(
        self,
        storage_adapter=None,
        persistence_path: Optional[str] = None,
        persistence_enabled: Optional[bool] = None,
    ):
        self.storage = storage_adapter
        self._cases: Dict[str, Case] = {}
        self._index_by_type: Dict[str, List[str]] = defaultdict(list)
        self._index_by_service: Dict[str, List[str]] = defaultdict(list)
        default_database = (
            getattr(storage_adapter, "ch_database", "")
            or (getattr(storage_adapter, "config", {}) or {}).get("clickhouse", {}).get("database", "logs")
            or "logs"
        )
        self.clickhouse_table = os.getenv("AI_CASE_STORE_CH_TABLE", f"{default_database}.ai_cases")
        self.clickhouse_latest_view = os.getenv(
            "AI_CASE_STORE_LATEST_VIEW",
            f"{default_database}.v_ai_cases_latest",
        )
        self.case_change_history_table = os.getenv(
            "AI_CASE_CHANGE_HISTORY_CH_TABLE",
            f"{default_database}.ai_case_change_history",
        )
        self._read_source_cache_ttl_seconds = max(
            5,
            int(os.getenv("AI_CASE_READ_SOURCE_CACHE_TTL_SECONDS", "30")),
        )
        env_enabled = os.getenv("AI_CASE_STORE_PERSIST", "true").strip().lower() == "true"
        self.persistence_enabled = env_enabled if persistence_enabled is None else persistence_enabled
        self.persistence_path = (
            persistence_path
            or os.getenv("AI_CASE_STORE_PATH")
            or "/tmp/logoscope-ai-cases.json"
        )
        self._case_change_history: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self._case_read_source_cache: Optional[Tuple[str, bool]] = None
        self._case_read_source_cache_checked_at = 0.0
        if self._is_clickhouse_available():
            self._ensure_clickhouse_table()
            self._ensure_case_change_history_table()
            self._load_cases_from_clickhouse()

    def attach_storage(self, storage_adapter) -> None:
        """挂载 storage adapter 并启用 ClickHouse 案例库。"""
        self.storage = storage_adapter
        self._case_read_source_cache = None
        self._case_read_source_cache_checked_at = 0.0
        if self._is_clickhouse_available():
            self._ensure_clickhouse_table()
            self._ensure_case_change_history_table()
            self._load_cases_from_clickhouse()

    def _is_clickhouse_available(self) -> bool:
        """判断是否可使用 ClickHouse 案例库。"""
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

    def _get_case_read_source(self) -> Tuple[str, bool]:
        """
        案例读取优先使用 latest 视图；如果不存在则回退 FINAL 表查询。
        """
        now_ts = time.time()
        cached = self._case_read_source_cache
        if cached is not None and (now_ts - self._case_read_source_cache_checked_at) < self._read_source_cache_ttl_seconds:
            return cached

        if self._table_exists(self.clickhouse_latest_view):
            self._case_read_source_cache = (self.clickhouse_latest_view, False)
        else:
            self._case_read_source_cache = (self.clickhouse_table, True)
        self._case_read_source_cache_checked_at = now_ts
        return self._case_read_source_cache

    @staticmethod
    def _now_iso() -> str:
        """返回当前 UTC ISO 时间。"""
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _parse_datetime(value: Any) -> datetime:
        """把多种时间格式转换为 datetime。"""
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

    @staticmethod
    def _to_iso(value: Any) -> str:
        """将 datetime 或其他值转换为 ISO 字符串。"""
        if isinstance(value, datetime):
            iso_text = value.isoformat()
            if value.tzinfo is None:
                return f"{iso_text}Z"
            if iso_text.endswith("+00:00"):
                return f"{iso_text[:-6]}Z"
            return iso_text
        text = str(value or "").strip()
        if not text:
            return ""
        if re.match(r".*[+-]\d{2}:\d{2}Z$", text):
            return text[:-1]
        return text

    @staticmethod
    def _safe_json_loads(value: Any, default: Any) -> Any:
        """安全解析 JSON。"""
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

    def _reset_cases(self):
        """重置内存案例与索引。"""
        self._cases = {}
        self._index_by_type = defaultdict(list)
        self._index_by_service = defaultdict(list)

    def _remove_case_from_indexes(self, case: Case):
        """从索引中移除案例，防止更新同 ID 时产生重复索引。"""
        if case.problem_type in self._index_by_type:
            self._index_by_type[case.problem_type] = [
                cid for cid in self._index_by_type[case.problem_type] if cid != case.id
            ]
            if not self._index_by_type[case.problem_type]:
                self._index_by_type.pop(case.problem_type, None)

        normalized_service = case.service_name.lower() if case.service_name else ""
        if normalized_service and normalized_service in self._index_by_service:
            self._index_by_service[normalized_service] = [
                cid for cid in self._index_by_service[normalized_service] if cid != case.id
            ]
            if not self._index_by_service[normalized_service]:
                self._index_by_service.pop(normalized_service, None)

    def _ensure_clickhouse_table(self):
        """确保 ClickHouse 案例表存在。"""
        if not self._is_clickhouse_available():
            return
        create_sql = f"""
        CREATE TABLE IF NOT EXISTS {self.clickhouse_table} (
            case_id String,
            problem_type String,
            severity String,
            summary String,
            log_content String,
            service_name String,
            root_causes_json String,
            solutions_json String,
            context_json String,
            tags_json String,
            similarity_features_json String,
            resolved UInt8,
            resolution String,
            created_at DateTime64(3, 'UTC'),
            updated_at DateTime64(3, 'UTC'),
            resolved_at Nullable(DateTime64(3, 'UTC')),
            llm_provider String,
            llm_model String,
            llm_metadata_json String,
            source String,
            is_deleted UInt8 DEFAULT 0
        )
        ENGINE = ReplacingMergeTree(updated_at)
        PARTITION BY toYYYYMM(created_at)
        ORDER BY (case_id)
        SETTINGS index_granularity = 8192
        """
        self.storage.ch_client.execute(create_sql)

    def _ensure_case_change_history_table(self):
        """确保知识库变更历史表存在。"""
        if not self._is_clickhouse_available():
            return
        create_sql = f"""
        CREATE TABLE IF NOT EXISTS {self.case_change_history_table} (
            event_id String,
            case_id String,
            event_type String,
            version UInt32,
            editor String,
            changed_fields_json String,
            changes_json String,
            requested_fields_json String,
            unchanged_requested_fields_json String,
            no_effective_change_reason String,
            effective_save_mode String,
            sync_status String,
            sync_error_code String,
            note String,
            source String,
            created_at DateTime64(3, 'UTC')
        )
        ENGINE = MergeTree()
        PARTITION BY toYYYYMM(created_at)
        ORDER BY (case_id, created_at, event_id)
        SETTINGS index_granularity = 8192
        """
        self.storage.ch_client.execute(create_sql)
        for column_sql in [
            "ADD COLUMN IF NOT EXISTS requested_fields_json String DEFAULT ''",
            "ADD COLUMN IF NOT EXISTS unchanged_requested_fields_json String DEFAULT ''",
            "ADD COLUMN IF NOT EXISTS no_effective_change_reason String DEFAULT ''",
        ]:
            try:
                self.storage.ch_client.execute(f"ALTER TABLE {self.case_change_history_table} {column_sql}")
            except Exception as e:
                logger.warning(
                    "Failed to ensure case change history column with SQL `%s`: %s",
                    column_sql,
                    e,
                )

    def _build_clickhouse_row(self, case: Case) -> Dict[str, Any]:
        """将案例对象转换为 ClickHouse 行。"""
        created_at_dt = self._parse_datetime(case.created_at)
        updated_at_dt = self._parse_datetime(case.updated_at or self._now_iso())
        resolved_at_dt = self._parse_datetime(case.resolved_at) if case.resolved_at else None
        return {
            "case_id": case.id,
            "problem_type": case.problem_type,
            "severity": case.severity,
            "summary": case.summary,
            "log_content": case.log_content,
            "service_name": case.service_name or "",
            "root_causes_json": json.dumps(case.root_causes or [], ensure_ascii=False),
            "solutions_json": json.dumps(case.solutions or [], ensure_ascii=False),
            "context_json": json.dumps(case.context or {}, ensure_ascii=False),
            "tags_json": json.dumps(case.tags or [], ensure_ascii=False),
            "similarity_features_json": json.dumps(case.similarity_features or {}, ensure_ascii=False),
            "resolved": 1 if case.resolved else 0,
            "resolution": case.resolution or "",
            "created_at": created_at_dt,
            "updated_at": updated_at_dt,
            "resolved_at": resolved_at_dt,
            "llm_provider": case.llm_provider or "",
            "llm_model": case.llm_model or "",
            "llm_metadata_json": json.dumps(case.llm_metadata or {}, ensure_ascii=False),
            "source": case.source or "manual",
            "is_deleted": 1 if case.is_deleted else 0,
        }

    def _upsert_case_to_clickhouse(self, case: Case):
        """将案例写入 ClickHouse（追加版本行）。"""
        if not self._is_clickhouse_available():
            return
        insert_sql = f"""
        INSERT INTO {self.clickhouse_table} (
            case_id, problem_type, severity, summary, log_content, service_name,
            root_causes_json, solutions_json, context_json, tags_json, similarity_features_json,
            resolved, resolution, created_at, updated_at, resolved_at,
            llm_provider, llm_model, llm_metadata_json, source, is_deleted
        ) VALUES
        """
        self.storage.ch_client.execute(insert_sql, [self._build_clickhouse_row(case)])

    def append_case_change_history(
        self,
        case_id: str,
        event: Dict[str, Any],
    ) -> Dict[str, Any]:
        """写入知识库变更历史。"""
        normalized_case_id = str(case_id or "").strip()
        if not normalized_case_id:
            raise ValueError("case_id is required")
        event_obj = event if isinstance(event, dict) else {}
        changed_fields = [
            str(item).strip()
            for item in event_obj.get("changed_fields", [])
            if str(item).strip()
        ] if isinstance(event_obj.get("changed_fields"), list) else []
        requested_fields = [
            str(item).strip()
            for item in event_obj.get("requested_fields", [])
            if str(item).strip()
        ] if isinstance(event_obj.get("requested_fields"), list) else []
        unchanged_requested_fields = [
            str(item).strip()
            for item in event_obj.get("unchanged_requested_fields", [])
            if str(item).strip()
        ] if isinstance(event_obj.get("unchanged_requested_fields"), list) else []
        changes = event_obj.get("changes") if isinstance(event_obj.get("changes"), dict) else {}
        created_at = str(event_obj.get("updated_at") or event_obj.get("created_at") or self._now_iso())
        row = {
            "event_id": str(event_obj.get("event_id") or f"chg-{uuid.uuid4().hex[:16]}"),
            "case_id": normalized_case_id,
            "event_type": str(event_obj.get("event_type") or "content_update"),
            "version": max(1, int(event_obj.get("version") or 1)),
            "editor": str(event_obj.get("editor") or "manual_content"),
            "changed_fields": changed_fields,
            "changes": changes,
            "requested_fields": requested_fields,
            "unchanged_requested_fields": unchanged_requested_fields,
            "no_effective_change_reason": str(event_obj.get("no_effective_change_reason") or ""),
            "effective_save_mode": str(event_obj.get("effective_save_mode") or "local_only"),
            "sync_status": str(event_obj.get("sync_status") or "not_requested"),
            "sync_error_code": str(event_obj.get("sync_error_code") or ""),
            "note": str(event_obj.get("note") or ""),
            "source": str(event_obj.get("source") or "api:/ai/cases/update"),
            "created_at": created_at,
        }
        if self._is_clickhouse_available():
            insert_payload = {
                "event_id": row["event_id"],
                "case_id": row["case_id"],
                "event_type": row["event_type"],
                "version": row["version"],
                "editor": row["editor"],
                "changed_fields_json": json.dumps(row["changed_fields"], ensure_ascii=False),
                "changes_json": json.dumps(row["changes"], ensure_ascii=False),
                "requested_fields_json": json.dumps(row["requested_fields"], ensure_ascii=False),
                "unchanged_requested_fields_json": json.dumps(row["unchanged_requested_fields"], ensure_ascii=False),
                "no_effective_change_reason": row["no_effective_change_reason"],
                "effective_save_mode": row["effective_save_mode"],
                "sync_status": row["sync_status"],
                "sync_error_code": row["sync_error_code"],
                "note": row["note"],
                "source": row["source"],
                "created_at": self._parse_datetime(row["created_at"]),
            }
            insert_sql_with_extended_columns = f"""
            INSERT INTO {self.case_change_history_table} (
                event_id, case_id, event_type, version, editor, changed_fields_json, changes_json,
                requested_fields_json, unchanged_requested_fields_json, no_effective_change_reason,
                effective_save_mode, sync_status, sync_error_code, note, source, created_at
            ) VALUES
            """
            try:
                self.storage.ch_client.execute(insert_sql_with_extended_columns, [insert_payload])
            except Exception as e:
                logger.warning(
                    "Failed to insert extended case change history row, falling back to legacy columns: %s",
                    e,
                )
                legacy_insert_sql = f"""
                INSERT INTO {self.case_change_history_table} (
                    event_id, case_id, event_type, version, editor, changed_fields_json, changes_json,
                    effective_save_mode, sync_status, sync_error_code, note, source, created_at
                ) VALUES
                """
                legacy_payload = dict(insert_payload)
                legacy_payload.pop("requested_fields_json", None)
                legacy_payload.pop("unchanged_requested_fields_json", None)
                legacy_payload.pop("no_effective_change_reason", None)
                self.storage.ch_client.execute(legacy_insert_sql, [legacy_payload])
        self._case_change_history[normalized_case_id].append(dict(row))
        self._case_change_history[normalized_case_id] = self._case_change_history[normalized_case_id][-500:]
        return row

    def list_case_change_history(
        self,
        case_id: str,
        limit: int = 50,
        event_type: str = "",
    ) -> List[Dict[str, Any]]:
        """查询知识库变更历史（按时间倒序）。"""
        normalized_case_id = str(case_id or "").strip()
        if not normalized_case_id:
            return []
        safe_limit = max(1, int(limit))
        normalized_event_type = str(event_type or "").strip()

        if self._is_clickhouse_available():
            where_clause = "case_id = %(case_id)s"
            params: Dict[str, Any] = {"case_id": normalized_case_id, "limit": safe_limit}
            if normalized_event_type:
                where_clause += " AND event_type = %(event_type)s"
                params["event_type"] = normalized_event_type
            select_sql = f"""
            SELECT
                event_id, case_id, event_type, version, editor, changed_fields_json, changes_json,
                requested_fields_json, unchanged_requested_fields_json, no_effective_change_reason,
                effective_save_mode, sync_status, sync_error_code, note, source, created_at
            FROM {self.case_change_history_table}
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT %(limit)s
            """
            try:
                rows = self.storage.ch_client.execute(select_sql, params)
            except Exception as e:
                logger.warning(
                    "Failed to query extended case change history columns, fallback to legacy query: %s",
                    e,
                )
                legacy_select_sql = f"""
                SELECT
                    event_id, case_id, event_type, version, editor, changed_fields_json, changes_json,
                    effective_save_mode, sync_status, sync_error_code, note, source, created_at
                FROM {self.case_change_history_table}
                WHERE {where_clause}
                ORDER BY created_at DESC
                LIMIT %(limit)s
                """
                rows = self.storage.ch_client.execute(legacy_select_sql, params)
            result: List[Dict[str, Any]] = []
            for row in rows:
                if not row or len(row) < 13:
                    continue
                try:
                    version = max(1, int(row[3] or 1))
                except Exception:
                    version = 1
                try:
                    changed_fields = json.loads(str(row[5] or "[]"))
                    if not isinstance(changed_fields, list):
                        changed_fields = []
                except Exception:
                    changed_fields = []
                try:
                    changes = json.loads(str(row[6] or "{}"))
                    if not isinstance(changes, dict):
                        changes = {}
                except Exception:
                    changes = {}
                requested_fields: List[str] = []
                unchanged_requested_fields: List[str] = []
                no_effective_change_reason = ""
                if len(row) >= 16:
                    try:
                        requested_fields = json.loads(str(row[7] or "[]"))
                        if not isinstance(requested_fields, list):
                            requested_fields = []
                    except Exception:
                        requested_fields = []
                    try:
                        unchanged_requested_fields = json.loads(str(row[8] or "[]"))
                        if not isinstance(unchanged_requested_fields, list):
                            unchanged_requested_fields = []
                    except Exception:
                        unchanged_requested_fields = []
                    no_effective_change_reason = str(row[9] or "")
                    effective_save_mode = str(row[10] or "")
                    sync_status = str(row[11] or "")
                    sync_error_code = str(row[12] or "")
                    note = str(row[13] or "")
                    source = str(row[14] or "")
                    updated_at = self._to_iso(row[15])
                else:
                    effective_save_mode = str(row[7] or "")
                    sync_status = str(row[8] or "")
                    sync_error_code = str(row[9] or "")
                    note = str(row[10] or "")
                    source = str(row[11] or "")
                    updated_at = self._to_iso(row[12])
                result.append(
                    {
                        "event_id": str(row[0] or ""),
                        "case_id": str(row[1] or ""),
                        "event_type": str(row[2] or ""),
                        "version": version,
                        "editor": str(row[4] or ""),
                        "changed_fields": changed_fields,
                        "changes": changes,
                        "requested_fields": requested_fields,
                        "unchanged_requested_fields": unchanged_requested_fields,
                        "no_effective_change_reason": no_effective_change_reason,
                        "effective_save_mode": effective_save_mode,
                        "sync_status": sync_status,
                        "sync_error_code": sync_error_code,
                        "note": note,
                        "source": source,
                        "updated_at": updated_at,
                    }
                )
            return result

        records = list(self._case_change_history.get(normalized_case_id, []))
        if normalized_event_type:
            records = [item for item in records if str(item.get("event_type") or "") == normalized_event_type]
        records.sort(key=lambda item: str(item.get("created_at") or item.get("updated_at") or ""), reverse=True)
        output: List[Dict[str, Any]] = []
        for item in records[:safe_limit]:
            output.append(
                {
                    "event_id": str(item.get("event_id") or ""),
                    "case_id": normalized_case_id,
                    "event_type": str(item.get("event_type") or "content_update"),
                    "version": max(1, int(item.get("version") or 1)),
                    "editor": str(item.get("editor") or ""),
                    "changed_fields": item.get("changed_fields") if isinstance(item.get("changed_fields"), list) else [],
                    "changes": item.get("changes") if isinstance(item.get("changes"), dict) else {},
                    "requested_fields": (
                        item.get("requested_fields")
                        if isinstance(item.get("requested_fields"), list)
                        else []
                    ),
                    "unchanged_requested_fields": (
                        item.get("unchanged_requested_fields")
                        if isinstance(item.get("unchanged_requested_fields"), list)
                        else []
                    ),
                    "no_effective_change_reason": str(item.get("no_effective_change_reason") or ""),
                    "effective_save_mode": str(item.get("effective_save_mode") or ""),
                    "sync_status": str(item.get("sync_status") or ""),
                    "sync_error_code": str(item.get("sync_error_code") or ""),
                    "note": str(item.get("note") or ""),
                    "source": str(item.get("source") or ""),
                    "updated_at": str(item.get("created_at") or item.get("updated_at") or ""),
                }
            )
        return output

    def count_case_change_history(self, case_id: str, event_type: str = "") -> int:
        """统计知识库变更历史条数。"""
        normalized_case_id = str(case_id or "").strip()
        if not normalized_case_id:
            return 0
        normalized_event_type = str(event_type or "").strip()
        if self._is_clickhouse_available():
            where_clause = "case_id = %(case_id)s"
            params: Dict[str, Any] = {"case_id": normalized_case_id}
            if normalized_event_type:
                where_clause += " AND event_type = %(event_type)s"
                params["event_type"] = normalized_event_type
            sql = (
                f"SELECT count() FROM {self.case_change_history_table} "
                f"WHERE {where_clause}"
            )
            rows = self.storage.ch_client.execute(sql, params)
            if rows and rows[0]:
                try:
                    return max(0, int(rows[0][0]))
                except Exception:
                    return 0
            return 0

        records = list(self._case_change_history.get(normalized_case_id, []))
        if normalized_event_type:
            records = [item for item in records if str(item.get("event_type") or "") == normalized_event_type]
        return len(records)

    def _row_to_case(self, row: Any) -> Optional[Case]:
        """将 ClickHouse 行解析为案例对象。"""
        if not row or len(row) < 21:
            return None
        return Case(
            id=str(row[0]),
            problem_type=str(row[1]),
            severity=str(row[2]),
            summary=str(row[3]),
            log_content=str(row[4]),
            service_name=str(row[5]),
            root_causes=self._safe_json_loads(row[6], []),
            solutions=self._safe_json_loads(row[7], []),
            context=self._safe_json_loads(row[8], {}),
            tags=self._safe_json_loads(row[9], []),
            similarity_features=self._safe_json_loads(row[10], {}),
            resolved=bool(row[11]),
            resolution=str(row[12] or ""),
            created_at=self._to_iso(row[13]),
            updated_at=self._to_iso(row[14]),
            resolved_at=self._to_iso(row[15]),
            llm_provider=str(row[16] or ""),
            llm_model=str(row[17] or ""),
            llm_metadata=self._safe_json_loads(row[18], {}),
            source=str(row[19] or "manual"),
            is_deleted=bool(row[20]),
        )

    def _load_cases_from_clickhouse(self, limit: int = 5000):
        """从 ClickHouse 拉取最新案例快照到内存索引。"""
        if not self._is_clickhouse_available():
            return
        query_limit = max(1, int(limit))
        source_table, use_final = self._get_case_read_source()
        final_clause = "FINAL" if use_final else ""
        select_sql = f"""
        SELECT
            case_id, problem_type, severity, summary, log_content, service_name,
            root_causes_json, solutions_json, context_json, tags_json, similarity_features_json,
            resolved, resolution, created_at, updated_at, resolved_at,
            llm_provider, llm_model, llm_metadata_json, source, is_deleted
        FROM {source_table}
        {final_clause}
        ORDER BY updated_at DESC
        LIMIT {query_limit}
        """
        rows = self.storage.ch_client.execute(select_sql)
        self._reset_cases()
        for row in rows:
            case = self._row_to_case(row)
            if not case or case.is_deleted:
                continue
            if case.id in self._cases:
                continue
            self.add_case(case, persist=False, sync_clickhouse=False)

    def _persist_cases(self):
        """将案例库写入本地 JSON（用于无本地 LLM 时的简易持久化）。"""
        if not self.persistence_enabled or not self.persistence_path:
            return

        try:
            parent = os.path.dirname(self.persistence_path)
            if parent:
                os.makedirs(parent, exist_ok=True)

            payload = {
                "version": 1,
                "generated_at": datetime.now().isoformat(),
                "cases": [case.to_dict() for case in self.get_all_cases()],
            }
            temp_path = f"{self.persistence_path}.tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(temp_path, self.persistence_path)
        except Exception as e:
            logger.warning(f"Failed to persist case store to {self.persistence_path}: {e}")

    def load_persisted_cases(self) -> int:
        """从本地 JSON 加载历史案例。"""
        if not self.persistence_enabled or not self.persistence_path:
            return 0
        if not os.path.exists(self.persistence_path):
            return 0

        try:
            with open(self.persistence_path, "r", encoding="utf-8") as f:
                payload = json.load(f)

            raw_cases = payload.get("cases", payload) if isinstance(payload, dict) else payload
            if not isinstance(raw_cases, list):
                logger.warning(f"Invalid case store payload format: {self.persistence_path}")
                return 0

            loaded = 0
            for item in raw_cases:
                if not isinstance(item, dict):
                    continue
                case_id = str(item.get("id", "")).strip()
                problem_type = str(item.get("problem_type", "")).strip()
                summary = str(item.get("summary", "")).strip()
                if not case_id or not problem_type or not summary:
                    continue

                case = Case(
                    id=case_id,
                    problem_type=problem_type,
                    severity=str(item.get("severity", "medium")),
                    summary=summary,
                    log_content=str(item.get("log_content", "")),
                    service_name=str(item.get("service_name", "")),
                    root_causes=item.get("root_causes", []) if isinstance(item.get("root_causes"), list) else [],
                    solutions=item.get("solutions", []) if isinstance(item.get("solutions"), list) else [],
                    created_at=str(item.get("created_at", "")),
                    resolved=bool(item.get("resolved", False)),
                    resolution=str(item.get("resolution", "")),
                    tags=item.get("tags", []) if isinstance(item.get("tags"), list) else [],
                    similarity_features=item.get("similarity_features", {})
                    if isinstance(item.get("similarity_features"), dict)
                    else {},
                )
                self.add_case(case, persist=False, sync_clickhouse=False)
                loaded += 1

            return loaded
        except Exception as e:
            logger.warning(f"Failed to load case store from {self.persistence_path}: {e}")
            return 0

    def add_case(self, case: Case, persist: bool = True, sync_clickhouse: bool = True):
        """添加案例"""
        now_iso = self._now_iso()
        if not case.created_at:
            case.created_at = now_iso
        if not case.updated_at:
            case.updated_at = now_iso

        if sync_clickhouse and self._is_clickhouse_available():
            case.updated_at = now_iso
            if case.resolved and not case.resolved_at:
                case.resolved_at = now_iso
            self._upsert_case_to_clickhouse(case)

        existing = self._cases.get(case.id)
        if existing:
            self._remove_case_from_indexes(existing)

        self._cases[case.id] = case
        self._index_by_type[case.problem_type].append(case.id)
        if case.service_name:
            self._index_by_service[case.service_name.lower()].append(case.id)
        if persist and not self._is_clickhouse_available():
            self._persist_cases()

    @staticmethod
    def get_case_status(case: Case) -> str:
        """读取案例状态（优先 metadata，其次 resolved 兜底）。"""
        llm_meta = case.llm_metadata if isinstance(case.llm_metadata, dict) else {}
        explicit = str(llm_meta.get("case_status", "")).strip().lower()
        if explicit:
            return explicit
        return "resolved" if case.resolved else "archived"

    def update_case(self, case: Case) -> Case:
        """更新已有案例。"""
        self.add_case(case, persist=True, sync_clickhouse=True)
        return case

    def get_case(self, case_id: str) -> Optional[Case]:
        """获取案例"""
        if self._is_clickhouse_available():
            self._load_cases_from_clickhouse()
        return self._cases.get(case_id)

    def get_cases_by_type(self, problem_type: str) -> List[Case]:
        """按类型获取案例"""
        if self._is_clickhouse_available():
            self._load_cases_from_clickhouse()
        return [self._cases[cid] for cid in self._index_by_type.get(problem_type, []) if cid in self._cases]

    def get_cases_by_service(self, service_name: str) -> List[Case]:
        """按服务获取案例"""
        if self._is_clickhouse_available():
            self._load_cases_from_clickhouse()
        return [self._cases[cid] for cid in self._index_by_service.get(service_name.lower(), []) if cid in self._cases]

    def get_all_cases(self) -> List[Case]:
        """获取所有案例"""
        if self._is_clickhouse_available():
            self._load_cases_from_clickhouse()
        return list(self._cases.values())

    def search(self, query: str, limit: int = 10) -> List[Case]:
        """搜索案例"""
        if self._is_clickhouse_available():
            self._load_cases_from_clickhouse()
        results = []
        query_lower = query.lower()

        for case in self._cases.values():
            if (query_lower in case.summary.lower() or
                query_lower in case.log_content.lower() or
                query_lower in case.service_name.lower()):
                results.append(case)

        return results[:limit]

    def delete_case(self, case_id: str) -> bool:
        """删除案例（ClickHouse 模式为软删除）。"""
        existing = self.get_case(case_id)
        if not existing:
            return False

        if self._is_clickhouse_available():
            deleted_case = Case(**existing.to_dict())
            deleted_case.is_deleted = True
            deleted_case.updated_at = self._now_iso()
            deleted_case.source = f"{existing.source or 'manual'}:deleted"
            self._upsert_case_to_clickhouse(deleted_case)

        self._remove_case_from_indexes(existing)
        self._cases.pop(case_id, None)
        if not self._is_clickhouse_available():
            self._persist_cases()
        return True

    def mark_case_resolved(self, case_id: str, resolution: str = "") -> Optional[Case]:
        """标记案例为已解决。"""
        existing = self.get_case(case_id)
        if not existing:
            return None

        updated = Case(**existing.to_dict())
        updated.resolved = True
        updated.resolution = resolution or existing.resolution or "已手动标记为已解决"
        updated.resolved_at = self._now_iso()
        updated.updated_at = self._now_iso()
        updated.source = existing.source or "manual"

        self.add_case(updated, persist=True, sync_clickhouse=True)
        return updated


class SimilarCaseRecommender:
    """相似案例推荐器"""

    def __init__(self, case_store: CaseStore):
        self.case_store = case_store

    def find_similar_cases(
        self,
        log_content: str,
        service_name: str = "",
        problem_type: str = "",
        context: Optional[Dict[str, Any]] = None,
        limit: int = 5,
        min_similarity: float = 0.3,
        include_draft: bool = False,
    ) -> List[SimilarCaseResult]:
        """查找相似案例"""
        query_features = FeatureExtractor.extract_features(log_content, service_name, context=context)

        if problem_type:
            query_features['problem_types'] = [problem_type] + query_features.get('problem_types', [])

        candidates = []

        if problem_type:
            candidates.extend(self.case_store.get_cases_by_type(problem_type))

        if service_name:
            candidates.extend(self.case_store.get_cases_by_service(service_name))

        for context_service in query_features.get('context_services', []):
            candidates.extend(self.case_store.get_cases_by_service(context_service))

        if not candidates:
            candidates = self.case_store.get_all_cases()

        seen_ids = set()
        unique_candidates = []
        for case in candidates:
            if case.id not in seen_ids:
                seen_ids.add(case.id)
                unique_candidates.append(case)

        results = []
        for case in unique_candidates:
            case_status = self.case_store.get_case_status(case)
            if not include_draft and case_status == "draft":
                continue
            case_features = case.similarity_features or FeatureExtractor.extract_features(
                case.log_content, case.service_name
            )

            similarity, matched = FeatureExtractor.compute_similarity(query_features, case_features)

            if similarity >= min_similarity:
                relevance_reason = self._generate_relevance_reason(case, matched, similarity)

                results.append(SimilarCaseResult(
                    case=case,
                    similarity_score=similarity,
                    matched_features=matched,
                    relevance_reason=relevance_reason
                ))

        results.sort(key=lambda x: x.similarity_score, reverse=True)

        return results[:limit]

    def _generate_relevance_reason(
        self,
        case: Case,
        matched_features: List[str],
        similarity: float
    ) -> str:
        """生成相关性原因描述"""
        reasons = []

        if 'problem_type' in matched_features:
            reasons.append(f"相同问题类型: {case.problem_type}")

        if 'service' in matched_features:
            reasons.append(f"相同服务: {case.service_name}")

        if any('keywords' in f for f in matched_features):
            reasons.append("包含相同关键词")

        if 'error_pattern' in matched_features:
            reasons.append("匹配相同错误模式")

        if 'call_edge' in matched_features:
            reasons.append("匹配相同上下游调用链路")

        if 'namespace' in matched_features:
            reasons.append("同命名空间上下文")

        if any('context_services' in f for f in matched_features):
            reasons.append("关联服务上下文相似")

        if reasons:
            return "，".join(reasons)
        else:
            return f"相似度: {similarity:.0%}"


_case_store: Optional[CaseStore] = None
_recommender: Optional[SimilarCaseRecommender] = None


# ──────────────────────────────────────────────────────────────────────────────
# Phase 5 additions: RemediationStep, RemediationPlan, FaultMatcher
# mark_case_verified() extension on CaseStore
# ──────────────────────────────────────────────────────────────────────────────

import hashlib as _hashlib


@dataclass
class RemediationStep:
    """
    One ordered step in a remediation plan.

    ``action`` is a short human-readable command or instruction.
    ``verification`` describes how to confirm the step succeeded.
    ``rollback``    describes how to undo the step if it made things worse.
    ``risk_level``  is one of "low" | "medium" | "high".
    ``auto_fixable`` marks whether this step can be run autonomously after
                      explicit human authorization.
    """
    action: str
    verification: str = ""
    rollback: str = ""
    risk_level: str = "low"
    auto_fixable: bool = False
    estimated_duration_s: int = 0
    requires_service_restart: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "verification": self.verification,
            "rollback": self.rollback,
            "risk_level": self.risk_level,
            "auto_fixable": self.auto_fixable,
            "estimated_duration_s": self.estimated_duration_s,
            "requires_service_restart": self.requires_service_restart,
        }


@dataclass
class RemediationPlan:
    """
    Structured fix plan produced after a successful diagnostic run.

    Stored in the knowledge base as part of a ``Case`` so that future
    similar faults can be matched and (with human authorization) auto-fixed.

    ``fault_fingerprint`` is a SHA-256 of
    (sorted(components) + error_category + sorted(keywords) + service)
    used for fast similarity matching.

    ``auto_fix_authorized`` is False by default; set to True via the
    POST /api/ai/remediation/{case_id}/authorize-auto-fix endpoint after
    a human has verified the plan is correct.
    """
    plan_id: str
    case_id: str
    run_id: str
    service: str
    error_category: str
    components: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    steps: List[RemediationStep] = field(default_factory=list)
    overall_risk: str = "low"
    estimated_total_duration_s: int = 0
    verification_summary: str = ""
    rollback_summary: str = ""
    # Meta
    fault_fingerprint: str = ""
    created_at: str = ""
    human_verified: bool = False
    verified_at: str = ""
    verified_by: str = ""
    verification_notes: str = ""
    auto_fix_authorized: bool = False
    authorized_by: str = ""
    authorized_at: str = ""
    execution_count: int = 0
    last_executed_at: str = ""

    def __post_init__(self) -> None:
        if not self.fault_fingerprint:
            self.fault_fingerprint = self._compute_fingerprint()
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if not self.plan_id:
            self.plan_id = f"rp-{uuid.uuid4().hex[:16]}"

    def _compute_fingerprint(self) -> str:
        """
        Compute a stable fault fingerprint for similarity matching.

        SHA-256 of canonical JSON of
        (sorted_components, error_category, sorted_keywords, service).
        """
        payload = {
            "components": sorted(c.lower() for c in self.components),
            "error_category": self.error_category.lower(),
            "keywords": sorted(k.lower() for k in self.keywords),
            "service": self.service.lower(),
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return _hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "case_id": self.case_id,
            "run_id": self.run_id,
            "service": self.service,
            "error_category": self.error_category,
            "components": list(self.components),
            "keywords": list(self.keywords),
            "steps": [s.to_dict() for s in self.steps],
            "overall_risk": self.overall_risk,
            "estimated_total_duration_s": self.estimated_total_duration_s,
            "verification_summary": self.verification_summary,
            "rollback_summary": self.rollback_summary,
            "fault_fingerprint": self.fault_fingerprint,
            "created_at": self.created_at,
            "human_verified": self.human_verified,
            "verified_at": self.verified_at,
            "verified_by": self.verified_by,
            "verification_notes": self.verification_notes,
            "auto_fix_authorized": self.auto_fix_authorized,
            "authorized_by": self.authorized_by,
            "authorized_at": self.authorized_at,
            "execution_count": self.execution_count,
            "last_executed_at": self.last_executed_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RemediationPlan":
        steps_raw = data.get("steps") or []
        steps = [
            RemediationStep(**{
                k: v for k, v in s.items()
                if k in RemediationStep.__dataclass_fields__
            })
            if isinstance(s, dict) else RemediationStep(action=str(s))
            for s in steps_raw
        ]
        return cls(
            plan_id=str(data.get("plan_id") or ""),
            case_id=str(data.get("case_id") or ""),
            run_id=str(data.get("run_id") or ""),
            service=str(data.get("service") or ""),
            error_category=str(data.get("error_category") or ""),
            components=list(data.get("components") or []),
            keywords=list(data.get("keywords") or []),
            steps=steps,
            overall_risk=str(data.get("overall_risk") or "low"),
            estimated_total_duration_s=int(data.get("estimated_total_duration_s") or 0),
            verification_summary=str(data.get("verification_summary") or ""),
            rollback_summary=str(data.get("rollback_summary") or ""),
            fault_fingerprint=str(data.get("fault_fingerprint") or ""),
            created_at=str(data.get("created_at") or ""),
            human_verified=bool(data.get("human_verified")),
            verified_at=str(data.get("verified_at") or ""),
            verified_by=str(data.get("verified_by") or ""),
            verification_notes=str(data.get("verification_notes") or ""),
            auto_fix_authorized=bool(data.get("auto_fix_authorized")),
            authorized_by=str(data.get("authorized_by") or ""),
            authorized_at=str(data.get("authorized_at") or ""),
            execution_count=int(data.get("execution_count") or 0),
            last_executed_at=str(data.get("last_executed_at") or ""),
        )


def mark_case_verified(
    store: "CaseStore",
    case_id: str,
    *,
    verified_by: str = "human",
    verification_notes: str = "",
    verification_result: str = "verified_correct",
) -> Optional["Case"]:
    """
    Mark a case's remediation plan as human-verified.

    Updates ``Case.verification_result``, ``Case.verification_notes``,
    and ``Case.knowledge_version`` (incremented).  The updated case is
    persisted back to the store.

    Returns the updated Case, or None if not found.
    """
    existing = store.get_case(case_id)
    if not existing:
        logger.warning("mark_case_verified: case %r not found", case_id)
        return None

    updated = Case(**existing.to_dict())
    updated.verification_result = verification_result
    updated.verification_notes = verification_notes
    updated.last_editor = verified_by
    updated.knowledge_version = (existing.knowledge_version or 1) + 1
    updated.updated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # Persist using the standard add_case path (upserts via ReplacingMergeTree)
    store.add_case(updated, persist=True, sync_clickhouse=True)

    logger.info(
        "mark_case_verified: case %r marked as %r (version=%d) by %r",
        case_id,
        verification_result,
        updated.knowledge_version,
        verified_by,
    )
    return updated


class FaultMatcher:
    """
    Match an incoming fault against stored remediation plans to find
    a previously verified, human-authorized auto-fix.

    Uses fault_fingerprint for exact matching, then falls back to
    feature-based similarity scoring.
    """

    # Minimum similarity score to suggest a plan (even without exact fingerprint)
    _MIN_SIMILARITY = 0.65

    def __init__(self, case_store: "CaseStore"):
        self.case_store = case_store

    def find_authorized_auto_fix(
        self,
        *,
        service: str,
        error_category: str,
        components: Optional[List[str]] = None,
        keywords: Optional[List[str]] = None,
        log_content: str = "",
    ) -> Optional[RemediationPlan]:
        """
        Search for a verified, auto-fix-authorized remediation plan that
        matches the current fault.

        Matching priority:
          1. Exact fault_fingerprint match on an authorized plan
          2. Feature similarity ≥ _MIN_SIMILARITY on an authorized plan

        Returns None if no authorized plan is found.
        """
        components = list(components or [])
        keywords = list(keywords or [])

        # Build the fingerprint for the incoming fault
        payload = {
            "components": sorted(c.lower() for c in components),
            "error_category": error_category.lower(),
            "keywords": sorted(k.lower() for k in keywords),
            "service": service.lower(),
        }
        target_fingerprint = _hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

        best_plan: Optional[RemediationPlan] = None
        best_score: float = 0.0

        all_cases = list(self.case_store._cases.values())
        for case in all_cases:
            if case.is_deleted:
                continue

            # Extract remediation plan from case context
            plan_dict = (case.context or {}).get("remediation_plan")
            if not isinstance(plan_dict, dict):
                continue

            plan = RemediationPlan.from_dict(plan_dict)

            # Must be human-verified AND auto-fix-authorized
            if not plan.human_verified or not plan.auto_fix_authorized:
                continue

            # Exact fingerprint match — best possible
            if plan.fault_fingerprint == target_fingerprint:
                logger.info(
                    "FaultMatcher: exact fingerprint match for case %r plan %r",
                    case.id,
                    plan.plan_id,
                )
                return plan

            # Feature similarity scoring
            score = self._compute_plan_similarity(
                plan=plan,
                service=service,
                error_category=error_category,
                components=components,
                keywords=keywords,
                log_content=log_content,
                case=case,
            )
            if score >= self._MIN_SIMILARITY and score > best_score:
                best_score = score
                best_plan = plan

        if best_plan:
            logger.info(
                "FaultMatcher: similarity match (score=%.2f) for plan %r",
                best_score,
                best_plan.plan_id,
            )

        return best_plan

    def _compute_plan_similarity(
        self,
        *,
        plan: RemediationPlan,
        service: str,
        error_category: str,
        components: List[str],
        keywords: List[str],
        log_content: str,
        case: "Case",
    ) -> float:
        """Compute a similarity score [0,1] between the fault and a stored plan."""
        score = 0.0

        # Service match
        if service and plan.service and service.lower() == plan.service.lower():
            score += 0.30

        # Error category match
        if error_category and plan.error_category:
            if error_category.lower() == plan.error_category.lower():
                score += 0.25

        # Component overlap
        if components and plan.components:
            overlap = set(c.lower() for c in components) & set(
                c.lower() for c in plan.components
            )
            score += min(len(overlap) * 0.08, 0.24)

        # Keyword overlap
        if keywords and plan.keywords:
            overlap = set(k.lower() for k in keywords) & set(
                k.lower() for k in plan.keywords
            )
            score += min(len(overlap) * 0.05, 0.15)

        # Log content feature similarity (use existing FeatureExtractor)
        if log_content and case.log_content:
            features_target = FeatureExtractor.extract_features(log_content, service)
            features_case = FeatureExtractor.extract_features(case.log_content, case.service_name)
            feat_score, _ = FeatureExtractor.compute_similarity(features_target, features_case)
            score += feat_score * 0.10

        return min(score, 1.0)


def get_case_store(storage_adapter=None) -> CaseStore:
    """获取案例存储实例"""
    global _case_store
    if _case_store is None:
        _case_store = CaseStore(storage_adapter=storage_adapter)
        if _case_store._is_clickhouse_available():
            logger.info("AI case store initialized with ClickHouse backend")
        else:
            _initialize_default_cases(_case_store)
            loaded = _case_store.load_persisted_cases()
            if loaded:
                logger.info(f"Loaded {loaded} persisted cases from local store")
    elif storage_adapter is not None and not _case_store.storage:
        _case_store.attach_storage(storage_adapter)
    return _case_store


def get_recommender(storage_adapter=None) -> SimilarCaseRecommender:
    """获取推荐器实例"""
    global _recommender
    if _recommender is None:
        _recommender = SimilarCaseRecommender(get_case_store(storage_adapter))
    return _recommender


def _initialize_default_cases(store: CaseStore):
    """初始化默认案例库"""
    default_cases = [
        Case(
            id="case-001",
            problem_type="database",
            severity="high",
            summary="数据库连接池耗尽导致服务不可用",
            log_content="ERROR: Connection pool exhausted. Unable to acquire connection from pool. Active connections: 100, Max connections: 100",
            service_name="order-service",
            root_causes=[
                "连接池配置过小",
                "存在连接泄漏",
                "请求量突增"
            ],
            solutions=[
                {"title": "增加连接池大小", "steps": ["检查当前配置", "调整 maxPoolSize 参数", "重启服务"]},
                {"title": "排查连接泄漏", "steps": ["检查代码中未关闭的连接", "添加连接池监控"]}
            ],
            created_at="2026-01-15T10:00:00Z",
            resolved=True,
            resolution="增加连接池大小到 200，修复连接泄漏问题",
            tags=["database", "connection-pool", "mysql"]
        ),
        Case(
            id="case-002",
            problem_type="network",
            severity="critical",
            summary="服务间调用超时导致级联故障",
            log_content="ERROR: Upstream request timeout. Service: payment-service, Timeout: 30s, Retries: 3",
            service_name="api-gateway",
            root_causes=[
                "下游服务响应慢",
                "网络延迟高",
                "超时配置不合理"
            ],
            solutions=[
                {"title": "优化超时配置", "steps": ["分析服务依赖", "调整超时时间", "添加熔断器"]},
                {"title": "排查下游服务", "steps": ["检查下游服务日志", "分析性能瓶颈"]}
            ],
            created_at="2026-01-18T14:30:00Z",
            resolved=True,
            resolution="添加熔断器，优化超时配置",
            tags=["network", "timeout", "circuit-breaker"]
        ),
        Case(
            id="case-003",
            problem_type="memory",
            severity="critical",
            summary="内存溢出导致服务崩溃",
            log_content="FATAL: java.lang.OutOfMemoryError: Java heap space. Heap size: 4GB, Used: 3.95GB",
            service_name="data-processor",
            root_causes=[
                "内存泄漏",
                "堆内存配置过小",
                "数据处理量过大"
            ],
            solutions=[
                {"title": "增加堆内存", "steps": ["调整 -Xmx 参数", "重启服务"]},
                {"title": "排查内存泄漏", "steps": ["生成 heap dump", "使用 MAT 分析", "修复泄漏代码"]}
            ],
            created_at="2026-01-20T09:15:00Z",
            resolved=True,
            resolution="修复内存泄漏，增加堆内存到 8GB",
            tags=["memory", "oom", "jvm"]
        ),
        Case(
            id="case-004",
            problem_type="auth",
            severity="high",
            summary="认证服务 Token 验证失败",
            log_content="ERROR: JWT token validation failed. Token expired at 2026-01-22T10:00:00Z, Current time: 2026-01-22T12:00:00Z",
            service_name="auth-service",
            root_causes=[
                "Token 过期",
                "系统时间不同步",
                "Token 刷新机制异常"
            ],
            solutions=[
                {"title": "刷新 Token", "steps": ["调用刷新接口", "更新本地存储"]},
                {"title": "检查时间同步", "steps": ["检查 NTP 配置", "同步系统时间"]}
            ],
            created_at="2026-01-22T12:00:00Z",
            resolved=True,
            resolution="修复 Token 刷新逻辑，配置 NTP 时间同步",
            tags=["auth", "jwt", "token"]
        ),
        Case(
            id="case-005",
            problem_type="performance",
            severity="medium",
            summary="数据库慢查询影响服务性能",
            log_content="WARN: Slow query detected. Query time: 5.2s, Query: SELECT * FROM orders WHERE user_id = ?",
            service_name="order-service",
            root_causes=[
                "缺少索引",
                "查询返回数据量过大",
                "数据库负载高"
            ],
            solutions=[
                {"title": "添加索引", "steps": ["分析查询计划", "添加 user_id 索引", "验证效果"]},
                {"title": "优化查询", "steps": ["添加分页", "只查询必要字段", "使用缓存"]}
            ],
            created_at="2026-01-25T16:00:00Z",
            resolved=True,
            resolution="添加索引，查询时间降低到 50ms",
            tags=["performance", "slow-query", "database"]
        ),
    ]

    for case in default_cases:
        case.similarity_features = FeatureExtractor.extract_features(case.log_content, case.service_name)
        store.add_case(case, persist=False)

    logger.info(f"Initialized {len(default_cases)} default cases")
