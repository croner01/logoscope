# AI Runtime v2 回归检查清单

本文档对应脚本 [scripts/ai-runtime-v2-regression-check.sh](/root/logoscope/scripts/ai-runtime-v2-regression-check.sh)。

目标不是“证明没有问题”，而是快速识别这批优化是否回退到老问题，尤其是以下高频投诉点：

1. 已经输入过排查目标，系统仍重复追问同一个问题。
2. 超时后追问方向不稳定，继续要求用户补“命令语义”而不是业务范围。
3. 会话里出现多段过期 `waiting_user_input` 信息，用户上翻时上下文混乱。
4. 命令执行卡片缺失目标上下文，用户无法理解“命令到底在哪个集群/命名空间/节点执行”。

## 一键执行

```bash
scripts/ai-runtime-v2-regression-check.sh
```

可选开关：

```bash
# 跳过前端 runtime transcript 合约测试
RUN_FRONTEND_TESTS=0 scripts/ai-runtime-v2-regression-check.sh
```

报告输出：

- 目录：`reports/ai-runtime-v2-regression/`
- 最新报告：`reports/ai-runtime-v2-regression/latest.json`

## 自动检查项（脚本内置）

`case01_question_kind_baseline_is_diagnosis_goal`
- 目的：确认 `unknown_semantics` 的基础追问是 `diagnosis_goal`。

`case02_dedup_diagnosis_goal_across_replan`
- 目的：确认用户已回答 `diagnosis_goal` 后，即使 `action_id` 变化（replan），也不会再次追问 `diagnosis_goal`，应转为 `execution_scope`。

`case03_timeout_maps_to_timeout_scope`
- 目的：确认超时恢复追问稳定收敛到 `timeout_scope`。

`case04_service_question_fallback_uses_summary_or_history`
- 目的：接口行为校验。先触发第 1 次 `waiting_user_input`（`diagnosis_goal`），提交一次用户输入后再次触发命令，确认第 2 次追问收敛为 `execution_scope`，从而验证回退链路在真实运行态生效。

`case05_latest_pending_user_input_state_only_tracks_current_action`
- 目的：通过 `/api/v2/runs/{run_id}/actions` 校验当前最新待答 action 是第二次追问动作（执行范围），不会把旧动作当作当前入口。

`case06_command_bridge_emits_target_context_fields`
- 目的：确认命令事件带上 `target_cluster_id / target_namespace / target_node_name / resolved_target_context`。

`case07_frontend_runtime_transcript_contract`
- 目的：执行前端 runtime transcript 合约测试（`npm --prefix frontend run test:agent-runtime`），覆盖“只保留最新 user_input 卡片、详情下沉到单一入口、命令目标上下文透传到展示层”。

## 失败判定与处置

判定规则：

1. 任一 `case.passed=false`，脚本整体返回非 0。
2. 报告 `overall_passed=false` 时，优先查看失败 case 的 `detail` 字段。
3. 若失败在 `case07`，先看 `detail.log_tail`；若失败在 backend case，直接看 `backend_probe.cases[]`.

建议排查顺序：

1. 先定位失败 case 对应模块（`user_question_adapter` / `service` / `command_bridge` / `frontend runtimeTranscript`）。
2. 再复跑对应最小单测（前端 `test:agent-runtime`，后端对应 `ai-service/tests/test_agent_runtime_*`）。
3. 最后做一次手工会话验证，确认用户视角是否和自动检查一致。

## 半自动人工补充（建议）

脚本通过后，建议追加 1 轮真实会话检查（5 分钟内）：

1. 发起排障目标后触发一次 `waiting_user_input`，确认回答后不再重复问“排查目标”。
2. 在聊天流中确认只有一个“查看详情”入口，主流保持连续对话。
3. 查看命令详情，确认可读到 cluster/namespace/node 上下文。
4. 连续追问两轮，确认对话记忆不丢失、不会退化为让用户补命令语法。
