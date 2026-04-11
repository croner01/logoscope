# AI Agent Runtime API 合同草案

> 状态: Draft  
> 目标: 为 AI Agent Runtime v1 提供统一的接口、事件和数据契约  
> 范围: `ai-service` 运行态接口 + `exec-service` 命令运行接口

---

## 1. 设计原则

### 1.1 总体原则

AI 分析模块从“请求返回答案”切换为“创建运行实例并持续产生日志事件”。

因此 API 合同的核心对象不再是单次 follow-up 响应，而是：

- `session`
- `run`
- `event`
- `tool_call`
- `command_run`

### 1.2 契约原则

- 前后端只认一套 canonical event schema
- 所有运行态事件都必须带 `run_id`
- 所有事件都必须带递增 `seq`
- 助手消息从 run 创建开始就具备稳定 `assistant_message_id`
- 前端不创建本地临时消息 ID 作为主渲染对象
- 风险动作必须经过审批接口，不允许前端伪造状态推进

### 1.3 v1 非目标

- 不直接暴露原始 chain-of-thought
- 不支持无审批写操作自动执行
- 不要求把旧 follow-up 流接口立即删除

---

## 2. 通用约定

## 2.1 认证

沿用现有服务认证方式，本草案不新增认证协议。

## 2.2 时间格式

统一使用 ISO 8601 UTC 字符串，例如：

```json
"2026-03-18T08:15:30.123Z"
```

## 2.3 ID 命名建议

- `session_id`: `sess-...`
- `run_id`: `run-...`
- `event_id`: `evt-...`
- `message_id`: `msg-...`
- `tool_call_id`: `tool-...`
- `command_run_id`: `cmdrun-...`
- `approval_id`: `apr-...`

## 2.4 错误响应

统一错误响应结构：

```json
{
  "error": {
    "code": "AIR-4001",
    "message": "question is required",
    "retryable": false,
    "details": {}
  }
}
```

字段说明：

- `code`: 稳定错误码
- `message`: 面向前端和日志的简短说明
- `retryable`: 是否建议前端重试
- `details`: 扩展上下文

---

## 3. AI Runtime 资源模型

## 3.1 Run 对象

```json
{
  "run_id": "run-7f8c9d1a",
  "session_id": "sess-1a2b3c4d",
  "status": "running",
  "analysis_type": "log",
  "engine": "agent-runtime-v1",
  "assistant_message_id": "msg-5f6a7b8c",
  "user_message_id": "msg-u-91ab23cd",
  "service_name": "query-service",
  "trace_id": "trace-001",
  "summary": {
    "title": "排查 query-service 数据库超时",
    "current_phase": "acting",
    "iteration": 2
  },
  "created_at": "2026-03-18T08:15:30.123Z",
  "updated_at": "2026-03-18T08:15:45.456Z",
  "ended_at": null
}
```

### `status` 可选值

- `queued`
- `running`
- `waiting_approval`
- `completed`
- `failed`
- `cancelled`

## 3.2 Event 对象

```json
{
  "event_id": "evt-000012",
  "run_id": "run-7f8c9d1a",
  "seq": 12,
  "event_type": "tool_call_started",
  "created_at": "2026-03-18T08:15:36.100Z",
  "payload": {
    "tool_call_id": "tool-logs-001",
    "tool_name": "logs.query",
    "title": "查询 query-service 最近 50 行错误日志"
  }
}
```

---

## 4. AI Runtime 接口

## 4.1 创建运行

### 请求

`POST /api/v1/ai/runs`

```json
{
  "session_id": "sess-1a2b3c4d",
  "question": "继续排查 query-service 为什么数据库超时",
  "analysis_context": {
    "analysis_type": "log",
    "service_name": "query-service",
    "trace_id": "trace-001",
    "input_text": "database timeout",
    "result": {
      "overview": {
        "description": "数据库连接超时"
      }
    }
  },
  "runtime_options": {
    "use_llm": true,
    "max_iterations": 4,
    "auto_exec_readonly": true
  }
}
```

### 响应

```json
{
  "run": {
    "run_id": "run-7f8c9d1a",
    "session_id": "sess-1a2b3c4d",
    "status": "running",
    "analysis_type": "log",
    "engine": "agent-runtime-v1",
    "assistant_message_id": "msg-5f6a7b8c",
    "user_message_id": "msg-u-91ab23cd",
    "service_name": "query-service",
    "trace_id": "trace-001",
    "summary": {
      "title": "排查 query-service 数据库超时",
      "current_phase": "initializing",
      "iteration": 0
    },
    "created_at": "2026-03-18T08:15:30.123Z",
    "updated_at": "2026-03-18T08:15:30.123Z",
    "ended_at": null
  }
}
```

### 说明

- 创建成功后，后端应立即生成 `assistant_message_id`
- 前端拿到 `run_id` 后应立即订阅 stream，而不是等待 final payload

---

## 4.2 查询运行快照

### 请求

`GET /api/v1/ai/runs/{run_id}`

### 响应

```json
{
  "run": {
    "run_id": "run-7f8c9d1a",
    "session_id": "sess-1a2b3c4d",
    "status": "waiting_approval",
    "analysis_type": "log",
    "engine": "agent-runtime-v1",
    "assistant_message_id": "msg-5f6a7b8c",
    "user_message_id": "msg-u-91ab23cd",
    "service_name": "query-service",
    "trace_id": "trace-001",
    "summary": {
      "title": "排查 query-service 数据库超时",
      "current_phase": "waiting_approval",
      "iteration": 3,
      "pending_approval_count": 1
    },
    "created_at": "2026-03-18T08:15:30.123Z",
    "updated_at": "2026-03-18T08:16:12.023Z",
    "ended_at": null
  }
}
```

---

## 4.3 查询运行事件列表

### 请求

`GET /api/v1/ai/runs/{run_id}/events?after_seq=0&limit=200`

### 响应

```json
{
  "run_id": "run-7f8c9d1a",
  "next_after_seq": 18,
  "events": [
    {
      "event_id": "evt-000001",
      "run_id": "run-7f8c9d1a",
      "seq": 1,
      "event_type": "run_started",
      "created_at": "2026-03-18T08:15:30.123Z",
      "payload": {
        "status": "running"
      }
    },
    {
      "event_id": "evt-000002",
      "run_id": "run-7f8c9d1a",
      "seq": 2,
      "event_type": "message_started",
      "created_at": "2026-03-18T08:15:30.125Z",
      "payload": {
        "assistant_message_id": "msg-5f6a7b8c"
      }
    }
  ]
}
```

### 用途

- 页面刷新后的事件回放
- 断线重连后的状态恢复
- 历史运行详情展示

---

## 4.4 运行事件流

### 请求

`GET /api/v1/ai/runs/{run_id}/stream?after_seq=0`

### 响应类型

`Content-Type: text/event-stream`

### SSE 格式

```text
event: run_started
data: {"run_id":"run-7f8c9d1a","seq":1,"payload":{"status":"running"}}

event: message_started
data: {"run_id":"run-7f8c9d1a","seq":2,"payload":{"assistant_message_id":"msg-5f6a7b8c"}}

event: assistant_delta
data: {"run_id":"run-7f8c9d1a","seq":17,"payload":{"assistant_message_id":"msg-5f6a7b8c","text":"我先检查最近的错误日志。"}}
```

### 流断开后前端策略

1. 记录已消费最大 `seq`
2. 重新拉起 `stream?after_seq={last_seq}`
3. 若 stream 无法恢复，则退化到 `GET /events`

---

## 4.5 审批动作

### 请求

`POST /api/v1/ai/runs/{run_id}/approve`

```json
{
  "approval_id": "apr-001",
  "decision": "approved",
  "comment": "允许执行写命令",
  "confirmed": true,
  "elevated": true
}
```

### 响应

```json
{
  "run": {
    "run_id": "run-7f8c9d1a",
    "status": "running",
    "updated_at": "2026-03-18T08:17:01.223Z"
  },
  "approval": {
    "approval_id": "apr-001",
    "decision": "approved"
  }
}
```

### `decision` 可选值

- `approved`
- `rejected`

---

## 4.6 取消运行

### 请求

`POST /api/v1/ai/runs/{run_id}/cancel`

```json
{
  "reason": "user_cancelled"
}
```

### 响应

```json
{
  "run": {
    "run_id": "run-7f8c9d1a",
    "status": "cancelled",
    "updated_at": "2026-03-18T08:17:20.990Z",
    "ended_at": "2026-03-18T08:17:20.990Z"
  }
}
```

---

## 5. AI Runtime 事件定义

## 5.1 事件总表

v1 建议支持以下事件：

- `run_started`
- `run_status_changed`
- `message_started`
- `reasoning_summary_delta`
- `reasoning_step`
- `tool_call_started`
- `tool_call_progress`
- `tool_call_output_delta`
- `tool_call_finished`
- `approval_required`
- `approval_resolved`
- `assistant_delta`
- `assistant_message_finalized`
- `run_finished`
- `run_failed`
- `run_cancelled`

---

## 5.2 `run_started`

```json
{
  "run_id": "run-7f8c9d1a",
  "seq": 1,
  "payload": {
    "status": "running",
    "analysis_type": "log",
    "engine": "agent-runtime-v1"
  }
}
```

## 5.3 `message_started`

```json
{
  "run_id": "run-7f8c9d1a",
  "seq": 2,
  "payload": {
    "assistant_message_id": "msg-5f6a7b8c"
  }
}
```

## 5.4 `reasoning_summary_delta`

用于展示可公开的“思考摘要”，不直接暴露原始 chain-of-thought。

```json
{
  "run_id": "run-7f8c9d1a",
  "seq": 3,
  "payload": {
    "step_id": "step-001",
    "phase": "planning",
    "text": "正在定位首个失败节点"
  }
}
```

## 5.5 `reasoning_step`

用于结构化展示阶段和标题。

```json
{
  "run_id": "run-7f8c9d1a",
  "seq": 4,
  "payload": {
    "step_id": "step-001",
    "phase": "acting",
    "title": "查询最近错误日志",
    "status": "in_progress",
    "iteration": 1
  }
}
```

## 5.6 `tool_call_started`

```json
{
  "run_id": "run-7f8c9d1a",
  "seq": 5,
  "payload": {
    "tool_call_id": "tool-logs-001",
    "step_id": "step-001",
    "tool_name": "logs.query",
    "title": "查询 query-service 最近 50 行错误日志",
    "input": {
      "service_name": "query-service",
      "limit": 50
    }
  }
}
```

## 5.7 `tool_call_output_delta`

适用于流式工具输出，尤其是命令执行。

```json
{
  "run_id": "run-7f8c9d1a",
  "seq": 9,
  "payload": {
    "tool_call_id": "tool-cmd-001",
    "command_run_id": "cmdrun-001",
    "stream": "stdout",
    "text": "2026-03-18 08:16:11 ERROR db pool exhausted\n"
  }
}
```

## 5.8 `tool_call_finished`

```json
{
  "run_id": "run-7f8c9d1a",
  "seq": 12,
  "payload": {
    "tool_call_id": "tool-logs-001",
    "status": "completed",
    "summary": {
      "matched_count": 4,
      "highlights": [
        "发现连接池耗尽错误",
        "错误集中在 query-service"
      ]
    }
  }
}
```

## 5.9 `approval_required`

```json
{
  "run_id": "run-7f8c9d1a",
  "seq": 15,
  "payload": {
    "approval_id": "apr-001",
    "tool_call_id": "tool-cmd-002",
    "action_id": "act-002",
    "title": "执行写命令需要审批",
    "command": "kubectl rollout restart deploy/query-service -n islap",
    "command_type": "repair",
    "risk_level": "high",
    "requires_confirmation": true,
    "requires_elevation": true,
    "message": "写命令需要提权审批"
  }
}
```

## 5.10 `assistant_delta`

```json
{
  "run_id": "run-7f8c9d1a",
  "seq": 20,
  "payload": {
    "assistant_message_id": "msg-5f6a7b8c",
    "text": "从最近日志看，query-service 出现数据库连接池耗尽。"
  }
}
```

## 5.11 `assistant_message_finalized`

```json
{
  "run_id": "run-7f8c9d1a",
  "seq": 25,
  "payload": {
    "assistant_message_id": "msg-5f6a7b8c",
    "content": "结论：query-service 首要问题是数据库连接池耗尽。\n建议先检查连接池配置和慢 SQL。",
    "references": [
      {
        "id": "L1",
        "type": "log",
        "title": "query-service ERROR log",
        "snippet": "db pool exhausted"
      }
    ]
  }
}
```

## 5.12 `run_finished`

```json
{
  "run_id": "run-7f8c9d1a",
  "seq": 26,
  "payload": {
    "status": "completed",
    "assistant_message_id": "msg-5f6a7b8c"
  }
}
```

## 5.13 `run_failed`

```json
{
  "run_id": "run-7f8c9d1a",
  "seq": 18,
  "payload": {
    "status": "failed",
    "error": {
      "code": "AIR-5002",
      "message": "runtime timeout",
      "retryable": true
    }
  }
}
```

---

## 6. Exec-Service 接口

## 6.1 创建命令运行

### 请求

`POST /api/v1/exec/runs`

```json
{
  "run_id": "run-7f8c9d1a",
  "tool_call_id": "tool-cmd-001",
  "message_id": "msg-5f6a7b8c",
  "action_id": "act-001",
  "command": "kubectl logs deploy/query-service -n islap --tail=50",
  "timeout_seconds": 20,
  "confirmed": true,
  "elevated": false
}
```

### 响应

```json
{
  "command_run": {
    "command_run_id": "cmdrun-001",
    "run_id": "run-7f8c9d1a",
    "tool_call_id": "tool-cmd-001",
    "status": "running",
    "command": "kubectl logs deploy/query-service -n islap --tail=50",
    "command_type": "query",
    "risk_level": "low",
    "started_at": "2026-03-18T08:15:36.800Z",
    "ended_at": null
  }
}
```

---

## 6.2 查询命令快照

### 请求

`GET /api/v1/exec/runs/{command_run_id}`

### 响应

```json
{
  "command_run": {
    "command_run_id": "cmdrun-001",
    "run_id": "run-7f8c9d1a",
    "tool_call_id": "tool-cmd-001",
    "status": "completed",
    "command": "kubectl logs deploy/query-service -n islap --tail=50",
    "command_type": "query",
    "risk_level": "low",
    "exit_code": 0,
    "started_at": "2026-03-18T08:15:36.800Z",
    "ended_at": "2026-03-18T08:15:38.101Z"
  }
}
```

---

## 6.3 命令事件流

### 请求

`GET /api/v1/exec/runs/{command_run_id}/events?after_seq=0`

### SSE 示例

```text
event: command_started
data: {"command_run_id":"cmdrun-001","seq":1,"payload":{"command":"kubectl logs deploy/query-service -n islap --tail=50"}}

event: command_output_delta
data: {"command_run_id":"cmdrun-001","seq":2,"payload":{"stream":"stdout","text":"ERROR db pool exhausted\n"}}

event: command_finished
data: {"command_run_id":"cmdrun-001","seq":3,"payload":{"status":"completed","exit_code":0}}
```

---

## 6.4 取消命令

### 请求

`POST /api/v1/exec/runs/{command_run_id}/cancel`

```json
{
  "reason": "user_cancelled"
}
```

### 响应

```json
{
  "command_run": {
    "command_run_id": "cmdrun-001",
    "status": "cancelled",
    "ended_at": "2026-03-18T08:16:30.231Z"
  }
}
```

---

## 7. 前端消费约定

## 7.1 创建运行后的行为

前端拿到 `run_id` 后应：

1. 存储 `run_id`
2. 立即打开 `/stream`
3. 同步渲染 `assistant_message_id`
4. 若流断开，走 `/events` 回放

## 7.2 禁止行为

前端不应：

- 生成主消息用的本地占位 assistant ID
- 依赖 final payload 作为唯一真相
- 通过本地逻辑猜测审批是否已通过

## 7.3 UI 状态来源

- 对话文本来自 `assistant_delta` 和 `assistant_message_finalized`
- 时间线来自 `reasoning_step`、`tool_call_*`
- 命令输出来自 `tool_call_output_delta`
- 审批卡片来自 `approval_required`
- 顶部状态来自 `run_status_changed`、`run_finished`

---

## 8. 向后兼容建议

## 8.1 旧 follow-up 接口

过渡期可以保留：

- `/api/v1/ai/follow-up`
- `/api/v1/ai/follow-up/stream`
- `/api/v1/ai/v2/follow-up/stream`

但内部建议逐步改为：

- 创建 `run`
- 由新 runtime 执行
- 最后将结果映射为旧格式响应

## 8.2 历史会话

老会话继续可读，但不要求都具备可回放的 run event。

建议仅新 runtime 产生的会话支持 run replay。

---

## 9. 待确认项

在正式编码前，还需要确认以下事项：

1. canonical transport 最终使用 SSE 还是 WebSocket
2. run/event 存储选型是 ClickHouse、Redis 还是混合
3. command output 是否需要全量持久化
4. approval 是否只支持单步审批，还是支持批量审批
5. 最终答案是否允许在 run 完成后继续增补“后处理事件”

---

## 10. 建议下一步

如果本 API 草案确认通过，下一步建议再补一份：

- 存储迁移草案
- 事件状态图
- 前端 reducer 状态草案

这样就可以直接进入实现前评审。
