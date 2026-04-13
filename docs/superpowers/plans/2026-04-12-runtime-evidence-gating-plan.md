# Runtime Evidence Gating Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove hard trace/service gates for runtime analysis, auto-downgrade trace mode when `trace_id` is missing, and clearly surface low-evidence behavior without blocking analysis.

**Architecture:** Normalize runtime analysis context at entry (backend) and in UI (frontend), so `trace` without `trace_id` becomes `log` while preserving downgrade metadata. Keep evidence extraction in `request_flow_agent` and mark low-evidence outputs via existing summary/softening logic.

**Tech Stack:** Python (FastAPI), TypeScript/React, pytest, Node test scripts.

---

## File Structure / Ownership

**Backend**
- Modify: `ai-service/ai/agent_runtime/service.py` (normalize analysis_type + downgrade metadata)
- Modify: `ai-service/api/ai.py` (normalize analysis_context before create run; propagate downgrade into summary)
- Modify: `ai-service/tests/test_agent_runtime_api.py` (new downgrade test)

**Frontend**
- Create: `frontend/src/utils/runtimeAnalysisMode.ts` (mode resolution + downgrade notice)
- Modify: `frontend/src/pages/AIAnalysis.tsx` (auto-downgrade trace flow; notice)
- Modify: `frontend/src/features/ai-runtime/hooks/useAgentRuntimeCommandFlow.ts` (pass resolved mode + downgrade metadata)
- Modify: `frontend/package.json` (include new util in `test:agent-runtime` compile list)
- Modify: `frontend/scripts/aiAgentRuntime.test.mjs` (unit tests for resolver)

---

### Task 0: Create an isolated worktree for implementation

**Files:** none

- [ ] **Step 1: Create a new worktree**

```bash
git worktree add /root/logoscope/.worktrees/runtime-evidence-gating -b runtime-evidence-gating
```

- [ ] **Step 2: Enter the worktree**

```bash
cd /root/logoscope/.worktrees/runtime-evidence-gating
```

- [ ] **Step 3: Confirm clean state**

```bash
git status -sb
```
Expected: clean working tree on branch `runtime-evidence-gating`.

---

### Task 1: Backend normalization + downgrade metadata

**Files:**
- Modify: `ai-service/ai/agent_runtime/service.py`
- Modify: `ai-service/api/ai.py`
- Test: `ai-service/tests/test_agent_runtime_api.py`

- [ ] **Step 1: Write failing backend test**

Add to `ai-service/tests/test_agent_runtime_api.py`:

```python

def test_create_ai_run_auto_downgrades_trace_without_trace_id(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    async def _run():
        return await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-trace-downgrade",
                question="排查 trace 模式但缺少 trace_id",
                analysis_context={"analysis_type": "trace"},
                runtime_options={"conversation_id": "conv-trace-downgrade"},
            )
        )

    result = asyncio.run(_run())
    run = result["run"]
    assert run["analysis_type"] == "log"
    assert run["context_json"]["analysis_type_downgraded"] is True
    assert run["context_json"]["analysis_type_original"] == "trace"
    assert run["summary_json"]["analysis_type_downgraded"] is True
    assert run["summary_json"]["analysis_type_downgrade_reason"] == "trace_id_missing"
```

- [ ] **Step 2: Run test to confirm failure**

```bash
pytest ai-service/tests/test_agent_runtime_api.py::test_create_ai_run_auto_downgrades_trace_without_trace_id -v
```
Expected: FAIL because downgrade behavior not implemented.

- [ ] **Step 3: Implement normalization in runtime service**

Update `ai-service/ai/agent_runtime/service.py` inside `create_run`:

```python
        safe_context = analysis_context if isinstance(analysis_context, dict) else {}
        analysis_type = _as_str(safe_context.get("analysis_type"), "log").strip().lower() or "log"
        trace_id = _as_str(safe_context.get("trace_id")).strip()
        if analysis_type == "trace" and not trace_id:
            safe_context = dict(safe_context)
            safe_context["analysis_type"] = "log"
            safe_context["analysis_type_original"] = "trace"
            safe_context["analysis_type_downgraded"] = True
            safe_context["analysis_type_downgrade_reason"] = "trace_id_missing"
            analysis_type = "log"
        else:
            safe_context = dict(safe_context)
            safe_context["analysis_type"] = analysis_type

        ...
        run = AgentRun(
            ...
            analysis_type=analysis_type,
            ...
            context_json=safe_context,
```

- [ ] **Step 4: Propagate downgrade metadata into run summary**

In `ai-service/api/ai.py` inside `_create_ai_run_impl` after `create_run`:

```python
        summary_updates = {}
        ctx = run.context_json if isinstance(getattr(run, "context_json", None), dict) else {}
        if ctx.get("analysis_type_downgraded"):
            summary_updates.update(
                {
                    "analysis_type_downgraded": True,
                    "analysis_type_original": _as_str(ctx.get("analysis_type_original")),
                    "analysis_type_downgrade_reason": _as_str(ctx.get("analysis_type_downgrade_reason")),
                }
            )
        if summary_updates:
            runtime_service._update_run_summary(run, **summary_updates)  # noqa: SLF001
```

- [ ] **Step 5: Run the test again**

```bash
pytest ai-service/tests/test_agent_runtime_api.py::test_create_ai_run_auto_downgrades_trace_without_trace_id -v
```
Expected: PASS.

- [ ] **Step 6: Commit backend changes**

```bash
git add ai-service/ai/agent_runtime/service.py ai-service/api/ai.py ai-service/tests/test_agent_runtime_api.py
git commit -m "fix(ai-runtime): auto-downgrade trace runs without trace_id"
```

---

### Task 2: Frontend auto-downgrade + UI notice

**Files:**
- Create: `frontend/src/utils/runtimeAnalysisMode.ts`
- Modify: `frontend/src/pages/AIAnalysis.tsx`
- Modify: `frontend/src/features/ai-runtime/hooks/useAgentRuntimeCommandFlow.ts`
- Modify: `frontend/package.json`
- Modify: `frontend/scripts/aiAgentRuntime.test.mjs`

- [ ] **Step 1: Add a small resolver utility (test-first)**

Create `frontend/src/utils/runtimeAnalysisMode.ts`:

```typescript
export type RuntimeAnalysisMode = 'log' | 'trace';

export interface AnalysisModeResolution {
  resolvedType: RuntimeAnalysisMode;
  downgraded: boolean;
  reason: '' | 'trace_id_missing';
}

export function resolveRuntimeAnalysisMode(params: {
  analysisType: RuntimeAnalysisMode;
  traceId?: string | null;
}): AnalysisModeResolution {
  const normalizedTraceId = String(params.traceId || '').trim();
  if (params.analysisType === 'trace' && !normalizedTraceId) {
    return {
      resolvedType: 'log',
      downgraded: true,
      reason: 'trace_id_missing',
    };
  }
  return {
    resolvedType: params.analysisType,
    downgraded: false,
    reason: '',
  };
}

export function buildRuntimeDowngradeNotice(reason: AnalysisModeResolution['reason']): string {
  if (reason === 'trace_id_missing') {
    return '未检测到 Trace ID，已自动降级为日志分析（使用时间窗口）。';
  }
  return '';
}
```

- [ ] **Step 2: Add frontend unit tests for resolver**

In `frontend/scripts/aiAgentRuntime.test.mjs` add:

```javascript
import {
  resolveRuntimeAnalysisMode,
  buildRuntimeDowngradeNotice,
} from '../.tmp-tests/utils/runtimeAnalysisMode.js';

// ... later in file

test('resolveRuntimeAnalysisMode downgrades trace without trace_id', () => {
  const resolved = resolveRuntimeAnalysisMode({ analysisType: 'trace', traceId: '' });
  assert.deepEqual(resolved, {
    resolvedType: 'log',
    downgraded: true,
    reason: 'trace_id_missing',
  });
  assert.equal(buildRuntimeDowngradeNotice(resolved.reason), '未检测到 Trace ID，已自动降级为日志分析（使用时间窗口）。');
});

test('resolveRuntimeAnalysisMode keeps trace when trace_id exists', () => {
  const resolved = resolveRuntimeAnalysisMode({ analysisType: 'trace', traceId: 'abc123' });
  assert.deepEqual(resolved, {
    resolvedType: 'trace',
    downgraded: false,
    reason: '',
  });
});
```

- [ ] **Step 3: Update `test:agent-runtime` build list**

In `frontend/package.json`, append the new util to the compile list:

```json
"test:agent-runtime": "rm -rf .tmp-tests && tsc ... src/utils/runtimeAnalysisMode.ts ... && node ..."
```

(Keep the existing list; insert `src/utils/runtimeAnalysisMode.ts` near other utils.)

- [ ] **Step 4: Wire resolver into AIAnalysis**

In `frontend/src/pages/AIAnalysis.tsx` near `handleAnalyze`:

```typescript
import {
  resolveRuntimeAnalysisMode,
  buildRuntimeDowngradeNotice,
} from '../utils/runtimeAnalysisMode';
```

Then adjust the trace/log branch:

```typescript
      const traceId = extractTraceId(inputText);
      const resolved = resolveRuntimeAnalysisMode({ analysisType, traceId });
      if (resolved.downgraded) {
        setAnalysisType('log');
        setAnalysisAssistNotice(buildRuntimeDowngradeNotice(resolved.reason));
      }

      if (resolved.resolvedType === 'log') {
        response = await runLogAnalysis({ ... });
        ...
      } else {
        response = await runTraceAnalysis({ traceId, service: serviceName || undefined, useLLM });
        ...
      }
```

- [ ] **Step 5: Wire resolver into runtime command flow**

In `frontend/src/features/ai-runtime/hooks/useAgentRuntimeCommandFlow.ts`:

```typescript
import { resolveRuntimeAnalysisMode } from '../../../utils/runtimeAnalysisMode';

// inside createSession
const resolved = resolveRuntimeAnalysisMode({ analysisType, traceId });
const analysisContext = {
  analysis_type: resolved.resolvedType,
  analysis_type_original: resolved.downgraded ? analysisType : undefined,
  analysis_type_downgraded: resolved.downgraded || undefined,
  analysis_type_downgrade_reason: resolved.downgraded ? resolved.reason : undefined,
  service_name: serviceName || undefined,
  trace_id: traceId || undefined,
  ...
};
```

- [ ] **Step 6: Run frontend tests**

```bash
npm --prefix frontend run test:agent-runtime
```
Expected: PASS.

- [ ] **Step 7: Run lint + typecheck (required before commit)**

```bash
npm --prefix frontend run lint
npm --prefix frontend run typecheck
```
Expected: PASS.

- [ ] **Step 8: Commit frontend changes**

```bash
git add frontend/src/utils/runtimeAnalysisMode.ts frontend/src/pages/AIAnalysis.tsx frontend/src/features/ai-runtime/hooks/useAgentRuntimeCommandFlow.ts frontend/package.json frontend/scripts/aiAgentRuntime.test.mjs
git commit -m "fix(frontend): auto-downgrade trace mode without trace id"
```

---

## Plan Self-Review

**Spec coverage:**
- Auto-downgrade trace→log when `trace_id` missing: Task 1 + Task 2.
- Allow log-only + time window: handled by removing UI hard stop and backend normalization; request_flow_agent already uses time window.
- Extract trace/request from logs: unchanged, preserved via `request_flow_agent`.
- Evidence warning / softened language: existing backend softening retained; downgrade notice surfaced in UI.

**Placeholder scan:** No TODO/TBD placeholders. All code steps include concrete snippets.

**Type consistency:** `analysis_type_downgrade_reason` uses literal `"trace_id_missing"` in backend + frontend resolver.

---

Plan complete and saved to `docs/superpowers/plans/2026-04-12-runtime-evidence-gating-plan.md`. Two execution options:

1. Subagent-Driven (recommended) - I dispatch a fresh subagent per task, review between tasks, fast iteration
2. Inline Execution - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
