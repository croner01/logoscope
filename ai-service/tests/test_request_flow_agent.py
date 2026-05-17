"""
Tests for ai/request_flow_agent.py
"""

from datetime import datetime, timezone

from ai.request_flow_agent import RequestFlowAgent


class MockStorageAdapter:
    """Minimal storage adapter stub for request-flow agent tests."""

    def execute_query(self, query, params=None):
        if "FROM logs.logs" in query:
            return [
                {
                    "id": "log-1",
                    "timestamp": "2026-03-14 06:40:00.000000000",
                    "service_name": "api-gateway",
                    "pod_name": "api-gateway-1",
                    "namespace": "islap",
                    "level": "INFO",
                    "message": "req-abc incoming request",
                    "trace_id": "trace-001",
                    "span_id": "span-1",
                    "attributes_json": '{"request_id":"req-abc"}',
                },
                {
                    "id": "log-2",
                    "timestamp": "2026-03-14 06:40:01.000000000",
                    "service_name": "payment-service",
                    "pod_name": "payment-service-1",
                    "namespace": "islap",
                    "level": "ERROR",
                    "message": "Database connection timeout",
                    "trace_id": "trace-001",
                    "span_id": "span-2",
                    "attributes_json": '{"request_id":"req-abc"}',
                },
            ]
        if "FROM logs.traces" in query:
            return [
                {
                    "timestamp": "2026-03-14 06:40:00.000000000",
                    "trace_id": "trace-001",
                    "span_id": "span-1",
                    "parent_span_id": "",
                    "service_name": "api-gateway",
                    "operation_name": "HTTP /pay",
                    "status": "ok",
                    "duration_ms": 12.5,
                },
                {
                    "timestamp": "2026-03-14 06:40:01.000000000",
                    "trace_id": "trace-001",
                    "span_id": "span-2",
                    "parent_span_id": "span-1",
                    "service_name": "payment-service",
                    "operation_name": "SQL insert",
                    "status": "error",
                    "duration_ms": 140.0,
                },
            ]
        return []


class CaptureParamsStorageAdapter:
    """Capture SQL params for timezone-window assertions."""

    def __init__(self):
        self.logs_params = {}

    def execute_query(self, query, params=None):
        if "FROM logs.logs" in query:
            self.logs_params = dict(params or {})
            return []
        if "FROM logs.traces" in query:
            return []
        return []


class TracebackHeavyStorageAdapter:
    """Provide many info logs + one heavy traceback error log."""

    def execute_query(self, query, params=None):
        if "FROM logs.logs" in query:
            rows = []
            for idx in range(24):
                rows.append(
                    {
                        "id": f"log-info-{idx}",
                        "timestamp": f"2026-03-14 06:40:{idx:02d}.000000000",
                        "service_name": "gateway",
                        "pod_name": "gateway-1",
                        "namespace": "islap",
                        "level": "INFO",
                        "message": f"regular info log {idx}",
                        "trace_id": "",
                        "span_id": "",
                        "attributes_json": "{}",
                    }
                )
            traceback_lines = "\n".join([f"  at com.example.Service.line{line}" for line in range(1, 120)])
            rows.append(
                {
                    "id": "log-error-traceback",
                    "timestamp": "2026-03-14 06:41:30.000000000",
                    "service_name": "payment-service",
                    "pod_name": "payment-service-1",
                    "namespace": "islap",
                    "level": "ERROR",
                    "message": f"Traceback (most recent call last):\n{traceback_lines}\nException: db timeout",
                    "trace_id": "trace-xyz",
                    "span_id": "span-err",
                    "attributes_json": '{"request_id":"req-heavy"}',
                }
            )
            return rows
        if "FROM logs.traces" in query:
            return []
        return []


class TimestampMismatchOnceStorageAdapter:
    """First logs query fails with timestamp mismatch, then succeeds with cast expression."""

    def __init__(self):
        self.logs_queries = []
        self._mismatch_emitted = False

    def execute_query(self, query, params=None):
        if "FROM logs.logs" in query:
            self.logs_queries.append(str(query))
            if (
                "parseDateTime64BestEffortOrNull(toString(timestamp), 9, 'UTC')" not in query
                and not self._mismatch_emitted
            ):
                self._mismatch_emitted = True
                raise Exception(
                    "Code: 43. DB::Exception: No operation greaterOrEquals between String and DateTime64(9, 'UTC')."
                )
            return [
                {
                    "id": "log-ts-1",
                    "timestamp": "2026-03-14 06:40:00.000000000",
                    "service_name": "query-service",
                    "pod_name": "query-service-1",
                    "namespace": "islap",
                    "level": "ERROR",
                    "message": "request_id=req-ts-1 timeout",
                    "trace_id": "trace-ts-1",
                    "span_id": "span-ts-1",
                    "attributes_json": '{"request_id":"req-ts-1"}',
                }
            ]
        if "FROM logs.traces" in query:
            return []
        return []


class TestRequestFlowAgent:
    """RequestFlowAgent behavioral tests."""

    def test_prepare_analysis_input_builds_agent_context(self):
        agent = RequestFlowAgent(MockStorageAdapter())
        prepared = agent.prepare_analysis_input(
            log_content="2026-03-14T06:40:00Z ERROR req-abc payment timeout",
            service_name="api-gateway",
            context={
                "source_log_timestamp": "2026-03-14T06:40:00Z",
            },
        )

        assert prepared.context["agent_mode"] == "request_flow"
        assert prepared.context["request_id"] == "req-abc"
        assert prepared.context["trace_id"] == "trace-001"
        assert prepared.request_flow["trace_span_count"] == 2
        assert len(prepared.related_logs) == 2
        assert "[agent-request-flow-summary]" in prepared.log_content

    def test_augment_result_fills_missing_data_flow(self):
        agent = RequestFlowAgent(MockStorageAdapter())
        prepared = agent.prepare_analysis_input(
            log_content="req-abc fail",
            service_name="api-gateway",
            context={
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
        raw_result = {
            "problem_type": "dependency",
            "severity": "high",
            "summary": "downstream timeout",
        }

        augmented = agent.augment_result(raw_result, prepared)

        assert "data_flow" in augmented
        assert augmented["data_flow"]["summary"]
        assert "agent" in augmented
        assert augmented["agent"]["mode"] == "request_flow"

    def test_prepare_analysis_input_applies_context_timezone_for_naive_timestamp(self):
        storage = CaptureParamsStorageAdapter()
        agent = RequestFlowAgent(storage)
        prepared = agent.prepare_analysis_input(
            log_content="2026-03-14 14:40:00 ERROR req-timezone failure",
            service_name="query-service",
            context={
                "timestamp": "2026-03-14 14:40:00",
                "input_timezone": "Asia/Shanghai",
            },
        )

        assert storage.logs_params.get("start_time", "").startswith("2026-03-14 06:35:00")
        assert storage.logs_params.get("end_time", "").startswith("2026-03-14 06:45:00")
        assert str(prepared.context.get("request_flow_input_timezone", "")).lower().find("asia") >= 0

    def test_prepare_analysis_input_prioritizes_traceback_error_logs(self):
        agent = RequestFlowAgent(TracebackHeavyStorageAdapter())
        agent.log_inject_limit = 5
        agent.log_raw_limit = 5
        prepared = agent.prepare_analysis_input(
            log_content="req-heavy failed",
            service_name="gateway",
            context={
                "source_log_timestamp": "2026-03-14T06:41:00Z",
            },
        )

        related_messages = [
            str(item.get("message", ""))
            for item in prepared.context.get("agent_related_logs", [])
            if isinstance(item, dict)
        ]
        assert any("Traceback" in message for message in related_messages)
        assert any("truncated traceback" in message for message in related_messages)

    def test_logs_query_tool_switches_timestamp_expression_after_mismatch(self, monkeypatch):
        monkeypatch.delenv("AI_AGENT_QUERY_API_BASE", raising=False)
        storage = TimestampMismatchOnceStorageAdapter()
        agent = RequestFlowAgent(storage)

        first_prepared = agent.prepare_analysis_input(
            log_content="2026-03-14T06:40:00Z ERROR req-ts-1 timeout",
            service_name="query-service",
            context={"source_log_timestamp": "2026-03-14T06:40:00Z"},
        )
        assert len(first_prepared.related_logs) >= 1
        assert any("parseDateTime64BestEffortOrNull(toString(timestamp), 9, 'UTC')" in query for query in storage.logs_queries)

        first_query_count = len(storage.logs_queries)
        second_prepared = agent.prepare_analysis_input(
            log_content="2026-03-14T06:41:00Z ERROR req-ts-1 timeout again",
            service_name="query-service",
            context={"source_log_timestamp": "2026-03-14T06:41:00Z"},
        )
        assert len(second_prepared.related_logs) >= 1
        second_round_queries = storage.logs_queries[first_query_count:]
        assert second_round_queries
        assert all("parseDateTime64BestEffortOrNull(toString(timestamp), 9, 'UTC')" in query for query in second_round_queries)

    def test_prepare_analysis_input_supports_manual_context_window(self):
        agent = RequestFlowAgent(MockStorageAdapter())
        prepared = agent.prepare_analysis_input(
            log_content="2026-03-14T06:40:00Z ERROR req-abc payment timeout",
            service_name="api-gateway",
            context={
                "source_log_timestamp": "2026-03-14T06:40:00Z",
                "pull_mode": "manual_context",
                "manual_before": 0,
                "manual_after": 0,
            },
        )

        assert len(prepared.related_logs) == 1
        assert prepared.related_logs[0]["id"] in {"log-1", "log-2"}

    def test_prepare_analysis_input_marks_partial_integrity(self):
        agent = RequestFlowAgent(MockStorageAdapter())
        prepared = agent.prepare_analysis_input(
            log_content="2026-03-14T06:40:00Z ERROR req-abc payment timeout",
            service_name="api-gateway",
            context={
                "source_log_timestamp": "2026-03-14T06:40:00Z",
                "expected_components": ["api-gateway", "payment-service", "mariadb"],
                "allow_partial": False,
            },
        )

        integrity = prepared.context.get("log_integrity", {})
        assert integrity.get("partial") is True
        assert "mariadb" in integrity.get("missing_components", [])
        assert integrity.get("next_action") == "repull_required"
