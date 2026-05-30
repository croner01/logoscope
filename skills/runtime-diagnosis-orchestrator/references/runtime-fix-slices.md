# Runtime Fix Slices (Duplicate Execution + Stream Loss)

This plan is designed for small, mergeable PRs. Apply in order.

## P0: Prevent Premature Stream Cutoff and Lost Terminal Events

Goal:
- Ensure AI bridge can always observe command terminal state.

Scope:
- `exec-service/api/execute.py`
- `ai-service/ai/agent_runtime/command_bridge.py`
- `ai-service/ai/agent_runtime/service.py`

Changes:
1. In exec stream endpoint, do not break immediately after first terminal-status observation if queue may still contain later events.
2. In AI bridge, when stream ends without terminal event:
   - fallback to `list_command_run_events(after_seq=last_seq)`,
   - fallback to `get_command_run(run_id)` snapshot check.
3. Only clear `active_command_run_id` after terminal evidence is confirmed.

Acceptance:
- For a completed exec run, AI run must include either:
  - `tool_call_finished` with matching `command_run_id`, or
  - explicit terminal snapshot reconciliation event.

## P1: Exactly-Once Command Submission Within Run Scope

Goal:
- Same action+command should not create multiple `command_run_id`.

Scope:
- `ai-service/ai/agent_runtime/service.py`
- runtime store/persistence layer used by summary state.

Changes:
1. Introduce stable idempotency key:
   - `run_id + action_id + normalized_command + purpose + target_identity`.
2. Maintain `command_key -> command_run_id` index in run summary/state.
3. On execute request:
   - if active mapping exists, return `running_existing`.
   - if terminal success mapping exists within evidence-valid window, reuse/skip.
4. Store attempted/executed fingerprints immediately when command run is created, not only after stream bridge terminal.

Acceptance:
- Repeated trigger of same action in one run yields one created exec run.
- Follow-up retries should return existing run id, not create a new run.

## P2: Frontend Visibility and Operational Diagnostics

Goal:
- Make duplicates and stream gaps immediately visible during debugging.

Scope:
- `frontend/src/features/ai-runtime/components/CommandOutputPanel.tsx`
- `frontend/src/features/ai-runtime/utils/runtimeView.ts`
- optional debug panel in runtime page.

Changes:
1. Show all command runs in debug mode (not only last 3).
2. Add indicators:
   - missing terminal event,
   - output chunk count,
   - `command_run_id` collisions by action.
3. Expose “replay-check” command hint in UI for fast operator workflow.

Acceptance:
- Operators can identify duplicate run ids and partial-output cases without backend log access.

## Regression Test Matrix

1. Normal path:
- single command run, complete stream, terminal event present.

2. Stream interruption path:
- artificial SSE interruption; verify fallback replay fills missing output and terminal state.

3. Duplicate trigger path:
- invoke same action concurrently; verify one run created and others return existing.

4. Approval path:
- approval-required -> approved -> command executes once.

5. Timeout/blocked path:
- approval timeout does not backfill fake command terminal for unrelated `command_run_id`.
