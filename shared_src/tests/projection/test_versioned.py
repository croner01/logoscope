import pytest
from shared_src.projection.versioned import VersionedProjectionRegistry
from shared_src.projection.base import Projection, ProjectionStatus
from shared_src.projection.checkpoint import ProjectionCheckpoint
from shared_src.event.envelope import EventEnvelope


class MockProjection(Projection):
    _counter = 0

    def __init__(self, name="mock", epoch="20260701"):
        self._name = name
        self._epoch = epoch
        self._applied = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def epoch(self) -> str:
        return self._epoch

    @property
    def upstream_topics(self):
        return ["platform.test"]

    def apply(self, envelope: EventEnvelope):
        self._applied += 1
        MockProjection._counter += 1

    def rebuild(self, event_source):
        for env in event_source:
            self.apply(env)

    def checkpoint(self):
        return ProjectionCheckpoint(projection=self.name, epoch=self.epoch)

    def status(self):
        return ProjectionStatus(
            projection_epoch=self.epoch,
            event_count=self._applied,
            checkpoint=self.checkpoint(),
        )


class MockEvent:
    def __init__(self, id="e1"):
        self.id = id


class TestVersionedProjectionRegistry:
    def test_traffic_split(self):
        registry = VersionedProjectionRegistry("graph")
        v1 = MockProjection("hashmap")
        v2 = MockProjection("list")
        registry.add_version(v1, traffic=0.9)
        registry.add_version(v2, traffic=0.1)

        calls_v1 = 0
        for _ in range(1000):
            target = registry.route(MockEvent())
            if target == v1:
                calls_v1 += 1
        assert 800 < calls_v1 < 1000

    def test_route_returns_version_for_each_call(self):
        registry = VersionedProjectionRegistry("graph")
        v1 = MockProjection("v1")
        registry.add_version(v1, traffic=1.0)
        target = registry.route(MockEvent())
        assert target is not None

    def test_promote(self):
        registry = VersionedProjectionRegistry("graph")
        v1 = MockProjection("hashmap")
        v2 = MockProjection("list")
        registry.add_version(v1, traffic=0.0)
        registry.add_version(v2, traffic=1.0)
        registry.promote("list")
        assert registry._traffic["list"] == 1.0

    def test_compare_results(self):
        registry = VersionedProjectionRegistry("graph")
        v1 = MockProjection("hashmap")
        v2 = MockProjection("list")
        registry.add_version(v1, traffic=0.0)
        registry.add_version(v2, traffic=0.0)
        registry.apply_all(MockEvent())
        results = registry.compare_results()
        assert "hashmap" in results
        assert "list" in results

    def test_multiple_versions(self):
        registry = VersionedProjectionRegistry("graph")
        v1 = MockProjection("v1")
        v2 = MockProjection("v2")
        v3 = MockProjection("v3")
        registry.add_version(v1, traffic=0.3)
        registry.add_version(v2, traffic=0.3)
        registry.add_version(v3, traffic=0.4)

        counts = {v1: 0, v2: 0, v3: 0}
        for _ in range(3000):
            target = registry.route(MockEvent())
            counts[target] += 1
        assert counts[v1] > 0
        assert counts[v2] > 0
        assert counts[v3] > 0
