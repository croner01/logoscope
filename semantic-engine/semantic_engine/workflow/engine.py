"""WorkflowEngine — 从 logs.logs 重建 OpenStack Workflow Execution。

核心设计：

    Workflow Execution = 一次 OpenStack 操作（如 CreateVM、LiveMigrate）
    的完整执行记录，包含参与的所有服务、每个服务的耗时、状态。

    数据来源：logs.logs 中 openstack_global_request_id 不为空的行。
    按 global_request_id 分组 → 时间排序 → 连续相同服务压缩 → 形成步骤序列。
    从首个 HTTP 请求推断操作类型，从日志级别判断成功/失败。

Phase 1: 批量扫描模式（周期性运行），后续可扩展为事件驱动。
"""

import hashlib
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── 表定义 ──────────────────────────────────────────────────────────────────

_TABLE_NAME = "logs.workflow_executions"

_CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {_TABLE_NAME} (
    execution_id String,
    operation_type String,
    resource_id String DEFAULT '',
    global_request_id String,
    status String,
    started_at DateTime64(3, 'UTC'),
    finished_at DateTime64(3, 'UTC'),
    duration_ms UInt64 DEFAULT 0,
    error_message String DEFAULT '',
    source_cluster String DEFAULT '',
    step_count UInt16 DEFAULT 0,
    steps Nested(
        service_name String,
        action String,
        started_at DateTime64(3, 'UTC'),
        duration_ms UInt64,
        status String,
        level String
    )
) ENGINE = ReplacingMergeTree()
PARTITION BY toDate(started_at)
ORDER BY (operation_type, started_at, execution_id)
TTL toDateTime(started_at) + INTERVAL 90 DAY DELETE
SETTINGS index_granularity = 8192
"""

_INSERT_SQL = f"""
INSERT INTO {_TABLE_NAME} (
    execution_id, operation_type, resource_id,
    global_request_id, status,
    started_at, finished_at, duration_ms,
    error_message, source_cluster, step_count,
    steps.service_name, steps.action,
    steps.started_at, steps.duration_ms,
    steps.status, steps.level
) VALUES
"""

_EXISTS_SQL = f"""
SELECT count() AS cnt
FROM {_TABLE_NAME}
WHERE execution_id = {{execution_id}}
FINAL
"""

# ── 操作类型检测模式 ─────────────────────────────────────────────────────────

# 正则: 从日志 message 中提取 HTTP 方法和路径
_HTTP_RE = re.compile(r'"((?:POST|GET|PUT|DELETE))\s+(\S+)\s+HTTP/\d\.\d"')

# 正则: 路径中的 UUID 格式
_UUID32_RE = re.compile(r'[a-f0-9]{32}')
_UUID36_RE = re.compile(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}')

# 正则: 日志正文中的 instance / volume UUID
_INSTANCE_RE = re.compile(r'\[instance:\s*([a-f0-9-]{8,36})\]', re.IGNORECASE)
_VOLUME_RE = re.compile(r'\[volume:\s*([a-f0-9-]{8,36})\]', re.IGNORECASE)

# HTTP 请求路径中的 UUID 提取（32 位 hex 或 36 位带连字符）
_PATH_UUID_RE = re.compile(r'/([a-f0-9]{32}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})')


def _path_normalize(path: str) -> str:
    """将 /v2.1/{uuid}/xxx 中的 UUID 替换为占位符用于模式匹配。"""
    return _PATH_UUID_RE.sub('/{id}', path).rstrip('/')


def _execution_id(global_request_id: str) -> str:
    """从 global_request_id 生成确定性 execution_id。"""
    return hashlib.md5(global_request_id.encode("utf-8")).hexdigest()[:16]


def _calc_duration_ms(start: Any, end: Any) -> int:
    """计算两个时间戳之间的毫秒差。"""
    try:
        if isinstance(start, str):
            start = datetime.fromisoformat(start.replace('Z', '+00:00'))
        if isinstance(end, str):
            end = datetime.fromisoformat(end.replace('Z', '+00:00'))
        if isinstance(start, datetime) and isinstance(end, datetime):
            return max(0, int((end - start).total_seconds() * 1000))
    except Exception:
        pass
    return 0


# ── 操作类型检测模式 ─────────────────────────────────────────────────────────

# 正则匹配 OPENSTACK API 请求路径，判断操作类型
# 路径格式: /v2.1/{tenant_id}/{resource} 或 /v3/{tenant_id}/{resource} 或 .../{id}/action
_OPENSTACK_API_PATH = re.compile(r'/v(?:2\.\d+|3)/([^/]+)/(\w+)(?:/([^/\s]+))?(?:/(action))?')

_OPERATION_PATTERNS = [
    # CreateVM: POST /v2.1/{tenant}/servers (servers 是最后一段)
    (lambda m, p: m == "POST" and re.match(r'/v(?:2\.\d+|3)/[^/\s]+/servers$', p), "CreateVM"),
    # DeleteVM: DELETE /v2.1/{tenant}/servers/{id}
    (lambda m, p: m == "DELETE" and re.match(r'/v(?:2\.\d+|3)/[^/\s]+/servers/', p), "DeleteVM"),
    # CreateVolume: POST /v2.1/{tenant}/volumes (volumes 是最后一段)
    (lambda m, p: m == "POST" and re.match(r'/v(?:2\.\d+|3)/[^/\s]+/volumes$', p), "CreateVolume"),
    # DeleteVolume: DELETE /v2.1/{tenant}/volumes/{id}
    (lambda m, p: m == "DELETE" and re.match(r'/v(?:2\.\d+|3)/[^/\s]+/volumes/', p), "DeleteVolume"),
    # ServerAction: POST /v2.1/{tenant}/servers/{id}/action
    (lambda m, p: m == "POST" and '/action' in p and '/servers/' in p, "ServerAction"),
    # VolumeAction: POST /v2.1/{tenant}/volumes/{id}/action
    (lambda m, p: m == "POST" and '/action' in p and '/volumes/' in p, "VolumeAction"),
    # CreateImage: POST /v2.1/{tenant}/images (images 是最后一段)
    (lambda m, p: m == "POST" and re.match(r'/v(?:2\.\d+|3)/[^/\s]+/images$', p), "CreateImage"),
    # CreateSnapshot: POST /v2.1/{tenant}/snapshots
    (lambda m, p: m == "POST" and re.match(r'/v(?:2\.\d+|3)/[^/\s]+/snapshots', p), "CreateSnapshot"),
]

# 对其他操作类型的 message 关键词检测
_ACTION_KEYWORDS = [
    (re.compile(r'\bmigrat\w*\b', re.IGNORECASE), "LiveMigrate"),
    (re.compile(r'\battach\b.*\bvolume\b|\bvolume\b.*\battach\b', re.IGNORECASE), "AttachVolume"),
    (re.compile(r'\bdetach\b.*\bvolume\b|\bvolume\b.*\bdetach\b', re.IGNORECASE), "DetachVolume"),
    (re.compile(r'\brebuild\b', re.IGNORECASE), "RebuildServer"),
    (re.compile(r'\bresize\b', re.IGNORECASE), "ResizeServer"),
    (re.compile(r'\bsnapshot\b', re.IGNORECASE), "CreateSnapshot"),
    (re.compile(r'\bbackup\b', re.IGNORECASE), "CreateBackup"),
]


class WorkflowEngine:
    """
    WorkflowEngine — 从 logs.logs 重建 OpenStack Workflow Execution。

    使用方法:
        engine = WorkflowEngine(storage)
        result = engine.build_workflows(since_hours=6)
        # → {"built": 12, "skipped": 3, "errors": 0}
    """

    def __init__(self, storage):
        self.storage = storage
        self.ch_client = storage.ch_client if storage and hasattr(storage, 'ch_client') else None
        self._ch_available = False
        self._ensure_table()

    # ── public API ──────────────────────────────────────────────────────────

    def build_workflows(self, since_hours: int = 6) -> Dict[str, Any]:
        """
        扫描最近 since_hours 小时内有 global_request_id 的日志，重建 Workflow。

        Args:
            since_hours: 回溯小时数

        Returns:
            {"built": int, "skipped": int, "errors": int,
             "scanned_requests": int, "groups_total": int}
        """
        if not self._ch_available:
            logger.warning("ClickHouse not available, skipping workflow build")
            return {"built": 0, "skipped": 0, "errors": 0, "scanned_requests": 0, "groups_total": 0}

        safe_hours = max(1, min(168, int(since_hours)))  # 1h ~ 7d
        logger.info("Building workflows from last %d hours of logs...", safe_hours)

        # Step 1: 从 logs.logs 查询有 global_request_id 的行
        rows = self._query_log_rows(safe_hours)
        if not rows:
            logger.info("No log entries with global_request_id found in the last %d hours", safe_hours)
            return {"built": 0, "skipped": 0, "errors": 0, "scanned_requests": 0, "groups_total": 0}

        # Step 2: 按 global_request_id 分组
        groups = self._group_by_global_request_id(rows)
        logger.debug("Found %d unique global_request_id groups from %d log rows", len(groups), len(rows))

        # Step 3: 逐组重建 Workflow
        result = {"built": 0, "skipped": 0, "errors": 0, "scanned_requests": len(rows), "groups_total": len(groups)}
        for rid, records in groups.items():
            if len(records) < 2:
                result["skipped"] += 1
                continue

            # 检查是否已经存在（execution_id 确定性，支持重入）
            eid = _execution_id(rid)
            if self._workflow_exists(eid):
                result["skipped"] += 1
                continue

            try:
                workflow = self._reconstruct_workflow(rid, eid, records)
                if workflow is None:
                    result["skipped"] += 1
                    continue
                self._save_workflow(workflow)
                result["built"] += 1
            except Exception as e:
                logger.error("Error building workflow for global_request_id=%s: %s", rid[:20], e)
                result["errors"] += 1

        logger.info(
            "Workflow build complete: %d built, %d skipped, %d errors (from %d groups, %d rows)",
            result["built"], result["skipped"], result["errors"],
            result["groups_total"], result["scanned_requests"],
        )
        return result

    # ── 数据查询 ────────────────────────────────────────────────────────────

    def _query_log_rows(self, since_hours: int) -> List[Dict]:
        """从 logs.logs 查询有 openstack_global_request_id 的行。"""
        query = f"""
        SELECT
            service_name,
            openstack_request_id,
            openstack_global_request_id,
            timestamp,
            level,
            message,
            source_cluster
        FROM logs.logs
        WHERE openstack_global_request_id != ''
          AND timestamp > now() - INTERVAL {since_hours} HOUR
        ORDER BY timestamp
        LIMIT 300000
        """
        return self.storage.execute_query(query) or []

    # ── 分组 ────────────────────────────────────────────────────────────────

    @staticmethod
    def _group_by_global_request_id(rows: List[Dict]) -> Dict[str, List[Dict]]:
        """按 openstack_global_request_id 分组。"""
        groups: Dict[str, List[Dict]] = defaultdict(list)
        for row in rows:
            if isinstance(row, dict):
                rid = str(row.get("openstack_global_request_id", "") or "").strip()
            elif isinstance(row, (list, tuple)) and len(row) >= 3:
                rid = str(row[2] or "").strip()  # openstack_global_request_id at index 2
                row = {
                    "service_name": str(row[0] or "").strip(),
                    "openstack_request_id": str(row[1] or "").strip(),
                    "openstack_global_request_id": rid,
                    "timestamp": row[3] if len(row) > 3 else None,
                    "level": str(row[4] or "").strip() if len(row) > 4 else "",
                    "message": str(row[5] or "").strip() if len(row) > 5 else "",
                    "source_cluster": str(row[6] or "").strip() if len(row) > 6 else "",
                }
            else:
                continue
            if not rid:
                continue
            groups[rid].append(row)
        return groups

    # ── Workflow 重建 ───────────────────────────────────────────────────────

    def _reconstruct_workflow(
        self,
        global_request_id: str,
        execution_id: str,
        records: List[Dict],
    ) -> Optional[Dict]:
        """从一组日志记录重建一个 Workflow Execution。"""
        # 按时间排序
        records.sort(key=lambda r: self._ts_key(r))

        # 连续相同服务压缩
        sequence = self._dedup_service_sequence(records)
        if len(sequence) < 2:
            return None

        # 检测操作类型和资源 ID
        operation_type = self._detect_operation_type(sequence)
        resource_id = self._detect_resource_id(sequence)

        # 检测状态
        status, error_msg = self._detect_status(sequence)

        # 构建步骤
        steps = self._build_steps(sequence)

        # 时间范围
        started_at = self._ts_value(sequence[0].get("timestamp"))
        finished_at = self._ts_value(sequence[-1].get("timestamp"))
        duration = _calc_duration_ms(started_at, finished_at)

        return {
            "execution_id": execution_id,
            "operation_type": operation_type or "Unknown",
            "resource_id": resource_id or "",
            "global_request_id": global_request_id,
            "status": status,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ms": duration,
            "error_message": error_msg,
            "source_cluster": self._detect_source_cluster(sequence),
            "step_count": len(steps),
            "steps.service_name": [s["service_name"] for s in steps],
            "steps.action": [s["action"] for s in steps],
            "steps.started_at": [s["started_at"] for s in steps],
            "steps.duration_ms": [s["duration_ms"] for s in steps],
            "steps.status": [s["status"] for s in steps],
            "steps.level": [s["level"] for s in steps],
        }

    # ── 工具方法 ────────────────────────────────────────────────────────────

    @staticmethod
    def _ts_key(row: Dict) -> str:
        """从行中提取可排序的时间戳 key。"""
        ts = row.get("timestamp")
        if ts is None:
            return ""
        if isinstance(ts, datetime):
            return ts.isoformat()
        return str(ts)

    @staticmethod
    def _ts_value(ts: Any) -> Any:
        """归一化时间戳为 datetime 或字符串。"""
        if isinstance(ts, datetime):
            return ts
        if isinstance(ts, str):
            try:
                return datetime.fromisoformat(ts.replace('Z', '+00:00'))
            except Exception:
                return ts
        return ts

    @staticmethod
    def _get_field(row: Dict, key: str, default: str = "") -> str:
        """安全地从 dict 或 tuple-like 行中取值。"""
        if isinstance(row, dict):
            return str(row.get(key, default) or default)
        return default

    @staticmethod
    def _dedup_service_sequence(
        records: List[Dict], key: str = "service_name",
    ) -> List[Dict]:
        """合并连续相同服务名的条目。"""
        if not records:
            return []
        sequence = [dict(records[0])]
        for record in records[1:]:
            if record.get(key) != sequence[-1].get(key):
                sequence.append(dict(record))
            else:
                # 合并同服务的多条日志：保留最早时间，更新最新时间，合并级别
                existing = sequence[-1]
                existing["_last_timestamp"] = record.get("timestamp")
                # 记录最高严重级别
                lvl = str(record.get("level", "") or "").upper()
                existing_lvl = str(existing.get("level", "") or "").upper()
                if _level_severity(lvl) > _level_severity(existing_lvl):
                    existing["level"] = record.get("level", "")
                # 累积 message（上限 100000 字符，确保 eventlet 格式的 HTTP 行不被截断）
                msg = str(record.get("message", "") or "")
                existing_msg = str(existing.get("message", "") or "")
                if msg and msg not in existing_msg:
                    existing["message"] = existing_msg + "\n" + msg if existing_msg and len(existing_msg) < 100000 else existing_msg
        return sequence

    # ── 操作类型检测 ────────────────────────────────────────────────────────

    @staticmethod
    def _detect_operation_type(sequence: List[Dict]) -> str:
        """从步骤序列中推断操作类型。

        策略：遍历步骤的所有 HTTP 请求，优先匹配已知操作模式。
        使用 finditer 确保同一 record（含合并的多条日志）中的所有 HTTP 请求都被检查。
        """
        for record in sequence:
            message = str(record.get("message", "") or "")
            for http_match in _HTTP_RE.finditer(message):
                method = http_match.group(1)
                path = http_match.group(2)

                # 按模式匹配
                for pattern_fn, op_type in _OPERATION_PATTERNS:
                    if pattern_fn(method, path):
                        # 如果是 ServerAction，进一步从 message 关键词区分
                        if op_type == "ServerAction":
                            sub_type = _detect_action_from_keywords(message)
                            if sub_type:
                                return sub_type
                        return op_type

        # Fallback: 从 message 关键词检测
        for record in sequence:
            message = str(record.get("message", "") or "")
            sub_type = _detect_action_from_keywords(message)
            if sub_type:
                return sub_type

        return "Unknown"

    @staticmethod
    def _detect_resource_id(sequence: List[Dict]) -> str:
        """从步骤序列中提取操作目标资源 ID。

        策略：
        1. 从 HTTP 路径中提取 UUID（instance / volume）
        2. 从日志消息的 [instance: uuid] / [volume: uuid] 段中提取
        使用 finditer 确保合并日志中的所有 HTTP 路径都被检查。
        """
        for record in sequence:
            message = str(record.get("message", "") or "")
            for http_match in _HTTP_RE.finditer(message):
                path = http_match.group(2)
                # 从路径中提取 UUID
                uuids = _UUID36_RE.findall(path) or _UUID32_RE.findall(path)
                if uuids:
                    # 第一个 UUID 是 tenant ID，第二个如果是 /servers/{uuid} 或 /volumes/{uuid} 才是资源
                    path_parts = path.strip("/").split("/")
                    for i, part in enumerate(path_parts):
                        if part in ("servers", "volumes", "images", "snapshots"):
                            if i + 1 < len(path_parts):
                                candidate = path_parts[i + 1]
                                if _UUID36_RE.match(candidate) or _UUID32_RE.match(candidate):
                                    return candidate

            # 从 [instance: uuid] 段提取
            instance_match = _INSTANCE_RE.search(message)
            if instance_match:
                return instance_match.group(1)
            # 从 [volume: uuid] 段提取
            volume_match = _VOLUME_RE.search(message)
            if volume_match:
                return volume_match.group(1)

        return ""

    # ── 状态检测 ────────────────────────────────────────────────────────────

    @staticmethod
    def _detect_status(sequence: List[Dict]) -> Tuple[str, str]:
        """判断 Workflow 整体状态。

        - 如果有任何 ERROR 或 FATAL 级别 → "failed"
        - 否则 → "success"
        """
        first_error = ""
        has_warning = False
        for record in sequence:
            lvl = str(record.get("level", "") or "").upper()
            if lvl in ("ERROR", "FATAL", "CRITICAL"):
                msg = str(record.get("message", "") or "")
                if not first_error:
                    first_error = msg[:300]
            elif lvl == "WARN":
                has_warning = True

        if first_error:
            return "failed", first_error
        if has_warning:
            return "success_with_warnings", ""
        return "success", ""

    # ── 步骤构建 ────────────────────────────────────────────────────────────

    def _build_steps(self, sequence: List[Dict]) -> List[Dict]:
        """为序列中的每个服务构建步骤数据。

        每个步骤包含：
        - service_name: 服务名
        - action: 首个 HTTP 方法或消息中的首个动作词
        - started_at: 步骤开始时间（该服务首次出现）
        - duration_ms: 步骤持续到下一不同服务出现（最后一步到结束）
        - status: 本步骤的级别
        - level: 最高日志级别
        """
        steps = []
        for i, record in enumerate(sequence):
            svc = str(record.get("service_name", "") or "")

            # action: HTTP 方法，或 message 的第一个词
            message = str(record.get("message", "") or "")
            action = self._extract_action(message)

            started_at = self._ts_value(record.get("timestamp"))

            # duration: 到下一个不同服务的时间
            if i + 1 < len(sequence):
                next_ts = self._ts_value(sequence[i + 1].get("timestamp"))
                duration = _calc_duration_ms(started_at, next_ts)
            elif "_last_timestamp" in record:
                last_ts = self._ts_value(record["_last_timestamp"])
                duration = _calc_duration_ms(started_at, last_ts)
            else:
                duration = 0

            # status: 本步骤级别
            lvl = str(record.get("level", "") or "").upper()
            if lvl in ("ERROR", "FATAL", "CRITICAL"):
                step_status = "failed"
            elif lvl == "WARN":
                step_status = "warning"
            else:
                step_status = "success"

            steps.append({
                "service_name": svc,
                "action": action,
                "started_at": started_at,
                "duration_ms": duration,
                "status": step_status,
                "level": lvl or "INFO",
            })

        return steps

    @staticmethod
    def _extract_action(message: str) -> str:
        """从日志消息中提取动作词。"""
        # 遍历所有 HTTP 匹配，返回第一个非平凡路径的动作
        for http_match in _HTTP_RE.finditer(message):
            method = http_match.group(1)
            path = http_match.group(2)
            # 从路径最后一段提取动作
            path_parts = path.strip("/").split("/")
            last_part = path_parts[-1] if path_parts else ""
            if last_part in ("action", ""):
                return f"HTTP {method}"
            # 跳过只含 UUID 的路径段（tenant ID），找资源路径段
            for p in reversed(path_parts):
                if p not in ("v2.1", "v2", "v1") and not re.match(r'^[a-f0-9]{32}$', p) and not re.match(r'^[a-f0-9-]{36}$', p):
                    return f"{method} {p}"
            return f"HTTP {method}"
        # Fallback: 非 HTTP 行的消息首词
        return message.split()[0] if message else ""

    @staticmethod
    def _detect_source_cluster(sequence: List[Dict]) -> str:
        """从序列中提取 source_cluster（取第一个非空值）。"""
        for record in sequence:
            cluster = str(record.get("source_cluster", "") or "")
            if cluster:
                return cluster
        return ""

    # ── 持久化 ──────────────────────────────────────────────────────────────

    def _ensure_table(self) -> None:
        """创建 workflow_executions 表（如不存在）。"""
        if not self.ch_client:
            logger.warning("No ClickHouse client available for WorkflowEngine")
            self._ch_available = False
            return
        try:
            self.ch_client.execute(_CREATE_TABLE_SQL)
            self._ch_available = True
            logger.info("WorkflowEngine: ensured table %s", _TABLE_NAME)
        except Exception as e:
            logger.warning("WorkflowEngine: failed to create table %s: %s", _TABLE_NAME, e)
            self._ch_available = False

    def _workflow_exists(self, execution_id: str) -> bool:
        """检查 execution_id 是否已存在（防止重复）。"""
        try:
            rows = self.storage.execute_query(_EXISTS_SQL, {"execution_id": execution_id})
            if rows:
                first = rows[0]
                if isinstance(first, dict):
                    return int(first.get("cnt") or 0) > 0
                if isinstance(first, (list, tuple)):
                    return int(first[0] or 0) > 0
            return False
        except Exception:
            return False

    def _save_workflow(self, workflow: Dict) -> bool:
        """将 Workflow 写入 ClickHouse。"""
        if not self.ch_client:
            return False
        try:
            self.ch_client.execute(_INSERT_SQL, [workflow])
            logger.debug("Saved workflow %s: %s %s (%d steps, %s)",
                         workflow["execution_id"][:8],
                         workflow["operation_type"],
                         workflow["resource_id"][:12] if workflow.get("resource_id") else "",
                         workflow["step_count"],
                         workflow["status"])
            return True
        except Exception as e:
            logger.error("Failed to save workflow %s: %s", workflow.get("execution_id", "?")[:8], e)
            return False

    # ── 查询 ────────────────────────────────────────────────────────────────

    def list_workflows(
        self,
        operation_type: Optional[str] = None,
        since_hours: int = 24,
        limit: int = 50,
    ) -> List[Dict]:
        """查询已保存的 Workflow 列表。

        Args:
            operation_type: 过滤操作类型（如 CreateVM），None 为全部
            since_hours: 回溯小时数
            limit: 最大返回条数
        """
        if not self._ch_available:
            return []

        conditions = [f"started_at > now() - INTERVAL {max(1, int(since_hours))} HOUR"]
        if operation_type:
            conditions.append(f"operation_type = '{_escape_sql_literal(operation_type)}'")

        query = f"""
        SELECT
            execution_id, operation_type, resource_id,
            global_request_id, status,
            started_at, finished_at, duration_ms,
            error_message, step_count
        FROM {_TABLE_NAME}
        WHERE {' AND '.join(conditions)}
        ORDER BY started_at DESC
        LIMIT {max(1, min(1000, int(limit)))}
        """
        return self.storage.execute_query(query) or []

    def get_workflow_detail(self, execution_id: str) -> Optional[Dict]:
        """查询单个 Workflow 详情（含步骤）。"""
        if not self._ch_available:
            return None
        query = f"""
        SELECT
            execution_id, operation_type, resource_id,
            global_request_id, status,
            started_at, finished_at, duration_ms,
            error_message, source_cluster, step_count,
            steps.service_name, steps.action,
            steps.started_at, steps.duration_ms,
            steps.status, steps.level
        FROM {_TABLE_NAME}
        WHERE execution_id = {{execution_id}}
        LIMIT 1
        """
        rows = self.storage.execute_query(query, {"execution_id": execution_id})
        return rows[0] if rows else None


# ── 模块级工具函数 ──────────────────────────────────────────────────────────

def _level_severity(lvl: str) -> int:
    """返回日志级别的数值严重程度。"""
    severity = {"TRACE": 0, "DEBUG": 1, "INFO": 2, "WARN": 3, "WARNING": 3,
                "ERROR": 4, "FATAL": 5, "CRITICAL": 5}
    return severity.get(lvl.upper(), 2)


def _detect_action_from_keywords(message: str) -> str:
    """从日志消息中检测具体操作类型（用于 ServerAction 细分）。"""
    for pattern, op_type in _ACTION_KEYWORDS:
        if pattern.search(message):
            return op_type
    return ""


def _escape_sql_literal(value: str) -> str:
    """转义 SQL 字符串字面量。"""
    return value.replace("'", "''")
