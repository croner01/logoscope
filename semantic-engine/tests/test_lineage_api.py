import pytest
import json
from datetime import datetime
from shared_src.event.envelope import EventEnvelope
from shared_src.event.raw_event_store import RawEventStore
from semantic_engine.api.lineage import LineageAPI, LineageNode, LineageEdge


class TestLineageAPI:
    def test_lineage_trace(self):
        """追踪一个 Event 的完整血缘链"""
        store = RawEventStore()

        raw = EventEnvelope(event_id="raw-001", event_type="raw.log",
                            payload=b"test", producer="ingest")
        norm = EventEnvelope(event_id="norm-001", event_type="normalized.event",
                              parent_event_ids=["raw-001"], producer="semantic-engine")
        finding = EventEnvelope(event_id="finding-001", event_type="finding.event",
                                 parent_event_ids=["norm-001", "raw-001"],
                                 producer="inference")

        store.append(raw)
        store.append(norm)
        store.append(finding)

        api = LineageAPI(store)
        result = api.trace("finding-001")

        assert result is not None
        assert result.root_event_id == "raw-001" or "raw-001" in [n.event_id for n in result.nodes]

    def test_lineage_returns_dag(self):
        """血缘关系以 DAG 格式返回"""
        store = RawEventStore()

        raw = EventEnvelope(event_id="raw-001", event_type="raw.log")
        norm = EventEnvelope(event_id="norm-001", parent_event_ids=["raw-001"])
        store.append(raw)
        store.append(norm)

        api = LineageAPI(store)
        result = api.trace("norm-001")

        assert len(result.nodes) >= 2
        assert len(result.edges) >= 1
        # 验证 edge 连接
        edge = result.edges[0]
        assert edge.source_id == "raw-001"
        assert edge.target_id == "norm-001"

    def test_lineage_nonexistent(self):
        store = RawEventStore()
        api = LineageAPI(store)
        assert api.trace("nonexistent") is None

    def test_lineage_multi_level(self):
        """三层血缘链"""
        store = RawEventStore()
        events = [
            EventEnvelope(event_id="raw-001"),
            EventEnvelope(event_id="norm-001", parent_event_ids=["raw-001"]),
            EventEnvelope(event_id="finding-001", parent_event_ids=["norm-001", "raw-001"]),
            EventEnvelope(event_id="action-001", parent_event_ids=["finding-001", "norm-001"]),
        ]
        for e in events:
            store.append(e)

        api = LineageAPI(store)
        result = api.trace("action-001")
        assert result is not None
        assert len(result.nodes) == 4
        assert "raw-001" in [n.event_id for n in result.nodes]
