# AI Agent Runtime 任务分解 v1（中文版）

## 文档目的

本文档将实施方案 v1 进一步细化为可执行的模块级、文件级任务，便于：

- 研发拆分
- 排期评估
- 并行协作
- 回归测试规划

---

## 1. 基本工作规则

### 规则 1

除迁移胶水代码外，不再继续向以下两个位置堆叠主要逻辑：

- `frontend/src/pages/AIAnalysis.tsx`
- `ai-service/api/ai.py`

### 规则 2

新能力优先进入新模块，新入口先代理到新模块，再逐步回收旧逻辑。

### 规则 3

运行中的 AI 调查会话，其唯一事实来源是：

- 后端 run 快照
- 后端 run event 日志

而不是前端本地 placeholder 消息状态。

---

## 2. 建议拆分为四条工作流

## Track A. 后端 run/event 基础能力

负责范围：

- `ai-service`
- run/event 存储与接口
- runtime 编排基础设施

## Track B. Exec 流式执行基础能力

负责范围：

- `exec-service`
- 命令生命周期
- stdout/stderr 实时输出

## Track C. 前端 runtime 壳与页面重构

负责范围：

- `frontend`
- run event 状态管理
- 对话区、时间线、审批区渲染

## Track D. 迁移、兼容与测试

负责范围：

- 新旧接口并存
- 灰度开关
- 集成回归

---

## 3. Track A：后端 Run/Event 基础能力

## A1. 新建 runtime 包

### 目标

把新的运行态能力从旧 follow-up 辅助函数里解耦出来。

### 建议新增文件

- `ai-service/ai/agent_runtime/__init__.py`
- `ai-service/ai/agent_runtime/models.py`
- `ai-service/ai/agent_runtime/event_protocol.py`
- `ai-service/ai/agent_runtime/status.py`
- `ai-service/ai/agent_runtime/emitter.py`
- `ai-service/ai/agent_runtime/store.py`
- `ai-service/ai/agent_runtime/service.py`

### 任务

- 定义 `AgentRun`
- 定义 `RunEvent`
- 定义 `ToolCall`
- 定义 `CommandRunRef`
- 定义 event type 常量
- 定义 run status 流转规则
- 定义 event append 接口

### 注意

这一阶段先不迁移全部工具执行逻辑，先把运行时结构搭起来。

---

## A2. 增加 run 持久化适配层

### 目标

在不破坏现有 AI 历史记录的前提下，引入 run 和 event 的持久化。

### 可复用模块

- `ai-service/ai/session_history.py`
- `ai-service/ai/followup_persistence_helpers.py`

### 任务

- 增加 run 级存储接口
- 增加 event append/list 接口
- 增加 run snapshot 查询接口
- 确定是扩展现有表还是新增 run/event 表

### 建议接口

- `create_run(...)`
- `update_run_status(...)`
- `append_run_event(...)`
- `list_run_events(run_id, after_seq=None, limit=...)`
- `get_run(run_id)`

### 待决策

正式实现前，需要确认 run/event 存储采用：

- 新 ClickHouse 表
- Redis 热存 + ClickHouse 落盘
- 混合模式

---

## A3. 新增 canonical run API

### 目标

让前端直接面对 run 生命周期，而不是旧 follow-up 返回体。

### 需要修改

- `ai-service/api/ai.py`

### 新接口

- `POST /api/v1/ai/runs`
- `GET /api/v1/ai/runs/{run_id}`
- `GET /api/v1/ai/runs/{run_id}/events`
- `POST /api/v1/ai/runs/{run_id}/cancel`
- `POST /api/v1/ai/runs/{run_id}/approve`

### 任务

- 接收问题并创建 `run_id`
- 在 run 初始化阶段立即创建 `assistant_message_id`
- 返回 run snapshot
- 支持事件回放
- 支持审批决策后恢复运行

---

## A4. 新增 canonical stream 接口

### 目标

对外只提供一套统一的运行事件流。

### 需要修改

- `ai-service/api/ai.py`
- 新 runtime emitter 模块

### 新接口

- `GET /api/v1/ai/runs/{run_id}/stream`

### 任务

- 先支持 SSE
- 只发送 canonical event
- 事件必须带 `seq`
- 支持 `after_seq` 断点续传

### 注意

不要继续把 legacy event 映射层做得更复杂。新协议应当成为最终主路径。

---

## A5. Runtime 编排骨架

### 目标

先在新的 run/event 外壳下跑起来，哪怕内部暂时还复用旧回答路径。

### 建议新增文件

- `ai-service/ai/agent_runtime/orchestrator.py`
- `ai-service/ai/agent_runtime/context_builder.py`
- `ai-service/ai/agent_runtime/finalizer.py`

### 任务

- 初始化 run
- 构造上下文
- 发出 planning 事件
- 先调用旧 answer path 跑通最小链路
- 发出 final answer event

### 价值

可以让新架构尽早形成闭环，不必等全部原生 agent loop 完成后再联调。

---

## A6. 原生迭代式 agent loop

### 目标

用“计划 -> 工具 -> 观察 -> 再判断”的循环替代一次性 answer-first 模型。

### 建议新增文件

- `ai-service/ai/agent_runtime/loop.py`
- `ai-service/ai/agent_runtime/memory.py`
- `ai-service/ai/agent_runtime/decision.py`

### 任务

- 实现迭代上限
- 实现 stop criteria
- 模型先输出下一步动作，而不是一次性最终答案
- 维护内部 scratch state
- 把 observation 摘要写回运行内存

### 依赖

- 依赖 Track B 的命令流式执行能力

---

## A7. 工具适配层

### 目标

把工具调用从 prompt helper 里抽出来，成为 runtime 的一等公民。

### 建议新增文件

- `ai-service/ai/agent_runtime/tools/__init__.py`
- `ai-service/ai/agent_runtime/tools/logs_query.py`
- `ai-service/ai/agent_runtime/tools/traces_query.py`
- `ai-service/ai/agent_runtime/tools/topology_query.py`
- `ai-service/ai/agent_runtime/tools/kb_search.py`
- `ai-service/ai/agent_runtime/tools/command_precheck.py`
- `ai-service/ai/agent_runtime/tools/command_execute.py`
- `ai-service/ai/agent_runtime/tools/command_stream.py`

### 可复用现有代码

- `ai-service/ai/request_flow_agent.py`
- `ai-service/ai/followup_command.py`
- `ai-service/ai/followup_confirmation_ticket_helpers.py`
- `ai-service/ai/kb_route_helpers.py`

### 任务

- 定义每个 tool 的输入输出 schema
- 把只读 observation helper 从 `langchain_runtime/tools.py` 中迁出
- 给每个 tool call 分配 `tool_call_id`
- 给每个 tool call 增加状态和摘要输出

---

## A8. LangChain 降级为辅助层

### 目标

LangChain 只保留为 prompt 和 structured output 支持层，不再充当 runtime 核心。

### 需要修改

- `ai-service/ai/langchain_runtime/service.py`
- `ai-service/ai/langchain_runtime/tools.py`

### 任务

- 收缩职责到 prompt build / parse
- 不再承载 agent orchestration
- 逐步把 tool observation 逻辑迁入 runtime 工具层

---

## 4. Track B：Exec 流式执行基础能力

## B1. 增加命令运行状态存储

### 目标

把命令运行抽象为可查询、可回放、可取消的一等对象。

### 建议新增文件

- `exec-service/core/run_store.py`
- `exec-service/core/event_store.py`

### 任务

- 创建 command run 记录
- 记录输出事件
- 提供运行快照查询
- 设计保留策略

---

## B2. 用异步流式执行替换阻塞执行

### 目标

把当前 `subprocess.run` 改成真正的实时输出模式。

### 需要修改

- `exec-service/core/runner.py`

### 任务

- 替换阻塞执行路径
- 使用异步子进程执行
- 进程存活时持续读取 stdout/stderr
- 生成 output delta 事件
- 支持 timeout 和 cancel

### 说明

v1 可以先使用标准 stdout/stderr pipe，不必强依赖 PTY。

---

## B3. 新增命令运行接口

### 需要修改

- `exec-service/api/execute.py`

### 新接口

- `POST /api/v1/exec/runs`
- `GET /api/v1/exec/runs/{command_run_id}`
- `GET /api/v1/exec/runs/{command_run_id}/events`
- `POST /api/v1/exec/runs/{command_run_id}/cancel`

### 任务

- 启动命令运行
- 返回命令快照
- 返回命令事件流
- 支持取消命令
- 保持 audit 记录能力

---

## B4. 审批与策略接入命令生命周期

### 可复用现有代码

- `exec-service/core/policy.py`
- `exec-service/core/ticket_store.py`
- `exec-service/core/audit_store.py`

### 任务

- 保留现有 precheck 语义
- 高风险命令必须要求 confirmation ticket
- 增加 blocked / waiting / executed / failed / cancelled 状态事件

---

## B5. AI runtime 订阅 exec 事件

### 目标

AI runtime 不再等待命令结束后一次性拿 stdout/stderr，而是实时消费 exec 事件。

### 需要新增/修改

- `ai-service/ai/agent_runtime/tools/command_execute.py`
- `ai-service/ai/agent_runtime/tools/command_stream.py`

### 任务

- 调起 exec run
- 订阅 exec stream
- 将命令事件翻译为 AI canonical event
- 对命令输出做摘要，供下一轮决策使用

---

## 5. Track C：前端 Runtime 壳与页面重构

## C1. 新建 AI runtime feature 区域

### 目标

从当前超大页组件中拆出运行态相关能力。

### 建议新增目录

- `frontend/src/features/ai-runtime/hooks/`
- `frontend/src/features/ai-runtime/components/`
- `frontend/src/features/ai-runtime/state/`
- `frontend/src/features/ai-runtime/types/`

### 建议新增文件

- `frontend/src/features/ai-runtime/types/events.ts`
- `frontend/src/features/ai-runtime/types/run.ts`
- `frontend/src/features/ai-runtime/state/runEventReducer.ts`
- `frontend/src/features/ai-runtime/hooks/useAgentRun.ts`
- `frontend/src/features/ai-runtime/hooks/useRunEvents.ts`
- `frontend/src/features/ai-runtime/components/RunTimeline.tsx`
- `frontend/src/features/ai-runtime/components/ConversationPane.tsx`
- `frontend/src/features/ai-runtime/components/ToolCallCard.tsx`
- `frontend/src/features/ai-runtime/components/CommandOutputPanel.tsx`
- `frontend/src/features/ai-runtime/components/ApprovalPanel.tsx`
- `frontend/src/features/ai-runtime/components/RunHeader.tsx`
- `frontend/src/features/ai-runtime/components/EvidenceRail.tsx`

---

## C2. 扩展前端 API 客户端

### 需要修改

- `frontend/src/utils/api.ts`

### 任务

- 增加 `createAiRun`
- 增加 `getAiRun`
- 增加 `listAiRunEvents`
- 增加 `streamAiRun`
- 增加 `approveAiRunAction`
- 增加 `cancelAiRun`
- 增加 `streamExecRunEvents`

### 约束

不要在新的 API 客户端层继续建立 placeholder merge 假设。

---

## C3. 替换本地 placeholder 流路径

### 需要修改

- `frontend/src/pages/AIAnalysis.tsx`

### 任务

- 移除主路径中的 `local-stream-*`
- 统一使用后端分配的 `assistant_message_id`
- 从 `run snapshot + events` 恢复页面状态
- 由 reducer 驱动消息与时间线更新

### 迁移策略

过渡期可通过 feature flag 保留旧路径作为兜底。

---

## C4. 页面布局重构

### 目标

让运行态信息成为页面主轴，而不是埋在右侧长结果卡片里。

### 当前问题

现在页面把以下能力叠在同一大区块：

- 分析摘要
- KB 提交
- 相似案例
- 快速操作
- 追问对话
- 审批
- 动作草案

### v1 布局建议

- 顶部：页面标题 + 运行概览
- 左上或上方：分析输入和上下文
- 中央主区域：对话 + 运行时间线
- 右侧边栏：证据、相似案例、知识库、后处理动作

### 需要实现的最小优化

- 把 conversation 区域和 KB/相似案例分离
- 把 tool call / command output 做成一等展示块
- 把 approval 面板从消息 metadata 中抽出来

### 不建议在 v1 先做的事情

- 纯视觉 polish
- 大规模动效优化
- 复杂拖拽布局

---

## C5. 事件 reducer 与回放能力

### 目标

支持刷新恢复、断线重连和运行态回放。

### 任务

- 从 canonical events 构建 reducer
- 支持事件回放
- 支持 replay 后继续 append
- 推导出视图状态：
  - assistant 消息
  - tool call
  - command output
  - approval
  - run status

---

## C6. 审批交互闭环

### 目标

审批必须作用于真实 run，而不是只作用于某条消息局部状态。

### 任务

- 在 run timeline 中显示待审批动作
- 在抽屉或 panel 中展示审批细节
- 通过 run approve API 提交决策
- 决策后继续在同一个 run 中恢复输出

---

## 6. Track D：迁移、兼容与测试

## D1. Feature Flag

### 目标

实现灰度发布，降低迁移风险。

### 建议开关

- `AI_AGENT_RUNTIME_V1_ENABLED`
- `AI_AGENT_RUNTIME_STREAM_ENABLED`
- `AI_AGENT_RUNTIME_EXEC_STREAM_ENABLED`
- `AI_AGENT_RUNTIME_UI_ENABLED`

### 可能涉及文件

- `deploy/ai-service.yaml`
- 前端 env 配置

---

## D2. 入口兼容

### 目标

在迁移期间保留当前分析链路可用。

### 任务

- 保留现有分析接口
- 保留历史读取接口
- 可选地把旧 follow-up 请求包装到新 run 模型下执行

---

## D3. 历史会话迁移策略

### 目标

老历史可读，新运行态可回放。

### 任务

- 定义旧 session 是否增加 `latest_run_id`
- 定义旧 assistant message 是否只读展示
- 定义 run replay 是否只对新 runtime 会话生效

### 建议

老会话继续可查看，但只要求新 runtime 生成的运行支持事件回放。

---

## D4. 发布阶段建议

### 阶段 1

run/event API 落地但不开启默认路径。

### 阶段 2

exec 流式执行落地但不开启默认路径。

### 阶段 3

前端新 runtime 壳对内灰度。

### 阶段 4

AI 分析页默认走新 runtime 主路径。

### 阶段 5

移除旧 placeholder stream 主路径。

---

## 7. 文件级改动清单

## 后端新增文件

- `ai-service/ai/agent_runtime/__init__.py`
- `ai-service/ai/agent_runtime/models.py`
- `ai-service/ai/agent_runtime/event_protocol.py`
- `ai-service/ai/agent_runtime/status.py`
- `ai-service/ai/agent_runtime/emitter.py`
- `ai-service/ai/agent_runtime/store.py`
- `ai-service/ai/agent_runtime/service.py`
- `ai-service/ai/agent_runtime/orchestrator.py`
- `ai-service/ai/agent_runtime/loop.py`
- `ai-service/ai/agent_runtime/context_builder.py`
- `ai-service/ai/agent_runtime/memory.py`
- `ai-service/ai/agent_runtime/finalizer.py`
- `ai-service/ai/agent_runtime/tools/*.py`

## 后端修改文件

- `ai-service/api/ai.py`
- `ai-service/ai/request_flow_agent.py`
- `ai-service/ai/session_history.py`
- `ai-service/ai/followup_command.py`
- `ai-service/ai/followup_confirmation_ticket_helpers.py`
- `ai-service/ai/langchain_runtime/service.py`
- `ai-service/ai/langchain_runtime/tools.py`

## Exec 新增文件

- `exec-service/core/run_store.py`
- `exec-service/core/event_store.py`

## Exec 修改文件

- `exec-service/core/runner.py`
- `exec-service/api/execute.py`
- `exec-service/main.py`

## 前端新增文件

- `frontend/src/features/ai-runtime/types/events.ts`
- `frontend/src/features/ai-runtime/types/run.ts`
- `frontend/src/features/ai-runtime/state/runEventReducer.ts`
- `frontend/src/features/ai-runtime/hooks/useAgentRun.ts`
- `frontend/src/features/ai-runtime/hooks/useRunEvents.ts`
- `frontend/src/features/ai-runtime/components/RunHeader.tsx`
- `frontend/src/features/ai-runtime/components/ConversationPane.tsx`
- `frontend/src/features/ai-runtime/components/RunTimeline.tsx`
- `frontend/src/features/ai-runtime/components/ToolCallCard.tsx`
- `frontend/src/features/ai-runtime/components/CommandOutputPanel.tsx`
- `frontend/src/features/ai-runtime/components/ApprovalPanel.tsx`
- `frontend/src/features/ai-runtime/components/EvidenceRail.tsx`

## 前端修改文件

- `frontend/src/pages/AIAnalysis.tsx`
- `frontend/src/utils/api.ts`
- `frontend/src/hooks/useNavigation.ts`

---

## 8. 测试拆分

## 后端测试

### 建议新增

- `ai-service/tests/test_agent_runtime_models.py`
- `ai-service/tests/test_agent_runtime_status.py`
- `ai-service/tests/test_agent_runtime_emitter.py`
- `ai-service/tests/test_agent_runtime_store.py`
- `ai-service/tests/test_agent_runtime_api.py`
- `ai-service/tests/test_agent_runtime_loop.py`
- `ai-service/tests/test_agent_runtime_approvals.py`

### 建议更新

- `ai-service/tests/test_ai_api.py`
- `ai-service/tests/test_request_flow_agent.py`
- `ai-service/tests/test_langchain_runtime_service.py`

## Exec 测试

### 建议新增

- `exec-service/tests/test_run_store.py`
- `exec-service/tests/test_event_store.py`
- `exec-service/tests/test_streaming_runner.py`
- `exec-service/tests/test_execute_api_streaming.py`
- `exec-service/tests/test_execute_cancel.py`

## 前端测试

### 建议新增

- event reducer 回放测试
- timeline 渲染测试
- approval 流程测试
- command output panel 测试
- reconnect hydration 测试

---

## 9. 建议分工

## 工程师 1

负责 `ai-service` 的 run/event 基础设施

## 工程师 2

负责 `exec-service` 的流式执行与命令生命周期

## 工程师 3

负责前端 runtime 壳、状态 reducer 和布局重构

## 工程师 4 或共享

负责集成测试、迁移 glue、开关和灰度

---

## 10. 依赖顺序

## 可以并行启动

- Track A 的 runtime 包骨架
- Track B 的 run store 骨架
- Track C 的前端 feature 骨架

## 必须等协议冻结后再做

- reducer 逻辑
- stream payload 解析
- approval 交互联调

## 必须等 exec 流式完成后再做

- 原生多轮 act/observe loop
- 命令输出实时展示的正式验收

---

## 11. 里程碑定义

## M1

run/event 基础能力完成。

### 演示标准

- 可创建 run
- 可查询 run
- 可列出 events
- 可流式接收 planning/final 事件

## M2

exec 流式执行完成。

### 演示标准

- 只读命令可实时输出
- 命令可取消

## M3

原生迭代式 agent loop 完成。

### 演示标准

- 一个问题可触发多步调查
- 最终回答引用同一 run 内的 observation

## M4

前端 runtime 壳完成。

### 演示标准

- 运行中刷新页面可恢复
- 审批后可在同一 run 内继续输出
- 对话与运行时间线是中心主视图

---

## 12. 下一份建议文档

当前任务分解确认后，建议下一步继续补：

- 存储迁移草案
- 事件状态图
- 前端 reducer 状态草案
- 页面 wireframe 草案

这样就可以从“计划”进入“实现前评审”。
