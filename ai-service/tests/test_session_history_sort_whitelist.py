"""
session_history 排序白名单测试
"""
import time
from datetime import datetime, timezone

from ai.session_history import AISession, AISessionMessage, AISessionStore, MESSAGE_METADATA_MAX_CHARS


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


def test_list_sessions_with_total_clickhouse_uses_single_query_total_count():
    storage = _FakeStorage()
    store = AISessionStore(storage_adapter=storage)

    storage.ch_client.calls.clear()

    now = datetime.now(timezone.utc)
    storage.ch_client.execute = lambda sql, params=None: [  # type: ignore[method-assign]
        (
            "ais-1",
            "log",
            "title",
            "svc-a",
            "input",
            "",
            "",
            "{}",
            "{}",
            "rule",
            "",
            "",
            "ai-analysis",
            "completed",
            now,
            now,
            0,
            0,
            0,
            7,
        )
    ]

    sessions, total_all = store.list_sessions_with_total(limit=5, offset=0)

    assert len(sessions) == 1
    assert sessions[0].session_id == "ais-1"
    assert total_all == 7


def test_get_message_counts_uses_cached_deleted_ids_without_per_session_query():
    class _MessageCountClient(_FakeCHClient):
        def execute(self, sql, params=None):
            self.calls.append((sql, params))
            normalized = _normalize_sql(sql).lower()
            if "from system.tables" in normalized:
                return [(1,)]
            if "from logs.ai_analysis_messages" in normalized:
                return [("ais-1", 5), ("ais-2", 3)]
            if "from logs.v_ai_analysis_sessions_latest" in normalized and "context_json" in normalized:
                raise AssertionError("should not query session context when cache already has sessions")
            return []

    storage = _FakeStorage()
    storage.ch_client = _MessageCountClient()
    store = AISessionStore(storage_adapter=storage)

    store._sessions["ais-1"] = AISession(
        session_id="ais-1",
        analysis_type="log",
        context={"deleted_message_ids": ["msg-1"]},
    )
    store._sessions["ais-2"] = AISession(
        session_id="ais-2",
        analysis_type="log",
        context={},
    )
    storage.ch_client.calls.clear()

    counts = store.get_message_counts(["ais-1", "ais-2"])

    assert counts["ais-1"] == 4
    assert counts["ais-2"] == 3


def test_session_read_source_cache_refreshes_after_ttl():
    store = AISessionStore(storage_adapter=None)
    store.session_table = "logs.ai_analysis_sessions"
    store.session_latest_view = "logs.v_ai_analysis_sessions_latest"
    store._read_source_cache_ttl_seconds = 1

    store._table_exists = lambda table_name: False  # type: ignore[method-assign]
    first_source = store._get_session_read_source()
    assert first_source == ("logs.ai_analysis_sessions", True)

    store._table_exists = lambda table_name: True  # type: ignore[method-assign]
    cached_source = store._get_session_read_source()
    assert cached_source == ("logs.ai_analysis_sessions", True)

    store._session_read_source_cache_checked_at = time.time() - 5
    refreshed_source = store._get_session_read_source()
    assert refreshed_source == ("logs.v_ai_analysis_sessions_latest", False)


def test_get_messages_light_avoids_reading_metadata_column():
    class _LightClient(_FakeCHClient):
        def execute(self, sql, params=None):
            self.calls.append((sql, params))
            normalized = _normalize_sql(sql).lower()
            if "from system.tables" in normalized:
                return [(1,)]
            if "from logs.ai_analysis_messages" in normalized:
                return [
                    ("ais-1", "msg-1", 0, "user", "hello", "", datetime.now(timezone.utc)),
                    ("ais-1", "msg-2", 1, "assistant", "world", "", datetime.now(timezone.utc)),
                ]
            return []

    storage = _FakeStorage()
    storage.ch_client = _LightClient()
    store = AISessionStore(storage_adapter=storage)
    storage.ch_client.calls.clear()

    messages = store.get_messages_light("ais-1", limit=20)

    assert len(messages) == 2
    assert messages[0].metadata == {}
    sql = ""
    for raw_sql, _params in storage.ch_client.calls:
        normalized = _normalize_sql(raw_sql).lower()
        if "from logs.ai_analysis_messages" in normalized:
            sql = normalized
            break
    assert sql
    assert "'' as metadata_json" in sql
    assert "metadata_json" not in sql.split("from")[0].replace("'' as metadata_json", "")


def test_get_session_clickhouse_point_lookup_uses_base_table_final():
    class _SessionLookupClient(_FakeCHClient):
        def execute(self, sql, params=None):
            self.calls.append((sql, params))
            normalized = _normalize_sql(sql).lower()
            if "from system.tables" in normalized:
                return [(1,)]
            if "from logs.v_ai_analysis_sessions_latest" in normalized:
                raise AssertionError("point lookup should not use latest session view")
            if "from logs.ai_analysis_sessions final" in normalized and "where session_id = %(session_id)s" in normalized:
                now = datetime.now(timezone.utc)
                return [
                    (
                        "ais-1",
                        "log",
                        "title",
                        "svc-a",
                        "input",
                        "",
                        "",
                        "{}",
                        "{}",
                        "rule",
                        "",
                        "",
                        "ai-analysis",
                        "completed",
                        now,
                        now,
                        0,
                        0,
                        0,
                    )
                ]
            return []

    storage = _FakeStorage()
    storage.ch_client = _SessionLookupClient()
    store = AISessionStore(storage_adapter=storage)
    storage.ch_client.calls.clear()

    session = store.get_session("ais-1")

    assert session is not None
    assert session.session_id == "ais-1"
    lookup_sql = _normalize_sql(storage.ch_client.calls[-1][0]).lower()
    assert "from logs.ai_analysis_sessions final" in lookup_sql
    assert "from logs.v_ai_analysis_sessions_latest" not in lookup_sql


def test_get_session_clickhouse_lookup_failure_returns_none_without_raise():
    class _SessionLookupFailureClient(_FakeCHClient):
        def execute(self, sql, params=None):
            self.calls.append((sql, params))
            normalized = _normalize_sql(sql).lower()
            if "from system.tables" in normalized:
                return [(1,)]
            if "from logs.ai_analysis_sessions final" in normalized:
                raise RuntimeError("Code: 241. memory limit exceeded")
            return []

    storage = _FakeStorage()
    storage.ch_client = _SessionLookupFailureClient()
    store = AISessionStore(storage_adapter=storage)

    session = store.get_session("ais-memory-pressure")

    assert session is None


def test_message_metadata_compaction_keeps_payload_bounded():
    store = AISessionStore(storage_adapter=None)
    oversized = "x" * 200000
    raw_metadata = {
        "react_loop": {"summary": "need replan", "replan": {"needed": True}},
        "action_observations": [
            {
                "status": "failed",
                "command": "kubectl logs deploy/query-service -n islap --tail=5000",
                "stdout": oversized,
                "stderr": oversized,
                "message": oversized,
            }
        ],
        "thoughts": [{"phase": "thought", "title": "t", "detail": oversized}],
    }
    rows = store._build_message_rows(  # noqa: SLF001
        [
            AISessionMessage(
                session_id="ais-1",
                message_id="msg-1",
                msg_index=1,
                role="assistant",
                content="answer",
                metadata=raw_metadata,
                created_at="2026-03-21T00:00:00Z",
            )
        ]
    )
    metadata_json = rows[0]["metadata_json"]
    assert isinstance(metadata_json, str)
    assert len(metadata_json) <= MESSAGE_METADATA_MAX_CHARS + 256
    assert "stdout_preview" in metadata_json
    assert "\"stdout\":" not in metadata_json
