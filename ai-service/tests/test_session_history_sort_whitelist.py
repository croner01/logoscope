"""
session_history 排序白名单测试
"""
from ai.session_history import AISessionStore


class _FakeCHClient:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return []


class _FakeStorage:
    def __init__(self):
        self.ch_client = _FakeCHClient()
        self.ch_database = "logs"
        self.config = {"clickhouse": {"database": "logs"}}


def _normalize_sql(sql: str) -> str:
    return " ".join(str(sql).split())


def test_list_sessions_clickhouse_order_by_whitelist_blocks_injection():
    storage = _FakeStorage()
    store = AISessionStore(storage_adapter=storage)

    store.list_sessions(
        limit=5,
        offset=0,
        sort_by="updated_at DESC; DROP TABLE logs.ai_analysis_sessions --",
        sort_order="asc; DROP TABLE logs.logs --",
        pinned_first=True,
    )

    sql = _normalize_sql(storage.ch_client.calls[-1][0])
    assert "DROP TABLE" not in sql.upper()
    assert "ORDER BY is_pinned DESC, updated_at DESC, session_id DESC" in sql


def test_list_sessions_clickhouse_order_by_uses_allowed_field_and_direction():
    storage = _FakeStorage()
    store = AISessionStore(storage_adapter=storage)

    store.list_sessions(
        limit=5,
        offset=0,
        sort_by="title",
        sort_order="asc",
        pinned_first=False,
    )

    sql = _normalize_sql(storage.ch_client.calls[-1][0])
    assert "ORDER BY title ASC, updated_at DESC, session_id DESC" in sql


def test_list_sessions_memory_fallback_respects_sort_whitelist():
    store = AISessionStore(storage_adapter=None)

    store.create_session(
        analysis_type="log",
        service_name="svc-a",
        input_text="input-a",
        session_id="ais-3",
        title="zeta",
    )
    store.create_session(
        analysis_type="log",
        service_name="svc-b",
        input_text="input-b",
        session_id="ais-2",
        title="beta",
    )
    store.create_session(
        analysis_type="log",
        service_name="svc-c",
        input_text="input-c",
        session_id="ais-1",
        title="alpha",
    )

    sessions = store.list_sessions(
        limit=10,
        offset=0,
        sort_by="title",
        sort_order="asc",
        pinned_first=False,
    )

    assert [item.session_id for item in sessions] == ["ais-1", "ais-2", "ais-3"]
