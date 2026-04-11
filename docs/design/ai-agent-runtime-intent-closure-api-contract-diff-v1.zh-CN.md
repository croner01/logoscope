# AI Runtime Intent Closure API Contract Diff v1（草案）

状态: Draft  
日期: 2026-04-06  
目标: 在不破坏现有客户端的前提下，引入 `intent/fingerprint/grant` 闭环字段。

---

## 1. 变更范围

本次合同变更遵循“增量字段优先”:

1. 现有 endpoint 不删除，不改语义的前提下新增字段。
2. 新对象通过新增 endpoint 暴露（`/api/v2/runs/{run_id}/intents`）。
3. 旧客户端忽略未知字段即可兼容。

---

## 2. 字段级别 Diff

## 2.1 `POST /api/v1/exec/precheck`

### Request（新增可选字段）

```json
{
  "session_id": "sess-001",
  "message_id": "msg-001",
  "action_id": "act-001",
  "command": "kubectl ...",
  "purpose": "collect evidence",
  "target_kind": "k8s_cluster",
  "target_identity": "namespace:islap",
  "intent_id": "int-001",
  "fingerprint_hint": "sha256:...",
  "target_scope": "cluster:cluster-local/ns:islap"
}
```

### Response（新增字段）

```json
{
  "status": "confirmation_required",
  "approval_policy": "confirmation_required",
  "message": "....",
  "fingerprint": "sha256:...",
  "intent_id": "int-001",
  "target_scope": "cluster:cluster-local/ns:islap",
  "intent_resolution": {
    "duplicate_in_run": false,
    "budget_remaining": 1,
    "budget_blocked": false
  },
  "approval_grant": {
    "grant_reusable": true,
    "matched_grant_id": "grt-001",
    "grant_expires_at": "2026-04-06T13:00:00.000Z",
    "reuse_count": 0,
    "max_reuse": 1
  }
}
```

兼容说明:
- `fingerprint/intent_id/approval_grant` 缺失时，按旧逻辑处理。

---

## 2.2 `POST /api/v2/runs/{run_id}/approvals/{approval_id}/resolve`

### Request（保持）

```json
{
  "decision": "approved",
  "comment": "",
  "confirmed": true,
  "elevated": false
}
```

### Response（新增字段）

```json
{
  "run": {},
  "approval": {},
  "grant": {
    "grant_id": "grt-001",
    "fingerprint": "sha256:...",
    "target_scope": "cluster:cluster-local/ns:islap",
    "ttl_expires_at": "2026-04-06T13:00:00.000Z",
    "max_reuse": 1,
    "reuse_count": 0
  }
}
```

---

## 2.3 `GET /api/v2/runs/{run_id}`

### `run.summary` 新增推荐字段

```json
{
  "intent_stats": {
    "total": 3,
    "planned": 0,
    "awaiting_approval": 1,
    "dispatched": 0,
    "settled": 2,
    "blocked": 0
  },
  "fingerprint_stats": {
    "unique": 2,
    "executed": 1,
    "approval_requested": 1,
    "approval_reused": 1
  },
  "execution_closure": {
    "completion_gap": 0,
    "last_settled_at": "2026-04-06T12:40:00.000Z"
  },
  "budget_state": {
    "max_total_approvals_per_run": 2,
    "used_total_approvals": 1,
    "blocked": false
  }
}
```

---

## 2.4 `GET /api/v2/runs/{run_id}/events` / stream

## `approval_required` payload 新增

```json
{
  "approval_id": "apr-001",
  "intent_id": "int-001",
  "fingerprint": "sha256:...",
  "target_scope": "cluster:cluster-local/ns:islap",
  "grant_reusable": true,
  "matched_grant_id": ""
}
```

## `approval_resolved` payload 新增

```json
{
  "approval_id": "apr-001",
  "decision": "approved",
  "intent_id": "int-001",
  "fingerprint": "sha256:...",
  "grant_id": "grt-001"
}
```

## `tool_call_finished` payload 新增

```json
{
  "tool_call_id": "tool-001",
  "command_run_id": "cmdrun-001",
  "status": "completed",
  "fingerprint": "sha256:...",
  "intent_id": "int-001",
  "settled_candidate": true,
  "execution_record_id": "exr-001"
}
```

---

## 3. 新增 endpoint

## 3.1 `GET /api/v2/runs/{run_id}/intents`

用途:
- 前端展示“按意图”的执行闭环，而不是按命令碎片。

Response:

```json
{
  "run_id": "run-001",
  "intents": [
    {
      "intent_id": "int-001",
      "fingerprint": "sha256:...",
      "intent_type": "collect_evidence",
      "evidence_gap": "clickhouse query_log",
      "target_scope": "cluster:cluster-local/ns:islap",
      "status": "awaiting_approval",
      "approval": {
        "approval_id": "apr-001",
        "grant_reusable": true
      },
      "execution": {
        "command_run_id": "cmdrun-001",
        "settled_status": "pending"
      }
    }
  ]
}
```

## 3.2 `POST /api/v2/intents/{intent_id}/reconcile`（内部/运维）

用途:
- 在 SSE 终态缺失时触发补偿收敛。

Response:

```json
{
  "intent_id": "int-001",
  "reconciled": true,
  "execution_record_id": "exr-001",
  "settled_status": "completed"
}
```

---

## 4. 向后兼容策略

1. 所有新增字段均可选，旧客户端可忽略。
2. 旧 `summary.executed_commands` 保留，但标注为展示字段。
3. 新客户端优先使用:
   - `intents[]`
   - `fingerprint_stats`
   - `execution_closure.completion_gap`

---

## 5. 版本协商建议

1. 头部协商:
   - `X-AIRuntime-Contract: v2-intent-closure-preview`
2. 或 query 开关:
   - `?contract=v2-intent-closure-preview`
3. 灰度阶段保持双写双读，默认回落到旧字段。

---

## 6. 前端最小改造建议

1. Action 列表旁显示 `fingerprint` 与 `intent_status`。
2. 审批弹窗增加“复用审批令牌”提示。
3. Run 详情页增加 `completion_gap` 红色告警。

