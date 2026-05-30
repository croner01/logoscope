# ADR: AI Runtime 意图驱动闭环（Intent-Led Execution Closure）v1

状态: Proposed  
作者: Runtime 组  
日期: 2026-04-06  
关联:
- `docs/design/ai-agent-runtime-v4-blueprint.zh-CN.md`
- `docs/design/ai-agent-runtime-v4-api-contract.zh-CN.md`
- `deploy/sql/release-4-intent-closure.sql`

---

## 1. 背景与问题

当前链路在高频追问场景出现两类系统性问题:

1. 同一诊断意图可重复落成相同命令，导致重复审批、重复执行。
2. exec 侧已完成，但 runtime 侧未稳定收敛到终态，去重依据失真。

这类问题不是单点 bug，而是对象模型与状态机粒度不一致:
- 计划层按“动作/命令”推进。
- 审批层按“单次请求”发票。
- 执行层按“cmdrun”结束。
- 运行时摘要按“best effort”更新。

结果是同一业务意图在多个层面没有统一主键，无法形成强一致闭环。

---

## 2. 决策

引入“意图主导”的三对象模型，并以 `fingerprint` 作为跨层幂等主键:

1. `Intent`: 表示证据采集意图（不是命令字符串）。
2. `ApprovalGrant`: 审批复用令牌（绑定 fingerprint+scope+risk）。
3. `ExecutionRecord`: 执行事实记录（绑定 intent+fingerprint+cmdrun）。

同时执行以下强约束:

1. 同一 `run_id + fingerprint` 最多进入一次 `dispatched`。
2. 审批先查可复用 `ApprovalGrant`，命中则不新建 ticket。
3. 终态收敛采用“双通道”: SSE 终态优先，`get_run` 兜底回填。
4. 状态机单轨推进，`awaiting_approval` 阶段禁止同 fingerprint replan。

---

## 3. 非目标

1. 不在 v1 改写 OPA/Rego 的策略语义，仅扩展输入与复用机制。
2. 不在 v1 解决所有 prompt 质量问题，仅提供“策略成本感知”接口位。
3. 不在 v1 引入跨 run 的全局学习推荐，仅支持同 run 和短期 TTL 复用。

---

## 4. 对象与主键

## 4.1 Intent

业务意义:
- “补齐某证据缺口”的最小执行单元。

主键:
- `intent_id`

关键唯一约束（应用层）:
- `run_id + fingerprint + target_scope` 活跃意图最多 1 条。

## 4.2 ApprovalGrant

业务意义:
- 同 fingerprint 命令的审批复用凭据。

主键:
- `grant_id`

复用键:
- `fingerprint + target_scope + risk_tier`

## 4.3 ExecutionRecord

业务意义:
- 执行结果事实源。

幂等键:
- `run_id + fingerprint + cmdrun_id`

---

## 5. 状态机

统一状态:

1. `planned`
2. `awaiting_approval`
3. `approved`
4. `dispatched`
5. `observed`
6. `settled`
7. `blocked`

状态规则:

1. `awaiting_approval` 时，同 fingerprint 不允许生成新 action/ticket。
2. `approved` 只能消费与该 fingerprint 匹配的 approval/grant。
3. `dispatched` 后必须出现 `observed`，最终必须出现 `settled` 或 `blocked`。
4. `replan` 仅可针对“未 settle 的不同 fingerprint”。

---

## 6. Fingerprint 规范

`fingerprint = sha256(normalized(tool, args, target_kind, target_identity, risk_level, purpose_class, evidence_gap_class))`

规范化要求:

1. 移除瞬态字段: ticket、message_id、trace timestamp。
2. 参数标准化: 键排序、空白折叠、等价 token 归一。
3. SQL 标准化: 关键字大小写统一、多余空白折叠。
4. 目标范围入 hash: `target_kind + target_identity + cluster_scope`。

---

## 7. 一致性与恢复

终态收敛流程:

1. 优先消费 `command_finished` SSE。
2. 若 SSE 未终态，在短窗口主动轮询 `get_run` 回填。
3. 用幂等键写 `ExecutionRecord`，重复写请求必须可重放且无副作用。

故障恢复:

1. Worker 重启后按 `Intent.status in (dispatched, observed)` 扫描补偿。
2. 若 exec 已 completed 但 runtime 未 settled，自动补写 settle 事件。

---

## 8. 预算与熔断

默认阈值:

1. `max_auto_exec_per_fingerprint_per_run = 1`
2. `max_approval_requests_per_fingerprint_per_run = 1`
3. `max_total_approvals_per_run = 2`
4. `max_replan_rounds_when_waiting_approval = 0`

命中阈值后:

1. run 进入 `blocked` 或 `manual_mode`。
2. 输出“诊断草稿 + 手动执行步骤”，停止自动执行。

---

## 9. 兼容性策略

1. API 采用“仅增字段”方式，旧客户端可忽略新字段。
2. 旧 summary 字段保留，但声明为展示字段，不再作为去重事实源。
3. 灰度阶段同时维护旧链路与新链路观测指标。

---

## 10. 验收指标

上线门槛:

1. `repeat_exec_rate < 5%`
2. `completion_gap = completed_cmdruns - settled_records = 0`
3. `approval_reuse_rate > 60%`（同 fingerprint）
4. `blocked_by_approval_timeout_rate < 2%`

---

## 11. 备选方案与取舍

备选 A: 仅在摘要中维护 executed_commands 去重  
问题:
- 摘要不是强一致事实源，重启/竞态后仍可重复。

备选 B: 仅在策略层放宽白名单  
问题:
- 不能解决“执行完成但 runtime 未收敛”的一致性问题。

本方案取舍:
- 增加数据模型复杂度与迁移成本。
- 换取幂等、可追溯、可恢复的运行时闭环。

---

## 12. 落地切分

1. Slice-1: 数据结构落库 + 影子写入（不改决策）
2. Slice-2: run 内 fingerprint 去重生效
3. Slice-3: approval grant 复用生效
4. Slice-4: 熔断与前端提示生效

