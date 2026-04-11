# AI Agent Runtime V4 API v2 合同草案（中文版）

> 关联文档：  
> - `docs/design/ai-agent-runtime-v4-blueprint.zh-CN.md`  
> - `docs/design/ai-agent-runtime-v4-task-breakdown.zh-CN.md`  
> 状态：Draft（评审版）

---

## 1. 设计原则

## 1.1 核心对象

V4 API v2 的核心对象为：

1. `thread`
2. `run`
3. `action`
4. `approval`
5. `policy_decision`
6. `event`

## 1.2 契约原则

1. 事件流是唯一前端事实来源
2. 事件必须带递增 `seq`
3. 所有高风险动作必须可追溯到 `decision_id`
4. 写命令必须带完整 `diagnosis_contract`
5. 未知目标默认 `manual_required`

### 📌 Decision: api/v2-canonical-model
- **Value**: 统一以 thread-run-action 模型承载运行态，不再依赖旧 follow-up 返回体拼装
- **Layer**: data
- **Tags**: api, runtime, contract
- **Rationale**: 避免旧接口语义耦合导致的状态不一致和回放困难
- **Alternatives**: 延续 v1 接口并附加字段
- **Tradeoffs**: 迁移成本上升，但契约稳定性更高

### 🚫 Constraint: security
- **Rule**: API 不提供任何绕过审批或策略裁决的快捷执行接口
- **Priority**: critical
- **Tags**: gate, approval, policy

---

## 2. 通用约定

## 2.1 时间格式

统一使用 ISO 8601 UTC：

```json
"2026-03-23T10:15:30.123Z"
```

## 2.2 统一 ID 前缀

1. `thread_id`: `thr-...`
2. `run_id`: `run-...`
3. `event_id`: `evt-...`
4. `action_id`: `act-...`
5. `approval_id`: `apr-...`
6. `decision_id`: `dec-...`
7. `command_run_id`: `cmdrun-...`

## 2.3 通用错误结构

```json
{
  "error": {
    "code": "AIRV2-4001",
    "message": "invalid diagnosis_contract",
    "retryable": false,
    "details": {}
  }
}
```

---

## 3. 资源模型

## 3.1 Thread

```json
{
  "thread_id": "thr-3f8c2a1b",
  "session_id": "sess-7a12bc34",
  "conversation_id": "conv-932ab1de",
  "title": "排查 query-service timeout",
  "status": "active",
  "created_at": "2026-03-23T10:00:00.000Z",
  "updated_at": "2026-03-23T10:08:00.000Z"
}
```

## 3.2 Run

```json
{
  "run_id": "run-8ab9123d",
  "thread_id": "thr-3f8c2a1b",
  "status": "running",
  "engine": {
    "outer": "temporal-v1",
    "inner": "langgraph-v1"
  },
  "assistant_message_id": "msg-a-001",
  "user_message_id": "msg-u-001",
  "summary": {
    "current_phase": "acting",
    "iteration": 2
  },
  "created_at": "2026-03-23T10:01:00.000Z",
  "updated_at": "2026-03-23T10:03:00.000Z",
  "ended_at": null
}
```

`status` 枚举：

1. `queued`
2. `running`
3. `waiting_approval`
4. `waiting_user_input`
5. `blocked`
6. `completed`
7. `failed`
8. `cancelled`

## 3.3 Action（命令动作示例）

```json
{
  "action_id": "act-cmd-001",
  "run_id": "run-8ab9123d",
  "type": "command",
  "status": "executed",
  "command": "kubectl get pods -n islap",
  "target_kind": "k8s_cluster",
  "target_identity": "namespace:islap",
  "executor_profile": "toolbox-k8s-readonly",
  "policy_decision_id": "dec-001",
  "command_run_id": "cmdrun-001",
  "created_at": "2026-03-23T10:02:00.000Z",
  "updated_at": "2026-03-23T10:02:05.000Z"
}
```

## 3.4 PolicyDecision

```json
{
  "decision_id": "dec-001",
  "run_id": "run-8ab9123d",
  "action_id": "act-cmd-001",
  "engine": "opa",
  "package": "runtime.command.v1",
  "result": "allow",
  "reason": "readonly whitelisted target",
  "input_hash": "sha256:...",
  "created_at": "2026-03-23T10:01:59.000Z"
}
```

`result` 枚举：

1. `allow`
2. `confirm`
3. `elevate`
4. `deny`
5. `manual_required`

## 3.5 Approval

```json
{
  "approval_id": "apr-001",
  "run_id": "run-8ab9123d",
  "action_id": "act-cmd-002",
  "status": "pending",
  "requires_elevation": true,
  "title": "执行写命令需要审批",
  "reason": "write command requires elevation",
  "expires_at": "2026-03-23T10:20:00.000Z",
  "created_at": "2026-03-23T10:05:00.000Z"
}
```

---

## 4. 事件模型

## 4.1 Event 对象

```json
{
  "event_id": "evt-000012",
  "run_id": "run-8ab9123d",
  "seq": 12,
  "event_type": "approval_required",
  "created_at": "2026-03-23T10:05:00.100Z",
  "payload": {}
}
```

## 4.2 关键事件类型

1. `run_created`
2. `run_status_changed`
3. `reasoning_step`
4. `reasoning_summary_delta`
5. `policy_decision_recorded`
6. `action_started`
7. `tool_call_started`
8. `tool_call_output_delta`
9. `tool_call_finished`
10. `tool_call_skipped_duplicate`
11. `approval_required`
12. `approval_resolved`
13. `action_waiting_user_input`
14. `action_resumed`
15. `contract_validation_failed`
16. `contract_reask_started`
17. `contract_blocked`
18. `run_completed`
19. `run_failed`
20. `run_cancelled`
21. `run_interrupted`

补充约定（2026-04）:
- `tool_call_skipped_duplicate` payload 应包含 `reason_code`，并可选携带:
  `evidence_reuse`, `reused_evidence_ids`, `evidence_slot_id`, `evidence_outcome`。
- `run_finished` payload 可携带 `diagnosis_status`, `fault_summary`, `gate_decision`。
- `run.summary_json` 建议包含:
  `plan_coverage`, `exec_coverage`, `evidence_coverage`, `final_confidence`,
  `missing_evidence_slots`, `next_best_commands`，用于前端解释性展示。

### 📌 Decision: api/v2-event-minimum
- **Value**: v2 事件流必须包含 policy、approval、contract 三类治理事件
- **Layer**: data
- **Tags**: event, policy, approval, contract
- **Rationale**: 治理链条若不进入主事件流将无法回放审计
- **Alternatives**: 仅在审计表保留治理数据
- **Tradeoffs**: 事件量增加，但可观测性和可追责性提升

---

## 5. 接口定义

## 5.1 创建 Thread

`POST /api/v2/threads`

请求：

```json
{
  "title": "排查 query-service timeout",
  "session_id": "sess-7a12bc34",
  "conversation_id": "conv-932ab1de",
  "metadata": {}
}
```

响应：

```json
{
  "thread": {}
}
```

## 5.2 获取 Thread

`GET /api/v2/threads/{thread_id}`

响应：

```json
{
  "thread": {},
  "latest_run": {}
}
```

## 5.3 创建 Run

`POST /api/v2/threads/{thread_id}/runs`

请求：

```json
{
  "question": "继续排查并确认根因",
  "analysis_context": {
    "service_name": "query-service"
  },
  "runtime_options": {
    "max_iterations": 6,
    "approval_timeout_seconds": 900
  }
}
```

响应：

```json
{
  "run": {}
}
```

## 5.4 获取 Run

`GET /api/v2/runs/{run_id}`

响应：

```json
{
  "run": {}
}
```

## 5.5 读取事件列表

`GET /api/v2/runs/{run_id}/events?after_seq=0&limit=500`

响应：

```json
{
  "events": [],
  "next_after_seq": 120
}
```

## 5.6 订阅事件流

`GET /api/v2/runs/{run_id}/events/stream?after_seq=0`

SSE 事件格式：

```text
event: run_status_changed
data: {"event_id":"evt-001","run_id":"run-001","seq":1,"payload":{}}
```

## 5.7 中断 Run

`POST /api/v2/runs/{run_id}/interrupt`

请求：

```json
{
  "reason": "user_interrupt_esc"
}
```

响应：

```json
{
  "run": {}
}
```

## 5.8 提交审批

`POST /api/v2/runs/{run_id}/approvals/{approval_id}/resolve`

请求：

```json
{
  "decision": "approved",
  "comment": "执行",
  "confirmed": true,
  "elevated": true
}
```

`decision` 枚举：

1. `approved`
2. `rejected`

响应：

```json
{
  "run": {},
  "approval": {},
  "next_action": {}
}
```

## 5.9 提交用户补充输入

`POST /api/v2/runs/{run_id}/input`

请求：

```json
{
  "text": "重点确认最近 10 分钟连接池耗尽链路"
}
```

响应：

```json
{
  "run": {},
  "user_input": {}
}
```

失败返回（上下文回灌阻断）：

1. 状态码：`409`
2. 触发条件：提交输入前，系统隐形执行“当前会话上下文回灌”；若回灌失败或超时，则阻断本次输入
3. 重试策略：单次 `60s` 超时，自动重试 `1` 次（总计 2 次尝试）
4. 前端语义：保留用户已输入文本，不清空；展示“重试”入口后再次提交同一输入

超时示例：

```json
{
  "detail": {
    "code": "context_hydration_timeout",
    "message": "载入当前会话上下文超时（60s，已重试 2 次），本次输入已阻断，请点击重试。",
    "retryable": true,
    "timeout_seconds": 60,
    "attempts": 2,
    "reason": "timeout"
  }
}
```

非超时失败示例：

```json
{
  "detail": {
    "code": "context_hydration_failed",
    "message": "载入当前会话上下文失败，本次输入已阻断，请点击重试。",
    "retryable": true,
    "timeout_seconds": 60,
    "attempts": 2,
    "reason": "analysis_session_not_found"
  }
}
```

## 5.10 提交命令动作（手工触发）

`POST /api/v2/runs/{run_id}/actions/command`

请求：

```json
{
  "command": "kubectl get pods -n islap",
  "purpose": "确认异常 pod 是否持续重启",
  "target_kind": "k8s_cluster",
  "target_identity": "namespace:islap",
  "diagnosis_contract": {
    "fault_summary": "...",
    "evidence_gaps": ["..."],
    "execution_plan": ["..."],
    "why_command_needed": "..."
  },
  "confirmed": false,
  "elevated": false
}
```

响应：

```json
{
  "action": {},
  "policy_decision": {},
  "approval": null
}
```

---

## 6. 高风险命令三条件 Gate 行为

## 6.1 判定流程

1. 命令预处理与目标解析
2. OPA 裁决
3. diagnosis_contract 校验
4. 人工审批（若需要）
5. 执行器准入
6. 执行与流式输出

## 6.2 失败返回约定

1. OPA deny：`403` + `policy_decision` + `reason`
2. 合同不完整：`422` + `missing_fields`
3. 审批未通过：`409` + `approval_state=pending|rejected`
4. 未知目标：`409` + `manual_required=true`

### 🚫 Constraint: security
- **Rule**: 任何写命令在 Gate 任一环节失败都必须终止，不允许降级执行
- **Priority**: critical
- **Tags**: write-gate, fail-closed

---

## 7. 状态机与接口行为约束

1. `waiting_approval` 时仅允许：
   - 审批 resolve
   - run cancel
   - run interrupt
2. `blocked` 时仅允许：
   - 新建 run（同 thread）
   - 查看历史
3. `completed/failed/cancelled` 为 terminal，不允许新增 action
4. `rejected` 后若触发 replan，最多 1 次，随后必须 blocked

### 📌 Decision: api/v2-state-guard
- **Value**: 接口按 run 状态实施严格可执行动作约束
- **Layer**: business
- **Tags**: state-machine, api-guard
- **Rationale**: 防止前端或调用方通过非法顺序推进状态
- **Alternatives**: 由前端自行约束调用顺序
- **Tradeoffs**: 后端校验逻辑增加，但状态一致性更强

---

## 8. 审计回放查询接口（建议）

## 8.1 单 Run 回放

`GET /api/v2/runs/{run_id}/replay`

响应：

```json
{
  "run": {},
  "events": [],
  "policy_decisions": [],
  "approvals": [],
  "actions": []
}
```

## 8.2 策略决策查询

`GET /api/v2/policy/decisions/{decision_id}`

响应：

```json
{
  "decision": {}
}
```

---

## 9. 兼容策略

1. 保留 v1 只读接口用于历史页面
2. 新建 v2 前端页面与 v2 客户端并行
3. 稳定后移除 v1 主执行入口

### 📌 Decision: api/v2-migration-window
- **Value**: v1/v2 保留短期迁移窗口，主执行链优先切到 v2
- **Layer**: infrastructure
- **Tags**: migration, compatibility
- **Rationale**: 在停服重构后仍需保留历史查询能力，降低切换风险
- **Alternatives**: 立即删除 v1 全部接口
- **Tradeoffs**: 维护成本短期增加，但上线风险降低

---

## 10. 验收清单（接口层）

1. API v2 能独立跑通完整 ReAct 排障链路
2. SSE 支持 `after_seq` 恢复
3. 写命令缺少合同字段时被拒绝
4. 未知目标返回 manual_required
5. policy decision 与 run 可双向追溯
6. 审批 reject 后最多一次 replan，再 blocked
