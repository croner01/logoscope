# AI Agent Runtime 存储迁移草案

> 状态: Draft  
> 目标: 为 AI Agent Runtime v1 提供 run / event / command run 的存储方案与迁移路径  
> 基线: 现有 AI 历史已经使用 ClickHouse 存储 `ai_analysis_sessions` 与 `ai_analysis_messages`

---

## 1. 现状基线

当前 AI 历史存储主要依赖：

- `logs.ai_analysis_sessions`
- `logs.ai_analysis_messages`
- `logs.v_ai_analysis_sessions_latest`

现有设计特点：

- `session` 采用 ReplacingMergeTree 存储最新状态
- `message` 采用 MergeTree 按会话顺序存储
- 已存在 latest view，避免读取时依赖 `FINAL`

这说明当前代码库已经有适合继续扩展的 ClickHouse 基础，不需要为了 run/event 强行引入一套完全不同的持久化体系。

---

## 2. 迁移目标

我们需要为新的 AI Runtime 增加以下持久化对象：

- `AgentRun`
- `RunEvent`
- `ToolCall`
- `CommandRun`

目标能力：

- 运行中可持续写入事件
- 页面刷新后可回放
- run 完成后可审计
- 可支持 approval pause/resume
- 可支持 exec 输出回放

---

## 3. 存储方案建议

## 3.1 推荐方案

推荐采用：

- **ClickHouse 作为持久化真相源**
- **Redis 作为可选的热态缓存/广播层**

即：

- run 和 event 最终必须落盘到 ClickHouse
- 如果后续需要更低延迟的活跃流分发，可以引入 Redis 作为 active run hot layer
- v1 不要求先把 Redis 引入为硬依赖

### 为什么不建议 v1 只用 Redis

- 刷新恢复和历史回放仍需要持久化
- 审计能力要求长期可查
- 已有 AI 历史已经在 ClickHouse 中
- 只用 Redis 会让历史和运行态分裂成两套体系

### 为什么不建议 v1 直接只用内存

- 无法支持页面重连恢复
- 无法支撑长会话中断重放
- 多副本部署下状态不可用

---

## 3.2 v1 推荐分层

### 第一层：ClickHouse 持久层

存储：

- run 快照
- run event
- tool call 快照
- command run 快照

### 第二层：进程内/可选 Redis 热态层

存储：

- active stream subscriber 路由
- 活跃 run 最近若干条 event 缓冲
- command live output fan-out

### v1 建议

v1 先做：

- ClickHouse 持久化
- 进程内事件队列 + SSE

v1.1 以后再评估：

- Redis Pub/Sub 或 Stream
- 多实例活跃事件广播

---

## 4. 数据模型建议

## 4.1 保留现有表

以下现有表保留，不强拆：

- `logs.ai_analysis_sessions`
- `logs.ai_analysis_messages`

它们继续承担：

- 会话级历史
- 用户消息与最终助手消息归档

### 新旧职责边界

- `session/message` 负责“人能读的对话历史”
- `run/event/tool/command` 负责“机器执行轨迹与恢复”

---

## 4.2 新增表：`logs.ai_agent_runs`

### 用途

存储一个运行实例的最新状态与摘要信息。

### 建议字段

```sql
CREATE TABLE IF NOT EXISTS logs.ai_agent_runs (
    run_id String,
    session_id String,
    analysis_type String,
    engine String,
    user_message_id String,
    assistant_message_id String,
    service_name String,
    trace_id String,
    status String,
    runtime_version String,
    input_json String,
    context_json String,
    summary_json String,
    error_code String,
    error_detail String,
    created_at DateTime64(3, 'UTC'),
    updated_at DateTime64(3, 'UTC'),
    ended_at Nullable(DateTime64(3, 'UTC'))
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY toYYYYMM(created_at)
ORDER BY (run_id)
SETTINGS index_granularity = 8192;
```

### 说明

- `ReplacingMergeTree(updated_at)` 适合存储 run 最新状态
- 不建议用 event 表去推导当前 run snapshot，读放大太高

---

## 4.3 新增表：`logs.ai_agent_run_events`

### 用途

存储运行过程中的完整事件日志，是前端回放和运行恢复的依据。

### 建议字段

```sql
CREATE TABLE IF NOT EXISTS logs.ai_agent_run_events (
    run_id String,
    event_id String,
    seq UInt64,
    event_type String,
    payload_json String,
    created_at DateTime64(3, 'UTC')
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(created_at)
ORDER BY (run_id, seq, created_at, event_id)
SETTINGS index_granularity = 8192;
```

### 说明

- `run_id + seq` 是主查询维度
- 前端回放通常按 `run_id` 顺序读取
- `payload_json` 建议保持 schema 宽松，避免频繁 DDL

---

## 4.4 新增表：`logs.ai_agent_tool_calls`

### 用途

存储工具调用级快照，便于：

- 定位具体哪一步失败
- 做运行态摘要
- 后续统计工具使用情况

### 建议字段

```sql
CREATE TABLE IF NOT EXISTS logs.ai_agent_tool_calls (
    tool_call_id String,
    run_id String,
    step_id String,
    tool_name String,
    title String,
    status String,
    input_json String,
    result_json String,
    error_code String,
    error_detail String,
    started_at DateTime64(3, 'UTC'),
    updated_at DateTime64(3, 'UTC'),
    ended_at Nullable(DateTime64(3, 'UTC'))
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY toYYYYMM(started_at)
ORDER BY (run_id, tool_call_id)
SETTINGS index_granularity = 8192;
```

### 说明

- 工具调用本质是快照对象，不需要保存所有中间 delta
- 真正 delta 仍通过 `run_events` 追踪

---

## 4.5 新增表：`logs.ai_agent_command_runs`

### 用途

存储命令执行级快照，便于：

- 审批恢复
- 命令审计
- 查询命令运行状态

### 建议字段

```sql
CREATE TABLE IF NOT EXISTS logs.ai_agent_command_runs (
    command_run_id String,
    run_id String,
    tool_call_id String,
    message_id String,
    action_id String,
    command String,
    command_type String,
    risk_level String,
    status String,
    requires_confirmation UInt8,
    requires_elevation UInt8,
    exit_code Int32,
    timed_out UInt8,
    output_truncated UInt8,
    error_code String,
    error_detail String,
    started_at DateTime64(3, 'UTC'),
    updated_at DateTime64(3, 'UTC'),
    ended_at Nullable(DateTime64(3, 'UTC'))
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY toYYYYMM(started_at)
ORDER BY (run_id, command_run_id)
SETTINGS index_granularity = 8192;
```

### 说明

- 命令运行本身保留 latest snapshot
- 输出明细不建议直接塞在 snapshot 表里

---

## 4.6 新增表：`logs.ai_agent_command_events`

### 用途

存储命令输出事件，主要用于：

- 命令输出回放
- 审计追踪
- 页面刷新后恢复 stdout/stderr 展示

### 建议字段

```sql
CREATE TABLE IF NOT EXISTS logs.ai_agent_command_events (
    command_run_id String,
    seq UInt64,
    event_type String,
    stream String,
    text String,
    payload_json String,
    created_at DateTime64(3, 'UTC')
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(created_at)
ORDER BY (command_run_id, seq, created_at)
SETTINGS index_granularity = 8192;
```

### 注意

如果命令输出量很大，后续要有截断与保留策略，避免日志型写入无限增长。

---

## 5. 建议的 latest view

为保持与当前 `ai_analysis_sessions` 一致的读取方式，建议为 run 和 tool call 也建立 latest view。

## 5.1 `logs.v_ai_agent_runs_latest`

用途：

- 避免 run 读取依赖 `FINAL`
- 支持快速按 `run_id` 查询最新状态

## 5.2 `logs.v_ai_agent_tool_calls_latest`

用途：

- 便于按 run 渲染当前工具状态

## 5.3 `logs.v_ai_agent_command_runs_latest`

用途：

- 便于前端刷新后读取命令运行快照

---

## 6. session 与 run 的关系

## 6.1 不建议用 run 替代 session

`session` 仍然需要保留，因为它负责：

- 用户可见历史会话
- 标题、归档、置顶
- 结果摘要

而 `run` 更接近一次“执行实例”。

## 6.2 建议关系

- 一个 `session` 下可以有多个 `run`
- 一个 `run` 对应一次用户追问或一次调查任务
- 一个 `run` 最终会沉淀为一条或多条 `message`

## 6.3 是否给 `ai_analysis_sessions` 增字段

建议增加但不强制：

- `latest_run_id String DEFAULT ''`
- `runtime_version String DEFAULT ''`

这样可以更方便从旧 session 跳到新 runtime run。

### 为什么不强制第一阶段就改

- 可以先通过 `run.session_id` 反查
- 避免第一批迁移面过大

---

## 7. 事件持久化策略

## 7.1 v1 原则

run event 必须持久化。

原因：

- 页面刷新恢复需要
- 错误排查需要
- 审批恢复需要

## 7.2 命令输出是否全量持久化

### v1 建议

采用“有限持久化”：

- 每个 `command_run` 的输出事件保留
- 单次命令总输出设上限
- 大输出进行截断并标记 `output_truncated`

### 不建议 v1 做法

- 完全不持久化命令输出
- 无限量保存命令输出

---

## 8. Redis 角色建议

## 8.1 v1 可不强依赖 Redis

如果当前部署中不希望扩大依赖面，v1 可以只用：

- ClickHouse 持久化
- 进程内事件队列

缺点：

- 多实例活跃 stream fan-out 不够优雅
- 进程重启中的活跃流体验一般

但优点是：

- 复杂度可控
- 与现有部署更一致

## 8.2 v1.1 建议引入的 Redis 角色

后续可评估 Redis 用于：

- active run subscriber registry
- 最近事件 ring buffer
- 多实例广播
- 活跃 command output fan-out

### Redis 不建议承担的角色

- 历史真相源
- 唯一 event 存储

---

## 9. 迁移顺序建议

## 阶段 1：新增表，不改现有读路径

### 任务

- 新建 `ai_agent_runs`
- 新建 `ai_agent_run_events`
- 新建 `ai_agent_tool_calls`
- 新建 `ai_agent_command_runs`
- 新建 `ai_agent_command_events`
- 新建 latest views

### 目标

不影响现有 AI 历史功能。

## 阶段 2：run 新写入，session 继续保留

### 任务

- 新 runtime 开始写入 run 和 event
- 最终结果继续写回 `ai_analysis_messages`
- 老页面仍可读消息历史

### 目标

新旧路径并存。

## 阶段 3：前端改为优先读 run/event

### 任务

- AI 分析页优先基于 `run + events` 恢复
- 历史页仍可继续读 session/message

### 目标

实现刷新恢复与流回放。

## 阶段 4：评估是否补 session 反向索引

### 任务

- 视需要给 session 表加 `latest_run_id`
- 或构建辅助 view 关联 run

### 目标

增强历史页跳转体验。

---

## 10. DDL 落地建议

现有仓库中已经有：

- `deploy/clickhouse-init-single.sql`
- `deploy/clickhouse-init-replicated.sql`
- `deploy/sql/release-2-structural.sql`

建议迁移方式：

### 初始化脚本

在以下文件中补新表 DDL：

- `deploy/clickhouse-init-single.sql`
- `deploy/clickhouse-init-replicated.sql`
- 对应 yaml 内嵌 SQL 模板

### 结构升级脚本

新增新的 release SQL，例如：

- `deploy/sql/release-3-ai-agent-runtime.sql`

内容包括：

- 新表
- latest view
- 可选 session 增字段

---

## 11. 读写路径建议

## 11.1 写路径

### AI Runtime

- 创建 run -> 写 `ai_agent_runs`
- 每发一个事件 -> 写 `ai_agent_run_events`
- 工具状态变化 -> upsert `ai_agent_tool_calls`
- 命令状态变化 -> upsert `ai_agent_command_runs`
- 命令输出 delta -> 写 `ai_agent_command_events`
- 最终答案 -> 继续写回 `ai_analysis_messages`

### 为什么最终消息还要写回旧 message 表

- 历史会话页可以继续复用
- 降低迁移期对旧功能的破坏

## 11.2 读路径

### AI 分析页新 runtime 路径

- run snapshot：读 `v_ai_agent_runs_latest`
- run replay：读 `ai_agent_run_events`
- command output replay：读 `ai_agent_command_events`

### 历史页

- 继续读 `ai_analysis_sessions`
- 继续读 `ai_analysis_messages`
- 若存在 `latest_run_id` 或 run 关联，则可跳转到 run 详情

---

## 12. 保留与清理策略

## 12.1 run/event 保留期建议

v1 建议：

- `ai_agent_runs`：长期保留
- `ai_agent_run_events`：保留 30 到 90 天
- `ai_agent_command_runs`：保留 30 到 90 天
- `ai_agent_command_events`：保留 7 到 30 天

## 12.2 为什么 command event 保留更短

- 体量更大
- 主要用于近期回放与排障
- 长期价值低于最终消息与最终 run 状态

---

## 13. 风险与对策

## 风险 1：ClickHouse 写放大

### 原因

event 和 command delta 都会放大写入量。

### 对策

- 控制命令输出上限
- 控制 event 粒度，避免每个字符都写一条
- `assistant_delta` 建议按 chunk，而不是逐 token 极细粒度持久化

## 风险 2：run 与 message 双写不一致

### 原因

runtime 最终答案既写 run，又写历史消息。

### 对策

- 以 run 为执行真相源
- message 只做最终归档
- 双写失败时允许 message 落后于 run，但不允许 run 缺失

## 风险 3：多实例下活跃流广播不足

### 原因

v1 若只靠进程内队列，多实例体验会受限。

### 对策

- v1 先保证持久化 + replay
- 活跃流广播在 v1.1 通过 Redis 解决

---

## 14. 推荐结论

v1 最推荐的存储方案是：

- **ClickHouse 持久化 run / event / tool_call / command_run / command_event**
- **旧 session/message 表继续保留**
- **活跃流先用进程内机制，后续再考虑 Redis 热态层**

这是当前代码库中实现风险最低、与现有架构最一致、又能支撑页面恢复和执行回放的方案。

---

## 15. 下一步建议

如果这份存储草案确认通过，下一步建议继续补：

1. `release-3-ai-agent-runtime.sql` 草案
2. run/event 状态图
3. 前端 reducer 状态模型草案
4. AI 分析页 workspace wireframe 草案
