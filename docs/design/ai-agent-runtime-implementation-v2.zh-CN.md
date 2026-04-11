# AI Agent Runtime 实施方案 v2（中文版）

## 文档目标

本文档把以下两份设计进一步收敛为可执行迁移路线：

- `docs/design/ai-agent-runtime-optimization-v2.zh-CN.md`
- `docs/design/ai-agent-runtime-execution-audit-draft.zh-CN.md`

本文档重点回答：

1. 先改前端还是先改执行架构
2. 方案 A 应如何分阶段落地
3. 哪些能力必须先上线，哪些能力可以延后
4. 如何在不中断现有 runtime 能力的前提下完成迁移

---

## 一、实施总原则

### 📌 Decision: rollout/ux-before-executor
- **Value**: 先完成前端主交互收敛，再逐步替换执行后端
- **Layer**: presentation
- **Tags**: rollout, frontend, ux
- **Rationale**: 当前用户最强烈的问题是“看起来不像 AI 持续干活”，优先解决交互主链路比优先重做 executor 更能快速验证产品方向
- **Alternatives**: 先重构 executor，再回头调整页面
- **Tradeoffs**: 短期内执行仍部分复用旧路径，但可以更快验证多轮对话与审批体验

### 📌 Decision: rollout/control-plane-first
- **Value**: `exec-service` 先从“本地执行器”重构为“预检 + 审批 + 分发 + 审计网关”，再逐步引入新的 executor backend
- **Layer**: infrastructure
- **Tags**: exec-service, rollout, dispatch
- **Rationale**: 只有先稳定控制平面，后续接 sandbox pod、SSH gateway、OpenStack executor 才不会出现多套协议并存
- **Alternatives**: 直接并行接多个 executor，再慢慢统一控制层
- **Tradeoffs**: 前期会多一层抽象，但中长期复杂度更低

### 📌 Decision: rollout/profile-by-profile
- **Value**: 按 executor profile 逐步上线，而不是一次性切换所有命令类型
- **Layer**: infrastructure
- **Tags**: profile, rollout, executor
- **Rationale**: Kubernetes 只读、Kubernetes 变更、主机 SSH、OpenStack CLI 的风险和依赖差异很大，应拆开验证
- **Alternatives**: 一次性导入全量 executor profile
- **Tradeoffs**: 上线周期更长，但风险更可控

### 📌 Decision: rollout/audit-early
- **Value**: 审计字段模型必须在 executor 切换前先冻结，避免后续 run/event 与 audit 数据断裂
- **Layer**: data
- **Tags**: audit, schema, rollout
- **Rationale**: 若先接新执行器再补审计字段，历史 run 将无法统一回溯
- **Alternatives**: 先跑功能，后补审计字段
- **Tradeoffs**: 前期设计工作更多，但可避免后续大规模返工

### 🚫 Constraint: architecture
- **Rule**: 不允许前端新交互和后端新执行协议同时走多套长期并存路径；灰度期间只能保留一个 canonical runtime contract
- **Priority**: high
- **Tags**: contract, runtime, architecture

### 🚫 Constraint: security
- **Rule**: 在 `ssh_gateway` 与高权限 profile 完成审批与审计闭环前，不允许把系统级命令或主机级变更命令暴露给 AI 默认使用
- **Priority**: critical
- **Tags**: ssh, security, approval

---

## 二、阶段划分

建议分五个阶段推进。

## Phase 1. 前端交互收敛

### 目标

先把 runtime 页面真正做成多轮会话产品，而不是多面板调试页。

### 范围

- 底部固定输入框
- 单列对话流
- assistant transcript
- 低价值状态压缩
- 正式审批弹窗

### 主要改动

前端：

- `frontend/src/pages/AIRuntimePlayground.tsx`
- `frontend/src/features/ai-runtime/components/*`
- `frontend/src/features/ai-runtime/utils/runtimeTranscript.ts`
- 新增底部 composer 与审批弹窗组件

后端：

- 保持现有 `run + event + approve` 协议
- 不急于替换执行 backend

### 交付结果

用户可以：

- 在底部输入框发起和继续多轮会话
- 在一个 assistant 卡片里看见思考、命令、审批、结论
- 对高风险命令进行正式审批

### 验收标准

- 页面主视图不再依赖多 panel
- 审批命令在主消息中可见
- 当前回答与最终回答连续
- 刷新后可恢复 run

---

## Phase 2. Exec 控制平面重构

### 目标

把 `exec-service` 从“本地执行命令”改造成“统一分发与审计网关”。

### 范围

- 预检模型升级
- 命令分类升级
- executor profile 选择
- dispatch 请求模型
- 审计字段冻结

### 主要改动

- `exec-service/core/policy.py`
- `exec-service/api/execute.py`
- `exec-service/core/runtime_service.py`
- 新增：
  - `exec-service/core/dispatch.py`
  - `exec-service/core/executor_registry.py`
  - `exec-service/core/audit_model.py`

### 输出

新的 precheck / dispatch 结果至少返回：

- `command_family`
- `approval_policy`
- `executor_type`
- `executor_profile`
- `target_kind`
- `target_identity`
- `requires_elevation`

### 验收标准

- 即使仍走旧本地执行，控制层也已经能表达“应在哪执行”
- 审计记录结构可覆盖未来 sandbox/SSH/OpenStack executor

---

## Phase 3. Kubernetes executor profile 上线

### 目标

先替换最常用的 Kubernetes 相关执行路径。

### 先上线的 profile

1. `toolbox-k8s-readonly`
2. `toolbox-k8s-mutating`

### 范围

- 动态创建短生命周期 toolbox pod/job
- 只读 profile 自动执行
- 变更 profile 审批后执行
- 输出通过 SSE 回传

### 主要改动

- 新增：
  - `exec-service/executors/k8s_toolbox.py`
  - `exec-service/executors/k8s_stream_adapter.py`
- 部署：
  - toolbox image
  - readonly serviceAccount
  - mutating serviceAccount

### 验收标准

- `kubectl get/logs/describe` 不再在 `exec-service` 本地执行
- `kubectl rollout restart/apply` 需审批后进入高权限 profile
- 审计记录能显示 executor profile、target namespace、service account

---

## Phase 4. SSH gateway 上线

### 目标

支持节点级、系统级、跨节点命令的受控执行。

### 范围

- `ssh_gateway` 服务
- bastion 跳转
- host profile
- 离散命令执行
- host 审批模型

### 主要改动

新增：

- `ssh-gateway/` 或 `exec-service/executors/ssh_gateway.py`
- 远程执行 request/response schema
- 主机命令 profile：
  - `host-ssh-readonly`
  - `host-ssh-elevated`

### 默认策略

- `host-ssh-readonly`
  - 需要审批
  - 仅允许只读系统命令

- `host-ssh-elevated`
  - 默认禁用
  - 仅对特定灰度用户/环境开放

### 验收标准

- 可以通过审批后的单次命令执行检查节点系统状态
- 审计可记录 `jump_host`、`target_host`、`remote_user`
- 前端审批弹窗能显示远程目标信息

---

## Phase 5. OpenStack 与外部控制面 executor

### 目标

补齐云平台和外部控制面场景。

### 范围

- `toolbox-openstack-readonly`
- 凭据注入规范
- 控制面审计

### 默认策略

- 默认只读
- 默认需要审批
- 凭据作用域必须最小化

### 验收标准

- `openstack server list` 等命令可通过专用 executor 执行
- 审计能显示 credential ref、region/project

---

## 三、推荐优先级

## 3.1 P0

- 底部输入框
- 单列 transcript
- 审批弹窗
- 低价值状态压缩
- precheck 结果结构扩展
- 审计字段冻结

## 3.2 P1

- `toolbox-k8s-readonly`
- `toolbox-k8s-mutating`
- executor profile 可视化
- 审计检索页基础能力

## 3.3 P2

- `ssh_gateway`
- 主机级只读命令
- host 审批策略

## 3.4 P3

- `toolbox-openstack-readonly`
- 外部控制面凭据治理
- 完整回放与审计报表

---

## 四、迁移顺序建议

## 4.1 前端迁移顺序

1. 先在 `AIRuntimePlayground` 完成底部输入和 transcript 模式
2. 验证多轮 follow-up、审批弹窗、状态压缩
3. 再抽成通用 runtime conversation shell
4. 最后迁回 `AIAnalysis.tsx`

## 4.2 后端迁移顺序

1. 先扩展 precheck / dispatch schema
2. 再补审计模型
3. 再接 `toolbox-k8s-readonly`
4. 再接 `toolbox-k8s-mutating`
5. 再接 `ssh_gateway`
6. 最后接 `toolbox-openstack-readonly`

---

## 五、对现有模块的处理建议

## 5.1 前端

保留并继续演进：

- `frontend/src/features/ai-runtime/utils/runtimeTranscript.ts`
- `frontend/src/features/ai-runtime/components/RuntimeConversationCard.tsx`

降级为辅助视图：

- `RuntimeActivityPanel`
- `ApprovalPanel`
- `CommandOutputPanel`
- `RunTimeline`

## 5.2 后端

保留：

- 当前 `run + event` 模型
- 当前 ticket/approve 语义

逐步替换：

- `exec-service/core/runner.py`
  - 从最终执行器变为 fallback local executor

新增：

- executor registry
- dispatch layer
- audit model
- sandbox executors
- SSH gateway executor

---

## 六、风险与应对

## 6.1 风险：前端先变，后端执行还没切

应对：

- 先保持控制协议不变
- 用审批弹窗和 transcript 先把产品体验收敛

## 6.2 风险：executor profile 引入后权限边界变复杂

应对：

- 先只做两个 K8s profile
- 所有 profile 在文档和配置中显式声明

## 6.3 风险：SSH 执行带来安全面扩大

应对：

- 只允许离散命令
- 强制审批
- 强制审计
- 默认不开高危 host profile

## 6.4 风险：OpenStack 工具链与凭据管理复杂

应对：

- 单独 executor
- 单独 credential ref
- 不和 busybox/k8s toolbox 混用

---

## 七、阶段性验收清单

### Phase 1 完成时

- 底部输入框可用
- 多轮会话可用
- 审批命令在主消息中可见
- 审批弹窗可用

### Phase 2 完成时

- precheck 能返回 executor profile
- 审计字段结构冻结
- dispatch 层存在

### Phase 3 完成时

- K8s 只读命令走 sandbox toolbox
- K8s 变更命令走审批后的高权限 toolbox

### Phase 4 完成时

- SSH bastion executor 可执行主机级只读命令
- 审计可回溯 jump/target host

### Phase 5 完成时

- OpenStack 只读命令走专用 executor
- 外部控制面审计完整

---

## 八、关联文档

- `docs/design/ai-agent-runtime-optimization-v2.zh-CN.md`
- `docs/design/ai-agent-runtime-execution-audit-draft.zh-CN.md`
- `docs/design/ai-agent-runtime-implementation-v1.md`
