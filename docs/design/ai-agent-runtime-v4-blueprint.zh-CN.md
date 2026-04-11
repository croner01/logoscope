# AI Agent Runtime V4 蓝图（Temporal 外环 + LangGraph 内环）

## 文档目标

本文档用于评审并冻结 V4 目标架构，面向以下已确认要求：

1. 最高优先级是安全合规
2. 只读命令白名单内自动执行，白名单外人工确认
3. 写命令必须满足 `人工审批 + OPA allow + diagnosis_contract 完整`
4. 审批 reject 默认 `replan`，最多 1 次，再 `blocked`
5. 未知目标默认 `manual_required`
6. 第一阶段删除 local fallback，强制受控沙箱执行
7. 策略决策必须可审计回放，`decision_id` 可关联 `run_id`
8. 允许 API 结构优化（可不兼容旧结构）
9. 依赖必须为 OSI 开源许可

---

## 一、可达成目标与能力边界

## 1.1 可达成目标

1. 实现 Trae 风格连续会话式 ReAct：`思考 -> 查询/执行 -> 观察 -> 再思考 -> 反馈`
2. 实现多轮长任务可中断、可审批、可恢复、可回放
3. 支持跨目标排查与执行，包括 Kubernetes、数据库、HTTP 控制面、主机系统命令
4. 在安全前提下实现“模型真干活”，而不是只给建议文本
5. 输出“证据驱动”的排查结论与修复建议，包含证据缺口与置信度

## 1.2 能力边界

1. 不承诺 100% 根因精准
2. 不允许无审批自动执行写命令
3. 不允许对未知目标自动执行
4. 不允许绕过 OPA、审批、合同校验直接下发命令
5. 不允许降级到本机非受控执行平面

### 📌 Decision: runtime/v4-goal-definition
- **Value**: V4 定位为“可执行、可审计、可恢复”的排障代理系统，而非纯问答助手
- **Layer**: business
- **Tags**: runtime, react, sre, operations
- **Rationale**: 用户目标是让模型真实执行排查动作并形成可落地修复方案
- **Alternatives**: 保持分析型问答助手，仅输出建议
- **Tradeoffs**: 系统复杂度上升，但业务价值显著提升

### 🚫 Constraint: security
- **Rule**: 任何写命令必须同时满足人工审批、OPA allow、合同完整三条件
- **Priority**: critical
- **Tags**: approval, opa, guardrails, write-command

### 🚫 Constraint: architecture
- **Rule**: 不允许 local fallback，命令必须走受控沙箱执行面
- **Priority**: critical
- **Tags**: executor, sandbox, compliance

---

## 二、总体架构

## 2.1 架构分层

1. 外环编排：Temporal  
负责 run 生命周期、超时、重试、审批等待、人工信号恢复、任务状态持久化
2. 内环推理：LangGraph  
负责 ReAct 推理图、工具调用决策、局部中断恢复
3. 策略层：OPA  
负责命令与目标授权决策，返回 `allow/confirm/elevate/deny` 与 `decision_id`
4. 执行层：受控沙箱  
优先 OpenHands Docker sandbox 或等价容器执行器
5. 合同层：Guardrails + Pydantic  
强制 `diagnosis_contract` 完整，失败重试后仍不满足则阻断执行
6. 审计层：ClickHouse  
统一存储 run、policy、approval、command 事件，支持全链路回放

### 📌 Decision: runtime/v4-hybrid-orchestration
- **Value**: 采用 Temporal 外环 + LangGraph 内环双层编排
- **Layer**: infrastructure
- **Tags**: temporal, langgraph, orchestration, durability
- **Rationale**: Temporal 擅长长生命周期可靠执行，LangGraph 擅长 agent ReAct 推理图
- **Alternatives**: 仅 LangGraph，或仅 Temporal
- **Tradeoffs**: 架构复杂度更高，但可靠性与开发效率更平衡

### 📌 Decision: runtime/v4-policy-source-of-truth
- **Value**: OPA 作为策略唯一裁决源，业务代码不再直接做最终策略裁决
- **Layer**: cross-cutting
- **Tags**: opa, policy-as-code, audit
- **Rationale**: 策略可测试、可审计、可灰度，满足合规需求
- **Alternatives**: 继续使用 Python 内置规则作为主策略
- **Tradeoffs**: 需要维护 Rego 与策略发布流程，但长期演进成本更低

### 🚫 Constraint: security
- **Rule**: 未知目标、未知能力、未知凭证范围默认 `manual_required` 或 `deny`
- **Priority**: critical
- **Tags**: target-registry, capability, least-privilege

## 2.2 存储边界（ClickHouse 与 Temporal）

1. 运行审计与回放数据（run/action/policy/approval/command event）统一落 ClickHouse
2. `exec-service` 的 policy decision 持久化在 k8s 默认使用 ClickHouse
3. `memory/sqlite` 仅保留本地开发与单测，不作为集群基线
4. 当 Phase 3 上线 Temporal 时，Temporal 自身 workflow 状态仍需独立事务型持久化（建议 PostgreSQL/MySQL）

### 📌 Decision: runtime/v4-storage-boundary
- **Value**: 业务审计数据统一 ClickHouse；编排内核状态与审计数据分层存储
- **Layer**: data
- **Tags**: clickhouse, temporal, persistence, audit
- **Rationale**: 审计回放场景偏 OLAP，ClickHouse 成本与查询效率更优；workflow 状态要求强一致与事务语义
- **Alternatives**: 所有数据统一写入同一 OLAP 库，或所有数据统一写入同一 OLTP 库
- **Tradeoffs**: 运维面存在双存储类型，但换来状态一致性与审计查询效率的平衡

---

## 三、核心数据模型与状态机

## 3.1 核心实体

1. `Thread`：会话级上下文，承载用户连续排障线程
2. `Run`：一次可执行任务实例
3. `Action`：一次工具动作或命令动作
4. `Approval`：审批对象与结果
5. `PolicyDecision`：OPA 决策记录
6. `Evidence`：执行输出、观测证据、引用

## 3.2 Run 状态机（V4）

1. `queued`
2. `running`
3. `waiting_approval`
4. `waiting_user_input`
5. `blocked`
6. `completed`
7. `failed`
8. `cancelled`

状态规则：

1. `waiting_approval` 只能由审批事件驱动恢复
2. `rejected` 默认进入 `replan`，最多 1 次
3. 超过重规划次数后固定落为 `blocked`
4. 所有 terminal 状态不可继续执行动作

### 📌 Decision: runtime/v4-reject-semantics
- **Value**: 审批 reject 触发一次 replan，超过 1 次后转 blocked，不再无限循环
- **Layer**: business
- **Tags**: approval, replan, state-machine
- **Rationale**: 避免模型陷入反复请求审批的无效循环
- **Alternatives**: reject 后直接终止，或无限 replan
- **Tradeoffs**: 提升稳定性，但可能错过少量可修复场景

---

## 四、API v2（评审草案）

## 4.1 资源与接口

1. `POST /api/v2/threads`
2. `GET /api/v2/threads/{thread_id}`
3. `POST /api/v2/threads/{thread_id}/runs`
4. `GET /api/v2/runs/{run_id}`
5. `GET /api/v2/runs/{run_id}/events/stream`
6. `POST /api/v2/runs/{run_id}/interrupt`
7. `POST /api/v2/runs/{run_id}/approvals/{approval_id}/resolve`
8. `POST /api/v2/runs/{run_id}/input`
9. `POST /api/v2/runs/{run_id}/actions/command`

## 4.2 强制字段

1. 每次动作必须带 `thread_id`、`run_id`、`trace_id`
2. 每次策略裁决必须带 `decision_id`
3. 每次命令执行必须带 `target_kind`、`target_identity`、`executor_profile`
4. 写命令必须带完整 `diagnosis_contract`

### 📌 Decision: api/v2-event-contract
- **Value**: API v2 采用 thread-run-action 三层模型并统一事件流契约
- **Layer**: data
- **Tags**: api, contract, event-stream
- **Rationale**: 旧接口语义混杂，难以支撑可恢复与可回放能力
- **Alternatives**: 在旧 API 上继续打补丁
- **Tradeoffs**: 迁移成本上升，但可大幅降低长期复杂度

---

## 五、执行目标与能力注册（支持“非硬编码命令域”）

## 5.1 目标注册模型

通过 `Target Registry` 注册目标，而非硬编码目标域：

1. 目标类型：`k8s_cluster`、`clickhouse_cluster`、`http_endpoint`、`host_node`、`openstack_project`
2. 能力集合：只读能力、变更能力、凭证范围
3. 执行器映射：可用 `executor_profile` 列表
4. 审批策略：自动、确认、提权确认、拒绝

## 5.2 执行准入规则

1. 目标已注册
2. 目标能力已声明
3. 凭证范围匹配
4. OPA 返回允许状态

### 📌 Decision: runtime/capability-driven-routing
- **Value**: 采用能力驱动路由替代命令域硬编码，提升扩展性
- **Layer**: infrastructure
- **Tags**: capability-registry, target-registry, routing
- **Rationale**: 用户希望不被固定命令域限制，但仍需可控安全边界
- **Alternatives**: 仅固定 kubectl/clickhouse/curl/openstack/helm/systemctl
- **Tradeoffs**: 需要维护能力元数据，但扩展新目标成本更低

### 🚫 Constraint: security
- **Rule**: 未注册目标不得执行，最多进入 manual_required
- **Priority**: critical
- **Tags**: target-governance, execution-gate

---

## 六、开源模块选型与许可

## 6.1 核心模块（必须）

1. Temporal（MIT）：外环 durable workflow
2. LangGraph（MIT）：内环 ReAct graph
3. OPA（Apache-2.0）：策略裁决与决策日志
4. Guardrails（Apache-2.0）：合同校验与 re-ask
5. OpenHands（MIT，enterprise 目录除外）：容器沙箱执行

## 6.2 诊断增强模块（可选）

1. K8sGPT（Apache-2.0）：K8s 场景故障扫描工具节点
2. Botkube（MIT）：ChatOps 场景事件订阅与辅助执行入口
3. NeMo Guardrails（Apache-2.0）：复杂对话护栏的替代方案

### 📌 Decision: dependency/osi-only
- **Value**: 所有运行时依赖强制 OSI 开源许可
- **Layer**: cross-cutting
- **Tags**: license, compliance, supply-chain
- **Rationale**: 满足企业合规和长期可持续维护要求
- **Alternatives**: 引入 fair-code 或 source-available 组件
- **Tradeoffs**: 选择面变窄，但许可证风险显著降低

### 🚫 Constraint: security
- **Rule**: 禁止引入非 OSI 开源协议的核心依赖进入主执行链路
- **Priority**: high
- **Tags**: legal, dependency

---

## 七、迁移路线（允许停服重构）

## Phase 0：冻结与准备

1. 冻结旧 runtime 需求
2. 输出 API v2 与事件协议
3. 产出策略基线与回放样本集

## Phase 1：执行面硬切（第一优先）

1. 删除 local fallback
2. 强制命令仅走受控沙箱
3. 新增失败码：`executor_unavailable`

## Phase 2：OPA 上线

1. 先影子比对
2. 再切主裁决
3. 写入 decision log 并关联 run

## Phase 3：Temporal 外环 + LangGraph 内环

1. 接管 run 生命周期
2. 接管审批中断恢复
3. 接管 ReAct 工具循环

## Phase 4：Guardrails 合同硬门禁

1. 强制 `diagnosis_contract` 完整性
2. 配置 re-ask 上限
3. 超限后阻断执行

## Phase 5：能力驱动扩展

1. 上线 Target Registry
2. 扩展可插拔诊断工具
3. 持续扩展新目标类型

### 📌 Decision: rollout/stop-service-refactor
- **Value**: 采用停服重构方式一次性切换关键控制平面，避免长期双轨
- **Layer**: infrastructure
- **Tags**: rollout, migration, cutover
- **Rationale**: 当前为开发测试环境，停服成本可接受，可显著降低双轨复杂度
- **Alternatives**: 长期灰度双轨迁移
- **Tradeoffs**: 短期中断服务，但整体实施周期更短

---

## 八、验收标准（评审通过后转测试用例）

1. 无 local fallback 执行路径
2. 无本机 `shell=True` 命令执行链路
3. 写命令三条件 gate 生效
4. 审批 reject 后最多一次 replan，再 blocked
5. policy 决策可按 `run_id + decision_id` 回放
6. 未知目标默认 manual_required
7. 任务在中断/重启后可恢复
8. 最终报告包含证据链、修复建议、风险与回滚说明

### 🚫 Constraint: performance
- **Rule**: Run 恢复时间应可控，平台重启后不允许出现大面积悬空运行态
- **Priority**: high
- **Tags**: resilience, recovery

---

## 九、主要风险与缓解

1. 双层编排复杂度高  
缓解：明确 Temporal 与 LangGraph 边界，禁止双向状态写入
2. 策略误杀导致可用性下降  
缓解：上线前进行回放样本对比，保留人工 override 入口
3. 执行器镜像能力不足  
缓解：按目标能力建镜像矩阵与最小权限模板
4. 合同门禁引入误阻断  
缓解：先在高风险写路径启用，再逐步扩展

### 📌 Decision: reliability/state-authority
- **Value**: Temporal 为 run 生命周期状态唯一权威，LangGraph 只维护推理子图状态
- **Layer**: infrastructure
- **Tags**: state-management, reliability
- **Rationale**: 避免双状态源引发恢复冲突与状态漂移
- **Alternatives**: Temporal 与 LangGraph 双主状态
- **Tradeoffs**: 需要适配层，但可靠性显著提升

---

## 十、评审输出项

本蓝图评审会需要明确以下结论：

1. 通过 V4 总体架构
2. 通过 API v2 方向
3. 通过第一阶段删除 local fallback
4. 通过 OPA 作为唯一策略裁决源
5. 通过写命令三条件 gate
6. 通过未知目标默认 manual_required

评审通过后进入《V4 实施任务拆解文档》。
