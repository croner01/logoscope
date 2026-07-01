"""DynamicRelProjection — 动态关联投影，按时间窗口统计交互频率。"""
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional


class DynamicRelProjection:
    """
    动态关联投影——按时间窗口统计服务间交互频率。

    - record_interaction(source, target): 记录一次交互
    - query_trend(source, target, windows): 查询时间窗口内的交互趋势
    - aggregate_by_hour(source, target): 按小时聚合
    """

    def __init__(self):
        self._interactions: List[Tuple[str, str, datetime]] = []

    def record_interaction(self, source: str, target: str,
                           timestamp: Optional[datetime] = None) -> None:
        ts = timestamp or datetime.utcnow()
        self._interactions.append((source, target, ts))

    def query_trend(self, source: str, target: str,
                     windows: List[str]) -> List[int]:
        """查询多个时间窗口内的交互计数。"""
        now = datetime.utcnow()
        results = []
        for window_str in windows:
            seconds = self._parse_window(window_str)
            cutoff = now - timedelta(seconds=seconds)
            count = sum(
                1 for s, t, ts in self._interactions
                if s == source and t == target and ts >= cutoff
            )
            results.append(count)
        return results

    def aggregate_by_hour(self, source: str, target: str,
                           hours: int = 24) -> List[Dict]:
        """按小时聚合交互次数。"""
        now = datetime.utcnow()
        cutoff = now - timedelta(hours=hours)
        hourly = defaultdict(int)

        for s, t, ts in self._interactions:
            if s == source and t == target and ts >= cutoff:
                hour_key = ts.replace(minute=0, second=0, microsecond=0)
                hourly[hour_key] += 1

        return [
            {"hour": hour.isoformat(), "count": count}
            for hour, count in sorted(hourly.items())
        ]

    def _parse_window(self, window: str) -> int:
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
