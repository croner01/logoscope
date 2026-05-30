# AI Runtime Intent Closure 实施任务清单（v1）

状态: Execution Draft  
日期: 2026-04-06  
关联文档:
- `docs/design/ai-agent-runtime-intent-closure-adr-v1.zh-CN.md`
- `docs/design/ai-agent-runtime-intent-closure-api-contract-diff-v1.zh-CN.md`
- `deploy/sql/release-4-intent-closure.sql`

---

## 1. 目标与边界

本清单把“意图驱动闭环”从架构草案拆成可排期任务，目标只有三件事:

1. 从根上消除同一意图重复审批/重复执行。
2. 让 exec 完成态与 runtime 终态可自动收敛，不再靠 `summary` 猜状态。
3. 把 `toolbox-gateway` 权限控制补齐到“可审计、可拒绝、可回放”。

非目标:

1. 不在本轮改 LLM 策略语义。
2. 不在本轮做跨 run 的长期学习推荐。
3. 不在本轮删除旧字段，仅降级其权威性。

---

## 2. 实施总原则

1. 事实源前置: `Intent / ApprovalGrant / ExecutionRecord` 先落库，再切决策读路径。
2. 单轨状态机: `awaiting_approval` 阶段禁止同 fingerprint 再规划。
3. 策略先于执行: 权限校验必须在执行前完成，执行后只允许回填，不允许“补审批”。
4. 灰度可回滚: 每个 Slice 都有独立开关和回滚点，不做“一次性大切”。

---

## 3. 里程碑与切片

1. Slice-1: 数据结构与影子写入（不改线上决策）。
2. Slice-2: run 内 fingerprint 去重生效（阻断重复 dispatch）。
3. Slice-3: approval grant 复用生效（阻断重复审批）。
4. Slice-4: 终态收敛 + 预算熔断 + 前端可视化。

建议节奏: 2 周内完成 Slice-1/2，3 周内完成 Slice-3/4 并灰度。

---

## 4. 任务拆解（按 Slice）

## 4.1 Slice-1 数据结构与影子写入

目标:
- 新对象落库并影子写，不改变当前执行决策，先建立可核对事实链。

任务:

1. DDL 上线与初始化
- 文件:
  - `/root/logoscope/deploy/sql/release-4-intent-closure.sql`
  - `/root/logoscope/deploy/clickhouse-init-single.sql`
  - `/root/logoscope/deploy/clickhouse-init-replicated.sql`
- 动作:
  - 合并 `ai_runtime_v4_intents / approval_grants / execution_records / reconcile_checkpoints`。
  - 增加 latest view 并约定查询只读 view。
- DoD:
  - 三张主表 + 四个 view 在 dev/staging 可查询。
  - 无 `FINAL` 依赖查询。

2. ai-service 影子写入 Intent/Grant
- 文件:
  - `/root/logoscope/ai-service/ai/runtime_v4/store.py`
  - `/root/logoscope/ai-service/api/ai_runtime_v2.py`
  - `/root/logoscope/ai-service/ai/runtime_v4/adapter/orchestration_bridge.py`
- 动作:
  - run 创建/状态变化时影子写 `Intent`。
  - 审批 resolve 时影子写 `ApprovalGrant`。
- DoD:
  - 对任一 run 能查到 intent 状态演进。
  - 不改变当前 API 返回行为。

3. exec-service 影子写入 ExecutionRecord
- 文件:
  - `/root/logoscope/exec-service/core/runtime_service.py`
  - `/root/logoscope/exec-service/core/audit_store.py`
- 动作:
  - dispatch_resolved、command_finished 时影子写 execution record。
  - 明确 `run_id + fingerprint + command_run_id` 幂等键。
- DoD:
  - 重放同一事件不产生重复业务记录。
  - 能从 execution record 反查 run/action。

4. 指标与核对报表
- 文件:
  - `/root/logoscope/reports/`
  - 新增 `scripts/ai-runtime-intent-closure-shadow-check.sh`
- 动作:
  - 产出 `completed_cmdruns` 与 `settled_records` 的差值报表。
- DoD:
  - 报表可每日自动产出。
  - 指标口径固定并有说明文档。

回滚点:
1. 关闭影子写开关，仅保留当前链路。
2. 保留表结构，不回删历史数据。

---

## 4.2 Slice-2 run 内 fingerprint 去重生效

目标:
- 在 runtime 主链路阻断同 `run_id + fingerprint` 的重复 dispatch。

任务:

1. 统一 fingerprint 计算与归一
- 文件:
  - `/root/logoscope/ai-service/ai/agent_runtime/service.py`
  - `/root/logoscope/ai-service/ai/runtime_v4/langgraph/nodes/acting.py`
  - `/root/logoscope/ai-service/ai/runtime_v4/langgraph/nodes/replan.py`
- 动作:
  - 固化规范化输入: `tool,args,target_kind,target_identity,risk_level,purpose_class,evidence_gap_class`。
  - 去除瞬态字段后哈希。
- DoD:
  - 同语义命令得到同 fingerprint。
  - 不同 target_scope 不会误判为同 fingerprint。

2. dispatch 前唯一门禁
- 文件:
  - `/root/logoscope/ai-service/ai/agent_runtime/service.py`
  - `/root/logoscope/ai-service/api/ai_runtime_v2.py`
- 动作:
  - dispatch 前检查 run 内是否已 `dispatched/settled`。
  - 命中后返回 `duplicate_in_run=true`，不再下发 execute。
- DoD:
  - 重放场景仅一次真实执行。
  - 事件流出现 `duplicate_blocked` 诊断事件。

3. awaiting_approval 阶段禁 replan 同 fingerprint
- 文件:
  - `/root/logoscope/ai-service/ai/agent_runtime/service.py`
  - `/root/logoscope/ai-service/ai/runtime_v4/temporal/workflows.py`
- 动作:
  - 在等待审批阶段对同 fingerprint 直接短路。
- DoD:
  - 不再出现“审批未决时新 ticket 再创建”。

回滚点:
1. 通过 feature flag 关闭强去重，回退为仅告警模式。

---

## 4.3 Slice-3 approval grant 复用生效（含 toolbox 权限补齐）

目标:
- 同 fingerprint 审批结果可复用，且 toolbox 执行权限由“命令头”升级为“能力与范围”双重校验。

任务:

1. ApprovalGrant 读路径接管
- 文件:
  - `/root/logoscope/ai-service/ai/agent_runtime/service.py`
  - `/root/logoscope/ai-service/api/ai_runtime_v2.py`
  - `/root/logoscope/ai-service/ai/runtime_v4/store.py`
- 动作:
  - precheck 前先查 grant（`fingerprint + target_scope + risk_tier`）。
  - 命中有效 grant 时跳过新审批单创建。
- DoD:
  - 同 run 二次同意图不再二次审批。
  - `approval_reuse_rate` 可观测。

2. exec-service 前置能力约束透传
- 文件:
  - `/root/logoscope/exec-service/api/execute.py`
  - `/root/logoscope/exec-service/core/target_registry_client.py`
  - `/root/logoscope/exec-service/core/runtime_service.py`
- 动作:
  - 将 `required_capabilities/target_scope/risk_tier/decision_id` 写入 dispatch payload。
  - 写入 execution 审计字段，供 toolbox 端二次校验。
- DoD:
  - 审计中可见“策略要求能力集”和“实际目标能力集”。

3. toolbox-gateway 权限补齐（关键）
- 文件:
  - `/root/logoscope/toolbox-gateway/app.py`
  - `/root/logoscope/deploy/toolbox-gateway.yaml`
- 动作:
  - 新增 `TOOLBOX_GATEWAY_ENFORCE_CAPABILITIES=true`。
  - 新增 `TOOLBOX_GATEWAY_ALLOWED_CAPABILITIES_BY_PROFILE_JSON` 配置。
  - 请求载荷必须包含:
    - `required_capabilities`
    - `target_scope`
    - `trace.decision_id`
  - gateway 在执行前进行三层拒绝:
    - profile 是否允许该 capability；
    - target_scope 是否与 resolved_target 一致；
    - decision_id 是否存在且格式合法。
- DoD:
  - 缺 capability 或 scope 不匹配返回 403（明确错误码）。
  - 日志可按 `decision_id` 全链路串联。
  - 与旧调用兼容开关可控，默认 staging 开启、prod 灰度开启。

4. 策略与权限矩阵回归
- 文件:
  - `/root/logoscope/exec-service/tests/test_execute_api_streaming.py`
  - `/root/logoscope/exec-service/tests/test_runtime_service_auto_retry.py`
  - 新增 `/root/logoscope/toolbox-gateway/tests/test_permission_gate.py`
- DoD:
  - “命令头允许但 capability 不允许”被正确拒绝。
  - “scope 漂移”被正确拒绝。

回滚点:
1. 关闭 capability 强制校验，回退到命令头白名单模式。
2. 保留字段透传，不回删审计字段。

---

## 4.4 Slice-4 终态收敛、熔断和前端可见性

目标:
- 修复“exec 已完成但 runtime 未收敛”。
- 防止审批/重规划无限循环。

任务:

1. 双通道终态收敛
- 文件:
  - `/root/logoscope/ai-service/ai/runtime_v4/adapter/event_mapper.py`
  - `/root/logoscope/ai-service/ai/runtime_v4/temporal/activities.py`
  - `/root/logoscope/ai-service/ai/runtime_v4/temporal/client.py`
- 动作:
  - SSE `tool_call_finished` 优先。
  - 超时窗口触发 `get_run` 回填 reconcile。
- DoD:
  - `completion_gap = completed_cmdruns - settled_records = 0`（灰度窗口内）。

2. 预算熔断
- 文件:
  - `/root/logoscope/ai-service/ai/agent_runtime/service.py`
  - `/root/logoscope/ai-service/api/ai_runtime_v2.py`
- 动作:
  - 执行 `max_auto_exec_per_fingerprint_per_run` 等预算阈值。
  - 命中后进入 `blocked/manual_mode` 并给出手动执行草稿。
- DoD:
  - 重复审批风暴可被自动截断。

3. API 与前端展示收敛字段
- 文件:
  - `/root/logoscope/ai-service/api/ai_runtime_v2.py`
  - `/root/logoscope/ai-service/ai/runtime_v4/api_models.py`
  - `/root/logoscope/frontend/src/pages/AIRuntimePlayground.tsx`
- 动作:
  - 返回 `intent_stats/fingerprint_stats/execution_closure/budget_state`。
  - 前端展示 `completion_gap`、`intent_status`、`approval grant 复用`。
- DoD:
  - 前端可直接识别“未收敛 run”并高亮。

回滚点:
1. 关闭 `completion_gap` 强告警，仅保留后台 reconcile。

---

## 5. PR 切片建议（可直接开工）

1. PR-IC-01: DDL + init SQL + latest view。
2. PR-IC-02: ai-service intent/grant 影子写。
3. PR-IC-03: exec-service execution-record 影子写。
4. PR-IC-04: run 内 fingerprint 去重与 awaiting_approval 禁 replan。
5. PR-IC-05: approval grant 复用读路径。
6. PR-IC-06: exec -> toolbox capability/scope/decision 透传。
7. PR-IC-07: toolbox-gateway 权限补齐与拒绝码标准化。
8. PR-IC-08: reconcile 双通道收敛。
9. PR-IC-09: budget 熔断策略。
10. PR-IC-10: 前端闭环视图与告警。

---

## 6. 验收指标与上线闸门

硬门槛:

1. `repeat_exec_rate < 5%`
2. `completion_gap = 0`
3. `approval_reuse_rate > 60%`（同 fingerprint）
4. `blocked_by_approval_timeout_rate < 2%`

toolbox 权限门槛:

1. 未携带 `required_capabilities` 的请求在强制模式下拒绝。
2. `decision_id` 缺失或非法格式拒绝。
3. profile/capability 不匹配拒绝。

---

## 7. 测试清单

单测:

1. fingerprint 归一与幂等键。
2. grant 复用命中/过期/撤销。
3. toolbox capability gate 正负例。

集成:

1. 同 run 同意图重复触发，仅一次真实执行。
2. 审批一次后同 fingerprint 复用通过。
3. exec 先完成、SSE 丢失场景下 `get_run` 回填成功。

E2E:

1. Runtime Lab 可见 intent 维度状态变化。
2. completion_gap 异常触发告警并可恢复归零。

---

## 8. 灰度与回滚策略

灰度顺序:

1. Dev 全量。
2. Staging 全量 + 压测。
3. Prod 单 namespace/单租户。
4. Prod 全量。

开关建议:

1. `AI_RUNTIME_INTENT_SHADOW_WRITE_ENABLED`
2. `AI_RUNTIME_FINGERPRINT_DEDUP_ENFORCED`
3. `AI_RUNTIME_APPROVAL_GRANT_REUSE_ENABLED`
4. `TOOLBOX_GATEWAY_ENFORCE_CAPABILITIES`
5. `AI_RUNTIME_EXECUTION_RECONCILE_ENABLED`

回滚原则:

1. 先关强约束，再关新读路径，最后关影子写。
2. 数据表不回滚删除，仅停止写入。

---

## 9. 责任分工建议

1. AI Runtime Owner:
- Slice-1/2/4 的状态机、reconcile、API 汇总字段。

2. Exec Owner:
- required_capabilities 推导与透传、execution_record 事实源。

3. Toolbox Owner:
- capability/scope/decision 三层权限校验与错误码。

4. Frontend Owner:
- intent 闭环视图、completion_gap 告警展示。

5. QA/SRE:
- 指标、压测、灰度、回滚演练。

---

## 10. 首周可执行 Sprint Backlog（建议）

1. Day 1:
- PR-IC-01（DDL）+ PR-IC-02（intent/grant 影子写骨架）

2. Day 2:
- PR-IC-03（execution_record 影子写）+ 影子核对脚本

3. Day 3:
- PR-IC-04（run 内 dedup）+ 单测

4. Day 4:
- PR-IC-05（grant 复用）+ API 字段对齐

5. Day 5:
- PR-IC-06/07（toolbox 权限补齐）+ 权限回归测试

