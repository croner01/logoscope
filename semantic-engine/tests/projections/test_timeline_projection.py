import pytest
from datetime import datetime, timedelta
from projections.timeline_projection import TimelineProjection, StateTransition


class TestTimelineProjection:
    def test_record_transition(self):
        tl = TimelineProjection()
        tl.record_transition(
            entity_id="INSTANCE:abc-123",
            from_state="BUILD",
            to_state="ACTIVE",
            timestamp=datetime(2026, 7, 1, 12, 0, 0),
        )
        timeline = tl.get_timeline("INSTANCE:abc-123", "1 HOUR")
        assert len(timeline) == 1
        assert timeline[0].from_state == "BUILD"
        assert timeline[0].to_state == "ACTIVE"

    def test_multiple_transitions(self):
        tl = TimelineProjection()
        base = datetime(2026, 7, 1, 12, 0, 0)
        for i, (f, t) in enumerate([("BUILD", "ACTIVE"), ("ACTIVE", "ERROR"), ("ERROR", "ACTIVE")]):
            tl.record_transition("INSTANCE:vm-1", f, t, base + timedelta(minutes=i))
        timeline = tl.get_timeline("INSTANCE:vm-1", "1 HOUR")
        assert len(timeline) == 3

    def test_time_window_filter(self):
        tl = TimelineProjection()
        base = datetime(2026, 7, 1, 12, 0, 0)
        tl.record_transition("INSTANCE:vm-1", "ACTIVE", "ERROR", base)
        tl.record_transition("INSTANCE:vm-1", "ERROR", "ACTIVE", base + timedelta(hours=2))
        # 1小时窗口——只包含第一条
        timeline = tl.get_timeline("INSTANCE:vm-1", "1 HOUR")
        assert len(timeline) == 1

    def test_empty_timeline(self):
        tl = TimelineProjection()
        assert tl.get_timeline("NONEXISTENT", "1 HOUR") == []

    def test_has_state_changed(self):
        tl = TimelineProjection()
        base = datetime(2026, 7, 1, 12, 0, 0)
        tl.record_transition("INSTANCE:vm-1", "ACTIVE", "ERROR", base)
        assert tl.has_state_changed("INSTANCE:vm-1", window_minutes=30) == True
        # 更旧的、不在窗口内的事件
        old = datetime(2026, 6, 1, 12, 0, 0)
        tl.record_transition("INSTANCE:vm-2", "ACTIVE", "SHUTOFF", old)
        assert tl.has_state_changed("INSTANCE:vm-2", window_minutes=30) == False
