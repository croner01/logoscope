import pytest
import json
from datetime import datetime
from shared_src.context.api import ContextAPI, ContextResult, ContextType
from shared_src.context.hasher import CanonicalContextHasher
from shared_src.context.builders import IncidentContext, TopologyContext
from shared_src.context.snapshot import ContextSnapshot
from shared_src.worldview.facade import WorldView


INSTANCE = "INSTANCE"
SERVICE = "SERVICE"


class MockTopology:
    def get_dependents(self, t, n):
        return [f"HOST:{n}_host"]
    def get_dependencies(self, t, n):
        return [f"SERVICE:rabbitmq"]
    def get_impact_set(self, t, n, depth=3):
        return [[f"INSTANCE:{n}_dep1", f"INSTANCE:{n}_dep2"]]
    def query_path(self, ft, fn, tt, tn):
        return []
    def estimate_vm_count(self, t, n, depth=3):
        return 3


class MockState:
    def get_state(self, t, n):
        return "ACTIVE" if n != "faulty" else "ERROR"
    def get_states(self, entities):
        return ["ACTIVE"] * len(entities)
    def get_timeline(self, eid, window="1 HOUR"):
        return []
    def has_state_changed(self, eid, window_minutes=5):
        return False
    def resolve_field(self, fp, t, n):
        return "ACTIVE"


class MockHistory:
    def get_recent_events(self, count=50):
        return []
    def get_alarms(self):
        return [{"id": "alarm-1", "severity": "critical"}]
    def get_events_by_type(self, t):
        return []


class MockKnowledgeStore:
    def __init__(self):
        self._docs = {}
    def add_document(self, doc):
        self._docs[doc.document_id] = doc
    def retrieve(self, query):
        return list(self._docs.values())


@pytest.fixture
def worldview():
    return WorldView(topology=MockTopology(), state=MockState(), history=MockHistory())


@pytest.fixture
def context_api(worldview):
    return ContextAPI(
        worldview=worldview,
        hasher=CanonicalContextHasher(),
        knowledge_store=MockKnowledgeStore(),
    )


class TestContextAPI:
    def test_context_hash_stable(self):
        """相同输入始终产出相同 context_hash"""
        wv = WorldView(topology=MockTopology(), state=MockState(), history=MockHistory())
        api = ContextAPI(worldview=wv, hasher=CanonicalContextHasher(), knowledge_store=MockKnowledgeStore())
        r1 = api.build(SERVICE, "nova-api")
        r2 = api.build(SERVICE, "nova-api")
        assert r1.context_hash == r2.context_hash

    def test_context_result_has_projection_epoch(self):
        """ContextResult 引用 Projection epoch，不包含 data copy"""
        api = ContextAPI(
            worldview=WorldView(topology=MockTopology(), state=MockState(), history=MockHistory()),
            hasher=CanonicalContextHasher(),
            knowledge_store=MockKnowledgeStore(),
        )
        result = api.build(SERVICE, "nova-api")
        assert result.projection_epoch != ""
        # 不包含 projection 数据本身
        assert not hasattr(result, "projection_data")

    def test_context_result_knowledge_refs(self):
        """knowledge_refs 记录 [(doc_id, version), ...]"""
        store = MockKnowledgeStore()
        doc = type("Doc", (), {"document_id": "kb-001", "title": "test"})()
        store.add_document(doc)
        api = ContextAPI(
            worldview=WorldView(topology=MockTopology(), state=MockState(), history=MockHistory()),
            hasher=CanonicalContextHasher(),
            knowledge_store=store,
        )
        result = api.build(SERVICE, "nova-api")
        assert isinstance(result.knowledge_refs, list)

    def test_context_api_hides_storage(self):
        """Context API 隐藏所有存储实现"""
        api = ContextAPI(
            worldview=WorldView(topology=MockTopology(), state=MockState(), history=MockHistory()),
            hasher=CanonicalContextHasher(),
            knowledge_store=MockKnowledgeStore(),
        )
        result = api.build(SERVICE, "nova-api")
        assert not hasattr(result.context, "neo4j_query")
        assert not hasattr(result.context, "clickhouse_sql")

    def test_context_snapshot_not_default(self):
        """Snapshot 默认不生成（use_snapshot=False）"""
        wv = WorldView(topology=MockTopology(), state=MockState(), history=MockHistory())
        api = ContextAPI(worldview=wv, hasher=CanonicalContextHasher(), knowledge_store=MockKnowledgeStore())
        result = api.build(SERVICE, "nova-api")
        assert result.snapshot_id == ""

    def test_context_snapshot_when_requested(self):
        wv = WorldView(topology=MockTopology(), state=MockState(), history=MockHistory())
        api = ContextAPI(worldview=wv, hasher=CanonicalContextHasher(), knowledge_store=MockKnowledgeStore())
        result = api.build(SERVICE, "nova-api", use_snapshot=True)
        assert result.snapshot_id != ""
        snapshot = api.get_snapshot(result.snapshot_id)
        assert snapshot is not None

    def test_context_api_uses_worldview(self):
        """ContextAPI 内部使用 WorldView 查询状态"""
        wv = WorldView(topology=MockTopology(), state=MockState(), history=MockHistory())
        api = ContextAPI(worldview=wv, hasher=CanonicalContextHasher(), knowledge_store=MockKnowledgeStore())
        result = api.build(SERVICE, "nova-api")
        assert result.context is not None
