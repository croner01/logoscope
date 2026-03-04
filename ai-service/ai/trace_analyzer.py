"""
Trace 分析服务

提供分布式追踪数据的深度分析：
- Trace 数据获取
- 调用链分析
- 性能瓶颈识别
- 异常检测
- 可视化数据生成

Date: 2026-02-22
"""

import logging
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass, field
import json

logger = logging.getLogger(__name__)


def _parse_tags(raw_tags: Any) -> Dict[str, Any]:
    """Normalize tags payload from dict/json string."""
    if isinstance(raw_tags, dict):
        return raw_tags
    if isinstance(raw_tags, str) and raw_tags.strip():
        try:
            parsed = json.loads(raw_tags)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _safe_duration_ms(value: Any, tags: Dict[str, Any]) -> int:
    """Resolve span duration with multi-source fallback."""
    candidates: List[Any] = [
        value,
        tags.get("duration_ms"),
        tags.get("span.duration_ms"),
    ]
    for candidate in candidates:
        if candidate in (None, ""):
            continue
        try:
            return int(float(candidate))
        except (TypeError, ValueError):
            continue

    duration_ns = tags.get("duration_ns") or tags.get("span.duration_ns")
    if duration_ns not in (None, ""):
        try:
            return int(float(duration_ns) / 1_000_000.0)
        except (TypeError, ValueError):
            pass
    return 0


def _normalize_start_time(raw_start: Any) -> str:
    if raw_start is None:
        return ""
    return str(raw_start)


@dataclass
class Span:
    """Span 数据结构"""
    span_id: str
    trace_id: str
    parent_span_id: Optional[str]
    operation_name: str
    service_name: str
    start_time: str
    duration_ms: int
    status: str = "ok"
    tags: Dict[str, Any] = field(default_factory=dict)
    logs: List[Dict[str, Any]] = field(default_factory=list)
    children: List['Span'] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "span_id": self.span_id,
            "trace_id": self.trace_id,
            "parent_span_id": self.parent_span_id,
            "operation_name": self.operation_name,
            "service_name": self.service_name,
            "start_time": self.start_time,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "tags": self.tags,
            "logs": self.logs,
            "children": [c.to_dict() for c in self.children],
        }


@dataclass
class TraceAnalysisResult:
    """Trace 分析结果"""
    trace_id: str
    total_duration_ms: int
    service_count: int
    span_count: int
    root_cause_spans: List[Dict[str, Any]]
    bottleneck_spans: List[Dict[str, Any]]
    error_spans: List[Dict[str, Any]]
    recommendations: List[str]
    service_timeline: List[Dict[str, Any]]
    critical_path: List[str]


class TraceAnalyzer:
    """Trace 分析器"""

    def __init__(self, storage_adapter=None):
        self.storage = storage_adapter

    def analyze_trace(self, trace_id: str) -> TraceAnalysisResult:
        """分析 Trace"""
        spans = self._get_trace_spans(trace_id)

        if not spans:
            return TraceAnalysisResult(
                trace_id=trace_id,
                total_duration_ms=0,
                service_count=0,
                span_count=0,
                root_cause_spans=[],
                bottleneck_spans=[],
                error_spans=[],
                recommendations=["未找到该 Trace 的数据"],
                service_timeline=[],
                critical_path=[],
            )

        span_tree = self._build_span_tree(spans)

        total_duration = max(s.duration_ms for s in spans) if spans else 0
        services = set(s.service_name for s in spans)

        error_spans = self._find_error_spans(spans)
        bottleneck_spans = self._find_bottlenecks(spans)
        root_cause_spans = self._find_root_causes(spans, error_spans)

        recommendations = self._generate_recommendations(
            error_spans, bottleneck_spans, total_duration
        )

        service_timeline = self._build_service_timeline(spans)
        critical_path = self._find_critical_path(span_tree)

        return TraceAnalysisResult(
            trace_id=trace_id,
            total_duration_ms=total_duration,
            service_count=len(services),
            span_count=len(spans),
            root_cause_spans=[self._span_to_dict(s) for s in root_cause_spans],
            bottleneck_spans=[self._span_to_dict(s) for s in bottleneck_spans],
            error_spans=[self._span_to_dict(s) for s in error_spans],
            recommendations=recommendations,
            service_timeline=service_timeline,
            critical_path=critical_path,
        )

    def _get_trace_spans(self, trace_id: str) -> List[Span]:
        """获取 Trace 的所有 Span"""
        if not self.storage:
            return self._get_mock_spans(trace_id)

        try:
            results = None
            get_trace_spans_fn = getattr(self.storage, "get_trace_spans", None)
            if callable(get_trace_spans_fn):
                fetched = get_trace_spans_fn(trace_id)
                if isinstance(fetched, list):
                    results = fetched

            if results is None:
                query = f"""
                SELECT
                    span_id, trace_id, parent_span_id, operation_name,
                    service_name, toString(timestamp) as timestamp_str, status, attributes_json
                FROM logs.traces
                WHERE trace_id = '{trace_id}'
                ORDER BY timestamp
                """
                results = self.storage.execute_query(query)

            spans = []
            for row in results:
                if not isinstance(row, dict):
                    continue
                tags = _parse_tags(row.get("tags") or row.get("attributes_json"))
                spans.append(Span(
                    span_id=row.get('span_id', ''),
                    trace_id=row.get('trace_id', ''),
                    parent_span_id=row.get('parent_span_id'),
                    operation_name=row.get('operation_name', ''),
                    service_name=row.get('service_name', ''),
                    start_time=_normalize_start_time(
                        row.get('start_time') or row.get('timestamp_str') or row.get('timestamp')
                    ),
                    duration_ms=_safe_duration_ms(row.get('duration_ms'), tags),
                    status=row.get('status', 'ok'),
                    tags=tags,
                ))

            return spans

        except Exception as e:
            logger.error(f"Failed to get trace spans: {e}")
            return self._get_mock_spans(trace_id)

    def _get_mock_spans(self, trace_id: str) -> List[Span]:
        """获取模拟 Span 数据（用于演示）"""
        base_time = datetime.now()
        
        return [
            Span(
                span_id="span-001",
                trace_id=trace_id,
                parent_span_id=None,
                operation_name="HTTP GET /api/orders",
                service_name="api-gateway",
                start_time=base_time.isoformat(),
                duration_ms=450,
                status="ok",
                tags={"http.method": "GET", "http.url": "/api/orders"},
            ),
            Span(
                span_id="span-002",
                trace_id=trace_id,
                parent_span_id="span-001",
                operation_name="order-service.getOrders",
                service_name="order-service",
                start_time=(base_time + timedelta(milliseconds=5)).isoformat(),
                duration_ms=380,
                status="ok",
                tags={"rpc.system": "grpc"},
            ),
            Span(
                span_id="span-003",
                trace_id=trace_id,
                parent_span_id="span-002",
                operation_name="database.query",
                service_name="order-db",
                start_time=(base_time + timedelta(milliseconds=10)).isoformat(),
                duration_ms=320,
                status="error",
                tags={"db.system": "mysql", "error": "connection timeout"},
                logs=[{"timestamp": base_time.isoformat(), "message": "Connection pool exhausted"}],
            ),
            Span(
                span_id="span-004",
                trace_id=trace_id,
                parent_span_id="span-002",
                operation_name="cache.get",
                service_name="redis",
                start_time=(base_time + timedelta(milliseconds=8)).isoformat(),
                duration_ms=2,
                status="ok",
                tags={"cache.hit": "false"},
            ),
            Span(
                span_id="span-005",
                trace_id=trace_id,
                parent_span_id="span-001",
                operation_name="auth-service.validate",
                service_name="auth-service",
                start_time=(base_time + timedelta(milliseconds=2)).isoformat(),
                duration_ms=15,
                status="ok",
                tags={"auth.type": "jwt"},
            ),
        ]

    def _build_span_tree(self, spans: List[Span]) -> Optional[Span]:
        """构建 Span 树"""
        span_map = {s.span_id: s for s in spans}
        root = None

        for span in spans:
            if span.parent_span_id:
                parent = span_map.get(span.parent_span_id)
                if parent:
                    parent.children.append(span)
            else:
                root = span

        return root

    def _find_error_spans(self, spans: List[Span]) -> List[Span]:
        """查找错误 Span"""
        return [
            s
            for s in spans
            if str(s.status or "").lower() == "error"
            or "ERROR" in str(s.status or "").upper()
            or bool(s.tags.get("error"))
        ]

    def _find_bottlenecks(self, spans: List[Span], threshold_pct: float = 0.3) -> List[Span]:
        """查找性能瓶颈"""
        if not spans:
            return []

        max_duration = max(s.duration_ms for s in spans)
        threshold = max_duration * threshold_pct

        bottlenecks = [
            s for s in spans
            if s.duration_ms >= threshold and s.status != "error"
        ]

        return sorted(bottlenecks, key=lambda s: s.duration_ms, reverse=True)[:3]

    def _find_root_causes(self, spans: List[Span], error_spans: List[Span]) -> List[Span]:
        """查找根因 Span"""
        if not error_spans:
            return []

        span_map = {s.span_id: s for s in spans}
        root_causes = []

        for error_span in error_spans:
            current = error_span
            while current.parent_span_id:
                parent = span_map.get(current.parent_span_id)
                if parent and parent.status == "error":
                    current = parent
                else:
                    break

            if current not in root_causes:
                root_causes.append(current)

        return root_causes

    def _generate_recommendations(
        self,
        error_spans: List[Span],
        bottleneck_spans: List[Span],
        total_duration: int
    ) -> List[str]:
        """生成优化建议"""
        recommendations = []

        for span in error_spans:
            error_msg = span.tags.get("error", "未知错误")
            recommendations.append(
                f"修复 {span.service_name} 服务的错误: {error_msg}"
            )

            if "timeout" in error_msg.lower():
                recommendations.append(f"检查 {span.service_name} 的超时配置")
            if "connection" in error_msg.lower():
                recommendations.append(f"检查 {span.service_name} 的连接池配置")

        for span in bottleneck_spans:
            recommendations.append(
                f"优化 {span.service_name}.{span.operation_name} (耗时 {span.duration_ms}ms)"
            )

        if total_duration > 1000:
            recommendations.append(
                f"整体调用链耗时较长 ({total_duration}ms)，建议检查服务间调用是否可以并行化"
            )

        if not recommendations:
            recommendations.append("调用链状态良好，无明显问题")

        return recommendations

    def _build_service_timeline(self, spans: List[Span]) -> List[Dict[str, Any]]:
        """构建服务时间线"""
        if not spans:
            return []

        sorted_spans = sorted(spans, key=lambda s: s.start_time)

        timeline = []
        for span in sorted_spans:
            timeline.append({
                "service_name": span.service_name,
                "operation": span.operation_name,
                "start_time": span.start_time,
                "duration_ms": span.duration_ms,
                "status": span.status,
            })

        return timeline

    def _find_critical_path(self, root: Optional[Span]) -> List[str]:
        """查找关键路径"""
        if not root:
            return []

        path = []

        def find_longest_path(span: Span) -> Tuple[int, List[str]]:
            if not span.children:
                return span.duration_ms, [span.service_name]

            max_child_duration = 0
            max_child_path = []

            for child in span.children:
                duration, child_path = find_longest_path(child)
                if duration > max_child_duration:
                    max_child_duration = duration
                    max_child_path = child_path

            total_duration = span.duration_ms + max_child_duration
            return total_duration, [span.service_name] + max_child_path

        _, path = find_longest_path(root)
        return path

    def _span_to_dict(self, span: Span) -> Dict[str, Any]:
        """Span 转换为字典"""
        return {
            "span_id": span.span_id,
            "service_name": span.service_name,
            "operation_name": span.operation_name,
            "duration_ms": span.duration_ms,
            "status": span.status,
            "error": span.tags.get("error"),
        }

    def get_trace_visualization_data(self, trace_id: str) -> Dict[str, Any]:
        """获取 Trace 可视化数据"""
        spans = self._get_trace_spans(trace_id)
        if not spans:
            return {"nodes": [], "edges": [], "waterfall": []}

        span_tree = self._build_span_tree(spans)
        analysis = self.analyze_trace(trace_id)

        nodes = []
        edges = []
        waterfall = []

        span_map = {s.span_id: s for s in spans}

        for span in spans:
            nodes.append({
                "id": span.span_id,
                "label": f"{span.service_name}\n{span.operation_name}",
                "service": span.service_name,
                "operation": span.operation_name,
                "duration_ms": span.duration_ms,
                "status": span.status,
            })

            if span.parent_span_id:
                edges.append({
                    "source": span.parent_span_id,
                    "target": span.span_id,
                })

        sorted_spans = sorted(spans, key=lambda s: s.start_time)
        base_time = datetime.fromisoformat(sorted_spans[0].start_time) if sorted_spans else datetime.now()

        for span in sorted_spans:
            try:
                start = datetime.fromisoformat(span.start_time)
                offset_ms = int((start - base_time).total_seconds() * 1000)
            except:
                offset_ms = 0

            waterfall.append({
                "span_id": span.span_id,
                "service": span.service_name,
                "operation": span.operation_name,
                "offset_ms": offset_ms,
                "duration_ms": span.duration_ms,
                "status": span.status,
                "depth": self._get_span_depth(span, span_map),
            })

        return {
            "trace_id": trace_id,
            "nodes": nodes,
            "edges": edges,
            "waterfall": waterfall,
            "analysis": {
                "total_duration_ms": analysis.total_duration_ms,
                "service_count": analysis.service_count,
                "span_count": analysis.span_count,
                "critical_path": analysis.critical_path,
                "error_count": len(analysis.error_spans),
            },
        }

    def _get_span_depth(self, span: Span, span_map: Dict[str, Span]) -> int:
        """计算 Span 深度"""
        depth = 0
        current = span
        while current.parent_span_id:
            depth += 1
            current = span_map.get(current.parent_span_id, current)
            if current == span:
                break
        return depth


_trace_analyzer: Optional[TraceAnalyzer] = None


def get_trace_analyzer(storage_adapter=None) -> TraceAnalyzer:
    """获取 Trace 分析器实例"""
    global _trace_analyzer
    if _trace_analyzer is None:
        _trace_analyzer = TraceAnalyzer(storage_adapter)
    return _trace_analyzer
