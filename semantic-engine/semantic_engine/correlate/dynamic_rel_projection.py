"""DynamicRelProjection — 动态关联投影，按时间窗口统计交互频率。

v2: 支持 ClickHouse 持久化。当 storage.ch_client 可用时写入 ClickHouse，
     否则回退到内存模式（向后兼容）。
"""
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional, Any


_INTERACTIONS_TABLE = "logs.interactions"

_CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {_INTERACTIONS_TABLE} (
    source String,
    target String,
    timestamp DateTime64(3, 'UTC'),
    interaction_id String DEFAULT generateUUIDv4(),
    failure_pattern String DEFAULT ''
) ENGINE = MergeTree()
PARTITION BY toDate(timestamp)
ORDER BY (source, target, timestamp)
TTL toDateTime(timestamp) + INTERVAL 30 DAY DELETE
SETTINGS index_granularity = 8192
"""

_INSERT_SQL = f"""INSERT INTO {_INTERACTIONS_TABLE} (source, target, timestamp, failure_pattern) VALUES"""

# 不带 failure_pattern 过滤的基础查询（向下兼容）
_COUNT_IN_WINDOW_SQL = f"""
SELECT count() AS cnt
FROM {_INTERACTIONS_TABLE}
WHERE source = %(source)s
  AND target = %(target)s
  AND timestamp > now() - INTERVAL %(seconds)s SECOND
"""

# 带 failure_pattern 过滤的查询
_COUNT_IN_WINDOW_FP_SQL = f"""
SELECT count() AS cnt
FROM {_INTERACTIONS_TABLE}
WHERE source = %(source)s
  AND target = %(target)s
  AND failure_pattern = %(failure_pattern)s
  AND timestamp > now() - INTERVAL %(seconds)s SECOND
"""

_AGGREGATE_HOURLY_SQL = f"""
SELECT
    toStartOfHour(timestamp) AS hour,
    count() AS count
FROM {_INTERACTIONS_TABLE}
WHERE source = %(source)s
  AND target = %(target)s
  AND timestamp > now() - INTERVAL %(hours)s HOUR
GROUP BY hour
ORDER BY hour
"""

_AGGREGATE_HOURLY_FP_SQL = f"""
SELECT
    toStartOfHour(timestamp) AS hour,
    count() AS count
FROM {_INTERACTIONS_TABLE}
WHERE source = %(source)s
  AND target = %(target)s
  AND failure_pattern = %(failure_pattern)s
  AND timestamp > now() - INTERVAL %(hours)s HOUR
GROUP BY hour
ORDER BY hour
"""


class DynamicRelProjection:
    """
    动态关联投影——按时间窗口统计服务间交互频率。

    支持 ClickHouse 持久化（storage.ch_client）和内存回退两种模式。
    自动创建 logs.interactions 表。

    v15: 增加 failure_pattern 维度，可按故障场景过滤交互统计。

    - record_interaction(source, target, timestamp, failure_pattern): 记录一次交互
    - query_trend(source, target, windows, failure_pattern): 查询时间窗口内的交互趋势
    - aggregate_by_hour(source, target, hours, failure_pattern): 按小时聚合
    """

    def __init__(self, storage: Optional[Any] = None):
        self._interactions: List[Tuple[str, str, datetime, str]] = []
        self._storage = storage
        self._ch_client = None
        self._clickhouse_available = False

        if storage and hasattr(storage, "ch_client") and storage.ch_client is not None:
            self._ch_client = storage.ch_client
            self._clickhouse_available = True
            self._ensure_table()

    # ── public API ──

    def record_interaction(self, source: str, target: str,
                           timestamp: Optional[datetime] = None,
                           failure_pattern: str = "") -> None:
        """记录一次交互，可用 failure_pattern 标记所属故障场景。"""
        ts = timestamp or datetime.utcnow()
        if self._clickhouse_available:
            self._ch_client.execute(
                _INSERT_SQL,
                [(source, target, ts, failure_pattern)],
            )
        else:
            self._interactions.append((source, target, ts, failure_pattern))

    def query_trend(self, source: str, target: str,
                     windows: List[str],
                     failure_pattern: Optional[str] = None) -> List[int]:
        """查询多个时间窗口内的交互计数。

        failure_pattern=None 时返回所有交互计数（不分故障场景）。
        failure_pattern="" 时只返回无故障场景标记的交互。
        failure_pattern="RabbitMQHeartbeatLost" 时只返回该场景下的交互。
        """
        if self._clickhouse_available:
            return self._query_trend_ch(source, target, windows, failure_pattern)
        return self._query_trend_memory(source, target, windows, failure_pattern)

    def aggregate_by_hour(self, source: str, target: str,
                           hours: int = 24,
                           failure_pattern: Optional[str] = None) -> List[Dict]:
        """按小时聚合交互次数，可按 failure_pattern 过滤。"""
        if self._clickhouse_available:
            return self._aggregate_hourly_ch(source, target, hours, failure_pattern)
        return self._aggregate_hourly_memory(source, target, hours, failure_pattern)

    # ── ClickHouse 实现 ──

    def _ensure_table(self) -> None:
        """创建 logs.interactions 表（如不存在）。"""
        try:
            self._ch_client.execute(_CREATE_TABLE_SQL)
        except Exception:
            # 表创建失败时静默降级到内存模式
            self._clickhouse_available = False

    def _query_trend_ch(self, source: str, target: str,
                        windows: List[str],
                        failure_pattern: Optional[str] = None) -> List[int]:
        results = []
        for window_str in windows:
            seconds = self._parse_window(window_str)
            params: dict = {"source": source, "target": target, "seconds": seconds}
            if failure_pattern is not None:
                query = _COUNT_IN_WINDOW_FP_SQL
                params["failure_pattern"] = failure_pattern
            else:
                query = _COUNT_IN_WINDOW_SQL
            rows = self._ch_client.execute(query, params)
            count = rows[0][0] if rows else 0
            results.append(count)
        return results

    def _aggregate_hourly_ch(self, source: str, target: str,
                              hours: int,
                              failure_pattern: Optional[str] = None) -> List[Dict]:
        params: dict = {"source": source, "target": target, "hours": hours}
        if failure_pattern is not None:
            query = _AGGREGATE_HOURLY_FP_SQL
            params["failure_pattern"] = failure_pattern
        else:
            query = _AGGREGATE_HOURLY_SQL
        rows = self._ch_client.execute(query, params)
        return [
            {"hour": row[0].isoformat() if hasattr(row[0], "isoformat") else str(row[0]),
             "count": row[1]}
            for row in rows
        ]

    # ── 内存回退实现 ──

    def _query_trend_memory(self, source: str, target: str,
                            windows: List[str],
                            failure_pattern: Optional[str] = None) -> List[int]:
        now = datetime.utcnow()
        results = []
        for window_str in windows:
            seconds = self._parse_window(window_str)
            cutoff = now - timedelta(seconds=seconds)
            count = sum(
                1 for s, t, ts, fp in self._interactions
                if s == source and t == target
                and ts >= cutoff
                and (failure_pattern is None or fp == failure_pattern)
            )
            results.append(count)
        return results

    def _aggregate_hourly_memory(self, source: str, target: str,
                                  hours: int,
                                  failure_pattern: Optional[str] = None) -> List[Dict]:
        now = datetime.utcnow()
        cutoff = now - timedelta(hours=hours)
        hourly = defaultdict(int)

        for s, t, ts, fp in self._interactions:
            if s == source and t == target and ts >= cutoff:
                if failure_pattern is not None and fp != failure_pattern:
                    continue
                hour_key = ts.replace(minute=0, second=0, microsecond=0)
                hourly[hour_key] += 1

        return [
            {"hour": hour.isoformat(), "count": count}
            for hour, count in sorted(hourly.items())
        ]

    # ── 工具方法 ──

    @staticmethod
    def _parse_window(window: str) -> int:
        window = window.upper().strip()
        parts = window.split()
        if len(parts) != 2:
            return 3600
        try:
            value = int(parts[0])
            unit = parts[1]
            if "SECOND" in unit:
                return value
            elif "MINUTE" in unit:
                return value * 60
            elif "HOUR" in unit:
                return value * 3600
            elif "DAY" in unit:
                return value * 86400
            return 3600
        except (ValueError, IndexError):
            return 3600
