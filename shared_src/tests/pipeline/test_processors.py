import pytest
import time
from shared_src.pipeline.processors import (
    EventPipeline, AggregateProcessor, DedupProcessor,
    SampleProcessor, EnrichProcessor, RouteProcessor,
)

class MockRawEvent:
    """模拟 RawEvent 用于测试"""
    def __init__(self, raw_payload="", host="", labels_json="", metadata=None):
        self.raw_payload = raw_payload
        self.host = host
        self.labels_json = labels_json
        self.metadata = metadata or {}
        self.event_category = ""

class TestAggregateProcessor:
    def test_aggregate_traceback(self):
        """AggregateProcessor 聚合 Python Traceback"""
        pipeline = EventPipeline([AggregateProcessor(window_seconds=5)])
        line1 = MockRawEvent(raw_payload="Traceback (most recent call last):")
        line2 = MockRawEvent(raw_payload='  File "main.py", line 10, in foo')
        line3 = MockRawEvent(raw_payload="Exception: OOM")
        assert len(pipeline.execute(line1)) == 0   # buffer
        assert len(pipeline.execute(line2)) == 0   # buffer
        assert len(pipeline.execute(line3)) == 1   # 完整 traceback

    def test_aggregate_timeout(self):
        """超时后未完成 traceback 也应输出"""
        pipeline = EventPipeline([AggregateProcessor(window_seconds=0)])
        line1 = MockRawEvent(raw_payload="Traceback (most recent call last):")
        result = pipeline.execute(line1)
        assert len(result) == 1

class TestDedupProcessor:
    def test_dedup_exponential_backoff(self):
        pipeline = EventPipeline([DedupProcessor(initial_window_ms=5000)])
        err = MockRawEvent(raw_payload="ERROR: Connection refused")
        assert len(pipeline.execute(err)) == 1     # 第一个
        assert len(pipeline.execute(err)) == 0     # 窗口内聚合
        assert len(pipeline.execute(err)) == 0     # 继续聚合

    def test_dedup_different_messages(self):
        pipeline = EventPipeline([DedupProcessor(initial_window_ms=5000)])
        err1 = MockRawEvent(raw_payload="ERROR: Connection refused")
        err2 = MockRawEvent(raw_payload="ERROR: Timeout")
        assert len(pipeline.execute(err1)) == 1
        assert len(pipeline.execute(err2)) == 1  # 不同消息不聚合

class TestSampleProcessor:
    def test_sample_info_one_percent(self):
        pipeline = EventPipeline([SampleProcessor({"INFO": 1.0})])  # 100%
        events = [MockRawEvent(raw_payload=f"INFO msg {i}") for i in range(100)]
        results = [e for ev in events for e in pipeline.execute(ev)]
        assert len(results) == 100

    def test_sample_zero_percent(self):
        pipeline = EventPipeline([SampleProcessor({"DEBUG": 0.0})])  # 0%
        events = [MockRawEvent(raw_payload="DEBUG msg") for _ in range(100)]
        results = [e for ev in events for e in pipeline.execute(ev)]
        assert len(results) == 0

class TestEnrichProcessor:
    def test_enrich_host_to_az(self):
        pipeline = EventPipeline([EnrichProcessor(host_map={"compute-01": "az-1"})])
        raw = MockRawEvent(host="compute-01")
        result = pipeline.execute(raw)
        assert "az-1" in result[0].labels_json

    def test_enrich_unknown_host(self):
        pipeline = EventPipeline([EnrichProcessor(host_map={})])
        raw = MockRawEvent(host="unknown-host")
        result = pipeline.execute(raw)
        assert result is not None

class TestRouteProcessor:
    def test_route_openstack(self):
        pipeline = EventPipeline([RouteProcessor()])
        raw = MockRawEvent(raw_payload="nova-api: ERROR")
        result = pipeline.execute(raw)
        assert result[0].metadata.get("platform") == "openstack"

class TestEventPipeline:
    def test_pipeline_chain(self):
        """多个 Processor 链式执行"""
        pipeline = EventPipeline([
            DedupProcessor(initial_window_ms=100),
            EnrichProcessor(host_map={"host-1": "az-1"}),
            RouteProcessor(),
        ])
        raw = MockRawEvent(raw_payload="neutron: ERROR", host="host-1")
        result = pipeline.execute(raw)
        assert len(result) >= 1
        assert "az-1" in result[0].labels_json

    def test_empty_pipeline(self):
        pipeline = EventPipeline([])
        raw = MockRawEvent(raw_payload="test")
        result = pipeline.execute(raw)
        assert len(result) == 1
