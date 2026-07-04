"""Tests for WorkflowEngine — 操作类型检测、步骤重建、持久化。"""
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from semantic_engine.workflow.engine import (
    WorkflowEngine,
    _HTTP_RE,
    _execution_id,
    _calc_duration_ms,
    _path_normalize,
    _detect_action_from_keywords,
    _level_severity,
)


class MockClickHouseClient:
    """模拟 ClickHouse 原生客户端。"""

    def __init__(self):
        self.executed_queries: list = []
        self.rows: dict = {}

    def execute(self, query, params=None, settings=None):
        condensed = " ".join(query.split()) if isinstance(query, str) else str(query)[:150]
        self.executed_queries.append((condensed, params))
        if "CREATE TABLE" in condensed:
            return []
        if "count() AS cnt" in condensed:
            return [{"cnt": 0}]
        return []


class MockStorage:
    """模拟 StorageAdapter。"""

    def __init__(self):
        self.ch_client = MockClickHouseClient()

    def execute_query(self, query, params=None):
        if "count() AS cnt" in query:
            return [{"cnt": 0}]
        if "SELECT" in query:
            return []
        return []


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def engine():
    """创建一个可用的 WorkflowEngine 实例。"""
    storage = MockStorage()
    eng = WorkflowEngine(storage)
    eng._ch_available = True
    return eng


def _ts(iso_str: str) -> datetime:
    """将 ISO 字符串转为 timezone-aware datetime。"""
    return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))


def _row(
    service_name: str,
    timestamp: str,
    level: str = "INFO",
    message: str = "",
    global_request_id: str = "test-rid-001",
    request_id: str = "req-xxx",
    source_cluster: str = "prod",
) -> dict:
    return {
        "service_name": service_name,
        "openstack_request_id": request_id,
        "openstack_global_request_id": global_request_id,
        "timestamp": _ts(timestamp),
        "level": level,
        "message": message,
        "source_cluster": source_cluster,
    }


# ── 工具函数测试 ────────────────────────────────────────────────────────────


class TestExecutionId:
    def test_deterministic(self):
        assert _execution_id("abc123") == _execution_id("abc123")

    def test_different_inputs(self):
        assert _execution_id("abc") != _execution_id("xyz")

    def test_length(self):
        assert len(_execution_id("anything")) == 16


class TestCalcDurationMs:
    def test_same_time(self):
        t = _ts("2026-07-03T10:00:00.000Z")
        assert _calc_duration_ms(t, t) == 0

    def test_one_second(self):
        start = _ts("2026-07-03T10:00:00.000Z")
        end = _ts("2026-07-03T10:00:01.000Z")
        assert _calc_duration_ms(start, end) == 1000

    def test_string_inputs(self):
        assert _calc_duration_ms("2026-07-03T10:00:00Z", "2026-07-03T10:00:05Z") == 5000

    def test_reverse_returns_zero(self):
        start = _ts("2026-07-03T10:00:05.000Z")
        end = _ts("2026-07-03T10:00:00.000Z")
        assert _calc_duration_ms(start, end) == 0


class TestPathNormalize:
    def test_replace_uuid_in_path(self):
        assert _path_normalize("/v2.1/a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4/servers") == "/v2.1/{id}/servers"

    def test_short_path_unchanged(self):
        assert _path_normalize("/health") == "/health"

    def test_multiple_uuids(self):
        result = _path_normalize("/v2.1/a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4/servers/b1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4")
        assert "/{id}" in result
        assert result.count("{id}") == 2


class TestLevelSeverity:
    def test_error_above_info(self):
        assert _level_severity("ERROR") > _level_severity("INFO")

    def test_fatal_equals_critical(self):
        assert _level_severity("FATAL") == _level_severity("CRITICAL")


# ── HTTP 消息解析 ───────────────────────────────────────────────────────────


class TestHttpRegex:
    def test_match_post(self):
        m = _HTTP_RE.search('10.0.0.1 "POST /v2.1/abc123/servers HTTP/1.1" status: 202')
        assert m is not None
        assert m.group(1) == "POST"
        assert "/servers" in m.group(2)

    def test_match_get(self):
        m = _HTTP_RE.search('"GET /v2.1/abc123/os-services HTTP/1.1" status: 200')
        assert m is not None
        assert m.group(1) == "GET"

    def test_no_match_raw_log(self):
        """非 HTTP 日志不匹配。"""
        assert _HTTP_RE.search("nova-compute: Spawning VM instance") is None


# ── 操作类型检测 ─────────────────────────────────────────────────────────────


class TestDetectOperationType:
    def test_create_vm(self, engine):
        seq = [
            _row("nova-api-osapi", "2026-07-03T10:00:00Z",
                 message='10.0.0.1 "POST /v2.1/abc123/servers HTTP/1.1" status: 202'),
            _row("nova-scheduler", "2026-07-03T10:00:01Z",
                 message="nova.scheduler.host_manager HostManager: selected host node-1"),
            _row("nova-compute", "2026-07-03T10:00:03Z",
                 message="nova.compute.manager [instance: abc-def-123] Creating server"),
        ]
        assert engine._detect_operation_type(seq) == "CreateVM"

    def test_delete_vm(self, engine):
        seq = [
            _row("nova-api-osapi", "2026-07-03T10:00:00Z",
                 message='"DELETE /v2.1/abc123/servers/i-abcd1234 HTTP/1.1" status: 204'),
            _row("nova-compute", "2026-07-03T10:00:02Z",
                 message="nova.compute.manager Destroying instance"),
        ]
        assert engine._detect_operation_type(seq) == "DeleteVM"

    def test_create_volume(self, engine):
        seq = [
            _row("cinder-api", "2026-07-03T10:00:00Z",
                 message='"POST /v2.1/abc123/volumes HTTP/1.1" status: 202'),
        ]
        assert engine._detect_operation_type(seq) == "CreateVolume"

    def test_live_migrate_from_keyword(self, engine):
        """ServerAction 从 message 关键词细分"""
        seq = [
            _row("nova-api-osapi", "2026-07-03T10:00:00Z",
                 message='10.0.0.1 "POST /v2.1/abc123/servers/i-xyz/action HTTP/1.1" status: 202 os-migrateLive'),
            _row("nova-conductor", "2026-07-03T10:00:01Z",
                 message="migrate_server: starting live migration"),
            _row("nova-compute", "2026-07-03T10:00:05Z",
                 message="nova.compute.manager Live migration started"),
        ]
        assert engine._detect_operation_type(seq) == "LiveMigrate"

    def test_attach_volume(self, engine):
        seq = [
            _row("cinder-api", "2026-07-03T10:00:00Z",
                 message='10.0.0.1 "POST /v2.1/abc123/servers/i-xyz/action HTTP/1.1" attach volume'),
        ]
        assert engine._detect_operation_type(seq) == "AttachVolume"

    def test_unknown_no_http(self, engine):
        """没有 HTTP 模式时返回 Unknown"""
        seq = [
            _row("rabbitmq", "2026-07-03T10:00:00Z",
                 message="RabbitMQ RPC call from nova to cinder"),
            _row("cinder-volume", "2026-07-03T10:00:01Z",
                 message="cinder.volume.manager Creating volume"),
        ]
        assert engine._detect_operation_type(seq) == "Unknown"


# ── 资源 ID 检测 ────────────────────────────────────────────────────────────


class TestDetectResourceId:
    def test_from_http_path_server(self, engine):
        seq = [
            _row("nova-api-osapi", "2026-07-03T10:00:00Z",
                 message='10.0.0.1 "DELETE /v2.1/abc123/servers/abcd1234-e5f6-7890-abcd-ef1234567890 HTTP/1.1" status: 204'),
        ]
        rid = engine._detect_resource_id(seq)
        assert "abcd1234-e5f6-7890-abcd-ef1234567890" in rid

    def test_from_instance_ref(self, engine):
        seq = [
            _row("nova-compute", "2026-07-03T10:00:00Z",
                 message="[instance: abcd1234-e5f6-7890-abcd-ef1234567890] Starting instance"),
        ]
        assert engine._detect_resource_id(seq) == "abcd1234-e5f6-7890-abcd-ef1234567890"

    def test_empty_when_not_found(self, engine):
        seq = [
            _row("rabbitmq", "2026-07-03T10:00:00Z", message="heartbeat"),
        ]
        assert engine._detect_resource_id(seq) == ""


# ── 状态检测 ────────────────────────────────────────────────────────────────


class TestDetectStatus:
    def test_success(self, engine):
        seq = [_row("nova-api", "2026-07-03T10:00:00Z", level="INFO")]
        assert engine._detect_status(seq) == ("success", "")

    def test_failed_on_error(self, engine):
        seq = [
            _row("nova-api", "2026-07-03T10:00:00Z", level="INFO"),
            _row("nova-compute", "2026-07-03T10:00:01Z", level="ERROR",
                 message="nova.compute.manager Failed to spawn instance"),
        ]
        status, msg = engine._detect_status(seq)
        assert status == "failed"
        assert "Failed to spawn" in msg

    def test_warning(self, engine):
        seq = [
            _row("nova-api", "2026-07-03T10:00:00Z", level="INFO"),
            _row("nova-compute", "2026-07-03T10:00:01Z", level="WARN"),
        ]
        assert engine._detect_status(seq) == ("success_with_warnings", "")


# ── 步骤序列重建 ────────────────────────────────────────────────────────────


class TestBuildSteps:
    def test_three_steps(self, engine):
        seq = [
            _row("nova-api", "2026-07-03T10:00:00Z",
                 message='10.0.0.1 "POST /v2.1/abc/servers HTTP/1.1"'),
            _row("nova-scheduler", "2026-07-03T10:00:01Z",
                 message="nova.scheduler Selected host"),
            _row("nova-compute", "2026-07-03T10:00:03Z",
                 message="Spawning instance"),
        ]
        steps = engine._build_steps(seq)
        assert len(steps) == 3
        assert steps[0]["service_name"] == "nova-api"
        assert steps[1]["service_name"] == "nova-scheduler"
        assert steps[2]["service_name"] == "nova-compute"
        # 步骤间有持续时间差
        assert steps[0]["duration_ms"] >= 900  # ~1000ms
        assert steps[1]["duration_ms"] >= 1900  # ~2000ms

    def test_single_step_not_reached(self, engine):
        """序列少于 2 个时不会被调用到（由 reconstruct 拦截）。"""
        seq = [_row("nova-api", "2026-07-03T10:00:00Z")]
        steps = engine._build_steps(seq)
        assert len(steps) == 1

    def test_step_status_propagation(self, engine):
        seq = [
            _row("nova-api", "2026-07-03T10:00:00Z", level="INFO"),
            _row("nova-compute", "2026-07-03T10:00:05Z", level="ERROR"),
        ]
        steps = engine._build_steps(seq)
        assert steps[0]["status"] == "success"
        assert steps[1]["status"] == "failed"

    def test_action_extraction(self, engine):
        seq = [
            _row("nova-api", "2026-07-03T10:00:00Z",
                 message='10.0.0.1 "POST /v2.1/abc/servers HTTP/1.1"'),
            _row("rabbitmq", "2026-07-03T10:00:01Z",
                 message="RPC call"),
        ]
        steps = engine._build_steps(seq)
        assert steps[0]["action"] == "POST servers"
        assert steps[1]["action"] == "RPC"


# ── 序列去重 ────────────────────────────────────────────────────────────────


class TestDedupServiceSequence:
    def test_removes_consecutive_duplicates(self, engine):
        rows = [
            _row("nova-api", "2026-07-03T10:00:00Z"),
            _row("nova-api", "2026-07-03T10:00:01Z"),
            _row("nova-scheduler", "2026-07-03T10:00:02Z"),
            _row("nova-api", "2026-07-03T10:00:03Z"),
        ]
        seq = engine._dedup_service_sequence(rows)
        # nova-api → nova-scheduler → nova-api (3 unique transitions)
        assert len(seq) == 3
        assert seq[0]["service_name"] == "nova-api"
        assert seq[1]["service_name"] == "nova-scheduler"
        assert seq[2]["service_name"] == "nova-api"

    def test_empty_input(self, engine):
        assert engine._dedup_service_sequence([]) == []

    def test_single_record(self, engine):
        assert len(engine._dedup_service_sequence([_row("nova-api", "2026-07-03T10:00:00Z")])) == 1


# ── Workflow 完整重建 ────────────────────────────────────────────────────────


class TestReconstructWorkflow:
    def test_full_reconstruction(self, engine):
        rows = [
            _row("nova-api-osapi", "2026-07-03T10:00:00Z",
                 message='10.0.0.1 "POST /v2.1/abc123/servers HTTP/1.1" status: 202',
                 level="INFO"),
            _row("nova-scheduler", "2026-07-03T10:00:01Z", level="INFO",
                 message="nova.scheduler.host_manager Selected host node-1"),
            _row("nova-compute", "2026-07-03T10:00:03Z", level="INFO",
                 message="nova.compute.manager [instance: abcd1234e5f67890abcd1234e5f67890] Spawning instance"),
            _row("neutron-server", "2026-07-03T10:00:06Z", level="INFO",
                 message="neutron.wsgi Binding port"),
            _row("nova-compute", "2026-07-03T10:00:08Z", level="INFO",
                 message="nova.compute.manager Instance spawned successfully"),
        ]
        wf = engine._reconstruct_workflow(
            global_request_id="test-rid",
            execution_id=_execution_id("test-rid"),
            records=rows,
        )
        assert wf is not None
        assert wf["operation_type"] == "CreateVM"
        assert wf["status"] == "success"
        assert wf["execution_id"] == _execution_id("test-rid")
        assert wf["resource_id"] == "abcd1234e5f67890abcd1234e5f67890"
        assert wf["step_count"] >= 4  # nova-api → scheduler → compute → neutron
        assert wf["duration_ms"] >= 8000
        assert wf["source_cluster"] == "prod"

    def test_skips_short_sequences(self, engine):
        """只有 1 个服务 → 返回 None。"""
        rows = [_row("nova-api", "2026-07-03T10:00:00Z")]
        wf = engine._reconstruct_workflow("rid", "eid", rows)
        assert wf is None

    def test_failed_workflow(self, engine):
        rows = [
            _row("nova-api-osapi", "2026-07-03T10:00:00Z",
                 message='"POST /v2.1/abc/servers HTTP/1.1"', level="INFO"),
            _row("nova-scheduler", "2026-07-03T10:00:01Z", level="INFO"),
            _row("nova-compute", "2026-07-03T10:00:03Z", level="ERROR",
                 message="nova.compute.manager Failed to spawn: No host available"),
        ]
        wf = engine._reconstruct_workflow("rid", "eid", rows)
        assert wf is not None
        assert wf["status"] == "failed"
        assert "No host available" in wf["error_message"]


# ── 分组逻辑 ────────────────────────────────────────────────────────────────


class TestGroupByGlobalRequestId:
    def test_groups_correctly(self, engine):
        rows = [
            _row("nova-api", "2026-07-03T10:00:00Z", global_request_id="abc"),
            _row("nova-scheduler", "2026-07-03T10:00:01Z", global_request_id="abc"),
            _row("cinder-api", "2026-07-03T10:00:00Z", global_request_id="xyz"),
        ]
        groups = engine._group_by_global_request_id(rows)
        assert len(groups) == 2
        assert len(groups["abc"]) == 2
        assert len(groups["xyz"]) == 1


# ── 持久化 ───────────────────────────────────────────────────────────────────


class TestSaveWorkflow:
    def test_save_success(self, engine):
        wf = {
            "execution_id": "test123",
            "operation_type": "CreateVM",
            "resource_id": "i-abc",
            "global_request_id": "rid-001",
            "status": "success",
            "started_at": _ts("2026-07-03T10:00:00Z"),
            "finished_at": _ts("2026-07-03T10:00:08Z"),
            "duration_ms": 8000,
            "error_message": "",
            "source_cluster": "prod",
            "step_count": 3,
            "steps.service_name": ["nova-api", "nova-scheduler", "nova-compute"],
            "steps.action": ["POST servers", "schedule", "spawn"],
            "steps.started_at": [
                _ts("2026-07-03T10:00:00Z"),
                _ts("2026-07-03T10:00:01Z"),
                _ts("2026-07-03T10:00:03Z"),
            ],
            "steps.duration_ms": [1000, 2000, 5000],
            "steps.status": ["success", "success", "success"],
            "steps.level": ["INFO", "INFO", "INFO"],
        }
        assert engine._save_workflow(wf) is True
        # 验证 INSERT 被调用
        ch = engine.ch_client
        insert_calls = [q for q, _ in ch.executed_queries if "INSERT INTO" in q]
        assert len(insert_calls) > 0


class TestBuildWorkflows:
    def test_no_data(self, engine):
        """无数据时返回空计数。"""
        result = engine.build_workflows(since_hours=6)
        assert result["built"] == 0
        assert result["errors"] == 0
        assert result["scanned_requests"] == 0

    def test_clickhouse_not_available(self, engine):
        engine._ch_available = False
        result = engine.build_workflows(since_hours=6)
        assert result["built"] == 0


# ── API 路径模式验证 ────────────────────────────────────────────────────────


class TestHttpPathPatterns:
    """验证关键 OpenStack API 路径的模式匹配（直接测试 _OPERATION_PATTERNS）。"""

    def _check_operation(self, method: str, path: str) -> str:
        """辅助: 对 method+path 运行 _OPERATION_PATTERNS 返回匹配的操作类型。"""
        from semantic_engine.workflow.engine import _OPERATION_PATTERNS
        for pattern_fn, op_type in _OPERATION_PATTERNS:
            if pattern_fn(method, path):
                return op_type
        return "Unknown"

    def test_create_vm_matches(self):
        assert self._check_operation("POST", "/v2.1/abc123def456abc123def456abc123def456/servers") == "CreateVM"
        assert self._check_operation("POST", "/v2.1/abc123/servers") == "CreateVM"

    def test_create_vm_not_matched(self):
        assert self._check_operation("GET", "/v2.1/abc123/servers") != "CreateVM"
        assert self._check_operation("POST", "/v2.1/abc123/servers/foo/action") != "CreateVM"
        assert self._check_operation("POST", "/v2.1/abc123/volumes") != "CreateVM"

    def test_delete_vm(self):
        assert self._check_operation("DELETE", "/v2.1/abc123/servers/i-xyz") == "DeleteVM"

    def test_create_volume(self):
        assert self._check_operation("POST", "/v2.1/abc123/volumes") == "CreateVolume"

    def test_delete_volume(self):
        assert self._check_operation("DELETE", "/v2.1/abc123/volumes/v-xyz") == "DeleteVolume"

    def test_server_action(self):
        assert self._check_operation("POST", "/v2.1/abc123/servers/i-xyz/action") == "ServerAction"

    def test_create_image(self):
        assert self._check_operation("POST", "/v2.1/abc123/images") == "CreateImage"


# ── 关键词检测 ──────────────────────────────────────────────────────────────


class TestDetectActionFromKeywords:
    def test_migrate(self):
        assert _detect_action_from_keywords("Live migration of server") == "LiveMigrate"

    def test_attach_volume(self):
        assert _detect_action_from_keywords("Attach volume to server") == "AttachVolume"

    def test_none(self):
        assert _detect_action_from_keywords("Heartbeat check") == ""
