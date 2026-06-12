"""
Tests for followup planning ReAct closure helpers.
"""

from ai.followup_planning_helpers import (
    _append_followup_react_summary,
    _build_followup_actions,
    _build_followup_react_loop,
    _build_followup_subgoals,
    _prioritize_followup_actions_with_react_memory,
    _resolve_followup_evidence_window,
)


def test_build_followup_react_loop_finalized_on_success():
    actions = [
        {
            "id": "a1",
            "title": "query pod logs",
            "command": "kubectl logs deploy/query-service -n islap --tail=20",
            "command_type": "query",
            "executable": True,
        }
    ]
    observations = [
        {
            "action_id": "a1",
            "status": "executed",
            "exit_code": 0,
            "command": "kubectl logs deploy/query-service -n islap --tail=20",
        }
    ]
    loop = _build_followup_react_loop(actions=actions, action_observations=observations)
    assert loop["phase"] == "finalized"
    assert loop["execute"]["executed_success"] == 1
    assert loop["replan"]["needed"] is False


def test_build_followup_react_loop_matches_observation_by_command_fallback():
    actions = [
        {
            "id": "a1",
            "title": "langchain command",
            "command": "echo health-check",
            "command_type": "query",
            "executable": True,
        },
        {
            "id": "a2",
            "title": "answer command duplicate",
            "command": "echo health-check",
            "command_type": "query",
            "executable": True,
        },
    ]
    observations = [
        {
            "action_id": "a1",
            "status": "executed",
            "exit_code": 0,
            "command": "echo health-check",
        }
    ]
    loop = _build_followup_react_loop(actions=actions, action_observations=observations)
    assert loop["execute"]["executed_success"] >= 1
    assert loop["observe"]["unresolved_actions"] == 0


def test_append_followup_react_summary_adds_closure_text():
    answer = "结论：先看连接池。"
    loop = {
        "execute": {"observed_actions": 1, "executed_success": 0, "executed_failed": 1},
        "observe": {"coverage": 0.5, "confidence": 0.4},
        "replan": {"needed": True, "next_actions": ["复核并重试命令：echo health-check"]},
    }
    merged = _append_followup_react_summary(answer=answer, react_loop=loop)
    assert "闭环状态" in merged
    assert "下一步" in merged


def test_append_followup_react_summary_reports_policy_skip_without_manual_copy():
    answer = "结论：需要补充环境证据。"
    loop = {
        "execute": {"observed_actions": 1, "executed_success": 0, "executed_failed": 0},
        "observe": {"coverage": 0.5, "confidence": 0.35},
        "replan": {"needed": True, "next_actions": [], "skipped_by_policy": 1},
    }
    merged = _append_followup_react_summary(answer=answer, react_loop=loop)
    assert "策略未自动执行: 1 条" in merged
    assert "人工执行并观察" not in merged


def test_append_followup_react_summary_separates_runnable_and_manual_actions():
    answer = "结论：先补证据。"
    loop = {
        "execute": {"observed_actions": 1, "executed_success": 1, "executed_failed": 0},
        "observe": {"coverage": 0.6, "confidence": 0.5},
        "replan": {"needed": False, "next_actions": []},
    }
    actions = [
        {
            "id": "tmpl-aa11",
            "title": "补日志",
            "source": "template_command",
            "command": "kubectl -n islap logs -l app=query-service --since=15m --tail=200",
            "command_type": "query",
            "executable": True,
            "command_spec": {
                "tool": "generic_exec",
                "args": {
                    "command_argv": [
                        "kubectl",
                        "-n",
                        "islap",
                        "logs",
                        "-l",
                        "app=query-service",
                        "--since=15m",
                        "--tail=200",
                    ],
                    "target_kind": "k8s_cluster",
                    "target_identity": "namespace:islap",
                    "timeout_s": 30,
                },
            },
        },
        {
            "id": "lc-1",
            "title": "检查 clickhouse 进程",
            "source": "langchain",
            "command": "",
            "command_type": "unknown",
            "executable": False,
            "reason": "missing_structured_spec",
        },
    ]
    merged = _append_followup_react_summary(answer=answer, react_loop=loop, actions=actions)
    assert "执行步骤（结构化）" in merged
    assert "待补全动作（未自动执行）" in merged
    assert "检查 clickhouse 进程" in merged


def test_append_followup_react_summary_includes_evidence_window_when_available():
    answer = "结论：先补证据。"
    loop = {
        "execute": {"observed_actions": 1, "executed_success": 1, "executed_failed": 0},
        "observe": {"coverage": 0.6, "confidence": 0.5},
        "replan": {"needed": False, "next_actions": []},
    }
    actions = [
        {
            "id": "tmpl-window-001",
            "title": "补日志",
            "source": "template_command",
            "command": "kubectl -n islap logs -l app=query-service --since-time=2026-04-11T12:58:33Z --tail=200",
            "command_type": "query",
            "executable": True,
            "evidence_window_start": "2026-04-11T12:58:33Z",
            "evidence_window_end": "2026-04-11T13:08:33Z",
            "command_spec": {
                "tool": "generic_exec",
                "args": {
                    "command_argv": [
                        "kubectl",
                        "-n",
                        "islap",
                        "logs",
                        "-l",
                        "app=query-service",
                        "--since-time=2026-04-11T12:58:33Z",
                        "--tail=200",
                    ],
                    "target_kind": "k8s_cluster",
                    "target_identity": "namespace:islap",
                    "timeout_s": 30,
                },
            },
        }
    ]
    merged = _append_followup_react_summary(answer=answer, react_loop=loop, actions=actions)
    assert "证据时间窗" in merged
    assert "2026-04-11T12:58:33Z ~ 2026-04-11T13:08:33Z" in merged


def test_build_followup_react_loop_unknown_actions_trigger_replan():
    actions = [
        {
            "id": "a1",
            "title": "unknown action",
            "command": "custom-op --do-something",
            "command_type": "unknown",
            "executable": False,
        }
    ]
    loop = _build_followup_react_loop(actions=actions, action_observations=[])
    assert loop["plan"]["unknown_actions"] == 1
    assert loop["replan"]["needed"] is True
    next_actions = [str(item) for item in loop["replan"]["next_actions"]]
    assert all("unknown 类型动作" not in item for item in next_actions)


def test_build_followup_react_loop_skipped_action_stays_in_structured_replan_items():
    actions = [
        {
            "id": "a1",
            "title": "curl health endpoint",
            "command": "curl -G https://example.com/health",
            "command_type": "query",
            "executable": True,
        }
    ]
    observations = [
        {
            "action_id": "a1",
            "status": "skipped",
            "command": "curl -G https://example.com/health",
            "message": "自动执行仅支持受控只读命令。",
            "reason_code": "policy_blocked",
        }
    ]
    loop = _build_followup_react_loop(actions=actions, action_observations=observations)
    assert loop["replan"]["needed"] is True
    assert loop["replan"]["skipped_by_policy"] == 1
    assert loop["replan"]["next_actions"] == []
    assert loop["replan"]["items"][0]["execution_disposition"] == "skipped_by_policy"
    assert "策略限制" in loop["replan"]["items"][0]["summary"]


def test_build_followup_react_loop_duplicate_skipped_does_not_require_replan():
    actions = [
        {
            "id": "a1",
            "title": "query health",
            "command": "curl -G https://example.com/health",
            "command_type": "query",
            "executable": True,
        },
        {
            "id": "a2",
            "title": "query health duplicated",
            "command": "curl -G https://example.com/health",
            "command_type": "query",
            "executable": True,
        }
    ]
    observations = [
        {
            "action_id": "a1",
            "status": "executed",
            "exit_code": 0,
            "command": "curl -G https://example.com/health",
            "stdout": "ok",
            "command_run_id": "cmdrun-health-001",
            "output_truncated": False,
        },
        {
            "action_id": "a2",
            "status": "skipped",
            "command": "curl -G https://example.com/health",
            "message": "同一 run 已执行过该命令，跳过重复执行。",
            "reason_code": "duplicate_skipped",
            "command_run_id": "cmdrun-health-001",
        }
    ]
    loop = _build_followup_react_loop(actions=actions, action_observations=observations)
    assert loop["phase"] == "finalized"
    assert loop["replan"]["needed"] is False
    assert loop["replan"]["skipped_duplicate"] == 1
    assert loop["replan"]["items"][0]["action_id"] == "a2"
    assert loop["replan"]["items"][0]["execution_disposition"] == "skipped_duplicate"
    assert "无需重试" in loop["replan"]["items"][0]["summary"]


def test_build_followup_react_loop_reports_exec_and_evidence_coverages_with_manual_actions():
    actions = [
        {
            "id": "a1",
            "title": "自动执行查询",
            "command": "kubectl get pods -n islap",
            "command_type": "query",
            "executable": True,
        },
        {
            "id": "a2",
            "title": "人工补充信息",
            "command": "",
            "command_type": "unknown",
            "executable": False,
        },
    ]
    observations = [
        {
            "action_id": "a1",
            "status": "executed",
            "exit_code": 0,
            "command": "kubectl get pods -n islap",
            "command_run_id": "cmdrun-a1",
        }
    ]
    loop = _build_followup_react_loop(actions=actions, action_observations=observations)
    observe = loop["observe"]
    assert observe["plan_coverage"] == 0.5
    assert observe["exec_coverage"] == 1.0
    assert observe["evidence_coverage"] == 1.0
    assert observe["evidence_filled_slots"] == 1
    assert observe["evidence_missing_slots"] == 0
    assert observe["final_confidence"] >= observe["model_confidence"]


def test_build_followup_react_loop_duplicate_skipped_without_valid_source_is_missing():
    actions = [
        {
            "id": "a1",
            "title": "查询健康状态",
            "command": "curl -G https://example.com/health",
            "expected_signal": "health check 返回 ok",
            "command_type": "query",
            "executable": True,
        }
    ]
    observations = [
        {
            "action_id": "a1",
            "status": "skipped",
            "reason_code": "duplicate_skipped",
            "command": "curl -G https://example.com/health",
            "command_run_id": "cmdrun-dup-001",
        }
    ]
    loop = _build_followup_react_loop(actions=actions, action_observations=observations)
    observe = loop["observe"]
    assert observe["evidence_coverage"] == 0.0
    assert observe["evidence_reused_slots"] == 0
    assert observe["evidence_missing_slots"] == 1
    slot_map = observe["evidence_slot_map"]
    assert slot_map["action:a1"]["status"] == "missing"
    assert slot_map["action:a1"]["evidence_reuse"] is False
    assert slot_map["action:a1"]["reason_code"] == "duplicate_reuse_without_valid_source"
    assert slot_map["action:a1"]["evidence_ids"] == ["cmdrun-dup-001"]


def test_build_followup_react_loop_duplicate_skipped_reuses_full_signal_matched_source():
    actions = [
        {
            "id": "a1",
            "title": "查询健康状态",
            "command": "curl -G https://example.com/health",
            "expected_signal": "health check 返回 ok",
            "command_type": "query",
            "executable": True,
        },
        {
            "id": "a2",
            "title": "查询健康状态（重复）",
            "command": "curl -G https://example.com/health",
            "expected_signal": "health check 返回 ok",
            "command_type": "query",
            "executable": True,
        },
    ]
    observations = [
        {
            "action_id": "a1",
            "status": "executed",
            "exit_code": 0,
            "command": "curl -G https://example.com/health",
            "stdout": "health check 返回 ok",
            "command_run_id": "cmdrun-ok-001",
            "output_truncated": False,
        },
        {
            "action_id": "a2",
            "status": "skipped",
            "reason_code": "duplicate_skipped",
            "command": "curl -G https://example.com/health",
            "command_run_id": "cmdrun-ok-001",
        },
    ]
    loop = _build_followup_react_loop(actions=actions, action_observations=observations)
    observe = loop["observe"]
    assert observe["evidence_coverage"] == 1.0
    assert observe["evidence_reused_slots"] == 1
    slot_map = observe["evidence_slot_map"]
    assert slot_map["action:a2"]["status"] == "reused"
    assert slot_map["action:a2"]["evidence_reuse"] is True
    assert slot_map["action:a2"]["signal_match"] is True


def test_build_followup_react_loop_same_action_duplicate_skip_keeps_success_evidence():
    actions = [
        {
            "id": "tmpl-450de615",
            "title": "查询 query-service 日志",
            "command": "kubectl -n islap logs -l app=query-service --since=15m --tail=200",
            "expected_signal": "query-service http request completed",
            "command_type": "query",
            "executable": True,
        }
    ]
    observations = [
        {
            "action_id": "tmpl-450de615",
            "status": "executed",
            "exit_code": 0,
            "command": "kubectl -n islap logs -l app=query-service --since=15m --tail=200",
            "stdout": "query-service http request completed",
            "command_run_id": "cmdrun-query-001",
            "output_truncated": False,
        },
        {
            "action_id": "tmpl-450de615",
            "status": "skipped",
            "reason_code": "duplicate_skipped",
            "message": "同一 run 已执行过该命令，跳过重复执行。",
            "command": "kubectl -n islap logs -l app=query-service --since=15m --tail=200",
            "command_run_id": "cmdrun-query-001",
        },
    ]

    loop = _build_followup_react_loop(actions=actions, action_observations=observations)

    assert loop["phase"] == "finalized"
    assert loop["replan"]["needed"] is False
    assert loop["observe"]["evidence_coverage"] == 1.0
    assert loop["observe"]["evidence_missing_slots"] == 0
    assert loop["observe"]["evidence_slot_map"]["action:tmpl-450de615"]["status"] == "filled"


def test_build_followup_react_loop_output_truncated_is_partial_evidence():
    actions = [
        {
            "id": "a1",
            "title": "查询日志",
            "command": "kubectl -n islap logs -l app=query-service --tail=200",
            "expected_signal": "ERROR stacktrace",
            "command_type": "query",
            "executable": True,
        }
    ]
    observations = [
        {
            "action_id": "a1",
            "status": "executed",
            "exit_code": 0,
            "command": "kubectl -n islap logs -l app=query-service --tail=200",
            "stdout": "line1\\nline2",
            "output_truncated": True,
        }
    ]
    loop = _build_followup_react_loop(actions=actions, action_observations=observations)
    observe = loop["observe"]
    assert observe["evidence_coverage"] == 0.0
    assert observe["evidence_partial_slots"] == 1
    assert observe["evidence_missing_slots"] == 1
    assert loop["replan"]["needed"] is True
    assert any("补采完整输出" in str(item) for item in loop["replan"]["next_actions"])


def test_prioritize_followup_actions_with_react_memory_reorders_existing_actions():
    actions = [
        {"id": "a1", "command": "kubectl get pods -n islap", "command_type": "query", "priority": 1},
        {"id": "a2", "command": "echo health-check", "command_type": "query", "priority": 2},
    ]
    react_memory = {"failed_commands": ["echo health-check"]}
    prioritized = _prioritize_followup_actions_with_react_memory(
        actions=actions,
        react_memory=react_memory,
    )
    assert prioritized[0]["command"] == "echo health-check"
    assert prioritized[0]["react_memory_priority"] is True


def test_prioritize_followup_actions_with_react_memory_appends_missing_failed_command():
    actions = [
        {"id": "a1", "command": "kubectl get pods -n islap", "command_type": "query", "priority": 1},
    ]
    react_memory = {"failed_commands": ["echo health-check"]}
    prioritized = _prioritize_followup_actions_with_react_memory(
        actions=actions,
        react_memory=react_memory,
    )
    assert any(item.get("command") == "echo health-check" for item in prioritized)


def test_build_followup_actions_prefers_classifier_when_model_marks_unknown_manual():
    actions = _build_followup_actions(
        question="获取 traces 表结构",
        answer="",
        reflection={},
        langchain_actions=[
            {
                "priority": 1,
                "title": "获取表字段、类型、默认表达式等信息",
                "action_type": "manual",
                "command_type": "unknown",
                "command": "kubectlexec-nislap-it $(kubectlgetpods-nislap-lapp=clickhouse-ojsonpath='{.items[0].metadata.name}')--clickhouse-client--query\"DESCRIBETABLElogs.traces\"",
            }
        ],
    )
    assert len(actions) == 1
    action = actions[0]
    assert action["command_type"] == "query"
    assert action["action_type"] == "query"
    assert action["executable"] is True
    assert action["command"].startswith("kubectl exec -n islap -i $(")
    assert "DESCRIBE TABLE logs.traces" in action["command"]


def test_build_followup_actions_compiles_command_spec_for_clickhouse_query():
    actions = _build_followup_actions(
        question="查看 SQL 执行计划",
        answer="",
        reflection={},
        langchain_actions=[
            {
                "priority": 1,
                "title": "获取 explain plan",
                "action_type": "query",
                "command_type": "query",
                "command_spec": {
                    "tool": "kubectl_clickhouse_query",
                    "namespace": "islap",
                    "pod_selector": "app=clickhouse",
                    "query": "EXPLAINPLAN SELECT parent.service_nameASsource_service FROM logs.tracesPRE WHERE timestamp>now()-INTERVAL1HOUR",
                },
                # 故意给错误 command，验证最终以 command_spec 编译结果为准
                "command": "kubectl --bad --spacing",
            }
        ],
    )
    assert len(actions) == 1
    action = actions[0]
    assert action["command_spec"] == {}
    assert action["command"] == ""
    assert action["command_type"] == "query"
    assert action["action_type"] == "query"
    assert action["executable"] is False
    assert "glued_sql_tokens" in str(action.get("reason") or "")


def test_build_followup_actions_repairs_glued_sql_spec_without_raw_command():
    actions = _build_followup_actions(
        question="定位 clickhouse query 异常",
        answer="",
        reflection={},
        langchain_actions=[
            {
                "priority": 1,
                "title": "回看 query_log 错误",
                "action_type": "query",
                "command_type": "query",
                "command_spec": {
                    "tool": "kubectl_clickhouse_query",
                    "args": {
                        "query": (
                            "SELECTevent_time,query_id FROM system.query_log"
                            "WHERE event_time>=now()-INTERVAL15MINUTE LIMIT 10"
                        ),
                        "namespace": "islap",
                        "pod_name": "clickhouse-0",
                        "target_kind": "clickhouse_cluster",
                        "target_identity": "database:logs",
                        "timeout_s": 30,
                    },
                },
            }
        ],
    )
    assert len(actions) == 1
    action = actions[0]
    assert action["command"].startswith("kubectl -n islap exec -i clickhouse-0 -- clickhouse-client --query ")
    assert action["command_spec"]["tool"] == "kubectl_clickhouse_query"
    assert action["executable"] is True
    assert "glued_sql_tokens" not in str(action.get("reason") or "")
    assert action["spec_repaired"] is True
    assert action["spec_repair_from_reason"] == "glued_sql_tokens"


def test_build_followup_actions_drops_glued_generic_exec_command_from_display():
    actions = _build_followup_actions(
        question="查看 islap 中的数据库 pod",
        answer="",
        reflection={},
        langchain_actions=[
            {
                "priority": 1,
                "title": "探索数据库 pod",
                "action_type": "query",
                "command_type": "query",
                "command_spec": {
                    "tool": "generic_exec",
                    "args": {
                        "command": "'kubectlgetpods-nislap-lappin(clickhouse,mysql,postgres,database)--show-labels'",
                        "target_kind": "k8s_cluster",
                        "target_identity": "namespace:islap",
                        "timeout_s": 30,
                    },
                },
                "command": "'kubectlgetpods-nislap-lappin(clickhouse,mysql,postgres,database)--show-labels'",
            }
        ],
    )
    assert len(actions) == 1
    action = actions[0]
    assert action["command"] == ""
    assert action["command_spec"] == {}
    assert action["executable"] is False
    assert (
        "unsupported_command_head" in str(action.get("reason") or "")
        or "glued_command_tokens" in str(action.get("reason") or "")
        or "invalid_kubectl_token" in str(action.get("reason") or "")
    )


def test_build_followup_actions_normalizer_fixes_glued_command_in_spec():
    """Glued command in command_spec is now fixed by _compact_command_normalizer,
    so the action compiles successfully without needing repair."""
    actions = _build_followup_actions(
        question="获取 query-service pod 列表",
        answer="",
        reflection={},
        langchain_actions=[
            {
                "priority": 1,
                "title": "获取 query-service pod 列表",
                "action_type": "query",
                "command_type": "query",
                "command_spec": {
                    "tool": "generic_exec",
                    "args": {
                        "command": "kubectl get pods-nislap-lapp=query-service",
                        "target_kind": "k8s_cluster",
                        "target_identity": "namespace:islap",
                        "timeout_s": 30,
                    },
                },
                "command": "kubectl get pods -n islap -l app=query-service",
                "reason": "unsupported_command_head",
            }
        ],
    )
    assert len(actions) == 1
    action = actions[0]
    assert action["command"] == "kubectl get pods -n islap -l app=query-service"
    assert action["command_spec"]["tool"] == "generic_exec"
    assert action["executable"] is True


def test_build_followup_actions_infers_command_spec_when_missing_for_query_command():
    actions = _build_followup_actions(
        question="检查 query-service pod 状态",
        answer="",
        reflection={},
        langchain_actions=[
            {
                "priority": 1,
                "title": "获取 query-service pod 列表",
                "action_type": "query",
                "command_type": "query",
                "command": "kubectl get pods -n islap -l app=query-service",
                "executable": False,
            }
        ],
    )
    assert len(actions) == 1
    action = actions[0]
    assert action["command_type"] == "query"
    assert action["executable"] is True
    assert action["command_spec"]["tool"] == "generic_exec"
    args = action["command_spec"]["args"]
    assert args["target_kind"] == "k8s_cluster"
    assert args["target_identity"] == "namespace:islap"
    assert "missing_structured_spec" not in str(action.get("reason") or "")


def test_build_followup_actions_promotes_manual_to_write_when_command_is_repair():
    actions = _build_followup_actions(
        question="删除异常 pod 并重建",
        answer="",
        reflection={},
        langchain_actions=[
            {
                "priority": 1,
                "title": "重建异常实例",
                "action_type": "manual",
                "command_type": "unknown",
                "command": "kubectl delete pod bad-pod-0 -n islap",
            }
        ],
    )
    assert len(actions) == 1
    action = actions[0]
    assert action["command_type"] == "repair"
    assert action["action_type"] == "write"
    assert action["executable"] is True
    assert action["requires_write_permission"] is True
    assert action["requires_elevation"] is True


def test_build_followup_actions_prefers_classifier_over_model_repair_for_readonly_query():
    actions = _build_followup_actions(
        question="查看 traces DDL",
        answer="",
        reflection={},
        langchain_actions=[
            {
                "priority": 1,
                "title": "查询表 DDL",
                "action_type": "write",
                "command_type": "repair",
                "command": "clickhouse-client --query \"SHOW CREATE TABLE logs.traces\"",
            }
        ],
    )
    assert len(actions) == 1
    action = actions[0]
    assert action["command_type"] == "query"
    assert action["action_type"] == "query"


def test_build_followup_actions_expands_skill_name_into_structured_steps():
    actions = _build_followup_actions(
        question="分析 query-service 的慢查询",
        answer="",
        reflection={},
        langchain_actions=[
            {
                "priority": 1,
                "title": "执行读路径延迟排查技能",
                "action": "优先收集读路径慢查询与资源证据",
                "skill_name": "observability_read_path_latency",
                "expected_outcome": "生成 query-service 与 ClickHouse 的结构化取证步骤",
            }
        ],
        analysis_context={
            "service_name": "query-service",
            "namespace": "islap",
        },
    )
    assert len(actions) == 4
    assert all(action["skill_name"] == "observability_read_path_latency" for action in actions)
    assert all(bool(action["executable"]) for action in actions)
    assert actions[0]["title"] == "拉取 query-service 读路径日志"
    assert actions[0]["command_spec"]["tool"] == "generic_exec"
    assert actions[1]["command_spec"]["tool"] == "kubectl_clickhouse_query"
    assert actions[1]["command_type"] == "query"


def test_build_followup_subgoals_uses_sql_evidence_hint_for_slow_query_context():
    subgoals = _build_followup_subgoals(
        question="请分析这个 ClickHouse 慢查询的根因并优化 SQL",
        analysis_context={
            "result": {
                "overview": {
                    "problem": "CH_QUERY_SLOW",
                    "description": "slow query on logs.obs_traces_1m",
                }
            }
        },
        references=[],
    )
    root_goal = next(item for item in subgoals if item.get("id") == "sg_root")
    assert "EXPLAIN" in str(root_goal.get("next_action"))
    assert "ERROR/Traceback" not in str(root_goal.get("next_action"))


def test_build_followup_actions_dedupes_same_command_between_langchain_and_answer():
    actions = _build_followup_actions(
        question="查看 query-service pod 状态",
        answer="先执行 `kubectl get pods -n islap -l app=query-service`。",
        reflection={},
        langchain_actions=[
            {
                "priority": 1,
                "title": "查询 query-service pod",
                "action_type": "query",
                "command_type": "query",
                "command": "kubectl get pods -n islap -l app=query-service",
            }
        ],
    )
    same_command_actions = [
        item for item in actions if item.get("command") == "kubectl get pods -n islap -l app=query-service"
    ]
    assert len(same_command_actions) == 1
    assert same_command_actions[0]["source"] == "langchain"


def test_build_followup_actions_marks_answer_command_as_non_executable_even_when_spec_can_be_inferred():
    actions = _build_followup_actions(
        question="检查 query-service pod 是否健康",
        answer="先执行 `kubectl get pods -l app=que`。",
        reflection={},
        langchain_actions=[],
    )
    assert len(actions) == 1
    action = actions[0]
    assert action["source"] == "answer_command"
    assert action["command"] == "kubectl get pods -l app=que"
    assert action["command_type"] == "query"
    assert action["executable"] is False
    assert action["requires_confirmation"] is False
    assert action["reason"] == "answer_command_requires_structured_action"
    assert action["command_spec"]["tool"] == "generic_exec"


def test_build_followup_react_loop_ignores_empty_command_unknown_in_next_actions():
    actions = [
        {
            "id": "a1",
            "title": "补充连接参数",
            "command": "",
            "command_type": "unknown",
            "executable": False,
        }
    ]
    loop = _build_followup_react_loop(actions=actions, action_observations=[])
    assert loop["plan"]["unknown_actions"] == 0
    next_actions = [str(item) for item in loop["replan"]["next_actions"]]
    assert all("语义不完整动作" not in item for item in next_actions)


def test_build_followup_react_loop_requires_replan_when_query_has_no_executable_candidates():
    actions = [
        {
            "id": "a1",
            "title": "获取 query-service pod 状态",
            "command": "kubectl get pods -n islap -l app=query-service",
            "command_type": "query",
            "executable": False,
        }
    ]
    loop = _build_followup_react_loop(actions=actions, action_observations=[])
    assert loop["replan"]["needed"] is True
    next_actions = [str(item) for item in loop["replan"]["next_actions"]]
    assert any("已生成结构化查询命令模板" in item for item in next_actions)
    assert all("请先补全 command_spec" not in item for item in next_actions)
    assert any(
        str(item.get("reason")) == "no_executable_query_candidates"
        for item in (loop["replan"]["items"] or [])
        if isinstance(item, dict)
    )


def test_build_followup_react_loop_suggests_templates_for_non_executable_manual_queries():
    actions = [
        {
            "id": "a1",
            "title": "查询Temporal服务在错误时间点前后的详细日志",
            "command": "",
            "command_type": "unknown",
            "executable": False,
            "reason": "command_argv contains blocked shell operators",
        },
        {
            "id": "a2",
            "title": "查询PostgreSQL数据库活动会话与慢查询历史",
            "command": "",
            "command_type": "unknown",
            "executable": False,
            "reason": "glued_sql_tokens",
        },
    ]
    loop = _build_followup_react_loop(
        actions=actions,
        action_observations=[],
        analysis_context={
            "namespace": "islap",
            "service_name": "query-service",
            "trace_id": "trace-1234567890abcdef",
        },
    )
    assert loop["replan"]["needed"] is True
    assert int(loop["plan"].get("spec_blocked_actions") or 0) >= 2
    next_actions = [str(item) for item in loop["replan"]["next_actions"]]
    assert any("可直接执行（已生成 command_spec）" in item for item in next_actions)
    assert any("kubectl logs -n " in item and "app=query-service" in item for item in next_actions)
    assert all("deploy/temporal" not in item for item in next_actions)
    assert all("deploy/postgresql" not in item for item in next_actions)


def test_build_followup_react_loop_marks_planning_incomplete_when_most_actions_blocked():
    actions = [
        {
            "id": "a1",
            "title": "查询ClickHouse慢查询详情",
            "command": "",
            "command_type": "unknown",
            "executable": False,
            "reason": "glued_sql_tokens",
        },
        {
            "id": "a2",
            "title": "检查ClickHouse节点状态",
            "command": "",
            "command_type": "unknown",
            "executable": False,
            "reason": "unsupported_command_head",
        },
        {
            "id": "a3",
            "title": "输出修复建议",
            "command": "",
            "command_type": "unknown",
            "executable": False,
            "reason": "missing_structured_spec",
        },
        {
            "id": "a4",
            "title": "查询 query-service 错误日志",
            "command": "kubectl -n islap logs deploy/query-service --since=15m --tail=200",
            "command_type": "query",
            "executable": True,
            "command_spec": {
                "tool": "generic_exec",
                "args": {
                    "command_argv": [
                        "kubectl",
                        "-n",
                        "islap",
                        "logs",
                        "deploy/query-service",
                        "--since=15m",
                        "--tail=200",
                    ],
                    "target_kind": "k8s_cluster",
                    "target_identity": "namespace:islap",
                    "timeout_s": 30,
                },
            },
        },
    ]

    loop = _build_followup_react_loop(
        actions=actions,
        action_observations=[],
        analysis_context={"namespace": "islap", "service_name": "query-service"},
    )

    assert loop["replan"]["needed"] is True
    assert loop["plan_quality"]["planning_blocked"] is False
    assert loop["plan_quality"]["spec_blocked_ratio"] == 0.75
    next_actions = [str(item) for item in loop["replan"]["next_actions"]]
    assert next_actions
    assert any("query-service" in item or "补执行" in item for item in next_actions)


def test_build_followup_react_loop_does_not_invent_temporal_or_postgres_without_context():
    actions = [
        {
            "id": "a1",
            "title": "查询Temporal服务在错误时间点前后的详细日志",
            "command": "",
            "command_type": "unknown",
            "executable": False,
            "reason": "command_argv contains blocked shell operators",
        },
        {
            "id": "a2",
            "title": "查询PostgreSQL数据库活动会话与慢查询历史",
            "command": "",
            "command_type": "unknown",
            "executable": False,
            "reason": "glued_sql_tokens",
        },
    ]
    loop = _build_followup_react_loop(actions=actions, action_observations=[])
    next_actions = [str(item) for item in loop["replan"]["next_actions"]]
    assert all("deploy/temporal" not in item for item in next_actions)
    assert all("deploy/postgresql" not in item for item in next_actions)
    assert any("clickhouse-client --query" in item or "get pods --show-labels" in item for item in next_actions)


def test_resolve_followup_evidence_window_supports_followup_related_aliases():
    window = _resolve_followup_evidence_window(
        {
            "followup_related_anchor_utc": "2026-04-12T13:31:14Z",
            "followup_related_start_time": "2026-04-12T13:26:14Z",
            "followup_related_end_time": "2026-04-12T13:36:14Z",
        }
    )

    assert window == {
        "start_iso": "2026-04-12T13:26:14Z",
        "end_iso": "2026-04-12T13:36:14Z",
    }


def test_resolve_followup_evidence_window_falls_back_to_alias_when_primary_window_is_invalid():
    window = _resolve_followup_evidence_window(
        {
            "request_flow_window_start": "not-a-time",
            "request_flow_window_end": "still-not-a-time",
            "followup_related_start_time": "2026-04-12T13:26:14Z",
            "followup_related_end_time": "2026-04-12T13:36:14Z",
        }
    )

    assert window == {
        "start_iso": "2026-04-12T13:26:14Z",
        "end_iso": "2026-04-12T13:36:14Z",
    }


def test_resolve_followup_evidence_window_supports_evidence_window_aliases():
    window = _resolve_followup_evidence_window(
        {
            "evidence_window_start": "2026-04-12T13:20:00Z",
            "evidence_window_end": "2026-04-12T13:40:00Z",
        }
    )

    assert window == {
        "start_iso": "2026-04-12T13:20:00Z",
        "end_iso": "2026-04-12T13:40:00Z",
    }


def test_build_followup_react_loop_does_not_mark_planning_incomplete_when_ready_templates_exist():
    actions = [
        {
            "id": "lc-1",
            "source": "langchain",
            "title": "查询ClickHouse错误码241的含义",
            "command": "",
            "command_type": "unknown",
            "executable": False,
            "reason": "glued_sql_tokens",
        },
        {
            "id": "tmpl-log-1",
            "source": "template_command",
            "title": "自动补证据命令：kubectl -n islap logs -l app=query-service --since-time=2026-04-12T13:26:14Z --tail=200",
            "command": "kubectl -n islap logs -l app=query-service --since-time=2026-04-12T13:26:14Z --tail=200",
            "command_type": "query",
            "executable": True,
            "reason": "structured_template_ready_for_auto_exec",
            "evidence_window_start": "2026-04-12T13:26:14Z",
            "evidence_window_end": "2026-04-12T13:36:14Z",
            "command_spec": {
                "tool": "generic_exec",
                "args": {
                    "command_argv": [
                        "kubectl",
                        "-n",
                        "islap",
                        "logs",
                        "-l",
                        "app=query-service",
                        "--since-time=2026-04-12T13:26:14Z",
                        "--tail=200",
                    ],
                    "target_kind": "k8s_cluster",
                    "target_identity": "namespace:islap",
                    "timeout_s": 30,
                },
            },
        },
    ]

    loop = _build_followup_react_loop(
        actions=actions,
        action_observations=[],
        analysis_context={
            "namespace": "islap",
            "service_name": "query-service",
            "request_flow_window_start": "2026-04-12T13:26:14Z",
            "request_flow_window_end": "2026-04-12T13:36:14Z",
        },
    )

    assert loop["plan_quality"]["planning_blocked"] is False
    assert int(loop["plan"].get("ready_template_actions") or 0) >= 1
    assert all(
        str(item.get("reason")) != "planning_incomplete"
        for item in loop["replan"]["items"]
        if isinstance(item, dict)
    )


def test_build_followup_react_loop_ignores_low_trust_answer_command_in_templates():
    actions = [
        {
            "id": "ans-1",
            "source": "answer_command",
            "title": "查看 query-service pod",
            "command": "kubectl get pods -l app=que",
            "command_type": "query",
            "executable": False,
            "reason": "answer_command_requires_structured_action",
        }
    ]
    loop = _build_followup_react_loop(actions=actions, action_observations=[])
    next_actions = [str(item) for item in loop["replan"]["next_actions"]]
    assert all("app=que" not in item for item in next_actions)
    assert any("kubectl get pods -A --show-labels" in item for item in next_actions)
    template_items = [
        item
        for item in (loop["replan"]["items"] or [])
        if isinstance(item, dict) and str(item.get("reason")) == "command_template_suggested"
    ]
    assert template_items
    assert str(template_items[0].get("suggested_command") or "").startswith("kubectl ")
    assert isinstance(template_items[0].get("suggested_command_spec"), dict)


def test_build_followup_react_loop_clickhouse_template_contains_suggested_command_spec():
    actions = [
        {
            "id": "a1",
            "source": "langchain",
            "title": "查询ClickHouse query_log 慢查询样本",
            "purpose": "定位 code:184 关联 SQL 与 query_id",
            "command": "",
            "command_type": "unknown",
            "executable": False,
            "reason": "glued_sql_tokens",
        }
    ]
    loop = _build_followup_react_loop(
        actions=actions,
        action_observations=[],
        analysis_context={"namespace": "islap", "service_name": "query-service"},
    )
    template_items = [
        item
        for item in (loop["replan"]["items"] or [])
        if isinstance(item, dict)
        and str(item.get("reason")) == "command_template_suggested"
        and "clickhouse-client --query" in str(item.get("suggested_command") or "")
    ]
    assert template_items
    suggested_spec = template_items[0].get("suggested_command_spec")
    assert isinstance(suggested_spec, dict) and suggested_spec
    assert str(suggested_spec.get("tool") or "") == "generic_exec"


# ── Task 1: _extract_namespace_from_observations ──────────────────────────


def test_extract_namespace_from_kubectl_get_pods_output():
    from ai.followup_planning_helpers import _extract_namespace_from_observations

    observations = [
        {
            "command": "kubectl get pods -A -l app=temporal",
            "stdout": (
                "NAMESPACE   NAME                       READY   STATUS    RESTARTS   AGE\n"
                "default     temporal-7b9c8f5d6-xk3m2   1/1     Running   0          5m\n"
                "default     temporal-7b9c8f5d6-ab12c   1/1     Running   0          5m\n"
            ),
            "status": "executed",
            "exit_code": 0,
        }
    ]
    result = _extract_namespace_from_observations(observations)
    assert result == {"temporal": "default"}


def test_extract_namespace_returns_empty_dict_when_no_matching_output():
    from ai.followup_planning_helpers import _extract_namespace_from_observations

    observations = [
        {
            "command": "kubectl logs deploy/query-service -n islap --tail=20",
            "stdout": "2024-01-01 10:00:00 INFO server started",
            "status": "executed",
            "exit_code": 0,
        }
    ]
    result = _extract_namespace_from_observations(observations)
    assert result == {}


def test_extract_namespace_handles_multiple_apps():
    from ai.followup_planning_helpers import _extract_namespace_from_observations

    observations = [
        {
            "command": "kubectl get pods -A -l app=temporal",
            "stdout": (
                "NAMESPACE   NAME                       READY   STATUS    RESTARTS   AGE\n"
                "default     temporal-7b9c8f5d6-xk3m2   1/1     Running   0          5m\n"
                "islap       clickhouse-6df48cc9f9-ab12   1/1     Running   0          10m\n"
            ),
            "status": "executed",
            "exit_code": 0,
        }
    ]
    result = _extract_namespace_from_observations(observations)
    assert result == {"temporal": "default", "clickhouse": "islap"}


def test_extract_namespace_skips_empty_stdout():
    from ai.followup_planning_helpers import _extract_namespace_from_observations

    observations = [
        {
            "command": "kubectl get pods -A",
            "stdout": "",
            "status": "executed",
            "exit_code": 0,
        }
    ]
    result = _extract_namespace_from_observations(observations)
    assert result == {}


# ── Task 2: 确定性证据传播 ──────────────────────────────────


def test_deterministic_propagation_fills_missing_evidence():
    from ai.followup_planning_helpers import _build_followup_react_loop

    actions = [
        {
            "id": "a1",
            "title": "list temporal pods",
            "command": "kubectl get pods -A -l app=temporal",
            "command_type": "query",
            "executable": True,
            "expected_signal": "返回temporal服务pod列表及其namespace",
        },
        {
            "id": "a2",
            "title": "query temporal logs",
            "command": "kubectl logs -n default deploy/temporal --tail=20",
            "command_type": "query",
            "executable": True,
            "expected_outcome": "确认temporal服务pod所在namespace",
            "expected_signal": "确认temporal服务pod所在namespace",
        },
    ]
    observations = [
        {
            "action_id": "a1",
            "status": "executed",
            "exit_code": 0,
            "command": "kubectl get pods -A -l app=temporal",
            "stdout": (
                "NAMESPACE   NAME                       READY   STATUS    RESTARTS   AGE\n"
                "default     temporal-7b9c8f5d6-xk3m2   1/1     Running   0          5m\n"
            ),
            "command_run_id": "run-abc123",
        },
    ]
    loop = _build_followup_react_loop(actions=actions, action_observations=observations)
    assert loop["observe"]["propagation_hits"] >= 1, (
        f"Expected propagation_hits >= 1, got {loop['observe'].get('propagation_hits')}"
    )
    slot_map = loop["observe"]["evidence_slot_map"]
    a2_slot = next(
        (s for s in slot_map.values() if s.get("action_id") == "a2"), None
    )
    assert a2_slot is not None, "a2 evidence slot should exist"
    assert a2_slot.get("status") == "cross_filled", (
        f"Expected cross_filled, got {a2_slot.get('status')}"
    )


def test_deterministic_propagation_no_hit_when_no_matching_observation():
    from ai.followup_planning_helpers import _build_followup_react_loop

    actions = [
        {
            "id": "a1",
            "title": "list temporal pods",
            "command": "kubectl get pods -A -l app=temporal",
            "command_type": "query",
            "executable": True,
        },
        {
            "id": "a2",
            "title": "check clickhouse connection",
            "command": "kubectl logs deploy/query-service -n islap --tail=20",
            "command_type": "query",
            "executable": True,
            "expected_signal": "确认clickhouse连接是否正常",
        },
    ]
    observations = [
        {
            "action_id": "a1",
            "status": "executed",
            "exit_code": 0,
            "command": "kubectl get pods -A -l app=temporal",
            "stdout": (
                "NAMESPACE   NAME    READY   STATUS    RESTARTS   AGE\n"
                "default     temporal-xxx   1/1     Running   0          5m\n"
            ),
            "command_run_id": "run-abc123",
        },
    ]
    loop = _build_followup_react_loop(actions=actions, action_observations=observations)
    assert loop["observe"].get("propagation_hits", 0) == 0


# ── Task 3: _build_llm_replan_context ──────────────────────


def test_build_llm_replan_context_includes_command_summaries():
    from ai.followup_planning_helpers import _build_llm_replan_context

    context = _build_llm_replan_context(
        original_question="确认temporal服务是否正常",
        analysis_context={"namespace": "islap"},
        all_observations=[
            {
                "command": "kubectl get pods -A -l app=temporal",
                "stdout": (
                    "NAMESPACE   NAME    READY   STATUS\n"
                    "default     temporal-xxx   1/1     Running\n"
                ),
                "status": "executed",
                "exit_code": 0,
            }
        ],
        executed_commands={"kubectl get pods -A -l app=temporal"},
        current_evidence_gaps=["需要确认temporal日志中是否有连接错误"],
        remaining_iterations=2,
        remaining_timeout=25.0,
    )
    assert "kubectl get pods -A -l app=temporal" in context
    assert "temporal-xxx" in context
    assert "Running" in context
    assert "连接错误" in context


def test_build_llm_replan_context_contains_evidence_gaps():
    from ai.followup_planning_helpers import _build_llm_replan_context

    context = _build_llm_replan_context(
        original_question="clickhouse 是否正常",
        analysis_context={},
        all_observations=[],
        executed_commands=set(),
        current_evidence_gaps=["需要查看clickhouse日志"],
        remaining_iterations=1,
        remaining_timeout=10.0,
    )
    assert "clickhouse日志" in context
    assert "剩余迭代: 1 轮" in context
    assert "10s" in context


def test_build_llm_replan_context_failed_command_includes_stderr():
    """失败命令的 stderr 应出现在重规划上下文中。"""
    from ai.followup_planning_helpers import _build_llm_replan_context
    observations = [
        {
            "command": "SELECT * FROM logs.logs WHERE level = 'ERROR'",
            "status": "executed",
            "exit_code": 1,
            "stdout": "",
            "stderr": "Code: 60. DB::Exception: Table logs.logs does not exist.",
            "message": "query failed",
        }
    ]
    context = _build_llm_replan_context(
        original_question="排查服务错误",
        analysis_context=None,
        all_observations=observations,
        executed_commands=set(),
        current_evidence_gaps=[],
        remaining_iterations=2,
        remaining_timeout=60,
    )
    assert "Table logs.logs does not exist" in context, \
        "stderr 内容应出现在重规划上下文中"
    assert "exit=1" in context, "exit_code 应出现在重规划上下文中"
    assert "失败" in context, "失败命令应标注为失败"


def test_build_llm_replan_context_success_command_no_failure_marker():
    """成功命令不应包含失败标记。"""
    from ai.followup_planning_helpers import _build_llm_replan_context
    observations = [
        {
            "command": "SELECT count() FROM logs.logs",
            "status": "executed",
            "exit_code": 0,
            "stdout": "1000\n",
            "stderr": "",
        }
    ]
    context = _build_llm_replan_context(
        original_question="test",
        analysis_context=None,
        all_observations=observations,
        executed_commands=set(),
        current_evidence_gaps=[],
        remaining_iterations=2,
        remaining_timeout=60,
    )
    assert "成功" in context, "成功命令应标注为成功"
    assert "失败" not in context, "成功命令不应包含失败标记"


def test_build_llm_replan_context_fallback_when_no_stderr():
    """当 stderr 和 message 都为空时，至少显示状态和 exit_code。"""
    from ai.followup_planning_helpers import _build_llm_replan_context
    observations = [
        {
            "command": "kubectl get pods",
            "status": "failed",
            "exit_code": 127,
            "stdout": "",
            "stderr": "",
            "message": "",
        }
    ]
    context = _build_llm_replan_context(
        original_question="test",
        analysis_context=None,
        all_observations=observations,
        executed_commands=set(),
        current_evidence_gaps=[],
        remaining_iterations=2,
        remaining_timeout=60,
    )
    assert "exit=127" in context, "无错误信息时至少显示 exit_code"
    assert "failed" in context or "失败" in context, "应有状态指示"
