"""
性能监控模块

提供 OpenTelemetry 集成和性能指标收集：
- 请求追踪
- 指标收集
- 性能分析

Date: 2026-02-22
"""

import os
import logging
import time
from typing import Dict, Any, Optional, Callable
from functools import wraps
from datetime import datetime

logger = logging.getLogger(__name__)


class PerformanceMetrics:
    """性能指标收集器"""

    def __init__(self):
        self._metrics: Dict[str, list] = {}
        self._counters: Dict[str, int] = {}
        self._histograms: Dict[str, list] = {}

    def record_latency(self, name: str, latency_ms: float, tags: Dict[str, str] = None):
        """记录延迟"""
        key = self._make_key(name, tags)
        if key not in self._histograms:
            self._histograms[key] = []
        self._histograms[key].append(latency_ms)

        # 只保留最近 1000 条
        if len(self._histograms[key]) > 1000:
            self._histograms[key] = self._histograms[key][-1000:]

    def increment_counter(self, name: str, value: int = 1, tags: Dict[str, str] = None):
        """增加计数器"""
        key = self._make_key(name, tags)
        if key not in self._counters:
            self._counters[key] = 0
        self._counters[key] += value

    def record_gauge(self, name: str, value: float, tags: Dict[str, str] = None):
        """记录仪表值"""
        key = self._make_key(name, tags)
        self._metrics[key] = [value]

    def get_stats(self, name: str, tags: Dict[str, str] = None) -> Dict[str, Any]:
        """获取统计信息"""
        key = self._make_key(name, tags)
        
        result = {
            "name": name,
            "tags": tags,
        }

        if key in self._histograms:
            values = self._histograms[key]
            if values:
                result["count"] = len(values)
                result["min"] = min(values)
                result["max"] = max(values)
                result["avg"] = sum(values) / len(values)
                result["p50"] = self._percentile(values, 50)
                result["p95"] = self._percentile(values, 95)
                result["p99"] = self._percentile(values, 99)

        if key in self._counters:
            result["count"] = self._counters[key]

        if key in self._metrics:
            result["value"] = self._metrics[key][-1] if self._metrics[key] else None

        return result

    def get_all_stats(self) -> Dict[str, Any]:
        """获取所有统计信息"""
        return {
            "histograms": {
                k: self._stats_from_values(v) 
                for k, v in self._histograms.items()
            },
            "counters": self._counters,
            "gauges": {k: v[-1] if v else None for k, v in self._metrics.items()},
        }

    def _make_key(self, name: str, tags: Dict[str, str] = None) -> str:
        """生成指标键"""
        if not tags:
            return name
        tag_str = ",".join(f"{k}={v}" for k, v in sorted(tags.items()))
        return f"{name}:{tag_str}"

    def _percentile(self, values: list, percentile: int) -> float:
        """计算百分位数"""
        if not values:
            return 0
        sorted_values = sorted(values)
        index = int(len(sorted_values) * percentile / 100)
        return sorted_values[min(index, len(sorted_values) - 1)]

    def _stats_from_values(self, values: list) -> Dict[str, float]:
        """从值列表计算统计"""
        if not values:
            return {}
        return {
            "count": len(values),
            "min": min(values),
            "max": max(values),
            "avg": sum(values) / len(values),
        }


_metrics: Optional[PerformanceMetrics] = None


def get_metrics() -> PerformanceMetrics:
    """获取性能指标实例"""
    global _metrics
    if _metrics is None:
        _metrics = PerformanceMetrics()
    return _metrics


def timed(name: str = None, tags: Dict[str, str] = None):
    """
    装饰器：测量函数执行时间
    
    Usage:
        @timed("api.request", {"endpoint": "/logs"})
        async def get_logs():
            ...
    """
    def decorator(func: Callable):
        metric_name = name or f"{func.__module__}.{func.__name__}"
        
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            start = time.perf_counter()
            try:
                result = await func(*args, **kwargs)
                return result
            finally:
                latency_ms = (time.perf_counter() - start) * 1000
                get_metrics().record_latency(metric_name, latency_ms, tags)
        
        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            start = time.perf_counter()
            try:
                result = func(*args, **kwargs)
                return result
            finally:
                latency_ms = (time.perf_counter() - start) * 1000
                get_metrics().record_latency(metric_name, latency_ms, tags)
        
        import asyncio
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper
    
    return decorator


def increment(name: str, value: int = 1, tags: Dict[str, str] = None):
    """增加计数器"""
    get_metrics().increment_counter(name, value, tags)


def gauge(name: str, value: float, tags: Dict[str, str] = None):
    """记录仪表值"""
    get_metrics().record_gauge(name, value, tags)


class RequestTracker:
    """请求追踪器"""

    def __init__(self):
        self._active_requests: Dict[str, datetime] = {}

    def start_request(self, request_id: str) -> str:
        """开始追踪请求"""
        self._active_requests[request_id] = datetime.now()
        increment("requests.active")
        return request_id

    def end_request(self, request_id: str, success: bool = True):
        """结束追踪请求"""
        start_time = self._active_requests.pop(request_id, None)
        if start_time:
            latency_ms = (datetime.now() - start_time).total_seconds() * 1000
            get_metrics().record_latency(
                "request.duration",
                latency_ms,
                {"success": str(success).lower()}
            )
            increment("requests.completed", tags={"success": str(success).lower()})
            increment("requests.active", -1)

    def get_active_count(self) -> int:
        """获取活跃请求数"""
        return len(self._active_requests)


_request_tracker: Optional[RequestTracker] = None


def get_request_tracker() -> RequestTracker:
    """获取请求追踪器实例"""
    global _request_tracker
    if _request_tracker is None:
        _request_tracker = RequestTracker()
    return _request_tracker


def setup_otel(service_name: str, service_version: str = "1.0.0"):
    """
    配置 OpenTelemetry
    
    环境变量:
        OTEL_ENABLED: 启用 OpenTelemetry (默认 false)
        OTEL_EXPORTER_OTLP_ENDPOINT: OTLP 导出端点
    """
    if os.getenv("OTEL_ENABLED", "false").lower() != "true":
        logger.info("OpenTelemetry is disabled")
        return

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        provider = TracerProvider()
        
        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
        otlp_exporter = OTLPSpanExporter(endpoint=endpoint)
        provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
        
        trace.set_tracer_provider(provider)
        
        logger.info(f"OpenTelemetry configured for {service_name}")
        
    except ImportError:
        logger.warning("OpenTelemetry packages not installed. Run: pip install opentelemetry-api opentelemetry-sdk")
    except Exception as e:
        logger.error(f"Failed to setup OpenTelemetry: {e}")
