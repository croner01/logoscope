# AI Analysis — Metadata-Driven Remote Execution

**Date:** 2026-06-05
**Status:** Design approved
**Scope:** AI analysis page — metadata injection, dual-channel command routing, cost preflight gating, session-level dedup

## Problem

When analyzing logs on the AI analysis page, the AI agent has access to rich Kubernetes metadata
(pod name, namespace, node name, labels, container name, host IP) from the log entry itself.
However, this metadata is not explicitly injected into the agent's execution context, so the
agent cannot use it to target diagnostics at the specific pod/node that produced the log.

Additionally, all commands — including simple log queries against ClickHouse — currently go
through `kubectl exec` into ClickHouse pods via exec-service. There is no distinction between
"local" analysis (log lookups that could use query-service) and "remote" analysis (pod/node
system state that truly requires cluster execution).

## Design Decisions

| Dimension | Decision |
|-----------|----------|
| Interaction mode | Automatic — AI decides what to execute |
| Scope | Full cluster — any namespace, pod, or node relevant to the problem |
| Operation type | Read-only only |
| Dedup | Session-level (not persisted across sessions) |
| Log query preference | query-service API first, fallback to ClickHouse SQL for complex aggregation |
| Cost control | Preflight cost estimation with manual approval gate for expensive commands |

## Architecture

### Component Overview

```
Frontend (AIAnalysis.tsx)
  │  extracts pod/namespace/node/labels from logData
  │  injects as source_target in analysis_context
  ▼
AI Service (api/ai.py)
  │  passes source_target into SkillContext
  ▼
Skill.plan_steps() / LLM react loop
  │  generates command_spec with precise target
  ▼
┌─────────────────────────────────────┐
│ CommandRouter (NEW)                  │
│                                      │
│  Routes based on command_family +    │
│  target_kind:                        │
│                                      │
│  clickhouse (simple SELECT)          │
│    → QueryServiceClient (NEW)        │
│    → GET /api/v1/logs                │
│                                      │
│  clickhouse (complex aggregation)    │
│    → ExecServiceClient (existing)    │
│    → kubectl exec clickhouse-client  │
│                                      │
│  kubernetes / shell / host_control   │
│    → ExecServiceClient (existing)    │
│    → toolbox-gateway                 │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│ ExecutionJournal (NEW)               │
│  fingerprint → skip or execute       │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│ CostPreflight (NEW)                  │
│  estimate → auto | warn | block      │
│  block → approval_required event     │
└─────────────────────────────────────┘
```

## Detailed Design

### 1. Metadata Injection

**analysis_context 新增字段:**

```python
"source_target": {
    "pod_name": "semantic-engine-7d5f8b9c-abc12",
    "namespace": "islap",
    "node_name": "node-3",
    "host_ip": "10.0.1.15",
    "container_name": "semantic-engine",
    "labels": {"app": "semantic-engine", "version": "v2.3"},
    "service_name": "semantic-engine"
}
```

**Frontend changes:**
- `runtimeFollowUpContext.ts`: `buildRuntimeFollowUpContext()` extracts pod/namespace/node/labels from
  `logData` and populates `source_target`.
- `AIAnalysis.tsx`: `buildLogAnalysisInput()` passes these fields through to `analysis_context`.

**AI Service changes:**
- `ai/skills/base.py`: `SkillContext` gains `source_target: dict | None` field.
- All skill `plan_steps()` methods can read `self.context.source_target` instead of extracting
  pod/namespace via regex from the question text.

### 2. Dual-Channel Command Router

**New file: `ai/agent_runtime/command_router.py`**

```python
class CommandRouter:
    """Routes command_spec to the appropriate execution channel."""

    ROUTE_LOCAL = "local"    # query-service API
    ROUTE_REMOTE = "remote"  # exec-service

    def route(self, command_spec: dict) -> tuple[str, str]:
        """
        Returns (channel, reason).

        Local channel: simple ClickHouse log queries → query-service
        Remote channel: kubectl, pod exec, complex ClickHouse SQL, host commands
        """
```

**Routing rules:**

| Condition | Channel | Mechanism |
|-----------|---------|-----------|
| `command_family == "clickhouse"` + simple SELECT (no JOIN/subquery/GROUP BY/window) | Local | Translate to query-service filter params |
| `command_family == "clickhouse"` + complex aggregation | Remote | kubectl exec clickhouse-client (existing) |
| `command_family == "kubernetes"` (get/logs/describe/top) | Remote | ExecServiceClient (existing) |
| `command_family == "shell"` (cat/ps/df/ss inside pod) | Remote | kubectl exec via toolbox-gateway (existing) |
| `command_family == "host_control"` (systemctl/service) | Remote | ssh-gateway via exec-service (existing) |

**New file: `ai/agent_runtime/query_client.py`**

```python
class QueryServiceClient:
    """Calls query-service /api/v1/logs for local log lookups."""

    async def query_logs(
        self,
        service_name: str | None = None,
        namespace: str | None = None,
        pod_name: str | None = None,
        trace_id: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        level: str | None = None,
        search: str | None = None,
        limit: int = 200,
    ) -> CommandResult:
        """Translate parameters to GET /api/v1/logs and return unified result."""
```

`CommandResult` is a unified response shape matching exec-service's output format so callers
don't care which channel was used.

### 3. Cost Preflight Gate

**New file: `ai/agent_runtime/cost_preflight.py`**

```python
class CostPreflight:
    """Estimates command cost and decides auto/warn/block."""

    THRESHOLDS = {
        "estimated_rows": 100_000,     # ClickHouse scanned rows
        "time_window_hours": 24,       # Query time range
        "target_nodes": 3,             # Distinct nodes involved
        "session_command_limit": 10,   # Commands per session
        "all_namespaces": True,        # -A / --all-namespaces triggers review
    }

    def evaluate(self, command_spec: dict, cost_tracker: dict) -> PreflightResult:
        """
        Returns:
            - Decision.AUTO: execute immediately
            - Decision.WARN: execute but notify frontend
            - Decision.BLOCK: require manual approval
        """
```

**Approval flow:**
- BLOCK decisions emit an `approval_required` SSE event (reusing existing event protocol).
- Frontend's existing approval UI in `AIAnalysis.tsx` renders the cost gate card.
- User confirms → command executes. User denies → command skipped, journal records denial.

**Session cost tracker** (in `AgentRun.summary_json`):

```python
"cost_tracker": {
    "commands_executed": 5,
    "estimated_rows_scanned": 42000,
    "targets_touched": {"pod": 2, "node": 0},
    "session_command_limit": 10
}
```

### 4. Session-Level Dedup & Result Memory

**New file: `ai/agent_runtime/execution_journal.py`**

```python
class ExecutionJournal:
    """Session-level command dedup and result memory."""

    def fingerprint(self, command_spec: dict) -> str:
        """Hash of (command_family, target_identity, normalized command text)."""

    def lookup(self, fingerprint: str) -> JournalEntry | None:
        """Return cached result if this exact command was already executed."""

    def record(self, fingerprint: str, command: str, result: CommandResult,
               ai_summary: str) -> None:
        """Record execution with AI-generated one-line summary."""
```

**Journal entry structure:**

```python
{
    "fingerprint": "a1b2c3d4",
    "command": "kubectl logs semantic-engine-7d5f8b -n islap --tail=100",
    "target_kind": "k8s_cluster",
    "target_identity": "pod:semantic-engine-7d5f8b/namespace:islap",
    "executed_at": "2026-06-05T10:30:00Z",
    "exit_code": 0,
    "summary": "最后 100 行日志中发现 3 条 OOMKilled 记录，发生在 10:28-10:30",
    "output_truncated_preview": "2026-06-05T10:28:15Z ERROR memory...\n..."
}
```

**LLM context injection:**
When the LLM plans the next diagnostic steps, the journal's `[command, summary]` pairs are
injected into the system prompt so the model knows what has already been checked and can
reference prior conclusions.

**Fingerprint granularity:**
- Same command with same parameters → same fingerprint → skip.
- Same command with different pod name → different fingerprint → execute.
- Parameter values ARE part of the fingerprint (we only skip exact duplicates).

### 5. Integration into Agent Run Lifecycle

Modified flow in `ai/agent_runtime/service.py`:

```
Skill.plan_steps() → [SkillStep(command_spec, ...)]
  │
  ▼
For each command_spec:
  1. CommandRouter.route(spec)       → channel (local/remote)
  2. ExecutionJournal.lookup(spec)   → cached? skip with summary
  3. CostPreflight.evaluate(spec)    → auto | warn | block
  4a.  auto/warn: execute via channel
  4b.  block: emit approval_required event, wait for user
  5. ExecutionJournal.record(spec, result, ai_summary)
  6. Emit observation event with result
```

## Files Changed

### New Files

| File | Purpose |
|------|---------|
| `ai-service/ai/agent_runtime/command_router.py` | Dual-channel routing logic |
| `ai-service/ai/agent_runtime/query_client.py` | query-service HTTP client |
| `ai-service/ai/agent_runtime/cost_preflight.py` | Cost estimation and gate decisions |
| `ai-service/ai/agent_runtime/execution_journal.py` | Session-level command dedup and memory |

### Modified Files

| File | Change |
|------|--------|
| `frontend/src/utils/runtimeFollowUpContext.ts` | Extract `source_target` from `logData` |
| `frontend/src/pages/AIAnalysis.tsx` | Pass `source_target` in analysis input; cost gate approval UI |
| `ai-service/ai/skills/base.py` | `SkillContext.source_target` field |
| `ai-service/ai/agent_runtime/service.py` | Integrate router, journal, preflight into run lifecycle |
| `ai-service/ai/agent_runtime/models.py` | `AgentRun`: `cost_tracker`, `execution_journal` fields |
| `ai-service/api/ai.py` | Cost gate confirm/deny endpoints (reuse existing approval pattern) |

### Unchanged

- exec-service — existing remote channel interface unchanged
- query-service — existing `/api/v1/logs` already covers needed query surface
- toolbox-gateway — unchanged
- semantic-engine, ingest-service — unchanged
- ClickHouse schema — unchanged

## Implementation Order

1. **Metadata Injection** — Frontend + SkillContext: wire `source_target` through the data flow
2. **QueryServiceClient** — Standalone local channel, prove query-service path works
3. **CommandRouter** — Dual-channel switching with routing rules
4. **ExecutionJournal** — Fingerprint dedup + LLM context injection
5. **CostPreflight** — Cost estimation + approval gate (last, depends on routing info from step 3)

## Testing Plan

- **Unit tests:** CommandRouter routing rules, ExecutionJournal fingerprint collisions,
  CostPreflight threshold evaluations, QueryServiceClient parameter translation.
- **Integration tests:** End-to-end flow from analysis_context with source_target through
  router → execution channel → journal recording.
- **Manual verification:** AI analysis page with a real log entry; verify that:
  - `source_target` appears in the agent context
  - Log queries route to query-service (not kubectl exec)
  - Duplicate commands are skipped with "previously checked" note
  - Expensive commands trigger the approval card
