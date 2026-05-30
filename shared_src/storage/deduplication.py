"""
数据去重策略和实现

分析并解决数据重复问题

Date: 2026-02-09
"""

import logging
from typing import Dict, Any, List, Optional, Set
from datetime import datetime, timedelta, timezone
from collections import defaultdict
import hashlib
import re

logger = logging.getLogger(__name__)


def _sanitize_interval(time_window: str, default_value: str = "1 HOUR") -> str:
    """规范化 INTERVAL 参数，避免 SQL 注入。"""
    pattern = re.compile(r"^\s*([1-9]\d{0,2})\s+(SECOND|MINUTE|HOUR|DAY|WEEK|MONTH)\s*$", re.IGNORECASE)
    match = pattern.match(str(time_window or ""))
    if not match:
        return default_value
    amount, unit = match.groups()
    return f"{int(amount)} {unit.upper()}"


def _sanitize_limit(value: Any, default_value: int = 1000, max_value: int = 10000) -> int:
    """限制 LIMIT 范围，避免异常值影响查询。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default_value
    if parsed < 1:
        return 1
    return min(parsed, max_value)


def _escape_sql_literal(value: Any) -> str:
    """转义 SQL 字符串字面量中的单引号。"""
    return str(value).replace("'", "''")


class DataDeduplicator:
    """
    数据去重器

    支持多种去重策略：
    1. 基于 event_id 的精确去重
    2. 基于 (service, timestamp, message_hash) 语义去重
    3. 基于时间窗口的去重（同一秒内的重复日志）
    4. 基于 Bloom Filter 的快速去重
    """

    def __init__(self, storage_adapter):
        """
        初始化数据去重器

        Args:
            storage_adapter: StorageAdapter 实例
        """
        self.storage = storage_adapter

        # 去重缓存（内存）
        self._event_id_cache: Set[str] = set()
        self._semantic_key_cache: Set[str] = set()
        self._cache_timestamp = datetime.now(timezone.utc)
        self._cache_ttl = timedelta(minutes=5)  # 缓存 5 分钟

        # 统计信息
        self._stats = {
            "total_processed": 0,
            "duplicates_found": 0,
            "duplicates_by_id": 0,
            "duplicates_by_semantic": 0
        }

    def is_duplicate_event(
        self,
        event: Dict[str, Any],
        check_existing: bool = True
    ) -> tuple[bool, Optional[str]]:
        """
        检查事件是否重复

        Args:
            event: 待检查的事件
            check_existing: 是否检查数据库中已存在的事件

        Returns:
            (is_duplicate, reason): 是否重复及原因
        """
        try:
            self._stats["total_processed"] += 1

            # 策略 1: 基于 event_id 的精确去重
            event_id = event.get("id", "")
            if event_id and self._is_duplicate_by_id(event_id, check_existing):
                self._stats["duplicates_found"] += 1
                self._stats["duplicates_by_id"] += 1
                return True, f"duplicate_id:{event_id[:20]}"

            # 策略 2: 语义去重（service + timestamp + message_hash）
            semantic_key = self._generate_semantic_key(event)
            if semantic_key and self._is_duplicate_by_semantic_key(semantic_key, check_existing):
                self._stats["duplicates_found"] += 1
                self._stats["duplicates_by_semantic"] += 1
                return True, f"duplicate_semantic:{semantic_key[:40]}"

            # 策略 3: 时间窗口去重（同一服务在同一秒内的相同日志）
            if self._is_duplicate_by_time_window(event):
                self._stats["duplicates_found"] += 1
                return True, "duplicate_time_window"

            return False, None

        except Exception as e:
            logger.error(f"Error checking duplicate event: {e}")
            # 出错时不阻止数据写入
            return False, None

    def _is_duplicate_by_id(
        self,
        event_id: str,
        check_existing: bool = True
    ) -> bool:
        """
        基于 event_id 检查是否重复

        Args:
            event_id: 事件 ID
            check_existing: 是否检查数据库

        Returns:
            bool: 是否重复
        """
        if not event_id:
            return False

        # 检查内存缓存
        if event_id in self._event_id_cache:
            return True

        # 检查数据库
        if check_existing and self.storage.ch_client:
            try:
                safe_event_id = _escape_sql_literal(event_id)
                query = f"""
                SELECT count()
                FROM logs.logs
                PREWHERE id = '{safe_event_id}'
                LIMIT 1
                """
                result = self.storage.execute_query(query)
                first_count = 0
                if result:
                    first_row = result[0]
                    if isinstance(first_row, dict):
                        first_count = int(first_row.get("count()", first_row.get("count", 0)) or 0)
                    elif isinstance(first_row, (list, tuple)) and first_row:
                        first_count = int(first_row[0] or 0)
                if first_count > 0:
                    # 添加到缓存
                    self._event_id_cache.add(event_id)
                    return True
            except Exception as e:
                logger.warning(f"Error checking duplicate by id: {e}")

        return False

    def _generate_semantic_key(self, event: Dict[str, Any]) -> Optional[str]:
        """
        生成语义去重键

        基于服务名、时间戳、消息内容的哈希值

        Args:
            event: 事件数据

        Returns:
            str: 语义键，或 None
        """
        try:
            # 提取关键字段
            service_name = event.get("entity", {}).get("name", "")
            timestamp = event.get("timestamp", "")
            message = str(event.get("event", {}).get("raw", ""))

            if not all([service_name, timestamp, message]):
                return None

            # 对消息进行哈希（避免过长的键）
            message_hash = hashlib.md5(message.encode('utf-8')).hexdigest()[:16]

            # 生成语义键：service + timestamp(精确到秒) + message_hash
            # 去除纳秒部分，避免因为时间戳精度差异导致的误判
            timestamp_second = timestamp.split('.')[0] if '.' in timestamp else timestamp

            semantic_key = f"{service_name}|{timestamp_second}|{message_hash}"
            return semantic_key

        except Exception as e:
            logger.warning(f"Error generating semantic key: {e}")
            return None

    def _is_duplicate_by_semantic_key(
        self,
        semantic_key: str,
        check_existing: bool = True
    ) -> bool:
        """
        基于语义键检查是否重复

        Args:
            semantic_key: 语义键
            check_existing: 是否检查数据库

        Returns:
            bool: 是否重复
        """
        if not semantic_key:
            return False

        # 检查内存缓存
        if semantic_key in self._semantic_key_cache:
            return True

        # 检查数据库（基于语义查询）
        if check_existing and self.storage.ch_client:
            try:
                # 解析语义键
                parts = semantic_key.split('|')
                if len(parts) != 3:
                    return False

                service_name, timestamp_second, message_hash = parts

                # 构建时间范围查询（该秒的前后 1 秒）
                timestamp_start = _escape_sql_literal(f"{timestamp_second}.000000")
                timestamp_end = _escape_sql_literal(f"{timestamp_second}.999999")
                safe_service_name = _escape_sql_literal(service_name)
                safe_message_hash = _escape_sql_literal(message_hash)

                query = f"""
                SELECT count()
                FROM logs.logs
                PREWHERE timestamp >= '{timestamp_start}'
                    AND timestamp <= '{timestamp_end}'
                WHERE service_name = '{safe_service_name}'
                  AND substring(MD5(message), 1, 16) = '{safe_message_hash}'
                LIMIT 1
                """
                result = self.storage.execute_query(query)
                first_count = 0
                if result:
                    first_row = result[0]
                    if isinstance(first_row, dict):
                        first_count = int(first_row.get("count()", first_row.get("count", 0)) or 0)
                    elif isinstance(first_row, (list, tuple)) and first_row:
                        first_count = int(first_row[0] or 0)
                if first_count > 0:
                    # 添加到缓存
                    self._semantic_key_cache.add(semantic_key)
                    return True
            except Exception as e:
                logger.warning(f"Error checking duplicate by semantic key: {e}")

        return False

    def _is_duplicate_by_time_window(self, event: Dict[str, Any]) -> bool:
        """
        基于时间窗口检查是否重复

        检查同一服务在同一秒内是否有相同的消息

        Args:
            event: 事件数据

        Returns:
            bool: 是否重复
        """
        try:
            service_name = event.get("entity", {}).get("name", "")
            timestamp = event.get("timestamp", "")
            message = str(event.get("event", {}).get("raw", ""))

            if not all([service_name, timestamp, message]):
                return False

            # 提取秒级时间戳
            timestamp_second = timestamp.split('.')[0] if '.' in timestamp else timestamp

            # 构建查询键
            time_key = f"{service_name}|{timestamp_second}"

            # 检查缓存
            if time_key in self._semantic_key_cache:
                # 进一步检查消息是否相同
                message_hash = hashlib.md5(message.encode('utf-8')).hexdigest()
                cached_key = f"{time_key}|{message_hash}"
                if cached_key in self._semantic_key_cache:
                    return True

            return False

        except Exception as e:
            logger.warning(f"Error checking duplicate by time window: {e}")
            return False

    def clear_cache(self):
        """清空去重缓存"""
        self._event_id_cache.clear()
        self._semantic_key_cache.clear()
        self._cache_timestamp = datetime.now(timezone.utc)
        logger.info("Deduplication cache cleared")

    def get_stats(self) -> Dict[str, Any]:
        """
        获取去重统计信息

        Returns:
            统计信息字典
        """
        stats = self._stats.copy()

        # 计算去重率
        if stats["total_processed"] > 0:
            stats["duplicate_rate"] = stats["duplicates_found"] / stats["total_processed"]
        else:
            stats["duplicate_rate"] = 0.0

        # 缓存大小
        stats["id_cache_size"] = len(self._event_id_cache)
        stats["semantic_cache_size"] = len(self._semantic_key_cache)
        stats["cache_age_seconds"] = (
            datetime.now(timezone.utc) - self._cache_timestamp
        ).total_seconds()

        return stats

    def analyze_duplicate_sources(
        self,
        time_window: str = "1 HOUR",
        limit: int = 1000
    ) -> Dict[str, Any]:
        """
        分析重复数据的来源

        Args:
            time_window: 时间窗口
            limit: 分析的数据量限制

        Returns:
            分析结果
        """
        try:
            if not self.storage.ch_client:
                return {"error": "ClickHouse client not available"}
            safe_time_window = _sanitize_interval(time_window, default_value="1 HOUR")
            safe_limit = _sanitize_limit(limit, default_value=1000, max_value=10000)

            # 查询可能重复的数据
            query = f"""
            SELECT
                service_name,
                toStartOfSecond(timestamp) as time_second,
                substring(MD5(message), 1, 16) as message_hash,
                count() as duplicate_count,
                groupArray(id) as event_ids
            FROM logs.logs
            PREWHERE timestamp > now() - INTERVAL {safe_time_window}
            GROUP BY service_name, time_second, message_hash
            HAVING duplicate_count > 1
            ORDER BY duplicate_count DESC
            LIMIT {safe_limit}
            """

            result = self.storage.execute_query(query)

            analysis = {
                "time_window": safe_time_window,
                "total_duplicate_groups": len(result),
                "duplicates_by_service": defaultdict(int),
                "worst_offenders": [],
                "total_duplicate_events": 0
            }

            for row in result:
                if isinstance(row, dict):
                    service_name = row.get("service_name")
                    time_second = row.get("time_second")
                    dup_count = int(row.get("duplicate_count", row.get("count", 0)) or 0)
                    event_ids = row.get("event_ids") or []
                elif isinstance(row, (list, tuple)) and len(row) >= 5:
                    service_name, time_second, _message_hash, dup_count, event_ids = row[:5]
                    dup_count = int(dup_count or 0)
                else:
                    continue

                if not service_name or dup_count <= 0:
                    continue

                analysis["duplicates_by_service"][service_name] += dup_count
                analysis["total_duplicate_events"] += dup_count

                if len(analysis["worst_offenders"]) < 10:
                    analysis["worst_offenders"].append({
                        "service": service_name,
                        "time": str(time_second),
                        "count": dup_count,
                        "sample_ids": list(event_ids)[:3]  # 只显示前 3 个
                    })

            # 转换 defaultdict 为普通 dict
            analysis["duplicates_by_service"] = dict(analysis["duplicates_by_service"])

            return analysis

        except Exception as e:
            logger.error(f"Error analyzing duplicate sources: {e}")
            return {"error": str(e)}


# 全局实例
_deduplicator = None


def get_deduplicator(storage_adapter) -> DataDeduplicator:
    """
    获取数据去重器实例

    Args:
        storage_adapter: StorageAdapter 实例

    Returns:
        DataDeduplicator 实例
    """
    global _deduplicator
    if _deduplicator is None:
        _deduplicator = DataDeduplicator(storage_adapter)
    return _deduplicator


# 集成到 StorageAdapter 的辅助函数

def save_event_with_deduplication(
    storage_adapter,
    event: Dict[str, Any]
) -> bool:
    """
    保存事件前进行去重检查

    Args:
        storage_adapter: StorageAdapter 实例
        event: 待保存的事件

    Returns:
        bool: 是否保存成功（如果重复则返回 True 但不实际保存）
    """
    deduplicator = get_deduplicator(storage_adapter)

    # 检查是否重复
    is_duplicate, reason = deduplicator.is_duplicate_event(event)

    if is_duplicate:
        logger.debug(f"Skipping duplicate event: {reason}")
        # 返回 True 表示"处理成功"，但实际没有写入
        return True

    # 不重复，正常保存
    return storage_adapter.save_event(event)
