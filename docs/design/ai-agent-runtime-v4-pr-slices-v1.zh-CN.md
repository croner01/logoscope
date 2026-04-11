# AI Agent Runtime V4 第一批 PR 切片清单（v1）

> 目标：在 V4 框架下给出可立即开工的第一批 PR 切片  
> 原则：并行开发、低冲突、可回滚、可验证  
> 关联文档：  
> - `docs/design/ai-agent-runtime-v4-task-breakdown.zh-CN.md`  
> - `docs/design/ai-agent-runtime-v4-api-contract.zh-CN.md`  
> - `docs/design/ai-agent-runtime-v4-opa-baseline.zh-CN.md`

---

## 0. 总览

建议第一批拆成 10 个 PR，分三周（开发测试环境停服重构模式）：

1. PR-01 安全硬切（local fallback）
2. PR-02 执行链去 shell 化
3. PR-03 OPA client 与策略输入输出 schema
4. PR-04 OPA 基线 Rego + 单元测试
5. PR-05 OPA 影子模式与差异报告
6. PR-06 API v2 基础资源（thread/run）
7. PR-07 API v2 事件流（SSE + after_seq）
8. PR-08 Temporal workflow 骨架
9. PR-09 LangGraph 子图骨架
10. PR-10 审计模型扩展（decision_id 贯通）

---

## 1. PR-01 安全硬切：删除 local fallback

## 目标

阻断任何本机退化执行路径。

## 主要改动

1. `exec-service/core/executor_registry.py`
2. `exec-service/api/execute.py`
3. `exec-service/core/runtime_service.py`

## 验收

1. 无模板配置时命令全部拒绝
2. 无命令落入 `local_process` 执行

## 回滚

1. 仅开发环境 feature flag 临时恢复旧 precheck 语义

---

## 2. PR-02 执行链去 shell 化

## 目标

将 `shell=True` 从执行主链路移除。

## 主要改动

1. `toolbox-gateway/app.py`
2. 新增 `toolbox-gateway/command_parser.py`
3. 新增 `toolbox-gateway/runner.py`

## 验收

1. 安全扫描不再命中主链路 shell 执行
2. 恶意拼接输入不触发 shell 扩展

## 回滚

1. 仅保留旧实现只读路径并加白名单（临时）

---

## 3. PR-03 OPA client 与策略合同

## 目标

完成 OPA 调用基础设施与 I/O schema 固化。

## 主要改动

1. 新增 `exec-service/core/policy_opa_client.py`
2. 新增 `exec-service/core/policy_models.py`
3. 修改 `exec-service/api/execute.py`

## 验收

1. precheck 可调用 OPA 并返回标准决策对象
2. OPA 不可用时 fail-closed

---

## 4. PR-04 OPA 基线 Rego

## 目标

实现 v1 规则矩阵。

## 主要改动

1. 新增 `exec-service/policies/runtime/command_v1.rego`
2. 新增 `exec-service/policies/runtime/target_v1.rego`
3. 新增 `exec-service/policies/runtime/contract_v1.rego`
4. 新增 `exec-service/policies/tests/*.yaml`

## 验收

1. 只读白名单 allow
2. 只读非白名单 confirm
3. 写命令 + 合同缺失 deny
4. 未知目标 manual_required

---

## 5. PR-05 OPA 影子模式

## 目标

双轨评估差异，准备切主。

## 主要改动

1. `exec-service/api/execute.py`
2. 新增 `scripts/opa-shadow-diff-report.sh`
3. 新增 `reports/policy-shadow/README.md`

## 验收

1. 生成 legacy vs opa 差异报告
2. 每次请求记录差异样本

---

## 6. PR-06 API v2 基础资源（thread/run）

## 目标

先建立 v2 资源骨架。

## 主要改动

1. 新增 `ai-service/api/ai_runtime_v2.py`
2. 新增 `ai-service/ai/runtime_v4/api_models.py`
3. 新增 `ai-service/ai/runtime_v4/store.py`
4. 修改 `ai-service/main.py`

## 验收

1. `POST /api/v2/threads`
2. `POST /api/v2/threads/{thread_id}/runs`
3. `GET /api/v2/runs/{run_id}`

---

## 7. PR-07 API v2 事件流

## 目标

提供可断点续传 SSE。

## 主要改动

1. `ai-service/api/ai_runtime_v2.py`
2. 新增 `ai-service/ai/runtime_v4/event_protocol.py`
3. 新增 `ai-service/ai/runtime_v4/emitter.py`

## 验收

1. `GET /api/v2/runs/{run_id}/events/stream?after_seq=...`
2. 前端可断连重连恢复

---

## 8. PR-08 Temporal 外环骨架

## 目标

接管 run 生命周期（先最小闭环）。

## 主要改动

1. 新增 `ai-service/ai/runtime_v4/temporal/workflows.py`
2. 新增 `ai-service/ai/runtime_v4/temporal/activities.py`
3. 新增 `ai-service/ai/runtime_v4/temporal/signals.py`
4. 新增 `deploy/temporal/*.yaml`（或外部依赖说明）

## 验收

1. run 创建后进入 Temporal workflow
2. interrupt/approval signal 可驱动状态变化

---

## 9. PR-09 LangGraph 内环骨架

## 目标

接管 ReAct 子图（先 read-only 工具）。

## 主要改动

1. 新增 `ai-service/ai/runtime_v4/langgraph/graph.py`
2. 新增 `ai-service/ai/runtime_v4/langgraph/state.py`
3. 新增 `ai-service/ai/runtime_v4/langgraph/nodes/planning.py`
4. 新增 `ai-service/ai/runtime_v4/langgraph/nodes/acting.py`
5. 新增 `ai-service/ai/runtime_v4/langgraph/nodes/observing.py`

## 验收

1. 至少一轮 read-only ReAct 闭环跑通
2. 事件映射到 API v2 canonical event

---

## 10. PR-10 审计模型扩展

## 目标

打通 `decision_id -> run_id -> action_id -> command_run_id`。

## 主要改动

1. 新增/修改 `deploy/sql/release-4-ai-agent-runtime-v4.sql`
2. `exec-service/core/audit_store.py`
3. `ai-service/ai/runtime_v4/store.py`
4. 新增 `query-service` 回放查询接口

## 验收

1. 单 run 回放包含策略、审批、执行全链路
2. 可按 decision_id 反查 run

---

## 11. 并行建议（避免冲突）

1. 小队 A：PR-01 + PR-02（执行安全）
2. 小队 B：PR-03 + PR-04 + PR-05（策略）
3. 小队 C：PR-06 + PR-07（API）
4. 小队 D：PR-08 + PR-09（编排）
5. 小队 E：PR-10（审计）

依赖关系：

1. PR-01 必须先于 PR-08/PR-09 进入主干
2. PR-03/PR-04 必须先于写命令 v2 路径
3. PR-06/PR-07 是前端联调前置条件

---

## 12. Definition of Done（首批）

1. 所有 PR 都有对应单元测试或集成测试
2. 安全红线无回退
3. 核心链路支持一次完整 E2E
4. 文档同步更新（蓝图/合同/策略）
5. 评审记录与回滚方案齐全

### 🚫 Constraint: security
- **Rule**: 任一 PR 不得引入“绕过 OPA 或审批”的后门路径
- **Priority**: critical
- **Tags**: secure-by-default, gate-integrity

