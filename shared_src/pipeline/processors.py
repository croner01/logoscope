from abc import ABC, abstractmethod
from typing import List, Dict, Optional, Any
import time
import re
import hashlib


class PipelineProcessor(ABC):
    """Pipeline Processor 基类"""
    @abstractmethod
    def process(self, event: Any) -> List[Any]:
        ...


class EventPipeline:
    """Event Pipeline——链式执行多个 Processor。"""

    def __init__(self, processors: List[PipelineProcessor]):
        self.processors = processors

    def execute(self, event: Any) -> List[Any]:
        results = [event]
        for processor in self.processors:
            new_results = []
            for ev in results:
                new_results.extend(processor.process(ev))
            results = new_results
            if not results:
                break
        return results


class AggregateProcessor(PipelineProcessor):
    """聚合 Processor——将属于同一 Traceback 的多行日志聚合为一条。"""

    TRACEBACK_START = re.compile(r"Traceback \(most recent call last\):")
    TRACEBACK_LINE = re.compile(r"^\s")  # 缩进行（File 行或代码行）

    def __init__(self, window_seconds: int = 5):
        self.window_seconds = window_seconds
        self._buffer: List[Any] = []
        self._buffer_time: float = 0.0

    def process(self, event: Any) -> List[Any]:
        now = time.time()
        payload = getattr(event, "raw_payload", "")

        if self.TRACEBACK_START.match(payload):
            if self.window_seconds <= 0:
                return [event]
            self._buffer = [event]
            self._buffer_time = now
            return []

        if self._buffer:
            # 检查超时
            if now - self._buffer_time >= self.window_seconds:
                return self._flush()
            # 非缩进行 → traceback 结束（异常行）
            if not self.TRACEBACK_LINE.match(payload):
                self._buffer.append(event)
                return self._flush()
            # 仍在 traceback 中
            self._buffer.append(event)
            return []

        return [event]

    def _flush(self) -> List[Any]:
        """将缓冲区内容合并为一条事件输出。"""
        result = self._buffer[:]
        self._buffer = []
        self._buffer_time = 0.0
        merged = result[0]
        merged.raw_payload = "\n".join(
            getattr(e, "raw_payload", "") for e in result
        )
        merged.event_category = "traceback"
        return [merged]


class DedupProcessor(PipelineProcessor):
    """去重 Processor——使用指数退避的窗口去重。"""

    def __init__(self, initial_window_ms: int = 5000):
        self.initial_window_ms = initial_window_ms
        self._seen: Dict[str, float] = {}

    def _fingerprint(self, event: Any) -> str:
        payload = getattr(event, "raw_payload", "") or ""
        return hashlib.md5(payload.encode()).hexdigest()

    def process(self, event: Any) -> List[Any]:
        now = time.time()
        fp = self._fingerprint(event)
        last_seen = self._seen.get(fp)

        if last_seen is None:
            self._seen[fp] = now
            return [event]

        elapsed_ms = (now - last_seen) * 1000
        if elapsed_ms >= self.initial_window_ms:
            self._seen[fp] = now
            # 指数退避——下次窗口翻倍
            self.initial_window_ms = int(self.initial_window_ms * 1.5)
            return [event]

        return []


class SampleProcessor(PipelineProcessor):
    """采样 Processor——按 event_category 设置采样率。"""

    def __init__(self, rates: Dict[str, float]):
        self.rates = rates
        self._counter: Dict[str, int] = {}

    def process(self, event: Any) -> List[Any]:
        payload = getattr(event, "raw_payload", "") or ""
        # 从 payload 推断 category
        category = "INFO"
        if "ERROR" in payload.upper():
            category = "ERROR"
        elif "WARN" in payload.upper():
            category = "WARN"
        elif "DEBUG" in payload.upper():
            category = "DEBUG"

        rate = self.rates.get(category, 1.0)
        if rate >= 1.0:
            return [event]

        if category not in self._counter:
            self._counter[category] = 0
        self._counter[category] += 1

        if (self._counter[category] - 1) / max(self._counter[category], 1) < rate:
            return [event]
        return []


class EnrichProcessor(PipelineProcessor):
    """富化 Processor——补充标签（如 host→az 映射）。"""

    def __init__(self, host_map: Dict[str, str]):
        self.host_map = host_map

    def process(self, event: Any) -> List[Any]:
        host = getattr(event, "host", "")
        if host and host in self.host_map:
            az = self.host_map[host]
            event.labels_json = f"az:{az}"
        return [event]


class RouteProcessor(PipelineProcessor):
    """路由 Processor——识别平台并添加元数据。"""

    OPENSTACK_PATTERN = re.compile(
        r"(nova|neutron|cinder|glance|keystone|horizon|heat|swift|ceilometer"
        r"|designate|manila|octavia|magnum|trove|sahara|ironic|zaqar|barbican"
        r"|mistral|senlin|vitrage|aodh|panko|qinling|kuryr|tacker)"
        r"[-_ ]",
        re.IGNORECASE,
    )

    def process(self, event: Any) -> List[Any]:
        payload = getattr(event, "raw_payload", "") or ""
        if self.OPENSTACK_PATTERN.search(payload):
            event.metadata["platform"] = "openstack"
        return [event]
