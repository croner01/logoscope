# AI Agent Runtime V4 实施任务拆解（中文版）

> 关联蓝图：`docs/design/ai-agent-runtime-v4-blueprint.zh-CN.md`  
> 状态：Draft（评审版）  
> 目标：将 V4 蓝图拆解为可排期、可并行、可回滚的工程任务

---

## 0. 适用范围与前提

本文档在以下前提下成立：

1. 已确认采用 `Temporal 外环 + LangGraph 内环`
2. 未知目标默认 `manual_required`
3. 第一阶段删除 local fallback，强制受控沙箱执行
4. 允许停服重构
5. 允许 API v2 与旧 API 并存短期过渡

---

## 1. 实施总原则

### 📌 Decision: rollout/v4-critical-path
- **Value**: 先改执行安全边界，再改策略主权，再改编排内核，最后改交互合同
- **Layer**: infrastructure
- **Tags**: rollout, security-first, critical-path
- **Rationale**: 执行面与策略面是高风险边界，必须先收敛再扩大自动化能力
- **Alternatives**: 先重做前端体验，再补执行合规
- **Tradeoffs**: 前期用户体验增量较少，但系统风险显著下降

### 🚫 Constraint: security
- **Rule**: 在 Phase 1 完成前，不得新增任何自动写命令能力
- **Priority**: critical
- **Tags**: write-safety, gating

### 🚫 Constraint: architecture
- **Rule**: 同一 `run_id` 在任意时间只允许一个主编排来源（旧 runtime 或 V4 runtime），禁止双主写入
- **Priority**: critical
- **Tags**: state-authority, migration

---

## 2. 目标交付物

## 2.1 代码交付物

1. 新 V4 runtime 包（ai-service）
2. OPA policy bundle 与策略客户端（exec-service）
3. 沙箱执行适配层（exec-service）
4. API v2 接口（ai-service + frontend）
5. 统一审计回放查询层（query-service/ai-service）

## 2.2 非代码交付物

1. OPA 策略回放样本集
2. 许可证清单与依赖审计（OSI only）
3. 运维手册（中断恢复、策略回滚、执行器故障处理）
4. 验收测试矩阵

---

## 3. 任务拆分与并行工作流

建议拆分 7 条 Track，按关键路径推进。

1. Track A：执行面硬切（删除 local fallback）
2. Track B：策略主权迁移（OPA）
3. Track C：编排内核（Temporal + LangGraph）
4. Track D：合同门禁（Guardrails）
5. Track E：API v2 与前端协议
6. Track F：目标与能力注册（Target/Capability Registry）
7. Track G：审计回放与合规治理

---

## 4. Track A：执行面硬切（最高优先）

## A1. 删除 local fallback 路径

### 目标

命令执行不再允许退化到本机进程执行。

### 主要改动文件

1. `exec-service/core/executor_registry.py`
2. `exec-service/api/execute.py`
3. `exec-service/core/runtime_service.py`

### 任务

1. `resolve_executor(...)` 在模板缺失时返回 `dispatch_ready=false` 且不可执行
2. `precheck` 对 `dispatch_degraded=true` 直接返回 `permission_required`
3. `create_run` 在不可执行时拒绝创建 command run

### 验收

1. 不配置任何执行模板时，命令全部被拒绝
2. 无命令落入本机 `local_process` 执行路径

### 回滚点

1. 可通过 feature flag 临时恢复旧 precheck 语义（仅开发环境）

---

## A2. 收敛 toolbox-gateway 风险

### 目标

淘汰 `shell=True` 执行模式，确保执行输入结构化。

### 主要改动文件

1. `toolbox-gateway/app.py`（或替换为新执行器代理）
2. 新增 `toolbox-gateway/command_parser.py`
3. 新增 `toolbox-gateway/runner.py`

### 任务

1. 接口改为接收结构化 argv（禁止原始 shell 字符串直传）
2. 执行路径改为 `subprocess.run([...], shell=False)`
3. 加入参数长度、参数字符集、输出大小上限

### 验收

1. 安全扫描中不再出现生产执行链路 `shell=True`
2. 恶意拼接输入无法触发 shell 解释执行

---

## A3. 接入受控沙箱执行器

### 目标

命令执行统一下沉至容器沙箱（OpenHands Docker runtime 或等价实现）。

### 主要改动文件

1. 新增 `exec-service/executors/sandbox_adapter.py`
2. 新增 `exec-service/executors/openhands_executor.py`
3. 修改 `exec-service/core/dispatch.py`
4. 修改 `deploy/exec-service.yaml`

### 任务

1. 支持按 `executor_profile` 选择镜像、资源、网络策略
2. 每次命令绑定 `run_id/action_id` 标签
3. 输出流按事件回传 exec-service

### 验收

1. 所有命令在沙箱内执行
2. 执行上下文可追踪到 run/action

---

## 5. Track B：策略主权迁移（OPA）

## B1. 引入 OPA sidecar/独立服务

### 目标

建立策略裁决基础设施。

### 主要改动文件

1. 新增 `deploy/opa.yaml`（或 Helm 模块）
2. 新增 `exec-service/core/policy_opa_client.py`
3. 新增 `exec-service/policies/*.rego`
4. 新增 `exec-service/policies/tests/*.yaml`

### 任务

1. 定义策略输入 schema（命令、目标、身份、上下文）
2. 定义策略输出 schema（`allow/confirm/elevate/deny`）
3. 接入 OPA `/v1/data/...` 决策查询

### 验收

1. precheck 能返回 OPA 决策结果
2. 失败时可识别为策略服务异常而非命令失败

---

## B2. 策略影子模式与差异回放

### 目标

降低策略迁移风险。

### 主要改动文件

1. `exec-service/api/execute.py`
2. 新增 `scripts/opa-shadow-diff-report.sh`
3. 新增 `reports/policy-shadow/*`

### 任务

1. Python 策略与 OPA 并行评估（不影响执行）
2. 记录差异并输出日报
3. 达到阈值后切换 OPA 为主裁决

### 验收

1. 差异率可视化
2. 切换前有明确风险评估

---

## B3. 决策日志入审计链路

### 目标

实现策略可追责。

### 主要改动文件

1. `exec-service/core/audit_store.py`
2. `ai-service/ai/agent_runtime/store.py`（或 v4 对应 store）
3. `deploy/sql/*`（新增 policy decision 表或列）

### 任务

1. 记录 `decision_id`、policy package、input hash、result
2. 绑定 `run_id/action_id/command_run_id`
3. 回放接口可按 run 查询对应决策

### 验收

1. 任意 run 可查到完整策略决策链
2. 决策日志与执行日志时间线一致

---

## 6. Track C：编排内核（Temporal + LangGraph）

## C1. Temporal 外环 Workflow 建模

### 目标

接管 run 生命周期和中断恢复。

### 建议新增文件

1. `ai-service/ai/runtime_v4/temporal/workflows.py`
2. `ai-service/ai/runtime_v4/temporal/activities.py`
3. `ai-service/ai/runtime_v4/temporal/client.py`
4. `ai-service/ai/runtime_v4/temporal/signals.py`

### 任务

1. 建立 `RunWorkflow`
2. 定义审批 signal、用户输入 signal、interrupt signal
3. 定义超时与重试策略

### 验收

1. 服务重启后 workflow 可恢复
2. 审批后可继续同一 run

---

## C2. LangGraph 内环 ReAct 子图

### 目标

接管 agent 推理与工具循环。

### 建议新增文件

1. `ai-service/ai/runtime_v4/langgraph/graph.py`
2. `ai-service/ai/runtime_v4/langgraph/nodes/*.py`
3. `ai-service/ai/runtime_v4/langgraph/state.py`
4. `ai-service/ai/runtime_v4/langgraph/checkpoint.py`

### 任务

1. 定义 planning/acting/observing/replan 节点
2. 定义工具选择与 stop criteria
3. 中断点与 Temporal signal 对齐

### 验收

1. 支持多轮工具调用
2. 中断恢复后状态一致

---

## C3. 外内环适配层

### 目标

避免双状态源冲突。

### 建议新增文件

1. `ai-service/ai/runtime_v4/adapter/orchestration_bridge.py`
2. `ai-service/ai/runtime_v4/adapter/event_mapper.py`

### 任务

1. Temporal 管 run 生命周期状态
2. LangGraph 管推理局部状态
3. 统一产出 canonical event

### 验收

1. 不出现同一 run 两处并发更新主状态
2. 事件序列可严格递增

---

## 7. Track D：合同门禁（Guardrails）

## D1. diagnosis_contract Schema 固化

### 目标

形成不可绕过的结构合同。

### 建议新增文件

1. `ai-service/ai/runtime_v4/contracts/diagnosis_contract.py`
2. `ai-service/ai/runtime_v4/contracts/validators.py`

### 任务

1. 定义必填字段与长度约束
2. 定义字段语义约束（步骤可执行、原因明确）

### 验收

1. 缺少字段时不允许进入写命令执行
2. 错误信息可直接展示给用户

---

## D2. Guardrails re-ask 链路

### 目标

在阻断前尝试自动补齐。

### 主要改动文件

1. `ai-service/ai/runtime_v4/contracts/guardrails_runner.py`
2. `ai-service/ai/runtime_v4/langgraph/nodes/contract_gate.py`

### 任务

1. 校验失败触发 re-ask
2. 超过阈值触发 `contract_blocked` 事件

### 验收

1. re-ask 次数可配置
2. 超限后 run 不再继续写动作

---

## 8. Track E：API v2 与前端协议

## E1. 后端 API v2 落地

### 主要改动文件

1. 新增 `ai-service/api/ai_runtime_v2.py`
2. 修改 `ai-service/main.py`（注册路由）
3. 新增 `ai-service/ai/runtime_v4/api_models.py`

### 任务

1. 提供 thread/run/action/approval 资源接口
2. 提供 `events/stream` 断点续传
3. 提供审批、输入、中断标准接口

### 验收

1. API v2 可独立跑通全链路
2. v1 可在迁移窗内并存

---

## E2. 前端 runtime v4 客户端与状态

### 主要改动文件

1. 新增 `frontend/src/features/ai-runtime-v4/*`
2. 修改 `frontend/src/utils/api.ts`
3. 新增 `frontend/src/pages/AIRuntimeV4.tsx`

### 任务

1. 前端统一使用 v2 contract
2. 审批强中断与恢复交互
3. 详情区展示 policy/approval/command 证据链

### 验收

1. 多轮 thread 体验稳定
2. 审批流与执行流状态一致

---

## 9. Track F：Target/Capability Registry

## F1. 注册中心与治理接口

### 建议新增文件

1. `ai-service/ai/runtime_v4/targets/models.py`
2. `ai-service/ai/runtime_v4/targets/service.py`
3. `ai-service/api/targets_v1.py`

### 任务

1. 注册目标、能力、凭证范围
2. 提供查询接口供策略与编排消费
3. 提供版本化更新与审计

### 验收

1. 未注册目标默认 manual_required
2. 策略输入可引用目标能力信息

---

## 10. Track G：审计回放与合规

## G1. 审计模型扩展

### 主要改动文件

1. `deploy/sql/release-*.sql`
2. `ai-service/ai/runtime_v4/audit/*.py`
3. `query-service` 查询接口扩展

### 任务

1. 增加 policy decision 事件模型
2. 增加 approval 与 command 关联字段
3. 增加 run 全链路回放查询

### 验收

1. 单 run 可完整回放
2. 审计字段满足合规抽检

---

## 11. 阶段计划（可停服重构版）

## Milestone 1（安全硬切）

1. A1, A2 完成
2. 关闭 local fallback
3. 关闭 shell 执行链

## Milestone 2（策略主权）

1. B1, B2, B3 完成
2. OPA 切主

## Milestone 3（编排切换）

1. C1, C2, C3 完成
2. Temporal + LangGraph 接管主链路

## Milestone 4（合同与 API）

1. D1, D2, E1, E2 完成
2. 前后端切 v2

## Milestone 5（扩展与收尾）

1. F1, G1 完成
2. 清理旧 v1 runtime 核心路径

### 📌 Decision: rollout/v4-milestone-gates
- **Value**: 采用里程碑闸门推进，每个阶段必须通过验收才进入下阶段
- **Layer**: infrastructure
- **Tags**: milestone, quality-gate, rollout
- **Rationale**: 降低一次性大迁移风险，确保关键能力逐步稳定
- **Alternatives**: 并行大爆炸式切换
- **Tradeoffs**: 进度更可控，但对阶段验收治理要求更高

---

## 12. 测试与验收矩阵（最小集）

1. `test_no_local_fallback_execution`
2. `test_unknown_target_manual_required`
3. `test_write_command_three_gates_required`
4. `test_approval_reject_single_replan_then_blocked`
5. `test_policy_decision_id_bound_to_run`
6. `test_temporal_resume_after_restart`
7. `test_langgraph_interrupt_resume_consistency`
8. `test_contract_reask_then_block`
9. `test_v2_stream_after_seq_resume`
10. `test_end_to_end_react_chain_with_evidence_output`

---

## 13. 回滚策略

1. 策略回滚：OPA bundle 版本回退
2. 编排回滚：切回旧 runtime orchestrator（短期保留只读兼容）
3. API 回滚：前端 feature flag 切回 v1 页面
4. 执行回滚：仅允许在开发环境临时启用兼容执行，不可用于生产

### 🚫 Constraint: security
- **Rule**: 回滚路径不得恢复“无审批写命令自动执行”或“local fallback 生产执行”
- **Priority**: critical
- **Tags**: rollback, safety

---

## 14. 评审检查项

1. 是否同意 Milestone 顺序
2. 是否同意 Track 负责人分配
3. 是否同意 API v2 切换窗口
4. 是否同意 OPA 切主阈值
5. 是否同意旧路径删除时间点

评审通过后输出《V4 API v2 合同文档》与《V4 OPA 策略基线文档》。
