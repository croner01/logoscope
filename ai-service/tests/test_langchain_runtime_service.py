"""
Tests for ai.langchain_runtime.service
"""

import asyncio

from ai.langchain_runtime.schemas import ActionItem, StructuredAnswer
from ai.langchain_runtime.service import (
    _extract_structured_actions,
    _normalize_action_command,
    _sanitize_json_like_answer,
    run_followup_langchain,
)


class DummyStreamingLLM:
    """简单的流式 LLM stub。"""

    def __init__(self, chunks):
        self._chunks = chunks

    async def chat_stream(self, message, context=None):
        for chunk in self._chunks:
            yield chunk

    async def chat(self, message, context=None):
        return "".join(self._chunks)


def _build_runtime_kwargs(llm_service):
    return {
        "question": "给出排查步骤",
        "analysis_context": {"service_name": "query-service"},
        "compacted_history": [],
        "compacted_summary": "",
        "references": [],
        "subgoals": [],
        "reflection": {},
        "long_term_memory": {},
        "llm_enabled": True,
        "llm_requested": True,
        "token_budget": 12000,
        "token_warning": False,
        "llm_timeout_seconds": 20,
        "llm_service": llm_service,
        "fallback_builder": lambda *args, **kwargs: "fallback",
        "llm_first_token_timeout_seconds": 5,
    }


def test_run_followup_langchain_disables_raw_json_token_stream_by_default(monkeypatch):
    """结构化 JSON 默认不应直接流到前端回答气泡。"""
    monkeypatch.delenv("AI_FOLLOWUP_LANGCHAIN_STREAM_RAW_TOKENS", raising=False)
    llm_service = DummyStreamingLLM(
        [
            '{"conclusion":"query-service 需要先检查连接池",',
            '"actions":[{"priority":1,"title":"查看日志","action":"查看日志","command":"kubectl logs deploy/query-service -n islap --tail=20","expected_outcome":"确认是否持续报错"}],',
            '"summary":"优先确认错误是否持续"}',
        ]
    )
    streamed_chunks = []

    async def _run():
        return await run_followup_langchain(
            **_build_runtime_kwargs(llm_service),
            stream_token_callback=streamed_chunks.append,
        )

    result = asyncio.run(_run())

    assert result["analysis_method"] == "langchain"
    assert streamed_chunks == []
    assert "结论：" in result["answer"]
    assert "查看日志" in result["answer"]
    assert isinstance(result.get("actions"), list)
    assert len(result.get("actions") or []) == 1


def test_run_followup_langchain_can_stream_raw_tokens_when_explicitly_enabled(monkeypatch):
    """仅在显式调试开关开启时，才转发原始 token。"""
    monkeypatch.setenv("AI_FOLLOWUP_LANGCHAIN_STREAM_RAW_TOKENS", "true")
    llm_service = DummyStreamingLLM(
        [
            '{"conclusion":"需要检查 query-service",',
            '"summary":"done"}',
        ]
    )
    streamed_chunks = []

    async def _run():
        return await run_followup_langchain(
            **_build_runtime_kwargs(llm_service),
            stream_token_callback=streamed_chunks.append,
        )

    result = asyncio.run(_run())

    assert result["analysis_method"] == "langchain"
    assert len(streamed_chunks) == 2
    assert "".join(streamed_chunks).startswith('{"conclusion"')


def test_run_followup_langchain_sanitizes_json_like_answer_when_parse_failed(monkeypatch):
    monkeypatch.setattr("ai.langchain_runtime.service._parse_structured_answer", lambda _content: None)
    llm_service = DummyStreamingLLM(
        [
            "```json\n",
            '{"conclusion":"query-service 存在慢查询","summary":"先补充 trace_id/request_id","missing_evidence":["trace_id"]}',
            "\n```",
        ]
    )

    async def _run():
        return await run_followup_langchain(
            **_build_runtime_kwargs(llm_service),
            stream_token_callback=None,
        )

    result = asyncio.run(_run())

    assert result["analysis_method"] == "langchain"
    assert "```json" not in result["answer"]
    assert "结论：" in result["answer"]
    assert "trace_id" in result["answer"]
    assert result.get("actions") == []


def test_run_followup_langchain_does_not_render_unstructured_glued_command_in_answer():
    llm_service = DummyStreamingLLM(
        [
            '{"conclusion":"需要先确认 clickhouse pod",'
            '"actions":[{"priority":1,"title":"探索数据库 pod","action":"列出 pod","command":"kubectlgetpods -n islap -l app=clickhouse","expected_outcome":"拿到 pod 列表"}],'
            '"summary":"先补证据"}',
        ]
    )

    async def _run():
        return await run_followup_langchain(
            **_build_runtime_kwargs(llm_service),
            stream_token_callback=None,
        )

    result = asyncio.run(_run())

    assert result["analysis_method"] == "langchain"
    assert "kubectlgetpods" not in result["answer"]
    assert "列出 pod" in result["answer"]


def test_run_followup_langchain_never_leaks_broken_json_text(monkeypatch):
    monkeypatch.setattr("ai.langchain_runtime.service._parse_structured_answer", lambda _content: None)
    llm_service = DummyStreamingLLM(
        [
            '```json{"conclusion":"query-service 慢查询"',
        ]
    )

    async def _run():
        return await run_followup_langchain(
            **_build_runtime_kwargs(llm_service),
            stream_token_callback=None,
        )

    result = asyncio.run(_run())

    assert result["analysis_method"] == "langchain"
    assert "```json" not in result["answer"]
    assert "已忽略原始 JSON" in result["answer"]


def test_run_followup_langchain_prompt_prefers_stable_readonly_commands(monkeypatch):
    captured = {}

    async def _fake_collect_chat_response(**kwargs):
        captured["message"] = kwargs.get("message", "")
        return '{"conclusion":"ok","summary":"done","actions":[]}'

    monkeypatch.setattr("ai.langchain_runtime.service.collect_chat_response", _fake_collect_chat_response)

    async def _run():
        return await run_followup_langchain(
            **_build_runtime_kwargs(DummyStreamingLLM([])),
            stream_token_callback=None,
        )

    result = asyncio.run(_run())

    assert result["analysis_method"] == "langchain"
    message = str(captured.get("message") or "")
    assert "默认优先使用 kubectl/rg/grep/cat/tail/head/jq/ls/echo/pwd" in message
    assert "curl（仅 GET/HEAD 或 -G 查询）" in message
    assert "clickhouse-client/clickhouse（仅 SELECT/SHOW/DESCRIBE/EXPLAIN 只读查询）" in message
    assert "禁止脚本化链式拼接（| && || ;）" in message
    assert "命令必须保留标准空格分词" in message
    assert "禁止用 echo/printf 把人工说明" in message


def test_extract_structured_actions_demotes_placeholder_echo_command():
    answer = StructuredAnswer(
        actions=[
            ActionItem(
                priority=1,
                title="查看错误日志",
                action="在日志查询页面执行搜索",
                command="echo '请在日志查询页面执行搜索，例如：service=checkout-service AND level=ERROR'",
                command_type="query",
                risk_level="low",
                executable=True,
                requires_confirmation=False,
                expected_outcome="拿到错误日志",
            )
        ]
    )

    actions = _extract_structured_actions(answer)

    assert len(actions) == 1
    assert actions[0]["command"].startswith("echo ")
    assert actions[0]["executable"] is False
    assert actions[0]["command_type"] == "unknown"
    assert "人工说明" in str(actions[0]["reason"])


def test_extract_structured_actions_prefers_command_spec_compile_over_raw_command_spacing():
    answer = StructuredAnswer(
        actions=[
            ActionItem(
                priority=1,
                title="分析慢 SQL 执行计划",
                action="获取 explain plan",
                command="kubectl -n islap exec -i $(kubectl -n islap get pods -l app=clickhouse -o jsonpath='{.items[0].metadata.name}') -- clickhouse-client --query \"EXPLAINPLAN SELECT parent.service_nameASsource_service FROM logs.tracesPRE WHERE timestamp>now()-INTERVAL1HOUR\"",
                command_spec={
                    "tool": "kubectl_clickhouse_query",
                    "args": {
                        "namespace": "islap",
                        "pod_selector": "app=clickhouse",
                        "query": "EXPLAINPLAN SELECT parent.service_nameASsource_service FROM logs.tracesPRE WHERE timestamp>now()-INTERVAL1HOUR",
                        "timeout_s": 60,
                    },
                },
                command_type="query",
                risk_level="low",
                executable=True,
            )
        ]
    )

    actions = _extract_structured_actions(answer)

    assert len(actions) == 1
    assert actions[0]["command_spec"]["tool"] == "kubectl_clickhouse_query"
    assert actions[0]["command"] == (
        "kubectl -n islap exec -i $(kubectl -n islap get pods -l app=clickhouse -o jsonpath='{.items[0].metadata.name}') "
        "-- clickhouse-client --query \"EXPLAINPLAN SELECT parent.service_nameASsource_service FROM logs.tracesPRE WHERE "
        "timestamp>now()-INTERVAL1HOUR\""
    )
    assert actions[0]["executable"] is False
    reason = str(actions[0]["reason"] or "")
    assert "unsupported_clickhouse_readonly_query" in reason or "glued_sql_tokens" in reason


def test_extract_structured_actions_missing_command_spec_is_not_executable():
    answer = StructuredAnswer(
        actions=[
            ActionItem(
                priority=1,
                title="查看 query-service 日志",
                action="查看 query-service 最新错误",
                command="kubectl logs deploy/query-service -n islap --tail=50",
                command_type="query",
                risk_level="low",
                executable=True,
            )
        ]
    )

    actions = _extract_structured_actions(answer)

    assert len(actions) == 1
    assert actions[0]["command"] == "kubectl logs deploy/query-service -n islap --tail=50"
    assert actions[0]["executable"] is False
    assert "missing_structured_spec" in str(actions[0]["reason"])


def test_extract_structured_actions_supports_generic_exec_command_spec():
    answer = StructuredAnswer(
        actions=[
            ActionItem(
                priority=1,
                title="查看 query-service pod",
                action="检查 pod 状态",
                command_spec={
                    "tool": "generic_exec",
                    "args": {
                        "command": "kubectl get pods -n islap -l app=query-service",
                        "target_kind": "k8s_cluster",
                        "target_identity": "namespace:islap",
                        "timeout_s": 30,
                    },
                },
                command_type="query",
                risk_level="low",
                executable=True,
            )
        ]
    )

    actions = _extract_structured_actions(answer)

    assert len(actions) == 1
    assert actions[0]["command_spec"]["tool"] == "generic_exec"
    assert actions[0]["command"] == "kubectl get pods -n islap -l app=query-service"
    assert actions[0]["executable"] is True


def test_normalize_action_command_repairs_spacing_for_kubectl_exec_pattern():
    raw = "kubectl -nislapexec -it$(kubectl -nislapgetpods -lapp=clickhouse -ojsonpath='{.items[0].metadata.name}') --clickhouse -client --query \"SHOWCREATETABLElogs.traces\""
    normalized = _normalize_action_command(raw)

    assert "-n islap exec" in normalized
    assert "-i $(" in normalized
    assert "-n islap get pods" in normalized
    assert "-- clickhouse-client" in normalized


def test_normalize_action_command_repairs_clickhouse_placeholder_flags():
    raw = "clickhouse-client --host<HOST>--port<PORT>--user<USER>--password<PASSWORD>--database<DATABASE>--query \"SHOWCREATETABLElogs.traces\""
    normalized = _normalize_action_command(raw)

    assert "--host <HOST>" in normalized
    assert "--port <PORT>" in normalized
    assert "--user <USER>" in normalized
    assert "--password <PASSWORD>" in normalized
    assert "--database <DATABASE>" in normalized
    assert "SHOW CREATE TABLE logs.traces" in normalized


def test_normalize_action_command_repairs_compact_clickhouse_query_keywords():
    raw = (
        "kubectl -n islap exec -it $(kubectl -n islap get pods -l app=clickhouse "
        "-o jsonpath='{.items[0].metadata.name}') -- clickhouse-client --query "
        "\"SELECTpartition,name,rows,bytes_on_diskFROMsystem.partsWHEREtable='traces'"
        "ANDdatabase='logs'ORDERBYpartitionDESCLIMIT10\""
    )
    normalized = _normalize_action_command(raw)

    assert "-n islap exec -i $(" in normalized
    assert (
        "--query \"SELECT partition,name,rows,bytes_on_disk FROM system.parts "
        "WHERE table='traces' AND database='logs' ORDER BY partition DESC LIMIT 10\""
    ) in normalized


def test_normalize_action_command_repairs_kubectl_pipeline_spacing():
    raw = "kubectldescribepods -n islap|grep-ierror"
    normalized = _normalize_action_command(raw)

    assert normalized == "kubectl describe pods -n islap | grep -i error"


def test_normalize_action_command_repairs_kubectldescribepod_compact():
    raw = "kubectldescribepod $(kubectl get pods -l app=query-service -o jsonpath='{.items[0].metadata.name}')"
    normalized = _normalize_action_command(raw)

    assert normalized.startswith("kubectl describe pod $(")


def test_normalize_action_command_repairs_compact_pipeline_tokens():
    raw = (
        "kubectl logs--tail=50$(kubectl get pods -l app=query-service "
        "-o jsonpath='{.items[0].metadata.name}')|grep -A20'Events:'|head-20"
    )
    normalized = _normalize_action_command(raw)

    assert "kubectl logs --tail=50 $(" in normalized
    assert "grep -A20 'Events:'" in normalized
    assert "| head -20" in normalized


def test_normalize_action_command_repairs_glued_selector_flags():
    raw = "kubectl logs -l app=query-service--timestamps | tail-50"
    normalized = _normalize_action_command(raw)

    assert "-l app=query-service --timestamps" in normalized
    assert "| tail -50" in normalized

    raw_get = "kubectl get pods -l app=query-service-owide"
    normalized_get = _normalize_action_command(raw_get)
    assert normalized_get.endswith("-l app=query-service -o wide")


def test_normalize_action_command_repairs_compact_long_flags_for_kubectl_logs():
    raw = "kubectl logs --namespaceislap --selectorapp=query-service --tail50 --no-headers"
    normalized = _normalize_action_command(raw)

    assert normalized == "kubectl logs --namespace islap --selector app=query-service --tail=50 --no-headers"


def test_sanitize_json_like_answer_uses_fallback_keys_when_conclusion_missing():
    raw = (
        '{"analysis_summary":"query-service 存在慢查询",'
        '"actions":[{"title":"先看 query-service 错误日志"}],'
        '"evidence_gaps":["trace_id"]}'
    )
    sanitized = _sanitize_json_like_answer(raw)

    assert "结论：query-service 存在慢查询" in sanitized["answer"]
    assert "建议动作：" in sanitized["answer"]
    assert "- 先看 query-service 错误日志" in sanitized["answer"]
    assert "仍缺失证据：" in sanitized["answer"]
    assert "trace_id" in sanitized["answer"]
