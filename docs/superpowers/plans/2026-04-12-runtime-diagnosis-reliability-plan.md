# Runtime Diagnosis Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unify runtime diagnosis entrypoints so fault anchors survive into planning and execution, runnable template commands are not mislabeled as `planning_incomplete`, and readonly auto-exec outcomes are explicitly explained.

**Architecture:** Normalize runtime diagnosis context in shared frontend helpers, preserve anchor/window aliases through backend planning/orchestration helpers, and separate planning failure from execution-policy or backend-readiness outcomes in runtime summaries. The implementation keeps existing request-flow and template-command machinery, but tightens contracts and adds targeted tests around run-level outcomes.

**Tech Stack:** TypeScript/React, Python/FastAPI, pytest, Node test runner, ESLint, TypeScript compiler.

---

## File Structure / Ownership

**Frontend**
- Modify: `frontend/src/utils/runtimeAnalysisMode.ts`
  - Owns resolved runtime analysis mode plus normalized analysis context payload.
- Modify: `frontend/src/utils/runtimeFollowUpContext.ts`
  - Owns request/trace/anchor/window merge logic for follow-up runtime.
- Modify: `frontend/src/pages/AIAnalysis.tsx`
  - Main follow-up runtime entry; must stop hand-building partial context.
- Modify: `frontend/src/pages/AIRuntimePlayground.tsx`
  - Lab entry; must align runtime semantics and policy messaging with main runtime.
- Modify: `frontend/src/features/ai-runtime/hooks/useAgentRuntimeCommandFlow.ts`
  - Command runtime sessions must inherit normalized analysis context.
- Modify: `frontend/scripts/aiAgentRuntime.test.mjs`
  - Lightweight contract tests for runtime helper behavior.
- Modify: `frontend/package.json`
  - Ensures new helper files compile in `test:agent-runtime`.

**Backend**
- Modify: `ai-service/ai/followup_planning_helpers.py`
  - Owns evidence window alias resolution and `planning_incomplete` vs `template_ready` state logic.
- Modify: `ai-service/ai/followup_orchestration_helpers.py`
  - Owns readonly template-action generation and execution-mode explanation.
- Modify: `ai-service/api/ai.py`
  - Owns runtime blocked-reason mapping and summary surface for planning vs policy/backend outcomes.
- Test: `ai-service/tests/test_followup_planning_helpers.py`
  - Contract tests for window resolution, plan quality, and ready-template behavior.
- Test: `ai-service/tests/test_followup_exec_streaming.py`
  - Contract tests for readonly auto-exec disabled/backend-unready messaging.
- Test: `ai-service/tests/test_agent_runtime_api.py`
  - API-level summary / blocked-reason outcome tests.

---

### Task 0: Prepare Isolated Worktree

**Files:** none

- [ ] **Step 1: Create or reuse the isolated worktree**

Run:
```bash
git worktree list
```
Expected: either an existing runtime-related worktree is visible or you confirm none exists.

If no suitable worktree exists, run:
```bash
git worktree add /root/logoscope/.worktrees/runtime-diagnosis-reliability -b runtime-diagnosis-reliability
```
Expected: new worktree created on branch `runtime-diagnosis-reliability`.

- [ ] **Step 2: Enter the worktree**

Run:
```bash
cd /root/logoscope/.worktrees/runtime-diagnosis-reliability
```
Expected: shell is now rooted in the isolated workspace.

- [ ] **Step 3: Confirm clean baseline**

Run:
```bash
git status -sb
```
Expected: clean worktree on `runtime-diagnosis-reliability` (or an explicitly reused isolated branch).

---

### Task 1: Frontend Context Contract Normalization

**Files:**
- Modify: `frontend/src/utils/runtimeAnalysisMode.ts`
- Modify: `frontend/src/utils/runtimeFollowUpContext.ts`
- Modify: `frontend/scripts/aiAgentRuntime.test.mjs`
- Modify: `frontend/package.json`

- [ ] **Step 1: Write failing frontend tests for normalized runtime analysis context**

Add to `frontend/scripts/aiAgentRuntime.test.mjs`:
```javascript
test('buildRuntimeAnalysisContext clears dirty trace_id after downgrade', () => {
  const context = buildRuntimeAnalysisContext({
    analysisType: 'trace',
    traceId: '   ',
    serviceName: 'query-service',
    baseContext: { agent_mode: 'followup_analysis_runtime' },
  });

  assert.deepEqual(context, {
    agent_mode: 'followup_analysis_runtime',
    analysis_type: 'log',
    analysis_type_original: 'trace',
    analysis_type_downgraded: true,
    analysis_type_downgrade_reason: 'trace_id_missing',
    service_name: 'query-service',
  });
});

test('buildRuntimeFollowUpContext carries explicit evidence window and anchor aliases', () => {
  const context = buildRuntimeFollowUpContext({
    analysisSessionId: 'sess-001',
    analysisType: 'log',
    serviceName: 'query-service',
    inputText: 'ERROR request failed',
    question: '为什么失败',
    llmInfo: { method: 'llm' },
    result: { overview: { problem: 'clickhouse_query_error' } },
    detectedTraceId: '',
    detectedRequestId: '',
    sourceLogTimestamp: '2026-04-12T13:31:14Z',
    sourceTraceId: '',
    sourceRequestId: '',
    followupRelatedMeta: {
      followup_related_anchor_utc: '2026-04-12T13:31:14Z',
      followup_related_start_time: '2026-04-12T13:26:14Z',
      followup_related_end_time: '2026-04-12T13:36:14Z',
      followup_related_request_id: 'req-123',
    },
  });

  assert.equal(context.request_id, 'req-123');
  assert.equal(context.related_log_anchor_timestamp, '2026-04-12T13:31:14Z');
  assert.equal(context.request_flow_window_start, '2026-04-12T13:26:14Z');
  assert.equal(context.request_flow_window_end, '2026-04-12T13:36:14Z');
});
```

- [ ] **Step 2: Run the helper test suite to verify failure**

Run:
```bash
npm --prefix frontend run test:agent-runtime
```
Expected: FAIL because the helper contract is not fully implemented yet.

- [ ] **Step 3: Implement shared normalization helpers**

Update `frontend/src/utils/runtimeAnalysisMode.ts` so it owns both mode resolution and normalized analysis context:
```typescript
export function buildRuntimeAnalysisContext(params: {
  analysisType: RuntimeAnalysisMode;
  traceId?: string | null;
  serviceName?: string | null;
  baseContext?: Record<string, unknown>;
}): Record<string, unknown> {
  const baseContext = params.baseContext && typeof params.baseContext === 'object'
    ? { ...params.baseContext }
    : {};
  const normalizedTraceId = String(params.traceId || '').trim();
  const normalizedServiceName = String(params.serviceName || '').trim();
  const resolved = resolveRuntimeAnalysisMode({
    analysisType: params.analysisType,
    traceId: normalizedTraceId,
  });

  baseContext.analysis_type = resolved.resolvedType;
  if (resolved.downgraded) {
    baseContext.analysis_type_original = params.analysisType;
    baseContext.analysis_type_downgraded = true;
    baseContext.analysis_type_downgrade_reason = resolved.reason;
    delete baseContext.trace_id;
  } else if (resolved.resolvedType === 'trace' && normalizedTraceId) {
    baseContext.trace_id = normalizedTraceId;
  }

  if (normalizedServiceName) {
    baseContext.service_name = normalizedServiceName;
  }
  return Object.fromEntries(Object.entries(baseContext).filter(([, value]) => value !== undefined));
}
```

Create or update `frontend/src/utils/runtimeFollowUpContext.ts` so it owns alias-compatible merge logic:
```typescript
export function buildRuntimeFollowUpContext(params: {
  analysisSessionId?: string | null;
  analysisType: 'log' | 'trace';
  serviceName?: string | null;
  inputText: string;
  question?: string | null;
  llmInfo?: Record<string, unknown> | null;
  result?: unknown;
  detectedTraceId?: string | null;
  detectedRequestId?: string | null;
  sourceLogTimestamp?: string | null;
  sourceTraceId?: string | null;
  sourceRequestId?: string | null;
  followupRelatedLogs?: unknown[] | null;
  followupRelatedLogCount?: number | null;
  followupRelatedMeta?: Record<string, unknown> | null;
}): Record<string, unknown> {
  // Merge explicit result fields, alias fields, source anchors, and extracted ids.
}
```
Implementation requirements:
- accept `followup_related_anchor_utc`
- accept `followup_related_start_time/end_time`
- accept `evidence_window_start/end`
- emit canonical fields `related_log_anchor_timestamp`, `request_flow_window_start/end`, `request_id`, `trace_id`

- [ ] **Step 4: Include the helper in the lightweight compile list**

Update `frontend/package.json` `test:agent-runtime` script to include:
```json
"src/utils/runtimeFollowUpContext.ts"
```
Expected result: helper is compiled into `.tmp-tests` during the frontend runtime test suite.

- [ ] **Step 5: Re-run the helper test suite**

Run:
```bash
npm --prefix frontend run test:agent-runtime
```
Expected: PASS.

- [ ] **Step 6: Commit helper-layer changes**

Run:
```bash
git add frontend/src/utils/runtimeAnalysisMode.ts frontend/src/utils/runtimeFollowUpContext.ts frontend/scripts/aiAgentRuntime.test.mjs frontend/package.json
git commit -m "fix(ai-runtime): normalize runtime diagnosis context contracts"
```
Expected: one focused commit for shared frontend contracts.

---

### Task 2: Frontend Entrypoint Alignment and Policy Messaging

**Files:**
- Modify: `frontend/src/pages/AIAnalysis.tsx`
- Modify: `frontend/src/pages/AIRuntimePlayground.tsx`
- Modify: `frontend/src/features/ai-runtime/hooks/useAgentRuntimeCommandFlow.ts`
- Test: `frontend/scripts/aiAgentRuntime.test.mjs`

- [ ] **Step 1: Add failing test coverage for policy and inheritance semantics**

Extend `frontend/scripts/aiAgentRuntime.test.mjs` with:
```javascript
test('buildRuntimeAnalysisContext keeps trace_id for trace mode when present', () => {
  const context = buildRuntimeAnalysisContext({
    analysisType: 'trace',
    traceId: 'trace-001',
    serviceName: 'query-service',
  });

  assert.deepEqual(context, {
    analysis_type: 'trace',
    service_name: 'query-service',
    trace_id: 'trace-001',
  });
});

test('buildRuntimeFollowUpContext prefers explicit result window over alias window', () => {
  const context = buildRuntimeFollowUpContext({
    analysisSessionId: 'sess-002',
    analysisType: 'log',
    serviceName: 'query-service',
    inputText: 'ERROR request failed',
    question: '继续排查',
    llmInfo: { method: 'llm' },
    result: {
      request_flow_window_start: '2026-04-12T13:20:00Z',
      request_flow_window_end: '2026-04-12T13:40:00Z',
      request_id: 'req-from-result',
    },
    detectedTraceId: '',
    detectedRequestId: '',
    sourceLogTimestamp: '2026-04-12T13:31:14Z',
    sourceTraceId: '',
    sourceRequestId: '',
    followupRelatedMeta: {
      followup_related_start_time: '2026-04-12T13:26:14Z',
      followup_related_end_time: '2026-04-12T13:36:14Z',
      followup_related_request_id: 'req-from-meta',
    },
  });

  assert.equal(context.request_id, 'req-from-result');
  assert.equal(context.request_flow_window_start, '2026-04-12T13:20:00Z');
  assert.equal(context.request_flow_window_end, '2026-04-12T13:40:00Z');
});
```

- [ ] **Step 2: Run frontend runtime tests to verify failure before page integration**

Run:
```bash
npm --prefix frontend run test:agent-runtime
```
Expected: FAIL if entrypoints still bypass shared helper semantics.

- [ ] **Step 3: Switch `AIAnalysis` follow-up runtime to the shared context builder**

In `frontend/src/pages/AIAnalysis.tsx`, replace the inline `followUpContext` assembly with:
```typescript
const followUpContext = buildRuntimeFollowUpContext({
  analysisSessionId,
  analysisType,
  serviceName,
  inputText,
  question,
  llmInfo: llmInfo || {},
  result,
  detectedTraceId,
  detectedRequestId,
  sourceLogTimestamp: String(sourceLogData?.timestamp || ''),
  sourceTraceId: String(sourceLogData?.trace_id || ''),
  sourceRequestId: extractRequestIdFromRecord(sourceLogData?.attributes || {}) || extractRequestId(inputText),
  followupRelatedLogs: options?.followupRelatedLogs,
  followupRelatedLogCount: options?.followupRelatedLogCount,
  followupRelatedMeta: options?.followupRelatedMeta,
});
```
Implementation note:
- no field should be manually reconstructed elsewhere in the same follow-up path.

- [ ] **Step 4: Align lab entry semantics and policy notice**

In `frontend/src/pages/AIRuntimePlayground.tsx`:
- replace hard gates that require `trace_id` in trace mode and `service_name + trace_id` in log mode
- use `buildRuntimeAnalysisContext(...)` when creating the run
- if downgrade happens, show:
```typescript
buildRuntimeDowngradeNotice(resolved.reason)
```
- if `autoExecReadonly === false`, show a notice like:
```typescript
'当前运行仅生成只读排查命令，不会自动执行；如需自动补证据，请开启“自动执行只读命令”。'
```
- update the explanatory hint text so lab semantics match the main runtime path

- [ ] **Step 5: Make command runtime sessions inherit normalized analysis context**

In `frontend/src/features/ai-runtime/hooks/useAgentRuntimeCommandFlow.ts`, replace manual `analysis_context` assembly with:
```typescript
analysis_context: buildRuntimeAnalysisContext({
  analysisType,
  traceId: normalizedTraceId,
  serviceName,
  baseContext: {
    source_message_id: params.sourceMessageId || undefined,
    source_command: params.command,
    agent_mode: 'followup_command_runtime',
  },
}),
```

- [ ] **Step 6: Run frontend verification suite**

Run:
```bash
npm --prefix frontend run test:agent-runtime
npm --prefix frontend run typecheck
npm --prefix frontend run lint
```
Expected:
- `test:agent-runtime`: PASS
- `typecheck`: PASS
- `lint`: PASS

- [ ] **Step 7: Commit entrypoint alignment changes**

Run:
```bash
git add frontend/src/pages/AIAnalysis.tsx frontend/src/pages/AIRuntimePlayground.tsx frontend/src/features/ai-runtime/hooks/useAgentRuntimeCommandFlow.ts frontend/scripts/aiAgentRuntime.test.mjs
git commit -m "fix(ai-runtime): align runtime entrypoint context and policy messaging"
```
Expected: one focused commit for frontend entrypoint behavior.

---

### Task 3: Backend Planning Contract and Evidence Window Aliases

**Files:**
- Modify: `ai-service/ai/followup_planning_helpers.py`
- Modify: `ai-service/tests/test_followup_planning_helpers.py`

- [ ] **Step 1: Add failing backend tests for alias resolution and template-ready planning semantics**

Add to `ai-service/tests/test_followup_planning_helpers.py`:
```python
def test_resolve_followup_evidence_window_supports_followup_related_aliases():
    window = _resolve_followup_evidence_window(
        {
            "followup_related_anchor_utc": "2026-04-12T13:31:14Z",
            "followup_related_start_time": "2026-04-12T13:26:14Z",
            "followup_related_end_time": "2026-04-12T13:36:14Z",
        }
    )

    assert window == {
        "start_iso": "2026-04-12T13:26:14Z",
        "end_iso": "2026-04-12T13:36:14Z",
    }


def test_build_followup_react_loop_does_not_mark_planning_incomplete_when_ready_templates_exist():
    actions = [
        {
            "id": "lc-1",
            "source": "langchain",
            "title": "查询ClickHouse错误码241的含义",
            "command": "",
            "command_type": "unknown",
            "executable": False,
            "reason": "glued_sql_tokens",
        },
        {
            "id": "tmpl-log-1",
            "source": "template_command",
            "title": "自动补证据命令：kubectl -n islap logs -l app=query-service --since-time=2026-04-12T13:26:14Z --tail=200",
            "command": "kubectl -n islap logs -l app=query-service --since-time=2026-04-12T13:26:14Z --tail=200",
            "command_type": "query",
            "executable": True,
            "reason": "structured_template_ready_for_auto_exec",
            "evidence_window_start": "2026-04-12T13:26:14Z",
            "evidence_window_end": "2026-04-12T13:36:14Z",
            "command_spec": {
                "tool": "generic_exec",
                "args": {
                    "command_argv": [
                        "kubectl", "-n", "islap", "logs", "-l", "app=query-service",
                        "--since-time=2026-04-12T13:26:14Z", "--tail=200",
                    ],
                    "target_kind": "k8s_cluster",
                    "target_identity": "namespace:islap",
                    "timeout_s": 30,
                },
            },
        },
    ]

    loop = _build_followup_react_loop(
        actions=actions,
        action_observations=[],
        analysis_context={
            "namespace": "islap",
            "service_name": "query-service",
            "request_flow_window_start": "2026-04-12T13:26:14Z",
            "request_flow_window_end": "2026-04-12T13:36:14Z",
        },
    )

    assert loop["plan_quality"]["planning_blocked"] is False
    assert int(loop["plan"].get("ready_template_actions") or 0) >= 1
```

- [ ] **Step 2: Run the targeted backend test file to verify failure**

Run:
```bash
cd ai-service && /root/logoscope/.venv/bin/pytest tests/test_followup_planning_helpers.py -q --no-cov
```
Expected: FAIL because alias fields and ready-template planning semantics are not yet fully implemented.

- [ ] **Step 3: Extend evidence-window alias resolution**

In `ai-service/ai/followup_planning_helpers.py`, update `_resolve_followup_evidence_window(...)` to accept alias fields:
```python
explicit_start = _parse_optional_iso_datetime(
    context_payload.get("request_flow_window_start")
    or context_payload.get("followup_related_start_time")
    or context_payload.get("evidence_window_start")
)
explicit_end = _parse_optional_iso_datetime(
    context_payload.get("request_flow_window_end")
    or context_payload.get("followup_related_end_time")
    or context_payload.get("evidence_window_end")
)
anchor_candidates = [
    context_payload.get("source_log_timestamp"),
    context_payload.get("related_log_anchor_timestamp"),
    context_payload.get("followup_related_anchor_utc"),
    context_payload.get("timestamp"),
]
```

- [ ] **Step 4: Separate template-ready from planning-incomplete**

In `ai-service/ai/followup_planning_helpers.py`, update `_build_followup_react_loop(...)` so `planning_blocked` is only true when no ready template actions exist:
```python
ready_template_actions_total = sum(
    1
    for item in safe_actions
    if isinstance(item, dict)
    and bool(item.get("executable"))
    and _as_str(item.get("source")).strip().lower() == "template_command"
) + generated_ready_templates

planning_blocked = (
    plan_total > 0
    and spec_blocked_total > 0
    and spec_blocked_ratio >= 0.5
    and observed_executable_actions <= 0
    and ready_template_actions_total <= 0
)
```
Also add `ready_template_actions` to `loop["plan"]`.

- [ ] **Step 5: Re-run targeted planning tests**

Run:
```bash
cd ai-service && /root/logoscope/.venv/bin/pytest tests/test_followup_planning_helpers.py -q --no-cov
```
Expected: PASS.

- [ ] **Step 6: Commit planning-contract changes**

Run:
```bash
git add ai-service/ai/followup_planning_helpers.py ai-service/tests/test_followup_planning_helpers.py
git commit -m "fix(ai-runtime): separate template-ready runs from planning failures"
```
Expected: one focused commit for planning contract changes.

---

### Task 4: Backend Execution Policy and Outcome Contract

**Files:**
- Modify: `ai-service/ai/followup_orchestration_helpers.py`
- Modify: `ai-service/api/ai.py`
- Modify: `ai-service/tests/test_followup_exec_streaming.py`
- Modify: `ai-service/tests/test_agent_runtime_api.py`

- [ ] **Step 1: Add failing tests for readonly-auto-exec-disabled and blocked-reason mapping**

Add to `ai-service/tests/test_followup_exec_streaming.py`:
```python
def test_followup_react_loop_disabled_auto_exec_emits_policy_notice(monkeypatch):
    monkeypatch.setenv("AI_FOLLOWUP_REACT_MAX_ITERATIONS", "1")
    events = []

    async def _emit(event_name: str, payload: dict):
        events.append((event_name, payload))

    async def _run():
        return await _run_followup_auto_exec_react_loop(
            session_id="sess-policy-001",
            message_id="msg-policy-001",
            actions=[
                {
                    "id": "rf-1",
                    "source": "reflection",
                    "title": "检查 clickhouse 慢查询",
                    "purpose": "补齐 clickhouse query_log 证据",
                    "command": "",
                    "command_type": "unknown",
                    "executable": False,
                    "reason": "missing_structured_spec",
                }
            ],
            analysis_context={"namespace": "islap", "service_name": "query-service"},
            run_blocking=None,
            build_react_loop_fn=lambda **kwargs: {
                "execute": {"observed_actions": len(kwargs.get("action_observations") or []), "executed_success": 0, "executed_failed": 0},
                "observe": {"confidence": 0.0, "unresolved_actions": 1},
                "replan": {"needed": True, "next_actions": ["手动执行模板命令"]},
                "summary": "",
            },
            allow_auto_exec_readonly=False,
            executed_commands=set(),
            initial_evidence_gaps=["clickhouse query_log"],
            initial_summary="",
            emit_iteration_thoughts=True,
            event_callback=_emit,
            logger=None,
        )

    result = asyncio.run(_run())
    assert result["action_observations"] == []
    assert any("当前运行已禁用只读自动执行" in str(payload.get("detail") or "") for event_name, payload in events if event_name == "thought")
```

Add to `ai-service/tests/test_agent_runtime_api.py`:
```python
def test_create_ai_run_followup_mode_marks_policy_block_when_templates_exist(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    async def _fake_run_follow_up_analysis_core(_request, event_callback=None):
        return {
            "analysis_session_id": "sess-policy-001",
            "conversation_id": "conv-policy-001",
            "analysis_method": "langchain",
            "followup_engine": "langchain",
            "answer": "已生成排查命令，但本轮未自动执行。",
            "references": [],
            "actions": [
                {
                    "id": "tmpl-1",
                    "source": "template_command",
                    "title": "自动补证据命令：kubectl -n islap logs -l app=query-service --since-time=2026-04-12T13:26:14Z --tail=200",
                    "command": "kubectl -n islap logs -l app=query-service --since-time=2026-04-12T13:26:14Z --tail=200",
                    "command_type": "query",
                    "executable": True,
                    "reason": "structured_template_ready_for_auto_exec",
                }
            ],
            "action_observations": [],
            "react_loop": {
                "phase": "replan",
                "plan": {"ready_template_actions": 1},
                "replan": {"needed": True, "next_actions": ["本轮只生成命令，不会自动执行"]},
                "plan_quality": {"planning_blocked": False},
                "execute": {"observed_actions": 0},
            },
            "react_iterations": [],
            "subgoals": [],
            "reflection": {},
            "thoughts": [],
            "context_pills": [],
        }

    monkeypatch.setattr("api.ai._run_follow_up_analysis_core", _fake_run_follow_up_analysis_core)
```
Test expectation after implementation:
```python
assert fetched["run"]["summary_json"]["blocked_reason"] == "readonly_auto_exec_disabled"
```

- [ ] **Step 2: Run targeted backend tests to verify failure**

Run:
```bash
cd ai-service && /root/logoscope/.venv/bin/pytest tests/test_followup_exec_streaming.py tests/test_agent_runtime_api.py -q --no-cov
```
Expected: FAIL because runtime summaries still collapse these cases into generic blocked outcomes.

- [ ] **Step 3: Preserve alias logic in orchestration helper**

In `ai-service/ai/followup_orchestration_helpers.py`, update `_resolve_followup_evidence_window(...)` with the same alias-compatible logic introduced in planning helpers. The implementation must mirror:
```python
context_payload.get("followup_related_start_time")
context_payload.get("followup_related_end_time")
context_payload.get("followup_related_anchor_utc")
```
This keeps generated template actions and execution-mode thought output aligned.

- [ ] **Step 4: Tighten readonly auto-exec messaging**

In `ai-service/ai/followup_orchestration_helpers.py`, keep `_describe_template_action_execution_mode(...)` as the canonical policy explanation and ensure disabled mode emits:
```python
"当前运行已禁用只读自动执行，请手动执行或开启自动执行后继续。"
```
Implementation requirement:
- do not translate this case into planning failure
- preserve template-ready state even when execution is skipped

- [ ] **Step 5: Map blocked reasons explicitly in API summary**

In `ai-service/api/ai.py`, when follow-up runtime returns with template-ready actions and `observed_actions == 0`, map outcomes using this priority:
```python
if ready_template_actions > 0 and auto_exec_readonly is False:
    blocked_reason = "readonly_auto_exec_disabled"
elif ready_template_actions > 0 and backend_unready:
    blocked_reason = "backend_unready"
elif ready_template_actions <= 0 and planning_blocked:
    blocked_reason = "planning_incomplete"
elif ready_template_actions > 0 and observed_actions <= 0:
    blocked_reason = "observation_missing"
```
Also write `blocked_reason_detail` into `summary_json` with a concise explanation.

- [ ] **Step 6: Re-run targeted backend tests**

Run:
```bash
cd ai-service && /root/logoscope/.venv/bin/pytest tests/test_followup_exec_streaming.py tests/test_agent_runtime_api.py -q --no-cov
```
Expected: PASS.

- [ ] **Step 7: Commit execution-policy and outcome-contract changes**

Run:
```bash
git add ai-service/ai/followup_orchestration_helpers.py ai-service/api/ai.py ai-service/tests/test_followup_exec_streaming.py ai-service/tests/test_agent_runtime_api.py
git commit -m "fix(ai-runtime): explain policy and backend blockers in runtime summaries"
```
Expected: one focused commit for execution and summary behavior.

---

### Task 5: Full Verification and Replay Acceptance

**Files:**
- Verify: `frontend/src/pages/AIAnalysis.tsx`
- Verify: `frontend/src/pages/AIRuntimePlayground.tsx`
- Verify: `ai-service/ai/followup_planning_helpers.py`
- Verify: `ai-service/ai/followup_orchestration_helpers.py`
- Verify: `ai-service/api/ai.py`

- [ ] **Step 1: Run complete frontend verification**

Run:
```bash
npm --prefix frontend run test:agent-runtime
npm --prefix frontend run typecheck
npm --prefix frontend run lint
```
Expected:
- all commands exit 0
- no TypeScript errors
- no ESLint errors

- [ ] **Step 2: Run backend regression suite with coverage**

Run:
```bash
cd ai-service && /root/logoscope/.venv/bin/pytest tests/test_followup_exec_streaming.py tests/test_followup_planning_helpers.py tests/test_agent_runtime_api.py tests/test_skill_registry.py
```
Expected:
- all tests pass
- total coverage stays above the repository threshold

- [ ] **Step 3: Inspect runtime-facing diff before acceptance**

Run:
```bash
git diff --stat HEAD~4..HEAD
```
Expected: diff is limited to runtime diagnosis context, planning, execution policy, and summary behavior.

- [ ] **Step 4: Replay one known failing run class**

If the environment supports replay tooling, run:
```bash
python3 /root/logoscope/scripts/ai-runtime-manual-entry.py --help
```
Then use the project-standard replay or manual-entry flow for a case equivalent to `run-a170121c4c7b`.

Acceptance criteria for the replay:
- log-mode runtime is not blocked for missing `trace_id`
- generated template commands carry explicit fault time window
- template-ready scenarios do not end as `planning_incomplete`
- if readonly auto-exec is disabled, the run clearly says so

- [ ] **Step 5: Final commit for verification-only follow-up if needed**

If replay or verification required small wording/test-only adjustments, run:
```bash
git add frontend ai-service
git commit -m "test(ai-runtime): finalize runtime diagnosis reliability verification"
```
Expected: only use this commit if Task 5 required a final narrow fix.

