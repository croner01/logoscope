# AI Agent Runtime V4 对齐清单（实施现状）

更新时间：2026-03-24

本文用于对照《[AI Agent Runtime V4 蓝图](/root/logoscope/docs/design/ai-agent-runtime-v4-blueprint.zh-CN.md)》评估当前实施进度，标记：

- `DONE`：已达成蓝图要求
- `PARTIAL`：部分达成，仍有缺口
- `TODO`：尚未进入主链路

## 1. 安全与执行面

| 能力项 | 目标 | 状态 | 证据 |
|---|---|---|---|
| 无 local fallback | 命令只走受控执行面 | DONE | [dispatch.py](/root/logoscope/exec-service/core/dispatch.py), [execute.py](/root/logoscope/exec-service/api/execute.py#L426) |
| 未知目标默认 manual_required | allow 也要被降级为人工确认 | DONE | [execute.py](/root/logoscope/exec-service/api/execute.py#L208) |
| 只读白名单外需确认 | query 未命中模板进入 confirmation | DONE | [execute.py](/root/logoscope/exec-service/api/execute.py#L444) |
| 写命令审批门禁 | 写命令必须 elevation/confirmation | DONE | [execute.py](/root/logoscope/exec-service/api/execute.py#L463) |
| 写命令三条件并行 | `人工审批 + OPA allow + diagnosis_contract 完整` | DONE | OPA v1.2.0 对写命令返回 `allow`（策略通过），本地预检 strictest 仍保留 `elevation_required` 审批门；合同不完整先 re-ask 再 blocked（[deploy/opa.yaml](/root/logoscope/deploy/opa.yaml), [service.py](/root/logoscope/ai-service/ai/agent_runtime/service.py#L1476), [execute.py](/root/logoscope/exec-service/api/execute.py#L362), [test_policy_opa_client.py](/root/logoscope/exec-service/tests/test_policy_opa_client.py)） |
| 审批拒绝策略 | 默认 replan，最多 1 次，再 blocked | DONE | [service.py](/root/logoscope/ai-service/ai/agent_runtime/service.py#L264), [test_agent_runtime_api.py](/root/logoscope/ai-service/tests/test_agent_runtime_api.py#L621) |
| OPA fail-closed | OPA 不可用拒绝放行 | DONE | [policy_opa_client.py](/root/logoscope/exec-service/core/policy_opa_client.py), [test_execute_api_streaming.py](/root/logoscope/exec-service/tests/test_execute_api_streaming.py#L184), [deploy/opa.yaml](/root/logoscope/deploy/opa.yaml), [12a-opa.yaml](/root/logoscope/charts/logoscope/templates/12a-opa.yaml) |

## 2. 审计回放与存储

| 能力项 | 目标 | 状态 | 证据 |
|---|---|---|---|
| decision_id ↔ run_id 关联 | 可按 run 回放策略决策 | DONE | [policy_decision_store.py](/root/logoscope/exec-service/core/policy_decision_store.py), [execute.py](/root/logoscope/exec-service/api/execute.py#L679) |
| 回放脚本与新门禁兼容 | `precheck(ticket) -> create run -> replay` 闭环 | DONE | [exec-runtime-replay-check.sh](/root/logoscope/scripts/exec-runtime-replay-check.sh), [replay 报告](/root/logoscope/reports/exec-runtime-replay-check/exec-runtime-replay-check-20260324-135406-2563.json) |
| run/event/audit 持久化 | 重启后仍可查询回放 | DONE | [runtime_history_store.py](/root/logoscope/exec-service/core/runtime_history_store.py), [run_store.py](/root/logoscope/exec-service/core/run_store.py), [event_store.py](/root/logoscope/exec-service/core/event_store.py), [audit_store.py](/root/logoscope/exec-service/core/audit_store.py) |
| 集群默认 ClickHouse-first | k8s 默认走 clickhouse 且 fail-closed | DONE | [deploy/exec-service.yaml](/root/logoscope/deploy/exec-service.yaml#L50), [values.yaml](/root/logoscope/charts/logoscope/values.yaml#L70) |
| sqlite 显式禁用 | 集群禁止误配 sqlite backend | DONE | [policy_decision_store.py](/root/logoscope/exec-service/core/policy_decision_store.py#L81), [test_policy_decision_store_config.py](/root/logoscope/exec-service/tests/test_policy_decision_store_config.py) |
| 初始化 DDL 完整 | 运行态审计表可预建 | DONE | [clickhouse-init-single.sql](/root/logoscope/deploy/clickhouse-init-single.sql#L437), [clickhouse-init-replicated.sql](/root/logoscope/deploy/clickhouse-init-replicated.sql#L437), [release-3 SQL](/root/logoscope/deploy/sql/release-3-ai-agent-runtime.sql#L103) |

## 3. 架构蓝图对齐缺口

| 能力项 | 目标 | 状态 | 说明 |
|---|---|---|---|
| Temporal 外环 | run 生命周期由 Temporal 托管 | DONE | 已落地真实 Temporal workflow/client/worker 主链路（[workflows.py](/root/logoscope/ai-service/ai/runtime_v4/temporal/workflows.py), [client.py](/root/logoscope/ai-service/ai/runtime_v4/temporal/client.py), [worker.py](/root/logoscope/ai-service/ai/runtime_v4/temporal/worker.py)），并在 [main.py](/root/logoscope/ai-service/main.py) 完成 worker 启停与 fail-closed 校验；部署配置已补齐 address/namespace/task-queue/env 开关（[deploy/ai-service.yaml](/root/logoscope/deploy/ai-service.yaml), [values.yaml](/root/logoscope/charts/logoscope/values.yaml)）。边界：生产仍需提供可用 Temporal 集群地址。 |
| LangGraph 内环 | ReAct 推理图由 LangGraph 驱动 | DONE | 已接入真实 LangGraph `StateGraph` 执行路径，并保留本地降级；同时将 checkpoint 升级为内存 + ClickHouse 持久化并接入 storage attach（[graph.py](/root/logoscope/ai-service/ai/runtime_v4/langgraph/graph.py), [checkpoint.py](/root/logoscope/ai-service/ai/runtime_v4/langgraph/checkpoint.py), [api.py](/root/logoscope/ai-service/api/ai.py#L252), [test_runtime_v4_langgraph_checkpoint.py](/root/logoscope/ai-service/tests/test_runtime_v4_langgraph_checkpoint.py)）。 |
| Guardrails 合同硬门禁 | 写命令前强制 diagnosis_contract 完整 + re-ask | DONE | 已在 runtime 主链路对写命令执行 `diagnosis_contract` 完整性校验，缺字段触发 re-ask，超限 `blocked`（`diagnosis_contract_incomplete`） |
| OPA 单一最终裁决源 | 业务代码不保留 local 最终裁决路径 | DONE | 非测试环境默认 `opa_enforced`，`local/opa_shadow` 仅在显式开启 `EXEC_POLICY_ALLOW_NON_ENFORCED_MODES=true` 时可用 |
| API v2 统一模型 | thread-run-action 新接口统一 | DONE | 已补齐 `/api/v2` cancel 路由并完成前端 runtime 调用切流：create/get/events/stream/approve/cancel/interrupt/input/command 全量转至 v2（[ai_runtime_v2.py](/root/logoscope/ai-service/api/ai_runtime_v2.py), [orchestration_bridge.py](/root/logoscope/ai-service/ai/runtime_v4/adapter/orchestration_bridge.py), [api.ts](/root/logoscope/frontend/src/utils/api.ts), [test_ai_runtime_v2_api.py](/root/logoscope/ai-service/tests/test_ai_runtime_v2_api.py)）。v1 仍保留兼容以支持历史脚本。 |
| Target Registry / Capability Registry | 非硬编码命令域、能力驱动路由 | DONE | 已新增 v4 目标能力注册中心与 API（注册/查询/resolve/按 identity 原子 resolve/停用/变更回放，未知目标默认 `manual_required`），并接入 ClickHouse 持久化与重启后回放读取；同时已接入 exec-service 预检强约束（target registry enforced 模式下，lookup/resolve 异常与能力不匹配均 fail-safe `manual_required`，identity 命中多个 target 时返回 `ambiguous` 并强制人工审批；决策写入 `input_payload.target_registry` 审计回放；exec 优先走单次 `resolve-by-identity`，对旧版本 ai-service 保留 404 兼容回退），见 [target_registry_client.py](/root/logoscope/exec-service/core/target_registry_client.py), [execute.py](/root/logoscope/exec-service/api/execute.py), [test_execute_api_streaming.py](/root/logoscope/exec-service/tests/test_execute_api_streaming.py), [test_target_registry_client.py](/root/logoscope/exec-service/tests/test_target_registry_client.py), [service.py](/root/logoscope/ai-service/ai/runtime_v4/targets/service.py), [ai_runtime_v2.py](/root/logoscope/ai-service/api/ai_runtime_v2.py), [test_ai_runtime_v2_api.py](/root/logoscope/ai-service/tests/test_ai_runtime_v2_api.py) |
| Target Registry 默认种子 | 阶段内目标可快速落地并可审计变更 | DONE | 新增 [seed-runtime-targets.sh](/root/logoscope/scripts/seed-runtime-targets.sh) 并在 `islap` 集群执行（[seed 报告](/root/logoscope/reports/runtime-target-seed/runtime-target-seed-20260324-144946-30538.json)），当前默认注册 `k8s/clickhouse/openstack/host` 四类目标 |

## 4. 建议下一阶段（按安全优先）

1. 在集群侧补齐 Temporal 运维基座（HA/备份/容量/告警/故障演练）；当前 `AI_RUNTIME_V4_OUTER_ENGINE` 已在 `islap` 集群启用 `temporal_required`。
2. 收尾 v1 runtime 兼容代码（当前入口已返回 `RUNTIME_V1_DISABLED`，仍保留兼容壳层与历史文档）。
3. ~~收敛 `frontend-critical-path-check` 的超时根因并补齐稳定性门禁。~~（2026-03-24 已完成：脚本新增 timeout/retry/backoff 参数化，FE-07/FE-08 与 `p0p1-regression` 门禁在 `islap` 集群全通过；最新通过报告分别为 [FE-08](/root/logoscope/reports/frontend-topology-or-thought-e2e/frontend-topology-or-thought-20260324-145815-999.json)、[P0P1](/root/logoscope/reports/p0p1-regression/p0p1-regression-20260324-145654-19332.json)、[backend-smoke](/root/logoscope/reports/ai-runtime-backend-smoke/ai-runtime-backend-smoke-20260324-145901-28104.json)。）
4. 补齐非 k8s 目标域的执行模板（`clickhouse/openstack/host/http` 当前模板默认空值，虽已注册目标但执行面仍按 fail-closed 阻断），并补对应 e2e 门禁脚本。
5. `http_endpoint` 当前按完整 URL 精确匹配 target identity；若需“同域多路径自动执行”，需引入前缀/通配匹配策略并同步 OPA 风险约束。
