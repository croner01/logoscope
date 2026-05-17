# OpenHands 替换 ai-service 智能体分析层方案

## 结论

不建议用 OpenHands 整体替换 `ai-service`。

推荐方案是保留 `ai-service` 作为产品控制面，只把 runtime v4 的内层 agent backend 替换为 OpenHands：

- `ai-service` 继续负责 `/api/v2` 的 `thread-run-action` 契约、run 状态机、事件协议、审批恢复、快照和前端兼容。
- `exec-service` 继续作为命令执行、策略预检、人工审批、审计回放的唯一真源。
- OpenHands 只负责分析、规划、工具意图生成、skills/MCP 接入，不直接执行高风险命令。

这个边界能获得 OpenHands 的 agent 编排收益，同时不破坏现有安全边界和产品协议。

## 为什么不能整体替换 ai-service

当前 `ai-service` 不只是 LLM agent，它还承担这些产品控制面职责：

- `/api/v2` 运行态 API：thread、run、action、events、stream。
- 审批状态机：`approval_required -> resolve -> resume/replan/blocked`。
- 前端事件协议：reasoning、tool call、approval、action 状态映射。
- 运行快照和刷新恢复能力。
- product-native `ai.skills`：`DiagnosticSkill -> SkillStep -> command_spec`。
- 与 `exec-service` 的 precheck、OPA、target、audit 链路。
- legacy `/api/v1/ai`、知识库、案例库、历史接口。

OpenHands 可以替换 agent 内核，但不能直接替代这些业务协议。

## 能力替换矩阵

| 能力 | 当前归属 | 替换策略 |
|---|---|---|
| `/api/v2` control plane | `ai-service` | 保留 |
| run 状态机和快照 | `ai-service` | 保留 |
| 人工审批 | `ai-service` + `exec-service` | 保留 |
| precheck / OPA / audit | `exec-service` | 保留 |
| LangGraph 内层规划 | runtime v4 | 可替换为 OpenHands backend |
| product-native skills | `ai.skills` | 第一阶段复用，映射到 OpenHands tool intent |
| MCP / 外部工具生态 | OpenHands | 后续接入 |
| legacy `/api/v1/ai` | `ai-service` | 暂不纳入 |
| KB / case / history | `ai-service` | 暂不纳入 |

## 目标架构

```text
Frontend
  -> ai-service /api/v2
      -> RuntimeV4OrchestrationBridge
          -> RuntimeBackend
              -> LangGraphBackend 或 OpenHandsBackend
          -> action intent / command_spec
      -> execute_command
          -> exec-service precheck / approval / execute / audit
```

关键原则：

1. OpenHands backend 只产出意图，不成为执行真源。
2. 所有命令，包括 OpenHands 规划动作，都必须转换为现有 `command_spec`。
3. 风险命令仍由 `exec-service` 和现有人工审批链判定。
4. skills 先复用现有 `ai.skills`，避免丢掉结构化诊断步骤。
5. `/api/v2` 返回结构和事件语义保持兼容。

## 已落地的第一阶段

当前分支已经实现：

- 新增 runtime v4 backend 抽象：`LangGraphBackend`、`OpenHandsBackend`。
- 支持请求级 backend 选择：`RunCreateRequest.runtime_backend="openhands"`。
- OpenHands backend feature flag：必须设置 `AI_RUNTIME_V4_OPENHANDS_ENABLED=true`。
- 快照中保留 `engine.inner=openhands-v1`。
- OpenHands planning preview 写入 run summary。
- `/api/v2/runs/{run_id}/actions` 在没有真实 action event 时回退展示 preview actions。
- `/api/v2/runs/{run_id}/actions/command` 支持只传 `action_id` 执行 preview action，内部补齐 `command_spec` 后继续走现有执行链。
- OpenHands backend 接入现有 `ai.skills` matcher，能把 `SkillStep` 映射成 `command.exec` preview action。
- preview action 保留 `skill_name`、`step_id`，便于前端展示和审计解释。
- `create_run` 在启动 outer Temporal run 前先做 OpenHands backend/provider readiness 校验，避免配置错误时写入脏 run。
- 已接入真实 `openharness` 包，但运行在独立 helper 子进程里，不直接嵌入 `ai-service` 主进程。
- helper 通过 capture-only tool registry 只产出 `generic_exec` / `kubectl_clickhouse_query` 意图，不直接执行命令。
- provider 产出的动作会与本地 `ai.skills` 产出的 preview action 合并，避免丢失已有结构化诊断步骤。

## 为什么要走隔离 helper，而不是直接把 OpenHands 装进 ai-service

已经确认 `openharness-ai==0.1.7` 会把 `starlette`、`anyio` 等依赖升级到与当前 `ai-service` FastAPI 栈不兼容的版本。

这意味着如果直接把 OpenHands 装进主运行时，会有两个后果：

1. `ai-service` 本身可能起不来，或者 runtime 行为不稳定。
2. 即使能起，也会把 OpenHands 默认工具链直接带进主进程，存在绕过 `exec-service` 的风险。

因此当前实现采用：

- 主进程 `/opt/venv`：继续承载 `ai-service`
- 独立 `/opt/openharness-venv`：只承载 `openharness-ai`
- `SubprocessOpenHandsProvider`：通过 helper 调用真实 OpenHarness `QueryEngine`
- capture-only tool registry：只输出工具意图，执行仍回到 `exec-service`

这个方案的核心收益是：既能真实调用 OpenHarness 模块，又不破坏既有安全边界和依赖稳定性。

## 风险与控制

高风险点：

- OpenHands 直接执行命令，绕过 `exec-service`。
- OpenHands 内置确认语义与现有审批语义冲突。
- skills 迁移为纯 prompt 后丢失 `SkillStep -> command_spec` 的结构化能力。
- 过早改动 legacy `/api/v1/ai`、KB、case、history，导致替换范围失控。

控制策略：

- 第一阶段只接 runtime v4 backend，不动 legacy API。
- OpenHands backend 输出全部归一为 runtime command payload。
- 风险命令是否需要人工审批，只由现有执行链决定。
- OpenHands 灰度必须显式 feature flag。
- 通过测试覆盖 engine 快照、actions preview、preview execution、skills metadata。

## 后续路线

1. 用真实 OpenHands SDK session 替换当前 skeleton，但仍只输出 tool intent。
2. 接入 OpenHands MCP 工具目录，统一映射到 `command_spec` 或受控 tool request。
3. 增加 OpenHands event 到 runtime event 的映射：thought、tool_call、tool_result、needs_input。
4. 扩展 action 状态：planned、waiting_approval、running、completed、failed、skipped。
5. 完成镜像构建和 K8s 灰度 smoke：readonly 自动动作、风险命令人工审批、reject 后 blocked、audit replay。
