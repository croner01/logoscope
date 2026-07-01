import pytest
from datetime import datetime
from shared_src.projection.base import Projection, ProjectionStatus
from shared_src.projection.checkpoint import ProjectionCheckpoint
from shared_src.event.envelope import EventEnvelope


class MockProjection(Projection):
    name = "mock"
    epoch = "20260701"

    def __init__(self):
        self._applied = 0

    @property
    def upstream_topics(self):
        return ["platform.test"]

    def apply(self, envelope: EventEnvelope):
        self._applied += 1

    def rebuild(self, event_source):
        for env in event_source:
            self.apply(env)

    def checkpoint(self):
        return ProjectionCheckpoint(projection=self.name, epoch=self.epoch)

    def status(self):
        cp = self.checkpoint()
        return ProjectionStatus(
            projection_epoch=self.epoch,
            event_count=self._applied,
            checkpoint=cp,
        )


class TestProjectionBase:
    def test_projection_interface(self):
        proj = MockProjection()
        assert proj.name == "mock"
        assert proj.epoch == "20260701"
        assert proj.upstream_topics == ["platform.test"]

    def test_apply_and_status(self):
        proj = MockProjection()
        proj.apply(EventEnvelope(event_id="e1"))
        assert proj.status().event_count == 1

    def test_rebuild(self):
        proj = MockProjection()
        events = [EventEnvelope(event_id=f"e{i}") for i in range(3)]
        proj.rebuild(events)
        assert proj.status().event_count == 3
