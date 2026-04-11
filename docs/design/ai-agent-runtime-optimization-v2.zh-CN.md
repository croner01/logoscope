# AI Agent Runtime 优化方案 v2

## 文档目标

本文档在现有实施方案 v1 基础上，补充最近一轮产品与架构审视后的优化方向，重点回答四个问题：

1. AI runtime 页面应该如何贴近 `Trae / Cursor` 的对话体验
2. 主视图里哪些内容应该保留，哪些内容应该移除
3. 方案 A 下命令执行应该在哪里发生
4. 审批、提权、执行环境、审计之间的边界如何收敛

---

## 一、核心结论

### 📌 Decision: frontend/runtime-ui-shape
- **Value**: AI runtime 主页面采用“单列对话流 + 底部固定输入框”形态，不再把运行状态拆成多个并列调试面板
- **Layer**: presentation
- **Tags**: frontend, runtime, ux, chat
- **Rationale**: `Trae / Cursor` 风格的 agent 体验强调连续任务叙事，而不是多 panel 状态罗列；多轮会话时底部输入框更符合视线和操作路径
- **Alternatives**: 顶部输入 + 多栏调试台，双栏消息区 + 侧边运行态面板
- **Tradeoffs**: 主体验更聚焦，但开发调试信息需要降级到次级入口

### 📌 Decision: frontend/runtime-main-content
- **Value**: 主视图只保留自然语言叙事、命令块、审批块、最终回答块；低价值状态输出默认不进入主视图
- **Layer**: presentation
- **Tags**: frontend, transcript, runtime
- **Rationale**: 当前大量 `planning/acting/observing`、空标题 thought、纯技术事件对用户没有决策价值，只会稀释 AI 真正干活的过程
- **Alternatives**: 全量事件可视化，timeline 常驻展示
- **Tradeoffs**: 主界面更干净，但需要额外的调试抽屉以保留事件追踪能力

### 📌 Decision: frontend/approval-visibility
- **Value**: 待审批命令必须直接嵌入 assistant 主消息中展示，并由正式审批弹窗承接确认与提权
- **Layer**: presentation
- **Tags**: frontend, approval, security
- **Rationale**: 审批是任务主链路的一部分，不能藏在侧边栏或次要 panel 中；用户必须清楚知道 AI 想执行什么、在哪里执行、需要什么权限
- **Alternatives**: 侧边审批面板，仅在调试区显示审批项
- **Tradeoffs**: assistant 卡片会变大，但任务连续性更强，审批更可理解

### 📌 Decision: execution/dispatch-model
- **Value**: 采用方案 A，`exec-service` 负责命令预检、审批、分发、审计，不再假定命令始终在 `exec-service` 容器内执行
- **Layer**: infrastructure
- **Tags**: exec, runtime, dispatch, audit
- **Rationale**: 直接在 `exec-service` 容器内运行命令难以隔离、审计和扩展到主机级命令、SSH 跳转、OpenStack CLI
- **Alternatives**: 继续在 `exec-service` 内本地执行，单一 busybox pod 模式
- **Tradeoffs**: 架构更复杂，但能获得更好的安全边界、执行弹性和审计能力

### 📌 Decision: execution/targets
- **Value**: 统一执行目标分为 sandbox toolbox pod、privileged toolbox pod、SSH bastion executor、external control plane executor 四类；busybox 仅作为轻量诊断 profile，而不是统一执行环境
- **Layer**: infrastructure
- **Tags**: executor, toolbox, ssh, busybox, openstack
- **Rationale**: busybox 不足以承载 `kubectl`、`helm`、`openstack`、数据库客户端等真实运维工具链，需要 profile 化执行器
- **Alternatives**: 单一 busybox 执行器，单一大杂烩 toolbox 镜像
- **Tradeoffs**: 需要维护多个 profile，但执行能力与权限边界更清晰

### 📌 Decision: security/approval-elevation-model
- **Value**: “提权”定义为执行器 profile、service account、credential scope、目标环境级别的提升，而不是简单映射为 Linux sudo
- **Layer**: cross-cutting
- **Tags**: security, approval, elevation
- **Rationale**: 在当前架构里，真正需要审批的是“允许 AI 使用更高权限执行环境”，而不是允许容器内任意 sudo
- **Alternatives**: 将提权简化为本机 sudo 或单一确认弹窗
- **Tradeoffs**: 审批模型更复杂，但和真实执行风险一致

### 🚫 Constraint: architecture
- **Rule**: 不再继续把 AI runtime 主体验做成多列 debug dashboard；调试事件、payload JSON、seq 等信息必须降级到次级入口
- **Priority**: high
- **Tags**: frontend, architecture, runtime

### 🚫 Constraint: security
- **Rule**: AI 不得获得未受控的持续交互式 SSH shell；远程主机命令必须通过受控 SSH gateway 以离散命令方式执行，并保留审计记录
- **Priority**: critical
- **Tags**: ssh, audit, security

### 🚫 Constraint: security
- **Rule**: 高风险或可变更命令不得在未审批状态下进入高权限 executor profile
- **Priority**: critical
- **Tags**: approval, elevation, exec

---

## 二、页面交互优化方向

## 2.1 页面骨架

建议页面只保留两块主区域：

1. 主对话流
- 历史用户消息
- 当前 AI runtime 消息
- 多轮 follow-up 历史

2. 底部固定输入框
- 默认显示简洁输入区
- 高级选项折叠展开
- 支持继续追问、停止、切换会话

## 2.2 主视图里保留的内容

主视图只保留以下四类：

1. AI 自然语言叙事
- 例如：正在检查 timeout 集中在哪条调用链

2. 命令执行块
- 只有 AI 真正执行命令时出现
- 命令原文直接显示
- stdout/stderr 细节可折叠

3. 审批块
- 必须出现在 assistant 主卡片内部
- 显示命令原文、风险、目标环境、执行器 profile、原因

4. 最终回答块
- 流式阶段显示“当前回答”
- 收尾时显示“最终回答”

## 2.3 主视图里移除的内容

默认不展示：

- `run_started`
- `run_status_changed`
- `current_phase=planning/acting/observing`
- 只有标题、没有 detail 的 thought block
- 原始 event payload
- seq 编号
- tool call started/finished 这类纯技术事件

这些内容移到：

- “查看调试详情”抽屉
- 审计页
- 开发态调试面板

## 2.4 低价值状态压缩规则

建议采用以下规则：

- thought 没有 detail，不展示
- 连续多个空状态合并成一句“正在继续分析...”
- 短时成功命令只显示摘要，不自动展开输出
- 失败命令、待审批命令、长输出命令自动展开
- 只有影响用户判断的内容才进入主 transcript

---

## 三、方案 A 的执行方向

## 3.1 方案 A 的定义

方案 A 不是“把命令从 `exec-service` 改到 busybox”。

方案 A 的正确定义是：

- `exec-service` 负责策略、分发、审批、审计
- 实际命令执行发生在明确的 execution target 上

## 3.2 执行目标分类

### 1. sandbox toolbox pod

适用：

- 只读 Kubernetes 诊断
- 日志、资源、拓扑、配置查询
- 数据库只读查询

示例：

- `kubectl get pods -n islap`
- `kubectl logs deploy/query-service -n islap --tail=200`
- `clickhouse-client --query "select ..."`

### 2. privileged toolbox pod

适用：

- Kubernetes 内的高风险、可变更动作

示例：

- `kubectl rollout restart ...`
- `kubectl apply -f ...`
- `helm upgrade ...`

说明：

- 只有审批通过后才可使用该 profile

### 3. SSH bastion executor

适用：

- 节点级、系统级命令
- 需要通过跳板机进入目标主机的操作

示例：

- `systemctl status kubelet`
- `journalctl -u containerd -n 200`
- `ss -lntp`

### 4. external control plane executor

适用：

- OpenStack 等外部控制面 CLI

示例：

- `openstack server list`
- `openstack hypervisor list`

## 3.3 Busybox 的定位

busybox 可以保留，但只应作为：

- 轻量诊断 profile
- 基础 shell / 网络检查 / 文件检查 profile

不应承担：

- `kubectl`
- `helm`
- `openstack`
- `clickhouse-client`

---

## 四、审批与提权交互优化

## 4.1 提权的真实含义

提权不应简单理解为 Linux sudo。

在本项目里，提权应映射为：

- 更高权限的 executor profile
- 更高权限的 service account
- 更高范围的 kubeconfig / credential scope
- 更高风险的 target host / target cluster 操作

## 4.2 审批弹窗建议字段

审批弹窗应显示：

- 命令原文
- 风险等级
- 命令类型
- executor profile
- 目标环境
- 是否需要提权
- 审批原因
- ticket 有效期
- AI 给出的执行目的

## 4.3 审批交互动作

至少包含：

- 批准并继续
- 拒绝
- 仅复制命令
- 查看完整上下文

---

## 五、与现有实现的偏差

当前实现与目标方案的主要偏差：

1. 输入框仍然偏上，不利于多轮 follow-up
2. 主视图仍保留较多低价值状态输出
3. 审批虽已进入主卡片，但仍缺少正式审批弹窗与执行环境说明
4. 命令仍在 `exec-service` 容器内执行
5. `openstack` 尚未纳入执行 profile
6. SSH 跳转执行与主机级审计尚未落地

---

## 六、实施优先级

建议按以下顺序推进：

1. 底部固定输入框与多轮会话交互
2. 主 transcript 继续压缩低价值状态
3. 正式审批弹窗
4. executor profile 模型
5. sandbox toolbox pod
6. SSH bastion executor
7. external control plane executor（OpenStack）
8. 全链路审计与回放

---

## 七、关联文档

- `docs/design/ai-agent-runtime-implementation-v1.md`
- `docs/design/ai-agent-runtime-implementation-v2.zh-CN.md`
- `docs/design/ai-agent-runtime-refactor-draft.md`
- `docs/design/ai-agent-runtime-task-breakdown-v1.md`
- `docs/design/ai-agent-runtime-task-breakdown-v1.zh-CN.md`
- `docs/design/ai-agent-runtime-execution-audit-draft.zh-CN.md`
