# AI Agent Runtime 优化方案 v3

## 文档目标

本文档在 v2 基础上，收敛最近一轮产品评审与实现约束，形成可直接排期的优化方案。目标不是继续堆叠前端卡片或局部修补，而是把 AI runtime 从“单次 run 调试页”升级为“可连续使用的专家型会话页”。

本文档重点回答六个问题：

1. 页面如何从单次 run 视图演进为多轮 thread 视图
2. 主会话流与执行细节的边界如何切分
3. 审批与提权如何成为强中断链路而不是隐藏操作
4. 如何引入产品内 expert profiles 提升回答质量
5. `skipped / approval / manual-needed / failed` 的状态语义如何收敛
6. 第一迭代应该只做哪些事情，做到可以直接开工

---

## 一、复审结论

### 1.1 当前最主要的问题不是单点 bug，而是模型不匹配

当前 runtime 页的数据模型仍然偏“单次 run 调试器”，而用户期望的是“类似 Trae / Cursor 的连续会话线程”。由此衍生出四类显著问题：

1. 页面只保留一个 `currentSession`，导致每次输入都覆盖上一轮
2. 主会话流直接展示 thinking / command / status 等内部事件，界面噪声高
3. `approval_required` 到达后只停流，不自动弹审批
4. `skipped` 被包装成普通自然语言建议，容易误导为系统已完成某种可信动作

因此，不建议继续按“修几个交互问题”的方式推进，而应按一次小范围架构收敛推进。

### 1.2 产品内回答质量提升必须走 expert profiles

本轮不考虑 Codex 本地 skill。要提升产品中的 AI 回答质量，必须在 `ai-service` 内引入 product-native expert profiles，而不是继续依赖单一的“可观测性助手”系统提示词。

建议首批支持四类专家：

- `sre-general`
- `system-expert`
- `k8s-expert`
- `openstack-expert`

它们不是装饰性 persona，而是结构化的领域路由、回答约束、检查清单和命令偏好。

### 1.3 当前方案不需要再继续上层调优

经过本轮复审，方向已足够稳定，不需要再做新的架构层级调整。后续应直接进入分期实施。

但在进入实现前，仍需补齐四个会直接影响返工概率的底层契约：

1. thread identity 必须明确为 `session_id + conversation_id`
2. reject approval 后的 run / turn 终态必须固定
3. history message 与 runtime run 的 turn join key 必须固定
4. ClickHouse 持久化升级必须有显式 migration 路径

保留的总体优先级如下：

1. 会话线程化
2. 主会话流收敛
3. 审批强中断
4. Expert Profiles Layer
5. 状态语义与文案收敛
6. 真实 dispatch 升级

---

## 二、总体方案

## 2.1 会话线程化

目标：解决“每次输入只能看到本次内容”。

关键动作：

- `run snapshot` 增加 `conversation_id`
- 页面状态从 `currentSession` 改成 `thread.turns[]`
- 新 run 自动复用 `session_id + conversation_id`
- 旧 run hydrate 时挂接到 thread 中对应 turn，而不是替换整页
- 当已知 `session_id` 时，优先通过 `ai/history/{session_id}` 回灌已有 user/assistant 历史，再挂接活跃 runtime turn
- thread merge 以 `session_id + conversation_id` 为准，而不是单独 `session_id`
- 当 history 无法稳定区分 `conversation_id` 时，第一迭代允许降级为“只展示活跃 runtime turn”

### 📌 Decision: frontend/runtime-thread-model
- **Value**: AI runtime 页面以 conversation thread 为一级模型，每个 runtime run 只对应 thread 中的一个 turn
- **Layer**: presentation
- **Tags**: frontend, runtime, thread, conversation
- **Rationale**: 页面当前的单 run 状态无法承载多轮会话历史，也无法正确表达审批中断和恢复后的同 turn 延续
- **Alternatives**: 保持单一 `currentSession` 并尝试追加历史文本，单纯依赖后端 `history` 结果回放
- **Tradeoffs**: 页面状态更复杂，但会话体验与用户心智模型对齐

### 📌 Decision: frontend/runtime-thread-identity
- **Value**: Runtime thread 的唯一身份使用 `session_id + conversation_id`，不使用单独 `session_id` 作为 thread merge key
- **Layer**: presentation
- **Tags**: frontend, runtime, thread, identity
- **Rationale**: 同一 analysis session 下可能存在多条 follow-up conversation；只按 `session_id` 回灌历史会把不同 conversation 错误混并到同一个 thread
- **Alternatives**: 仅用 `session_id` 作为 thread identity，由前端按文案或时间戳猜测归属
- **Tradeoffs**: identity 规则更严格，但需要前后端显式处理 legacy 数据和缺失 `conversation_id` 的回退逻辑

### 🚫 Constraint: architecture
- **Rule**: 第一迭代不支持多 turn 并发 streaming；同一 thread 任一时刻只允许一个 active streaming turn
- **Priority**: high
- **Tags**: runtime, thread, streaming, v1

## 2.2 主会话流收敛

目标：解决“界面像调试器，不像对话”。

主会话流只保留三类内容：

- `user`
- `assistant`
- `approval`

以下内容降级到 details：

- reasoning steps
- bootstrap/system step
- command 列表
- stdout/stderr
- dispatch backend / executor profile / target identity
- skipped/policy reason
- stream error 详情

### 📌 Decision: frontend/runtime-main-chat-minimal
- **Value**: 主会话流默认只承载对用户有直接决策价值的消息，不再平铺 runtime 内部事件
- **Layer**: presentation
- **Tags**: frontend, transcript, runtime, ux
- **Rationale**: 思考步骤、命令输出和状态事件本质是执行细节，直接放入主流会稀释 AI 任务叙事
- **Alternatives**: 保持 block-first transcript，全量展示 runtime 内部事件
- **Tradeoffs**: 主界面更清晰，但需要一个完整的 details 容器承接调试能力

## 2.3 审批强中断

目标：解决“没有提权弹窗”和“审批隐藏太深”。

规则：

- 收到 `approval_required` 时自动弹出 modal
- 当前 turn 中保留审批消息
- composer 上方显示 pending approval sticky 提示
- approve 后继续 stream，且不再立刻自我截停
- reject 后当前 turn 明确落为 `blocked/rejected`

### 📌 Decision: frontend/runtime-approval-interrupt
- **Value**: 审批是任务主链路中的强中断状态，必须自动唤起并在主会话流中留下记录
- **Layer**: presentation
- **Tags**: frontend, approval, security, runtime
- **Rationale**: 用户不能被迫先从大量卡片中识别“有审批待处理”，再手动触发 modal
- **Alternatives**: 继续依赖审批块内按钮触发 modal，仅在细节区展示审批
- **Tradeoffs**: 会更频繁打断当前浏览，但可显著减少遗漏审批的风险

### 📌 Decision: runtime/approval-reject-terminal-semantics
- **Value**: approval 被 reject 后，当前 turn 必须落为可恢复展示但不可继续误读为执行中的终态；第一迭代建议使用 `blocked` / `rejected`
- **Layer**: cross-cutting
- **Tags**: runtime, approval, status, semantics
- **Rationale**: 若 reject 后 run 仍显示为 `running`，前端即使自动弹窗也无法表达“主链路已被人工阻断”的真实状态
- **Alternatives**: reject 后回到 `running` 并仅在 approval item 上显示 `rejected`，保持 `completed` 但额外挂 `outcome=rejected`
- **Tradeoffs**: 需要补充状态映射和文案，但能避免审批拒绝后状态语义继续混乱

## 2.4 Expert Profiles Layer

目标：解决“回答太笨”。

建议在 `ai-service` 新增：

```text
ai-service/ai/expert_profiles/
  __init__.py
  schemas.py
  registry.py
  router.py
  composer.py
  profiles/
    sre-general.yaml
    system-expert.yaml
    k8s-expert.yaml
    openstack-expert.yaml
  references/
    sre-general.md
    system-expert.md
    k8s-expert.md
    openstack-expert.md
```

建议路由优先级：

1. 前端显式选择的 `expert_profile`
2. `command_family / executor_profile / target_kind` 强路由
3. question keywords 补充加权
4. 默认回落到 `sre-general`

建议首批回答元数据回传：

- `expert_profile`
- `secondary_expert_profiles`
- `expert_route_reason`
- `expert_prompt_version`

### 📌 Decision: ai-runtime/expert-profiles
- **Value**: runtime 回答链路支持按领域路由的产品内 expert profiles，不再只依赖统一的 observability assistant 提示词
- **Layer**: application
- **Tags**: ai, runtime, expert, prompt, routing
- **Rationale**: K8s、系统层、OpenStack 的排障顺序、缺失证据清单和命令偏好明显不同，统一 prompt 无法稳定提供资深工程师级输出
- **Alternatives**: 继续用单一 system prompt，完全依赖更强模型自行归纳领域特征
- **Tradeoffs**: 需要维护 profile 配置和路由逻辑，但回答质量、可控性和可解释性显著提升

## 2.5 状态语义与文案收敛

目标：避免 `skipped` 被误读为已执行建议。

建议明确拆分：

- `approval_required`
- `skipped_by_policy`
- `manual_followup_needed`
- `failed`

前端主消息只显示低歧义说明，详细命令与原因进入 details。

### 📌 Decision: runtime/status-semantics
- **Value**: 审批、策略未执行、失败、人工补执行等状态使用不同语义路径，不再统一包装成普通 assistant 建议
- **Layer**: cross-cutting
- **Tags**: runtime, policy, transcript, semantics
- **Rationale**: 当前 `skipped` 被渲染成“改为人工执行并观察”，容易被理解为可信执行结论
- **Alternatives**: 继续保持自然语言混合表达
- **Tradeoffs**: 前后端需要多一层状态映射，但表达更准确

### 🚫 Constraint: architecture
- **Rule**: 第一迭代除文案调整外，至少要补一个结构化执行语义字段（如 `execution_disposition`），不得继续只依赖自然语言文案区分 `skipped / manual followup / failed`
- **Priority**: high
- **Tags**: runtime, semantics, planner, v1

## 2.6 真实 dispatch 升级

目标：从“只展示 executor metadata”升级到“真实分发到 toolbox / busybox / ssh-gateway”。

这一项重要，但不进入第一迭代。理由：

- 当前用户痛点首先是“会话不清晰”和“回答不够专业”
- dispatch 升级收益大，但不是当前最短路径上的用户问题

---

## 三、第一迭代范围

第一迭代只聚焦：

- A：会话线程化
- B：主会话流收敛
- C：审批强中断
- E：状态语义与文案第一轮收敛

本迭代明确不做：

- D：Expert Profiles 正式落地
- F：真实 dispatch 升级

原因：

- 第一迭代先解决“能不能清楚地连续用”
- 第二迭代再解决“答得是否足够像专家”

---

## 四、第一迭代实施清单

以下清单按“文件 -> 预期新增/删除的函数和类型”展开，目标是做到可以直接开工。

## 4.1 后端：补齐 conversation_id

### 文件：[models.py](/root/logoscope/ai-service/ai/agent_runtime/models.py)

#### 预期改动

新增字段：

- `conversation_id: str = ""`

修改函数：

- `AgentRun.to_dict()`
  - 新增返回 `conversation_id`

#### 预期结果

- runtime snapshot 正式具备 thread identity
- legacy run 在 `conversation_id == ""` 时仍可兼容读取，但前端不得将其强行与其他 conversation 合并

### 文件：[service.py](/root/logoscope/ai-service/ai/agent_runtime/service.py)

#### 预期改动

修改函数：

- `create_run(...)`
  - 从 `runtime_options` 中读取 `conversation_id`
  - 写入 `AgentRun.conversation_id`
  - 继续保留 `summary_json.runtime_options` 作为调试上下文
- `resolve_approval(...)`
  - reject 后不再回到 `running`
  - 明确写入 `blocked/rejected` 语义所需的 run status / summary 字段

#### 暂不改动

- 不改当前 bootstrap event 的发出逻辑
- bootstrap step 是否展示由前端控制

### 文件：[store.py](/root/logoscope/ai-service/ai/agent_runtime/store.py)

#### 预期改动

ClickHouse schema：

- run table 增加 `conversation_id String`
- latest view 增加 `argMax(conversation_id, _updated_at) AS conversation_id`

修改函数：

- `_ensure_clickhouse_tables()`
- `_insert_run_to_clickhouse()`
- run 查询与反序列化逻辑

#### 风险说明

- 这是第一迭代里唯一带持久化变更的改动
- 要保证现有表结构升级策略明确，不允许只改 dataclass 不改存储

#### migration 要求

- 不能依赖 `_ensure_clickhouse_tables()` 的 `CREATE IF NOT EXISTS` 语义完成升级
- 需要显式提供 release migration，至少包含：
  - `ALTER TABLE <run_table> ADD COLUMN IF NOT EXISTS conversation_id String DEFAULT ''`
  - 重建 latest view，使其包含 `argMax(conversation_id, _updated_at) AS conversation_id`
  - 明确 legacy 数据在 `conversation_id == ''` 时的读取与前端降级策略

### 📌 Decision: runtime/storage-migration-conversation-id
- **Value**: `conversation_id` 持久化升级通过显式 migration 完成，不依赖运行时 `CREATE TABLE/VIEW IF NOT EXISTS` 自愈
- **Layer**: data
- **Tags**: clickhouse, runtime, migration, conversation
- **Rationale**: 已存在的 ClickHouse 表和 view 不会因为应用启动时的建表逻辑自动补列或重建投影，若只改 dataclass 和查询代码会造成读写契约失配
- **Alternatives**: 仅修改应用层模型并假设启动时自动修复表结构，先只写 `summary_json.runtime_options.conversation_id`
- **Tradeoffs**: migration 编排略复杂，但能保证线上已有数据结构与新读取逻辑一致

## 4.2 前端：thread 状态替换 currentSession

### 文件：[aiAgentRuntime.ts](/root/logoscope/frontend/src/utils/aiAgentRuntime.ts)

#### 预期改动

修改类型：

- `AgentRunSnapshot`
  - 新增 `conversation_id?: string`

### 文件：[view.ts](/root/logoscope/frontend/src/features/ai-runtime/types/view.ts)

#### 预期改动

新增类型：

- `RuntimeConversationThreadView`
- `RuntimeConversationTurnView`
- `RuntimeAssistantTurnView`
- `RuntimeTurnDetailsView`
- 可选 `RuntimeStatusItemView`

保留但降级：

- `RuntimeTranscriptBlock`
- `RuntimeTranscriptMessage`

#### 预期结果

- 页面主模型由 block-first 转为 turn-first
- turn identity 在第一迭代固定使用 `assistant_message_id`，用于对齐 history assistant message 与 runtime run

### 📌 Decision: frontend/runtime-turn-join-key
- **Value**: 第一迭代中 runtime turn 的 join key 使用 `assistant_message_id`
- **Layer**: presentation
- **Tags**: frontend, runtime, turn, identity
- **Rationale**: history 回灌与活跃 run 挂接都需要稳定 join key；直接让前端按时间戳或文本内容猜测会导致重复 bubble 和错挂 turn
- **Alternatives**: 新增独立 `turn_id` 并同步改造历史消息结构，前端按时间顺序与相邻 role 猜测归属
- **Tradeoffs**: 与当前消息模型兼容最好，但要求 runtime 和 history 都能稳定回传 assistant message identity

### 文件：[AIRuntimePlayground.tsx](/root/logoscope/frontend/src/pages/AIRuntimePlayground.tsx)

#### 预期删除或降级的状态

- `currentSession`
- 直接以单条 `transcriptMessage` 驱动整页的渲染路径
- 顶部大块 executor readiness 常驻展示区域

#### 预期新增状态

- `thread`
- `activeTurnId`
- `pendingApproval`

#### 预期新增或重写的函数

- `buildTurnFromSnapshot(run)`
- `appendTurn(question, run)`
- `updateTurnFromEvent(runId, event)`
- `hydrateThreadFromRun(runId)`
- `hydrateThreadHistory(sessionId)`
- `syncThreadSessionIdentifiers(run)`
- `findLatestPendingApproval(turn)`
- `dedupeTurnByAssistantMessageId(turn)`

#### 预期重写的函数

- `handleStartRun()`
  - 不再覆盖页面唯一 session
  - 改为 append 新 turn
- `streamRun()`
  - 只更新对应 turn
- `handleRefreshCurrentRun()`
  - 只刷新 active turn
- `handleLoadExistingRun()`
  - 挂接到 thread，而不是替换整页

#### 预期结果

- 同一页面中可保留完整多轮会话
- 复用已有 `session_id` 时，可看到先前 user/assistant 历史，而不只是活跃 run
- 同一 `run_id` 被重复 hydrate 或 history + live run 双来源挂接时，不产生重复 turn / 重复 assistant bubble

### 文件：[api.ts](/root/logoscope/frontend/src/utils/api.ts)

#### 第一迭代使用方式调整

- 复用现有 `getAIHistoryDetail(sessionId)` 能力
- 在 runtime 页已知 `session_id` 时回灌历史消息
- 回灌历史时先按 `conversation_id` 过滤；无法稳定过滤时，降级为只展示活跃 runtime turn，而不是错误混并 thread

#### 第一迭代原则

- 不新增新的 history API
- 先复用现有 session history detail，满足“看见本次会话历史”的目标
- 不允许为了避免新增 API 而牺牲 thread identity 的正确性

## 4.3 前端：主会话流收敛

### 文件：[runtimeTranscript.ts](/root/logoscope/frontend/src/features/ai-runtime/utils/runtimeTranscript.ts)

#### 预期改动方向

不再作为“主页面数据源”，而改为：

- `buildRuntimeTurnDetails(...)`
- 可选保留兼容函数 `buildRuntimeTranscriptMessage(...)`

#### 预期新增函数

- `buildRuntimeAssistantTurnView(state, title)`
- `buildRuntimeTurnDetailsView(state)`
- `buildRuntimeApprovalMessages(state, runId)`

#### 预期弱化或移除的输出

默认不再输出到主流的 block：

- `thinking`
- `command`
- `status`

#### 预期结果

- details 仍完整
- 主流不再被内部事件淹没

### 文件：[RuntimeConversationCard.tsx](/root/logoscope/frontend/src/features/ai-runtime/components/RuntimeConversationCard.tsx)

#### 预期改动方向

从“万能 block 容器”收缩为“assistant + approval + details 容器”。

#### 建议新增的局部组件

可在同文件内先局部拆分，后续再独立文件化：

- `renderAssistantMessage(...)`
- `renderApprovalMessage(...)`
- `renderTurnDetails(...)`

#### 预期弱化或移除的展示路径

默认不再直接渲染：

- `renderThinkingBlock(...)`
- `renderCommandBlock(...)`

这些内容进入 `details`

#### 预期结果

- 主消息默认只保留用户最关心的信息

## 4.4 前端：审批强中断

### 文件：[AIRuntimePlayground.tsx](/root/logoscope/frontend/src/pages/AIRuntimePlayground.tsx)

#### 预期新增逻辑

在 `streamRun()` 的 `onEvent` 里：

- 发现 `approval_required`
  - 更新当前 turn 的 approvals
  - 自动设置 `pendingApproval`
  - 自动打开 modal
- 发现 `approval_resolved(rejected)`
  - 当前 turn 落为 `blocked/rejected`
  - sticky approval 切换为结果态，不再显示为 pending

#### 预期新增辅助函数

- `openLatestApprovalForRun(runId)`
- `resolveApprovalAndResume(decision)`
- `resumeTurnAfterApproval(runId, approvalId, elevated?)`

#### 预期修改逻辑

- approve 后继续 stream 时使用 `stopOnApproval: false`
- modal 关闭后 sticky approval 仍可再次打开

### 文件：[useAgentRuntimeCommandFlow.ts](/root/logoscope/frontend/src/features/ai-runtime/hooks/useAgentRuntimeCommandFlow.ts)

#### 第一迭代建议

- 暂不重构为通用 hook
- 仅参考其 `resumeApprovalFlow` 和 `stopOnApproval` 逻辑

#### 第二迭代再考虑

- 抽公共 `useRuntimeApprovalFlow`

## 4.5 前后端：状态语义与文案第一轮收敛

### 文件：[followup_planning_helpers.py](/root/logoscope/ai-service/ai/followup_planning_helpers.py)

#### 预期改动

修改 `reason == "skipped"` 时生成的文案：

从：

- `命令被策略跳过，改为人工执行并观察：{command}`

调整为更低歧义的说明，例如：

- `该命令未被系统自动执行，原因是策略限制：{command}`

#### 第一迭代范围

- 文案必须调整，同时补最小结构化字段用于前端区分 `executed / approval_required / skipped_by_policy / manual_followup_needed / failed`

### 文件：[runtimeTranscript.ts](/root/logoscope/frontend/src/features/ai-runtime/utils/runtimeTranscript.ts)

#### 预期改动

- `skipped` 相关说明进入 details
- 主回答不再直接拼接“人工执行并观察”类文案

### 文件：[RuntimeConversationCard.tsx](/root/logoscope/frontend/src/features/ai-runtime/components/RuntimeConversationCard.tsx)

#### 预期改动

- details 中显示策略未执行原因
- 主流只显示低歧义状态提示

## 4.6 第一迭代最小契约草图

为避免实现阶段再次争论“thread / turn / approval 到底挂哪一层”，第一迭代建议以以下最小契约为准。

### 4.6.1 前端 thread / turn shape

```ts
interface RuntimeConversationThreadView {
  sessionId: string;
  conversationId: string;
  turns: RuntimeConversationTurnView[];
  activeTurnId?: string;
}

interface RuntimeConversationTurnView {
  turnId: string;
  runId?: string;
  userMessage: {
    messageId?: string;
    content: string;
    timestamp?: string;
  };
  assistant: {
    messageId?: string;
    content: string;
    finalized: boolean;
    streaming: boolean;
  };
  approvals: RuntimeApprovalEntry[];
  details: RuntimeTurnDetailsView;
  status: 'streaming' | 'waiting_approval' | 'completed' | 'blocked' | 'failed' | 'cancelled';
  updatedAt?: string;
}
```

### 4.6.2 第一迭代挂载规则

- `thread` 级只维护身份与 turn 列表，不直接承载 reasoning / command / error
- `turn` 级承载 assistant、approval、details 和 status
- `pendingApproval` 只是当前弹窗引用，真实状态仍保存在 `turn.approvals[]`
- `turnId` 第一迭代固定为 `assistant_message_id`
- history 回灌与 live runtime 挂接都先按 `assistant_message_id` 去重

### 4.6.3 后端状态映射

第一迭代建议收敛为以下 run / turn 终态：

- `running`
  - 正常规划 / 执行中
- `waiting_approval`
  - 已发出 `approval_required`，等待用户确认
- `blocked`
  - 用户 reject approval，当前 turn 被人工阻断
- `completed`
  - 已形成最终 assistant answer
- `failed`
  - runtime 异常或命令失败导致无法继续
- `cancelled`
  - 用户主动取消

对应前端展示规则：

- `waiting_approval`：主流保留 approval message，并自动弹 modal
- `blocked`：主流显示“该步骤已因审批拒绝而停止”
- `failed`：主流显示低歧义失败提示，错误详情进入 details
- `completed`：主流显示最终 answer，details 保留执行痕迹

## 4.7 建议实施顺序

为降低 thread 化与审批改造同时落地的风险，建议按以下顺序实施：

1. 先补后端 `conversation_id` 持久化与 snapshot 回传
2. 再补前端 thread / turn 新类型，但暂时先用旧 transcript 渲染
3. 在 thread 模型稳定后，再切主流为 `assistant + approval + details`
4. 最后接入审批自动弹窗、reject 终态和 sticky 入口
5. 文案与 `execution_disposition` 收尾，并补齐恢复/去重验收

不建议的顺序：

- 先改 UI，再回头补 `conversation_id`
- 先做审批弹窗，再保留 reject 为 `running`
- 先做 details 容器，再让 history 与 live run 重复挂接

## 4.8 建议测试覆盖

第一迭代至少补以下测试：

- 后端：`create_run()` 会写入并回传 `conversation_id`
- 后端：`resolve_approval(rejected)` 后 run 不再回到 `running`
- 后端：ClickHouse 读取 legacy run 时，`conversation_id == ""` 仍可兼容
- 前端：history + live run 合并时，按 `assistant_message_id` 去重
- 前端：`approval_required` 到达时自动打开 modal
- 前端：`approval_resolved(rejected)` 后 turn 呈现 `blocked/rejected`
- 前端：刷新 active run 不覆盖其他 turn

## 4.9 建议任务拆分清单

以下清单按可并行度与依赖顺序拆分，目标是让第一迭代可以直接进入任务创建。

### 4.9.1 Backend

#### B1. runtime run model 补齐 `conversation_id`

- [ ] 在 `AgentRun` 中新增 `conversation_id`
- [ ] 在 `AgentRun.to_dict()` 中回传 `conversation_id`
- [ ] 在 `create_run(...)` 中从 `runtime_options` 读取并写入 `conversation_id`
- [ ] 为缺失 `conversation_id` 的 legacy run 保持默认空字符串兼容

#### B2. approval reject 状态机收敛

- [ ] 修改 `resolve_approval(...)`，reject 后不再把 run 设回 `running`
- [ ] 为 reject 路径写入稳定的 status / summary 字段
- [ ] 确认 approve 路径仍保持原有 resume 能力
- [ ] 明确 API 返回给前端的 reject 结果语义

#### B3. 执行语义结构化

- [ ] 为 action / observation / assistant metadata 增加最小结构化字段，如 `execution_disposition`
- [ ] 首批值收敛为 `executed / approval_required / skipped_by_policy / manual_followup_needed / failed`
- [ ] 保留原文案输出，但前端展示不再只依赖自然语言

### 4.9.2 Migration

#### M1. ClickHouse migration

- [ ] 为 run table 增加 `conversation_id` 列
- [ ] 重建 latest view，把 `conversation_id` 纳入 `argMax(...)`
- [ ] 校验 migration 可在已有表结构上重复执行
- [ ] 明确 release 执行顺序与回滚策略

#### M2. legacy 数据兼容

- [ ] 定义 `conversation_id == ""` 的读取语义
- [ ] 定义前端收到 legacy run 时的降级展示策略
- [ ] 避免 legacy 数据误并入新 thread

### 4.9.3 Frontend

#### F1. thread / turn 新模型落地

- [ ] 在 `AgentRunSnapshot` 中增加 `conversation_id`
- [ ] 新增 `RuntimeConversationThreadView` / `RuntimeConversationTurnView` / `RuntimeTurnDetailsView`
- [ ] 用 `thread` 替换 `currentSession`
- [ ] 明确 `activeTurnId` 与单 active streaming turn 规则

#### F2. history + live run 合并

- [ ] 新增 `hydrateThreadHistory(sessionId)` 逻辑
- [ ] 新增 `hydrateThreadFromRun(runId)` 逻辑
- [ ] 按 `assistant_message_id` 做 turn join 和去重
- [ ] 对缺失或无法匹配 `conversation_id` 的 history 走降级路径

#### F3. 主流收敛为 `assistant + approval + details`

- [ ] `runtimeTranscript.ts` 不再作为主页面唯一数据源
- [ ] 主流默认不再直接渲染 thinking / command / status block
- [ ] details 容器完整承接 reasoning / command / policy reason / stream error
- [ ] `RuntimeConversationCard.tsx` 收缩为 assistant + approval + details 容器

#### F4. approval 强中断

- [ ] `approval_required` 到达时自动设置 `pendingApproval`
- [ ] 自动弹出 modal
- [ ] modal 关闭后保留 sticky reopen 入口
- [ ] `approval_resolved(rejected)` 后 turn 状态切到 `blocked/rejected`
- [ ] approve 后 resume stream 使用 `stopOnApproval: false`

#### F5. 低歧义状态展示

- [ ] 主流只显示低歧义状态文案
- [ ] `skipped` / policy reason 进入 details
- [ ] `failed` 与 `blocked` 的视觉状态区分清楚
- [ ] refresh active run 时不覆盖其他 turn

### 4.9.4 Tests

#### T1. Backend tests

- [ ] `test_agent_runtime_models.py` 增加 `conversation_id` roundtrip
- [ ] `test_agent_runtime_api.py` 覆盖 create run 回传 `conversation_id`
- [ ] `test_agent_runtime_approvals.py` 覆盖 reject 后不回到 `running`
- [ ] store / migration 相关测试覆盖 legacy run 兼容读取

#### T2. Frontend tests

- [ ] thread reducer / merge helper 覆盖去重逻辑
- [ ] `approval_required` 自动弹窗覆盖
- [ ] reject 后 turn 状态覆盖
- [ ] refresh active run 不覆盖整页覆盖
- [ ] mobile composer 不遮挡最新 approval 入口的回归检查

## 4.10 并行实施建议

为了减少冲突，第一迭代建议按以下 write scope 并行：

- 后端任务 owner：
  - `ai-service/ai/agent_runtime/models.py`
  - `ai-service/ai/agent_runtime/service.py`
  - `ai-service/ai/agent_runtime/store.py`
  - `ai-service/ai/followup_planning_helpers.py`
- 前端任务 owner：
  - `frontend/src/utils/aiAgentRuntime.ts`
  - `frontend/src/utils/aiAgentRuntimeReducer.ts`
  - `frontend/src/features/ai-runtime/types/view.ts`
  - `frontend/src/features/ai-runtime/utils/runtimeTranscript.ts`
  - `frontend/src/features/ai-runtime/components/RuntimeConversationCard.tsx`
  - `frontend/src/pages/AIRuntimePlayground.tsx`
- migration / release owner：
  - ClickHouse release SQL
  - deployment / release note

建议并行方式：

1. Backend + migration 先行，确保 `conversation_id` 与 reject 状态机契约稳定
2. Frontend 在 mock / 本地假数据上先完成 thread 与主流收敛
3. 最后联调 approval interrupt、history merge 与恢复场景

---

## 五、第一迭代暂不进入范围的事项

以下事项已确认重要，但不进入第一迭代：

### 5.1 Expert Profiles 正式落地

不进入第一迭代的原因：

- 先把会话体验、审批和状态表达收敛
- 否则回答质量改进会被糟糕的会话体验掩盖

### 5.2 真实 dispatch 到 toolbox / busybox / ssh-gateway

不进入第一迭代的原因：

- 当前更痛的是“看不清”和“听不懂”，不是“命令究竟跑在哪”

---

## 六、第一迭代验收标准

第一迭代完成后，应至少满足：

1. 连续发送 3 轮问题后，页面仍能看到完整历史
2. `approval_required` 到达时自动弹审批 modal
3. 主会话流默认不再显示 bootstrap / thinking / command 卡片
4. `skipped` 不再以“人工执行并观察”的自然语言混入主回答
5. 刷新当前 run 只更新对应 turn，不覆盖整页
6. 移动端底部 composer 不遮挡最近消息和审批入口
7. 同一 `run_id` 重复 hydrate 不会生成重复 turn
8. 先回灌 history 再挂接活跃 run 时，不会重复生成 assistant bubble
9. `approval_required` 到达后刷新页面，仍能恢复 pending approval 入口
10. `approval_resolved(rejected)` 后刷新页面，turn 仍保持 `blocked/rejected` 语义，不回退为 `running`
11. 当 history 无法稳定区分 `conversation_id` 时，页面允许降级为仅展示活跃 runtime turn，但不得错误混并不同 conversation

---

## 七、第二迭代预告

第二迭代将聚焦：

- Expert Profiles Layer
- expert router / composer
- 前端 expert selector
- 状态语义进一步结构化

这部分已不再需要继续做架构层调优，可直接在第一迭代完成后展开。
