import pytest
from shared_src.projection.checkpoint import ProjectionCheckpoint


class TestProjectionCheckpoint:
    def test_update_offset(self):
        cp = ProjectionCheckpoint(projection="inventory", epoch="20260701")
        cp.update("platform.entity", 0, 100)
        assert cp.records["platform.entity"][0] == 100

    def test_offset_monotonic(self):
        """offset 严格递增，不回退"""
        cp = ProjectionCheckpoint(projection="inventory", epoch="20260701")
        cp.update("platform.entity", 0, 100)
        cp.update("platform.entity", 0, 90)  # 小于当前——应忽略
        assert cp.records["platform.entity"][0] == 100

    def test_lag_calculation(self):
        cp = ProjectionCheckpoint(projection="inventory", epoch="20260701")
        cp.update("platform.entity", 0, 100)
        assert cp.get_lag("platform.entity", 0, 150) == 50

    def test_multiple_topics(self):
        cp = ProjectionCheckpoint(projection="graph", epoch="20260701")
        cp.update("platform.entity", 0, 200)
        cp.update("platform.interaction", 1, 300)
        assert cp.records["platform.entity"][0] == 200
        assert cp.records["platform.interaction"][1] == 300
