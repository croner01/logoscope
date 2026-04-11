# AI Agent Runtime 执行架构与审计设计草案

## 文档目标

本文档细化方案 A 的执行架构与审计设计，回答以下问题：

1. 命令最终在哪里执行
2. 如何支持 Kubernetes、主机级命令、SSH 跳转、OpenStack CLI
3. 如何实现审批、提权、执行分发、输出回流
4. 如何做到可追溯、可审计、可回放

---

## 一、设计原则

### 📌 Decision: execution/service-role
- **Value**: `exec-service` 仅负责预检、审批、分发、状态聚合与审计落盘，不再默认承担命令本地执行器角色
- **Layer**: infrastructure
- **Tags**: exec-service, dispatch, audit
- **Rationale**: 直接在 `exec-service` 容器内执行命令会混淆控制平面与执行平面，限制隔离、审计和扩展能力
- **Alternatives**: 继续本地执行，统一 busybox 执行器
- **Tradeoffs**: 调度链路更长，但边界更清晰，后续扩展更稳

### 📌 Decision: execution/target-abstraction
- **Value**: 所有命令先映射为 `execution target + executor profile + approval policy`，再决定如何执行
- **Layer**: business
- **Tags**: target, policy, executor
- **Rationale**: 同一条命令的风险不仅由命令文本决定，还取决于执行位置、执行身份和目标环境
- **Alternatives**: 仅按命令头做白名单分类
- **Tradeoffs**: 模型更复杂，但更接近真实运维风险

### 📌 Decision: execution/ssh-model
- **Value**: 主机级命令通过受控 `SSH bastion executor` 以离散命令方式执行，不向 AI 暴露自由交互式远程 shell
- **Layer**: infrastructure
- **Tags**: ssh, bastion, host
- **Rationale**: 离散命令便于审计、审批、限权和回放；自由 shell 难以控制与追踪
- **Alternatives**: AI 直接 SSH 到目标机，自由交互 shell
- **Tradeoffs**: 灵活性略低，但安全与可审计性显著提升

### 📌 Decision: audit/three-layer-model
- **Value**: 审计必须覆盖任务层、审批层、执行层三层数据，而不是只记录命令字符串
- **Layer**: data
- **Tags**: audit, traceability, compliance
- **Rationale**: 仅记录命令无法回答“为什么执行、谁批准、在哪里执行、以什么身份执行”
- **Alternatives**: 仅记录 stdout/stderr 与命令文本
- **Tradeoffs**: 审计模型更重，但能满足回溯与合规要求

### 📌 Decision: execution/busybox-role
- **Value**: busybox 仅作为轻量诊断 executor profile，不作为统一执行环境
- **Layer**: infrastructure
- **Tags**: busybox, toolbox
- **Rationale**: busybox 无法承载 kubectl、helm、openstack、数据库客户端等真实运维工具链
- **Alternatives**: busybox 作为唯一执行器
- **Tradeoffs**: 需要维护额外 toolbox 镜像，但能力边界清晰

### 🚫 Constraint: security
- **Rule**: 高风险 profile、主机级命令和外部控制面命令必须经过审批后才能执行
- **Priority**: critical
- **Tags**: approval, security, exec

### 🚫 Constraint: security
- **Rule**: 不允许 AI 获取持续交互式 root shell；所有远程执行必须以“单次离散命令”形式经过审计链路
- **Priority**: critical
- **Tags**: ssh, host, root

### 🚫 Constraint: architecture
- **Rule**: 执行平面必须与 `exec-service` 控制平面分离，避免命令长期在 API 服务容器内本地执行
- **Priority**: high
- **Tags**: architecture, exec-service

---

## 二、总体架构

## 2.1 角色划分

### 1. `ai-service`

负责：

- 规划与推理
- 选择工具
- 接收执行结果
- 继续生成后续动作或最终回答

### 2. `exec-service`

负责：

- 命令归一化
- 风险分类
- 预检
- 审批状态管理
- executor 分发
- 输出流聚合
- 审计落盘

### 3. executor backend

负责：

- 在特定环境中真实执行命令
- 回传 stdout/stderr/status
- 执行结束后提交结果

---

## 三、执行目标模型

## 3.1 execution target

建议统一定义：

- `target_kind`
  - `k8s_resource`
  - `k8s_cluster`
  - `host_node`
  - `host_vm`
  - `openstack_control_plane`
  - `database`

- `target_identity`
  - 集群、命名空间、资源名、节点名、主机名、region、project 等

- `executor_type`
  - 实际执行后端类型

- `executor_profile`
  - 具体权限与工具组合

## 3.2 executor type

### 1. `sandbox_pod`

执行位置：

- K8s 集群内短生命周期 toolbox pod / job

适用：

- 只读 K8s 命令
- 基础日志查询
- 只读数据库命令

### 2. `privileged_sandbox_pod`

执行位置：

- K8s 集群内高权限 toolbox pod / job

适用：

- `kubectl apply`
- `kubectl rollout restart`
- `helm upgrade`

### 3. `ssh_gateway`

执行位置：

- 受控 bastion / SSH gateway
- 再由其跳转到目标主机

适用：

- `systemctl status kubelet`
- `journalctl -u containerd`
- `df -h`
- `ss -lntp`

### 4. `external_control_plane`

执行位置：

- 专用 OpenStack 或其他平台 executor

适用：

- `openstack server list`
- `openstack hypervisor list`

---

## 四、executor profile 设计

## 4.1 推荐 profile 列表

### `busybox-readonly`

工具：

- `sh`
- `cat`
- `ls`
- `echo`
- `nslookup`
- `wget/curl`

用途：

- 极简轻量诊断

### `toolbox-k8s-readonly`

工具：

- `kubectl`
- `helm`
- `curl`
- `jq`
- `grep`
- `sed`
- `awk`

权限：

- 只读 serviceAccount / kubeconfig

### `toolbox-k8s-mutating`

工具：

- 同 `toolbox-k8s-readonly`

权限：

- 更高权限 serviceAccount / kubeconfig scope

### `toolbox-db-readonly`

工具：

- `clickhouse-client`
- `curl`
- `jq`

### `toolbox-openstack-readonly`

工具：

- `openstack`
- `curl`
- `jq`
- 必要 Python runtime

### `host-ssh-readonly`

工具：

- 由 bastion 执行目标主机上的只读命令

### `host-ssh-elevated`

工具：

- 由 bastion 执行高风险主机级命令

说明：

- 默认禁用
- 只有明确审批和灰度开关后才允许

---

## 五、命令分类与分发

## 5.1 命令分类模型

建议在当前 `command head + read/write` 模型基础上，增加：

- `command_family`
  - `kubernetes`
  - `linux_readonly`
  - `linux_mutating`
  - `openstack`
  - `database`
  - `http_query`

- `approval_policy`
  - `auto`
  - `confirm`
  - `elevated_confirm`
  - `forbidden`

- `recommended_executor_profile`

## 5.2 例子

### `kubectl get pods -n islap`

- `command_family = kubernetes`
- `approval_policy = auto`
- `executor_profile = toolbox-k8s-readonly`

### `kubectl rollout restart deployment/frontend -n islap`

- `command_family = kubernetes`
- `approval_policy = elevated_confirm`
- `executor_profile = toolbox-k8s-mutating`

### `systemctl status kubelet`

- `command_family = linux_readonly`
- `approval_policy = confirm`
- `executor_profile = host-ssh-readonly`

### `openstack server list`

- `command_family = openstack`
- `approval_policy = confirm`
- `executor_profile = toolbox-openstack-readonly`

---

## 六、审批与提权状态机

## 6.1 状态定义

- `draft`
- `prechecked`
- `approval_required`
- `approved`
- `rejected`
- `dispatching`
- `running`
- `completed`
- `failed`
- `cancelled`

## 6.2 关键转换

### 1. `prechecked -> approval_required`

触发条件：

- 命令需要确认
- 或命令需要更高权限 profile

### 2. `approval_required -> approved`

触发条件：

- 用户审批通过
- ticket 有效
- profile 与目标环境匹配

### 3. `approved -> dispatching -> running`

触发条件：

- `exec-service` 选定 executor backend
- 创建执行会话

### 4. `running -> completed/failed/cancelled`

触发条件：

- executor 返回终态

---

## 七、SSH bastion executor 设计

## 7.1 为什么需要 bastion executor

目标：

- 执行系统级命令
- 跳转其他节点
- 访问主机环境
- 仍然保持可审计

## 7.2 不推荐的方式

不要：

- 让 AI 自由 SSH
- 给 AI 一个可交互 shell
- 在 frontend 直接拼 SSH 命令
- 在 `exec-service` 容器里长期保存 SSH 私钥并自由使用

## 7.3 推荐方式

`exec-service` 生成一个离散执行请求：

- 目标主机
- 跳板机
- 远程用户
- 命令文本
- 审批票据
- trace/audit 元数据

然后发给 `ssh_gateway`：

- 使用短时凭据
- 连接 jump host
- 再跳转 target host
- 执行单次命令
- 回传 stdout/stderr/exit code
- 记录 session metadata

## 7.4 SSH 审计必须记录

- `jump_host`
- `target_host`
- `remote_user`
- `credential_ref`
- `requested_command`
- `executed_command`
- `approved_by`
- `started_at`
- `ended_at`
- `stdout`
- `stderr`
- `exit_code`

---

## 八、审计模型

## 8.1 三层审计

### 1. 任务层

回答：

- 为什么执行这条命令

字段建议：

- `run_id`
- `session_id`
- `message_id`
- `tool_call_id`
- `action_id`
- `user_prompt`
- `reasoning_summary`
- `subgoal_id`

### 2. 审批层

回答：

- 谁批准了什么

字段建议：

- `approval_id`
- `approval_policy`
- `approval_actor`
- `approval_result`
- `approval_reason`
- `ticket_id`
- `approved_at`
- `expires_at`

### 3. 执行层

回答：

- 到底在哪里、以什么身份、跑了什么

字段建议：

- `command_run_id`
- `executor_type`
- `executor_profile`
- `target_kind`
- `target_cluster`
- `target_namespace`
- `target_pod`
- `target_node`
- `target_host`
- `jump_host`
- `container_image`
- `service_account`
- `credential_ref`
- `remote_user`
- `command`
- `stdout`
- `stderr`
- `exit_code`
- `duration_ms`
- `started_at`
- `ended_at`

## 8.2 审计存储建议

建议在现有 runtime run / event 基础上新增：

- `command_runs` 扩展字段
- `command_run_events`
- `approval_records`
- `execution_audit_records`

如果继续使用 ClickHouse，建议：

- runtime 事件表保留流式回放能力
- 审计表保留结构化检索能力

---

## 九、前端审批弹窗所需字段

审批弹窗应显示：

- 命令原文
- 命令类型
- 风险等级
- `executor_profile`
- `executor_type`
- `target_kind`
- `target_identity`
- `jump_host`
- `target_host`
- 是否需要提权
- AI 给出的执行目的
- ticket 剩余有效期

---

## 十、与当前实现的差距

当前系统尚未完成：

1. `exec-service` 与执行平面分离
2. executor profile 化
3. SSH bastion executor
4. OpenStack 专用 executor
5. 三层审计落盘
6. 审批弹窗中的环境/权限说明

---

## 十一、分阶段落地建议

### Phase 1

- 保持现有 precheck/ticket 模型
- 新增 `executor_profile` 字段
- 前端审批弹窗显示执行环境

### Phase 2

- 引入 `sandbox_pod` / `privileged_sandbox_pod`
- 默认 K8s 与 DB 命令不再在 `exec-service` 本地执行

### Phase 3

- 引入 `ssh_gateway`
- 支持主机级离散命令

### Phase 4

- 引入 `openstack executor`
- 完善凭据与审计管理

### Phase 5

- 完整审计检索、回放、报表

---

## 十二、关联文档

- `docs/design/ai-agent-runtime-optimization-v2.zh-CN.md`
- `docs/design/ai-agent-runtime-implementation-v1.md`
- `docs/design/ai-agent-runtime-refactor-draft.md`
