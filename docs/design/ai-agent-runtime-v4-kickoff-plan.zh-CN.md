# AI Agent Runtime V4 启动排期与负责人模板（3 周）

> 用途：评审通过后可直接用于 kickoff  
> 关联文档：  
> - `docs/design/ai-agent-runtime-v4-pr-slices-v1.zh-CN.md`  
> - `docs/design/ai-agent-runtime-v4-review-checklist.zh-CN.md`

---

## 1. 组织方式

建议 5 个小队并行：

1. 小队 A（执行安全）
2. 小队 B（策略与 OPA）
3. 小队 C（API v2）
4. 小队 D（编排内核）
5. 小队 E（审计与回放）

每队最少角色：

1. 1 名开发 owner
2. 1 名评审 owner
3. 1 名 QA owner（可跨队）

---

## 2. 三周排期（建议）

## Week 1（安全与策略基础）

目标：

1. 完成 PR-01、PR-02、PR-03
2. 环境中彻底禁止 local fallback
3. OPA client 接入 precheck

里程碑门槛：

1. 安全扫描通过（无主链路 shell）
2. precheck 支持 OPA 决策响应

## Week 2（策略切主准备 + API v2 基础）

目标：

1. 完成 PR-04、PR-05、PR-06、PR-07
2. OPA 影子模式开始产出日报
3. API v2 thread/run/events 基础联调可用

里程碑门槛：

1. 差异报告可追溯
2. SSE after_seq 重连通过

## Week 3（编排骨架 + 审计贯通）

目标：

1. 完成 PR-08、PR-09、PR-10
2. Temporal + LangGraph 最小闭环跑通
3. decision_id 与 run_id 贯通查询

里程碑门槛：

1. 最小 E2E（read-only）跑通
2. 单 run 审计回放完整

---

## 3. DRI 分配模板

| Track | PR | 开发 DRI | 评审 DRI | QA DRI | 计划完成日 |
|---|---|---|---|---|---|
| A | PR-01 | TBD | TBD | TBD | TBD |
| A | PR-02 | TBD | TBD | TBD | TBD |
| B | PR-03 | TBD | TBD | TBD | TBD |
| B | PR-04 | TBD | TBD | TBD | TBD |
| B | PR-05 | TBD | TBD | TBD | TBD |
| C | PR-06 | TBD | TBD | TBD | TBD |
| C | PR-07 | TBD | TBD | TBD | TBD |
| D | PR-08 | TBD | TBD | TBD | TBD |
| D | PR-09 | TBD | TBD | TBD | TBD |
| E | PR-10 | TBD | TBD | TBD | TBD |

---

## 4. 每日节奏（建议）

1. 09:30-09:45：跨队 standup（阻塞同步）
2. 14:00-14:30：技术设计快审（当天 PR 中高风险点）
3. 18:00：日报输出（风险、差异、回归状态）

---

## 5. 必须追踪的看板字段

1. PR 状态：`draft/review/merged/blocked`
2. 风险级别：`critical/high/medium/low`
3. 安全影响：`yes/no`
4. 回滚方案：`ready/not_ready`
5. 测试状态：`not_run/partial/pass/fail`

---

## 6. Week 1 出口标准（不达标不进 Week 2）

1. local fallback 已禁用
2. shell 主链路已移除
3. OPA client 已接入且 fail-closed 生效
4. 至少 10 条策略样本可回放

### 🚫 Constraint: security
- **Rule**: 若 Week 1 未达标，禁止进入 API v2 大规模联调
- **Priority**: critical
- **Tags**: gate, milestone, security

---

## 7. 风险升级机制

出现以下情况，必须在 2 小时内升级到你（技术负责人）：

1. 发现可绕过审批的执行路径
2. 发现策略误放行高危写命令
3. 发现 run 状态双写冲突
4. 发现审计字段丢失无法回放

