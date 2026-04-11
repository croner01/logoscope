# AI Agent Runtime 前端 Reducer 状态草案

> 状态: Draft  
> 目标: 为新的 AI Analysis runtime UI 提供前端状态模型与事件归并规则  
> 范围: 运行态页面，不覆盖旧历史页的所有兼容细节

---

## 1. 目标

新的前端状态模型要解决当前页面的三个核心问题：

- 不能再依赖本地 placeholder assistant message
- 不能再把 stream 和 final history 做二次合并
- 必须支持刷新恢复、断线重连和审批恢复

因此前端状态必须满足：

- 可由 `run snapshot + event list` 完整恢复
- 可持续消费 `stream` 事件增量更新
- 对话、时间线、审批、命令输出共享同一份事实来源

---

## 2. 顶层状态结构

建议状态拆为三层：

1. `runMeta`
2. `entities`
3. `derivedView`

其中：

- `runMeta` 保存运行整体信息
- `entities` 保存归一化实体
- `derivedView` 不直接持久化，由 selector 从 `entities` 推导

---

## 3. 顶层 TypeScript 草案

```ts
export interface AgentRunState {
  hydrated: boolean;
  hydrating: boolean;
  streaming: boolean;
  streamError?: string;
  lastSeq: number;
  runMeta: RunMetaState | null;
  entities: RunEntityState;
}

export interface RunMetaState {
  runId: string;
  sessionId: string;
  status: RunStatus;
  analysisType: 'log' | 'trace' | string;
  engine: string;
  assistantMessageId: string;
  userMessageId?: string;
  serviceName?: string;
  traceId?: string;
  createdAt?: string;
  updatedAt?: string;
  endedAt?: string | null;
  currentPhase?: string;
  iteration?: number;
}

export interface RunEntityState {
  messagesById: Record<string, MessageEntity>;
  messageOrder: string[];
  stepsById: Record<string, ReasoningStepEntity>;
  stepOrder: string[];
  toolCallsById: Record<string, ToolCallEntity>;
  toolCallOrder: string[];
  commandRunsById: Record<string, CommandRunEntity>;
  commandRunOrder: string[];
  approvalsById: Record<string, ApprovalEntity>;
  approvalOrder: string[];
  events: RunEventEnvelope[];
}
```

---

## 4. 实体模型草案

## 4.1 MessageEntity

```ts
export interface MessageEntity {
  messageId: string;
  role: 'user' | 'assistant';
  content: string;
  finalized: boolean;
  references: ReferenceItem[];
  createdAt?: string;
  updatedAt?: string;
}
```

说明：

- `assistant_delta` 追加到 `content`
- `assistant_message_finalized` 将 `finalized=true`
- 前端不自己生成临时主消息 ID

## 4.2 ReasoningStepEntity

```ts
export interface ReasoningStepEntity {
  stepId: string;
  phase: string;
  title: string;
  status: 'pending' | 'in_progress' | 'completed' | 'failed' | string;
  iteration?: number;
  summaryText: string;
  createdAt?: string;
  updatedAt?: string;
}
```

说明：

- `reasoning_summary_delta` 追加到 `summaryText`
- `reasoning_step` 更新阶段标题与状态

## 4.3 ToolCallEntity

```ts
export interface ToolCallEntity {
  toolCallId: string;
  stepId?: string;
  toolName: string;
  title?: string;
  status: 'pending' | 'running' | 'completed' | 'failed' | string;
  input?: Record<string, unknown>;
  summary?: Record<string, unknown>;
  commandRunId?: string;
  createdAt?: string;
  updatedAt?: string;
}
```

## 4.4 CommandRunEntity

```ts
export interface CommandRunEntity {
  commandRunId: string;
  toolCallId?: string;
  actionId?: string;
  command: string;
  commandType?: string;
  riskLevel?: string;
  status: 'running' | 'completed' | 'failed' | 'cancelled' | 'waiting_approval' | string;
  stdout: string;
  stderr: string;
  outputTruncated?: boolean;
  exitCode?: number;
  timedOut?: boolean;
  createdAt?: string;
  updatedAt?: string;
  endedAt?: string | null;
}
```

说明：

- `tool_call_output_delta` 追加 stdout/stderr
- 不建议前端保留过多切片数组，v1 先维护累计文本即可

## 4.5 ApprovalEntity

```ts
export interface ApprovalEntity {
  approvalId: string;
  toolCallId?: string;
  actionId?: string;
  title?: string;
  command: string;
  commandType?: string;
  riskLevel?: string;
  requiresConfirmation: boolean;
  requiresElevation: boolean;
  status: 'pending' | 'approved' | 'rejected' | 'expired' | string;
  message?: string;
  createdAt?: string;
  updatedAt?: string;
}
```

---

## 5. Reducer 输入事件

Reducer 只接收 canonical event，不直接处理 legacy stream 事件。

```ts
export interface RunEventEnvelope {
  eventId?: string;
  runId: string;
  seq: number;
  eventType: string;
  createdAt?: string;
  payload: Record<string, unknown>;
}
```

---

## 6. Reducer Action 草案

建议 reducer action 只有四类：

```ts
type RunReducerAction =
  | { type: 'hydrate_snapshot'; payload: { run: RunSnapshot } }
  | { type: 'hydrate_events'; payload: { events: RunEventEnvelope[] } }
  | { type: 'append_event'; payload: { event: RunEventEnvelope } }
  | { type: 'reset'; payload?: { runId?: string } };
```

说明：

- `hydrate_snapshot` 初始化 `runMeta`
- `hydrate_events` 用于刷新或重连后批量回放
- `append_event` 用于 stream 增量追加
- `reset` 用于切换会话或离开页面

---

## 7. 事件归并规则

## 7.1 通用规则

- 只接受 `seq > lastSeq` 的事件
- 如果 `seq <= lastSeq`，视为重复事件，忽略
- `hydrate_events` 时先按 `seq` 排序再回放

## 7.2 `run_started`

更新：

- `runMeta.status = running`
- 若 runMeta 不存在则初始化

## 7.3 `run_status_changed`

更新：

- `runMeta.status`
- `runMeta.currentPhase`
- `runMeta.updatedAt`

## 7.4 `message_started`

更新：

- 新建 assistant message entity
- `messageOrder` 插入 assistant message
- `runMeta.assistantMessageId = payload.assistant_message_id`

## 7.5 `reasoning_summary_delta`

更新：

- 通过 `stepId` 找到 reasoning step
- 若不存在则创建空 step
- 追加 `summaryText`

## 7.6 `reasoning_step`

更新：

- upsert `ReasoningStepEntity`
- 更新 `phase/title/status/iteration`

## 7.7 `tool_call_started`

更新：

- upsert `ToolCallEntity`
- 状态设为 `running`
- 若有 `step_id`，绑定到对应 step

## 7.8 `tool_call_progress`

更新：

- 更新 tool call 进度型摘要

说明：

v1 可以简单写入 `summary.progressText`

## 7.9 `tool_call_output_delta`

更新：

- 若 payload 有 `command_run_id`，upsert `CommandRunEntity`
- 根据 `stream` 追加到 `stdout` 或 `stderr`
- 同时可在对应 `ToolCallEntity` 上记录 `commandRunId`

## 7.10 `tool_call_finished`

更新：

- 更新 `ToolCallEntity.status`
- 写入 `summary`
- 如为命令型 tool call，命令状态也同步收敛

## 7.11 `approval_required`

更新：

- 新建或更新 `ApprovalEntity`
- 状态设为 `pending`
- `runMeta.status = waiting_approval`

## 7.12 `approval_resolved`

更新：

- 更新对应 `ApprovalEntity.status`
- 若已通过，则 `runMeta.status = running`

## 7.13 `assistant_delta`

更新：

- 找到 `assistantMessageId`
- 追加 `content`
- 更新时间

## 7.14 `assistant_message_finalized`

更新：

- 覆盖或补齐最终 content
- 更新 references
- `finalized = true`

## 7.15 `run_finished`

更新：

- `runMeta.status = completed`
- `streaming = false`
- `runMeta.endedAt = createdAt`

## 7.16 `run_failed`

更新：

- `runMeta.status = failed`
- `streaming = false`
- 写入 `streamError`

## 7.17 `run_cancelled`

更新：

- `runMeta.status = cancelled`
- `streaming = false`

---

## 8. Selector 草案

Reducer 只负责维护实体，不直接维护复杂 UI 结构。

建议通过 selector 派生：

- `selectConversationItems`
- `selectTimelineItems`
- `selectPendingApprovals`
- `selectVisibleToolCalls`
- `selectRunHeader`

---

## 9. Conversation 派生结构

建议 conversation 只放：

- user message
- assistant message

不要把 tool call 当成 assistant 文本内的 metadata 小块来渲染。

### `selectConversationItems`

返回：

```ts
type ConversationItem =
  | { type: 'message'; message: MessageEntity }
  | { type: 'system_status'; status: string; text: string };
```

说明：

- assistant message 是对话主线
- run 状态提示可用轻量 system item 补充

---

## 10. Timeline 派生结构

建议运行态时间线是独立主区域，按顺序展示：

1. reasoning step
2. tool call
3. command output
4. approval

### `selectTimelineItems`

返回：

```ts
type TimelineItem =
  | { type: 'reasoning_step'; step: ReasoningStepEntity }
  | { type: 'tool_call'; toolCall: ToolCallEntity }
  | { type: 'approval'; approval: ApprovalEntity };
```

说明：

- `CommandRunEntity` 不一定单独成为顶层 timeline item
- 更建议作为 `ToolCallCard` 展开的子内容

---

## 11. 页面结构建议

前端 state 模型应服务于新的 workspace 结构。

建议区域：

- `RunHeader`
  显示 run 状态、session、service、trace、当前阶段

- `ConversationPane`
  显示用户提问、助手逐步输出、最终答案

- `RunTimeline`
  显示 reasoning step、tool call、approval、command output

- `EvidenceRail`
  显示 references、similar cases、KB 搜索结果、后处理动作

---

## 12. Hydration 与重连策略

## 12.1 初次进入页面

1. 请求 `GET /runs/{run_id}`
2. 请求 `GET /runs/{run_id}/events`
3. reducer 回放全部 events
4. 再打开 `/stream?after_seq={lastSeq}`

## 12.2 流断开

1. 记录 `lastSeq`
2. 再次连接 `/stream?after_seq={lastSeq}`
3. 若失败，提示“实时流已断开，可手动刷新恢复”

## 12.3 页面刷新

1. 优先从 URL 或页面状态拿 `run_id`
2. 重新执行 hydration 流程

---

## 13. 错误与边界处理

## 13.1 重复事件

策略：

- 使用 `seq` 去重

## 13.2 乱序事件

策略：

- `hydrate_events` 时排序
- `stream` 事件若乱序出现，v1 可直接丢弃 `seq <= lastSeq` 事件

## 13.3 assistant_delta 先于 message_started

策略：

- reducer 可临时创建空 assistant message
- 一旦 `message_started` 到来，补齐标准 metadata

## 13.4 approval_resolved 先于 approval_required

策略：

- 允许 upsert 一个最小 ApprovalEntity
- 后续 `approval_required` 补齐字段

---

## 14. v1 简化建议

为了控制实现复杂度，v1 可以做以下简化：

- `CommandRunEntity.stdout/stderr` 先用累计字符串
- `events` 仅保留最近 N 条在内存中，其余可按需重新拉取
- `derivedView` 不常驻 state，全部走 selector
- 不做多窗口共享本地状态

---

## 15. 建议文件落点

### 新增

- `frontend/src/features/ai-runtime/types/events.ts`
- `frontend/src/features/ai-runtime/types/run.ts`
- `frontend/src/features/ai-runtime/state/runEventReducer.ts`
- `frontend/src/features/ai-runtime/state/runSelectors.ts`
- `frontend/src/features/ai-runtime/hooks/useAgentRun.ts`
- `frontend/src/features/ai-runtime/hooks/useRunEvents.ts`

### 需要修改

- `frontend/src/utils/api.ts`
- `frontend/src/pages/AIAnalysis.tsx`

---

## 16. 下一步建议

如果这份 reducer 草案确认通过，下一步建议继续补：

1. 前端 wireframe 草案
2. `runEventReducer.ts` 伪代码草案
3. API 到 reducer 的映射样例测试用例
