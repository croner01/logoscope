# AI Agent Runtime Implementation Plan v1

## Scope

This document converts the refactor draft into an execution-oriented plan.

It answers four questions:

1. what should be implemented first
2. which modules should be kept, wrapped, or replaced
3. what the first usable product version should look like
4. whether the AI analysis page layout should be optimized during this work


## Executive Summary

### Recommended implementation direction

Build v1 around a durable `run + event` model and make the frontend consume one canonical event stream.

The main delivery should be:

- one stable backend run model
- one stable event protocol
- one true streaming command execution path
- one frontend run timeline UI

### Implementation update after runtime UX review

After the latest runtime UX review, v1 should no longer optimize the current page as a multi-panel debug console.

The recommended product shape is now:

- bottom-fixed multi-turn composer
- one primary assistant transcript container per run
- collapsible thinking/command/approval detail blocks
- low-value status output removed from the main view
- debug payloads moved behind a secondary debug drawer

This update also changes the execution direction:

- commands should no longer be treated as "always executed inside `exec-service`"
- `exec-service` should evolve into a policy + dispatch + audit gateway
- actual command execution should be dispatched to auditable execution targets
- default execution target should be isolated sandbox/toolbox pods
- high-risk cluster actions should use a higher-privilege execution profile after approval
- host-level/system commands should go through a controlled SSH bastion executor
- external control plane commands such as `openstack` should use dedicated executors

Reference docs:

- `docs/design/ai-agent-runtime-optimization-v2.zh-CN.md`
- `docs/design/ai-agent-runtime-execution-audit-draft.zh-CN.md`

### What v1 is not

v1 is not a polish pass on the current follow-up box.

It is also not a fully autonomous multi-agent platform.

### Product outcome for v1

After v1, the user should be able to:

- ask the AI to investigate a problem
- watch the system perform multiple investigation steps in one run
- see tool calls and command output live
- approve risky actions in place
- reconnect or refresh without losing the run state


## Current Module Mapping

### Keep and reuse

- `ai-service/ai/request_flow_agent.py`
  Keep as a context-enrichment and bounded data-access helper.

- current AI session history storage
  Keep the existing session concept and adapt it to host runs.

- current command policy and confirmation ticket logic
  Keep the safety policy model and ticket semantics.

- current LLM provider abstraction
  Keep `llm_service` as the provider layer.

### Wrap temporarily

- `ai-service/api/ai.py`
  Keep existing endpoints during migration, but stop adding new behavior directly inside the monolithic follow-up core.

- `ai-service/ai/langchain_runtime/*`
  Keep only as prompt/build/parse helpers while runtime ownership moves elsewhere.

### Replace or phase out

- placeholder-based frontend stream merge logic
- legacy `follow-up` to `v2` translation layering
- one-shot command execution in `exec-service`
- the current oversized AI analysis follow-up render path


## v1 Deliverables

### Backend deliverables

1. canonical run model
2. canonical event model
3. event persistence
4. streaming run transport
5. true streaming exec path
6. approval pause/resume flow

### Frontend deliverables

1. run-based conversation rendering
2. event timeline panel
3. tool call cards
4. live command output view
5. approval action panel
6. reconnect/recovery support

### Compatibility deliverables

1. existing analysis session can still be opened
2. old history remains readable
3. old non-stream analysis still works during transition


## Phase Plan

## Phase 0. Protocol and Boundary Freeze

### Goal

Stop expanding the current mixed protocol and freeze a canonical runtime boundary.

### Tasks

- define canonical event schema
- define run status transitions
- define tool invocation schema
- define command run schema
- define approval event schema
- define final answer event schema

### Files likely involved

- new: `ai-service/ai/agent_runtime/event_protocol.py`
- new: `ai-service/ai/agent_runtime/models.py`
- new: `ai-service/ai/agent_runtime/status.py`
- docs: this implementation plan and the refactor draft

### Exit criteria

- backend and frontend teams agree on one event contract
- no new UI logic is added to the old placeholder stream path


## Phase 1. Backend Run/Event Foundation

### Goal

Create a durable backend run model before changing the product experience.

### Tasks

- add `AgentRun` persistence
- add `RunEvent` persistence
- create `assistant_message_id` at run start
- emit `run_started` and `message_started` before any model work
- store event `seq` for replay and reconnect
- expose run snapshot and run event listing APIs

### Suggested modules

- new: `ai-service/ai/agent_runtime/store.py`
- new: `ai-service/ai/agent_runtime/service.py`
- new: `ai-service/ai/agent_runtime/emitter.py`
- new: `ai-service/ai/agent_runtime/session_adapter.py`
- update: `ai-service/api/ai.py`

### Suggested APIs

- `POST /api/v1/ai/runs`
- `GET /api/v1/ai/runs/{run_id}`
- `GET /api/v1/ai/runs/{run_id}/events`
- `POST /api/v1/ai/runs/{run_id}/cancel`

### v1 behavior at end of phase 1

- every follow-up request creates a durable run
- assistant message identity exists before output begins
- frontend can recover run state from backend, not local component memory

### Exit criteria

- run snapshot can be fetched independently from active stream
- events can be replayed after page refresh


## Phase 2. Exec-Service Streaming Refactor

### Goal

Make command execution truly stream-native.

### Tasks

- replace blocking `subprocess.run` execution path
- launch commands asynchronously
- stream stdout/stderr output deltas while command is alive
- persist command run metadata
- support command cancel
- preserve audit trail

### Suggested modules

- update: `exec-service/core/runner.py`
- update: `exec-service/api/execute.py`
- new: `exec-service/core/run_store.py`
- new: `exec-service/core/event_store.py`

### Suggested APIs

- `POST /api/v1/exec/runs`
- `GET /api/v1/exec/runs/{command_run_id}`
- `GET /api/v1/exec/runs/{command_run_id}/events`
- `POST /api/v1/exec/runs/{command_run_id}/cancel`

### v1 behavior at end of phase 2

- command output appears continuously, not only after completion
- AI run can subscribe to command events and re-emit summarized observations

### Exit criteria

- at least one readonly command can stream visible output line-by-line or chunk-by-chunk
- canceling an active command updates the run state correctly


## Phase 3. Native Agent Loop

### Goal

Replace one-shot follow-up generation with iterative planning and acting.

### Minimal v1 loop

1. initialize run
2. build bounded context
3. ask model for next step
4. execute one tool call
5. summarize observation
6. ask model whether to continue
7. repeat until stop
8. produce final answer

### Constraints for v1

- readonly tools only for auto-execution
- write actions always require approval
- bounded iteration count
- bounded token budget
- bounded command count

### Suggested modules

- new: `ai-service/ai/agent_runtime/orchestrator.py`
- new: `ai-service/ai/agent_runtime/loop.py`
- new: `ai-service/ai/agent_runtime/memory.py`
- new: `ai-service/ai/agent_runtime/tools/*.py`
- update: `ai-service/ai/request_flow_agent.py`

### Tool set for v1

- `logs.query`
- `traces.query`
- `topology.query`
- `kb.search`
- `command.precheck`
- `command.execute`
- `command.stream`

### What not to include in v1

- arbitrary web search by default
- self-modifying workflows
- hidden autonomous write actions

### Exit criteria

- one run can perform multiple act/observe rounds
- the final answer reflects tool observations from the same run


## Phase 4. Frontend Runtime Shell Rewrite

### Goal

Move the AI analysis page from local-state merge logic to backend-run-driven rendering.

### Tasks

- build run event store in frontend
- subscribe to backend run stream
- normalize events into message timeline and tool timeline
- remove local placeholder assistant message creation
- support event replay on reconnect
- add explicit run status display

### Suggested frontend modules

- new: `frontend/src/features/ai-runtime/hooks/useAgentRun.ts`
- new: `frontend/src/features/ai-runtime/hooks/useRunEvents.ts`
- new: `frontend/src/features/ai-runtime/components/RunTimeline.tsx`
- new: `frontend/src/features/ai-runtime/components/ToolCallCard.tsx`
- new: `frontend/src/features/ai-runtime/components/CommandOutputPanel.tsx`
- new: `frontend/src/features/ai-runtime/components/ApprovalPanel.tsx`
- update: `frontend/src/pages/AIAnalysis.tsx`
- update: `frontend/src/utils/api.ts`

### Exit criteria

- no local `local-stream-*` assistant placeholder exists in the new path
- reconnect can rebuild visible state from backend events


## Phase 5. Deprecation and Cleanup

### Goal

Reduce maintenance burden by removing redundant protocols and UI merge paths.

### Tasks

- deprecate old `follow-up/stream` placeholder-dependent rendering
- deprecate `agent_v2` compatibility mapping if canonical protocol replaces it
- delete dead metadata merge logic
- simplify tests around one event protocol

### Exit criteria

- one protocol
- one frontend stream path
- one backend run model


## Frontend Layout Decision

## Short answer

Yes, the page layout should be optimized, but not as a standalone visual pass first.

The current layout is overloaded and should be restructured during phase 4, after the run/event model exists.

## Why layout optimization is needed

The current AI analysis page mixes too many responsibilities inside one large right-side result card:

- analysis summary
- KB submission
- KB search
- similar cases
- quick actions
- follow-up conversation
- action drafts
- approval interactions

This is visible in the current page structure around [AIAnalysis.tsx](/root/logoscope/frontend/src/pages/AIAnalysis.tsx#L4756) and especially the dense conversation and action area starting near [AIAnalysis.tsx](/root/logoscope/frontend/src/pages/AIAnalysis.tsx#L5474).

The result is:

- the page feels like one long stacked control surface
- the conversation area competes with summary cards and KB operations
- streamed runtime state is visually buried
- approvals and command outputs are hard to scan

## What should change in v1

The layout should move from a "left input / right everything else" structure to a "workspace" structure.

### Recommended v1 layout

- Left rail or top section: investigation input and context
- Main center pane: conversation and live run timeline
- Right rail: evidence, references, quick actions, KB, similar cases

### Proposed information hierarchy

1. investigation header
   run status, service, session, trace context

2. conversation pane
   user message, assistant message, reasoning summaries, final answer

3. live execution pane
   tool calls, command output, approvals, run status

4. evidence rail
   references, related logs, similar cases, KB search results

5. post-run actions
   ticket, runbook, alert suppression, KB submit

### Why this is better

- the live runtime becomes the center of attention
- evidence and post-processing stop interrupting the conversation
- command output can expand without collapsing the reading flow
- approvals become contextual to the run instead of floating inside a message blob


## Layout Optimization Priority

### Must-do in v1

- separate conversation area from KB and auxiliary actions
- introduce explicit run timeline region
- make approval and command output first-class UI blocks
- reduce stacked utility sections inside the conversation area

### Can wait until v1.1

- advanced visual polish
- animation refinement
- typography redesign
- custom density modes
- drag-resizable panes

### Recommendation

Do not spend time polishing the current layout before the runtime rewrite.

If layout work happens before the protocol/runtime shift, most of that work will be discarded.


## Testing Strategy

## Backend

- unit tests for event emitter ordering
- unit tests for run status transitions
- unit tests for command lifecycle events
- unit tests for approval pause/resume
- integration tests for run replay after reconnect

## Frontend

- event store reducer tests
- reconnect hydration tests
- tool output rendering tests
- approval flow tests
- run cancel and failure state tests

## End-to-end

- start run -> readonly command executes -> output streams -> final answer appears
- start run -> write command proposed -> approval required -> approve -> command streams -> final answer updates
- refresh during active run -> page resumes same run state


## Implementation Order Recommendation

1. protocol and backend run model
2. streaming exec refactor
3. native agent loop
4. frontend runtime shell and layout restructuring
5. cleanup

This order is important.

If the team starts with frontend layout or component refactoring first, the core execution model problems will remain and the UI will keep absorbing backend complexity.


## Acceptance Criteria for v1

v1 is accepted when all of the following are true:

- one user prompt creates a durable backend run
- the frontend can reconnect to a running investigation
- at least one readonly command streams output continuously
- approvals can pause and resume a run
- the assistant can perform more than one act/observe round in the same run
- the AI analysis page has a dedicated runtime area instead of burying execution state inside the result card
- the old placeholder-merge stream path is no longer the primary product path


## Open Decisions

These decisions are still needed before implementation starts.

1. SSE or WebSocket for canonical transport
2. whether run events live in ClickHouse, Redis, or mixed storage
3. whether command output persistence should be lossless or clipped with retention
4. whether LangGraph should be evaluated after v1 or deferred entirely
5. whether KB actions remain on the AI analysis page or move to a post-run drawer


## Suggested Next Step

After approving this v1 plan, the next document should be a task breakdown by module, with:

- file-level changes
- API contract drafts
- storage migration notes
- test list
- rollout and fallback strategy
