# AI Agent Runtime Task Breakdown v1

## Purpose

This document breaks the implementation plan into concrete work items at module and file level.

It is intended to support:

- engineering sequencing
- ownership split
- rollout planning
- test planning


## Working Rules

### Rule 1

Do not continue adding major behavior into `frontend/src/pages/AIAnalysis.tsx` or the monolithic follow-up core in `ai-service/api/ai.py` unless it is migration glue.

### Rule 2

New runtime logic should land in new modules first, then old entrypoints should delegate into them.

### Rule 3

The canonical source of truth for a running investigation is:

- backend run snapshot
- backend run event log

not local frontend placeholder state.


## Suggested Delivery Tracks

Split the work into four parallel tracks after protocol freeze.

### Track A. Backend run/event foundation

Owner scope:

- `ai-service`
- storage interfaces
- run/event APIs

### Track B. Exec streaming foundation

Owner scope:

- `exec-service`
- command lifecycle
- output streaming

### Track C. Frontend runtime shell

Owner scope:

- `frontend`
- event store
- conversation/timeline rendering

### Track D. Migration, compatibility, and testing

Owner scope:

- integration tests
- old/new path coexistence
- rollout controls


## Track A. Backend Run/Event Foundation

## A1. Create runtime package

### Goal

Introduce a new runtime package so new logic does not keep expanding legacy follow-up helpers.

### New files

- `ai-service/ai/agent_runtime/__init__.py`
- `ai-service/ai/agent_runtime/models.py`
- `ai-service/ai/agent_runtime/event_protocol.py`
- `ai-service/ai/agent_runtime/status.py`
- `ai-service/ai/agent_runtime/emitter.py`
- `ai-service/ai/agent_runtime/store.py`
- `ai-service/ai/agent_runtime/service.py`

### Tasks

- define `AgentRun`
- define `RunEvent`
- define `ToolCall`
- define `CommandRunRef`
- define event type constants
- define status transition rules
- define event append interface

### Notes

Do not migrate tool execution into this package yet. The first purpose is run/event structure.


## A2. Add run persistence adapter

### Goal

Persist runs and events without breaking existing AI history.

### Candidate existing modules to integrate with

- `ai-service/ai/session_history.py`
- `ai-service/ai/followup_persistence_helpers.py`

### Tasks

- add run-level storage adapter
- add event append/list methods
- add run snapshot read method
- decide whether to extend existing ClickHouse-backed session tables or add new run/event tables

### Suggested storage interfaces

- `create_run(...)`
- `update_run_status(...)`
- `append_run_event(...)`
- `list_run_events(run_id, after_seq=None, limit=...)`
- `get_run(run_id)`

### Decision checkpoint

Before implementation, confirm whether run/event storage should be:

- new ClickHouse tables
- Redis hot cache plus ClickHouse durable sink
- hybrid with Redis for active runs and ClickHouse for history


## A3. Add canonical APIs for runs

### Goal

Expose run lifecycle independent of old follow-up endpoints.

### Files to update

- `ai-service/api/ai.py`

### New endpoints

- `POST /api/v1/ai/runs`
- `GET /api/v1/ai/runs/{run_id}`
- `GET /api/v1/ai/runs/{run_id}/events`
- `POST /api/v1/ai/runs/{run_id}/cancel`
- `POST /api/v1/ai/runs/{run_id}/approve`

### Tasks

- accept a user prompt and create `run_id`
- create assistant message shell immediately
- return run snapshot quickly
- allow event replay without active stream
- allow approval decisions to resume waiting runs


## A4. Introduce canonical stream endpoint

### Goal

Stream run events from one canonical protocol.

### Files to update

- `ai-service/api/ai.py`
- new runtime emitter modules

### New endpoint

- `GET /api/v1/ai/runs/{run_id}/stream`

### Tasks

- support SSE first
- stream canonical event types only
- emit sequence numbers
- allow client reconnect with `after_seq`

### Notes

Do not keep adding mappings from legacy event names if avoidable. The canonical protocol should be the target path.


## A5. Runtime orchestrator skeleton

### Goal

Create a minimal orchestrator that can run under the new run/event model before full iterative agent logic lands.

### New files

- `ai-service/ai/agent_runtime/orchestrator.py`
- `ai-service/ai/agent_runtime/context_builder.py`
- `ai-service/ai/agent_runtime/finalizer.py`

### Tasks

- initialize run
- load bounded context
- emit planning step
- call old answer path under the new run envelope
- emit final answer event

### Why this matters

This gives early end-to-end value before the true act/observe loop replaces legacy logic.


## A6. Native iterative loop

### Goal

Replace one-shot answer-first behavior with a loop.

### New files

- `ai-service/ai/agent_runtime/loop.py`
- `ai-service/ai/agent_runtime/memory.py`
- `ai-service/ai/agent_runtime/decision.py`

### Tasks

- implement iteration cap
- implement stop criteria
- ask the model for next step, not full answer only
- store structured internal scratch state
- summarize each observation back into runtime memory

### Dependencies

- requires Track B command streaming foundation


## A7. Tool adapter layer

### Goal

Stop mixing tool logic into prompt helper modules.

### New files

- `ai-service/ai/agent_runtime/tools/__init__.py`
- `ai-service/ai/agent_runtime/tools/logs_query.py`
- `ai-service/ai/agent_runtime/tools/traces_query.py`
- `ai-service/ai/agent_runtime/tools/topology_query.py`
- `ai-service/ai/agent_runtime/tools/kb_search.py`
- `ai-service/ai/agent_runtime/tools/command_precheck.py`
- `ai-service/ai/agent_runtime/tools/command_execute.py`
- `ai-service/ai/agent_runtime/tools/command_stream.py`

### Existing code to reuse

- `ai-service/ai/request_flow_agent.py`
- `ai-service/ai/followup_command.py`
- `ai-service/ai/followup_confirmation_ticket_helpers.py`
- `ai-service/ai/kb_route_helpers.py`

### Tasks

- define input/output schema per tool
- move readonly observation helpers out of `langchain_runtime/tools.py`
- add tool status reporting
- attach each tool call to one `tool_call_id`


## A8. LangChain integration downgrade

### Goal

Keep LangChain as a helper, not the runtime owner.

### Files to update

- `ai-service/ai/langchain_runtime/service.py`
- `ai-service/ai/langchain_runtime/tools.py`

### Tasks

- reduce ownership to prompt build and structured parse
- stop using this package as the central execution coordinator
- optionally move tool observation collection into runtime adapters

### Outcome

- `langchain_runtime` remains a support package
- runtime orchestration lives in `agent_runtime`


## Track B. Exec Streaming Foundation

## B1. Add command run state store

### Goal

Represent a command run as a first-class persistent object.

### New files

- `exec-service/core/run_store.py`
- `exec-service/core/event_store.py`

### Tasks

- create command run records
- append output events
- load active or finished run state
- support retention policy


## B2. Replace blocking runner with async streaming runner

### Goal

Convert command execution to true streaming.

### Files to update

- `exec-service/core/runner.py`

### Tasks

- replace `subprocess.run` path
- use async subprocess execution
- read stdout/stderr as the process runs
- emit output delta events
- support timeout and cancellation

### Notes

Need a decision on:

- plain stdout/stderr pipes
- PTY mode for interactive-like formatting

For v1, plain stdout/stderr pipes are enough unless specific commands require TTY behavior.


## B3. Add command lifecycle APIs

### Files to update

- `exec-service/api/execute.py`

### New endpoints

- `POST /api/v1/exec/runs`
- `GET /api/v1/exec/runs/{command_run_id}`
- `GET /api/v1/exec/runs/{command_run_id}/events`
- `POST /api/v1/exec/runs/{command_run_id}/cancel`

### Tasks

- launch command run
- return command run snapshot
- stream command events
- cancel active command
- preserve audit append behavior


## B4. Integrate approval and policy into command run lifecycle

### Existing code to reuse

- `exec-service/core/policy.py`
- `exec-service/core/ticket_store.py`
- `exec-service/core/audit_store.py`

### Tasks

- preserve current precheck semantics
- require confirmation ticket for elevated runs
- emit command status events for blocked, waiting, executed, failed, cancelled


## B5. Connect AI runtime to exec events

### Goal

AI runtime must subscribe to command output rather than waiting for one final blob.

### Files to update

- new `ai-service/ai/agent_runtime/tools/command_execute.py`
- new `ai-service/ai/agent_runtime/tools/command_stream.py`

### Tasks

- start command run through exec API or local adapter
- subscribe to command event stream
- translate command events into canonical run events
- summarize command output for model continuation


## Track C. Frontend Runtime Shell

## C1. Create AI runtime feature area

### Goal

Stop growing `AIAnalysis.tsx` as the single runtime container.

### New directories

- `frontend/src/features/ai-runtime/hooks/`
- `frontend/src/features/ai-runtime/components/`
- `frontend/src/features/ai-runtime/state/`
- `frontend/src/features/ai-runtime/types/`

### New files

- `frontend/src/features/ai-runtime/types/events.ts`
- `frontend/src/features/ai-runtime/types/run.ts`
- `frontend/src/features/ai-runtime/state/runEventReducer.ts`
- `frontend/src/features/ai-runtime/hooks/useAgentRun.ts`
- `frontend/src/features/ai-runtime/hooks/useRunEvents.ts`
- `frontend/src/features/ai-runtime/components/RunTimeline.tsx`
- `frontend/src/features/ai-runtime/components/ConversationPane.tsx`
- `frontend/src/features/ai-runtime/components/ToolCallCard.tsx`
- `frontend/src/features/ai-runtime/components/CommandOutputPanel.tsx`
- `frontend/src/features/ai-runtime/components/ApprovalPanel.tsx`
- `frontend/src/features/ai-runtime/components/RunHeader.tsx`
- `frontend/src/features/ai-runtime/components/EvidenceRail.tsx`


## C2. Extend frontend API client for run model

### Files to update

- `frontend/src/utils/api.ts`

### Tasks

- add `createAiRun`
- add `getAiRun`
- add `listAiRunEvents`
- add `streamAiRun`
- add `approveAiRunAction`
- add `cancelAiRun`
- add `streamExecRunEvents`

### Important rule

Do not build new API client behavior around the old placeholder merge assumptions.


## C3. Replace local placeholder streaming path

### Files to update

- `frontend/src/pages/AIAnalysis.tsx`

### Tasks

- remove `local-stream-*` placeholder path from the primary flow
- use backend-issued `assistant_message_id`
- hydrate runtime state from run snapshot + event log
- render deltas from reducer state

### Migration note

The old path can temporarily remain behind a feature flag until the new run path stabilizes.


## C4. Layout restructuring

### Goal

Make runtime state central to the page.

### Current problem area

The current page stacks summary, KB, similar cases, actions, and conversation into one overloaded result column.

### New layout tasks

- keep top page header
- keep initial analysis input section
- split result area into:
  - center main pane for conversation and live run timeline
  - side evidence rail for similar cases, KB, references, and post-run actions
- keep approval UI contextual to run items
- make command output expandable and readable

### Files to update

- `frontend/src/pages/AIAnalysis.tsx`
- new runtime components

### Recommendation

Do not redesign the whole visual language in v1.

Focus on information hierarchy and operational clarity.


## C5. State reducer and replay behavior

### Goal

The page should recover active runs after refresh or reconnect.

### Tasks

- build reducer from canonical events
- support replay from event list
- support streaming append after replay
- derive view state:
  - assistant messages
  - tool calls
  - command output
  - approvals
  - run status


## C6. Approval UX flow

### Goal

Approvals must pause and resume a real run rather than acting on message-local metadata only.

### Tasks

- show pending approvals in run timeline
- show approval details in contextual drawer or panel
- submit approval decision to backend run API
- reflect resumed run output in the same timeline


## Track D. Migration and Compatibility

## D1. Feature flags

### Goal

Roll out safely without breaking the current page for all users at once.

### Suggested flags

- `AI_AGENT_RUNTIME_V1_ENABLED`
- `AI_AGENT_RUNTIME_STREAM_ENABLED`
- `AI_AGENT_RUNTIME_EXEC_STREAM_ENABLED`
- `AI_AGENT_RUNTIME_UI_ENABLED`

### Files likely involved

- `deploy/ai-service.yaml`
- frontend env configuration path


## D2. Entry-point compatibility

### Goal

Allow current flows to keep working while new runtime path is introduced.

### Tasks

- keep existing analysis endpoints
- keep old history read APIs
- optionally wrap old follow-up request into new run creation under the hood


## D3. History migration strategy

### Goal

Preserve existing AI history usability.

### Tasks

- define whether old sessions gain `latest_run_id`
- define whether old assistant messages remain read-only transcript items
- decide if run replay is available for old sessions or only new ones

### Recommendation

Old sessions should remain viewable.

Replayable run events can be limited to new runtime runs.


## D4. Rollout plan

### Stage 1

Backend run/event APIs land dark.

### Stage 2

Exec streaming lands dark.

### Stage 3

Frontend new runtime shell behind feature flag for internal users.

### Stage 4

New runtime becomes default for AI analysis follow-up.

### Stage 5

Legacy placeholder stream path deprecated and removed.


## File-Level Change List

## Backend likely new files

- `ai-service/ai/agent_runtime/__init__.py`
- `ai-service/ai/agent_runtime/models.py`
- `ai-service/ai/agent_runtime/event_protocol.py`
- `ai-service/ai/agent_runtime/status.py`
- `ai-service/ai/agent_runtime/emitter.py`
- `ai-service/ai/agent_runtime/store.py`
- `ai-service/ai/agent_runtime/service.py`
- `ai-service/ai/agent_runtime/orchestrator.py`
- `ai-service/ai/agent_runtime/loop.py`
- `ai-service/ai/agent_runtime/context_builder.py`
- `ai-service/ai/agent_runtime/memory.py`
- `ai-service/ai/agent_runtime/finalizer.py`
- `ai-service/ai/agent_runtime/tools/*.py`

## Backend likely modified files

- `ai-service/api/ai.py`
- `ai-service/ai/request_flow_agent.py`
- `ai-service/ai/session_history.py`
- `ai-service/ai/followup_command.py`
- `ai-service/ai/followup_confirmation_ticket_helpers.py`
- `ai-service/ai/langchain_runtime/service.py`
- `ai-service/ai/langchain_runtime/tools.py`

## Exec likely new files

- `exec-service/core/run_store.py`
- `exec-service/core/event_store.py`

## Exec likely modified files

- `exec-service/core/runner.py`
- `exec-service/api/execute.py`
- `exec-service/main.py`

## Frontend likely new files

- `frontend/src/features/ai-runtime/types/events.ts`
- `frontend/src/features/ai-runtime/types/run.ts`
- `frontend/src/features/ai-runtime/state/runEventReducer.ts`
- `frontend/src/features/ai-runtime/hooks/useAgentRun.ts`
- `frontend/src/features/ai-runtime/hooks/useRunEvents.ts`
- `frontend/src/features/ai-runtime/components/RunHeader.tsx`
- `frontend/src/features/ai-runtime/components/ConversationPane.tsx`
- `frontend/src/features/ai-runtime/components/RunTimeline.tsx`
- `frontend/src/features/ai-runtime/components/ToolCallCard.tsx`
- `frontend/src/features/ai-runtime/components/CommandOutputPanel.tsx`
- `frontend/src/features/ai-runtime/components/ApprovalPanel.tsx`
- `frontend/src/features/ai-runtime/components/EvidenceRail.tsx`

## Frontend likely modified files

- `frontend/src/pages/AIAnalysis.tsx`
- `frontend/src/utils/api.ts`
- `frontend/src/hooks/useNavigation.ts`


## Test Breakdown

## Backend tests

### New tests to add

- `ai-service/tests/test_agent_runtime_models.py`
- `ai-service/tests/test_agent_runtime_status.py`
- `ai-service/tests/test_agent_runtime_emitter.py`
- `ai-service/tests/test_agent_runtime_store.py`
- `ai-service/tests/test_agent_runtime_api.py`
- `ai-service/tests/test_agent_runtime_loop.py`
- `ai-service/tests/test_agent_runtime_approvals.py`

### Existing tests to update

- `ai-service/tests/test_ai_api.py`
- `ai-service/tests/test_request_flow_agent.py`
- `ai-service/tests/test_langchain_runtime_service.py`

## Exec tests

### New tests to add

- `exec-service/tests/test_run_store.py`
- `exec-service/tests/test_event_store.py`
- `exec-service/tests/test_streaming_runner.py`
- `exec-service/tests/test_execute_api_streaming.py`
- `exec-service/tests/test_execute_cancel.py`

## Frontend tests

### New tests to add

- reducer tests for event replay
- component tests for timeline rendering
- approval flow tests
- command output panel tests
- reconnect hydration tests


## Recommended Ownership Split

### Engineer 1

Backend run/event foundation in `ai-service`

### Engineer 2

`exec-service` streaming execution and command lifecycle

### Engineer 3

Frontend runtime shell, reducer, and layout restructuring

### Engineer 4 or shared

integration tests, migration glue, rollout flags


## Sequencing Dependencies

### Can start immediately in parallel

- Track A package skeleton
- Track B run store skeleton
- Track C frontend feature area skeleton

### Must wait for protocol freeze

- reducer logic
- stream payload parsing
- approval UX wiring

### Must wait for exec streaming

- full native act/observe loop
- live command output panel acceptance


## Milestone Definition

## Milestone M1

Run/event foundation complete.

Expected demo:

- create run
- fetch run
- list events
- stream planning/final events

## Milestone M2

Streaming exec foundation complete.

Expected demo:

- run readonly command
- see live output
- cancel command

## Milestone M3

Native iterative loop complete.

Expected demo:

- ask investigation question
- runtime performs multiple steps
- final answer references observations from the same run

## Milestone M4

Frontend runtime shell complete.

Expected demo:

- page refresh during active run resumes state
- approval pauses and resumes same run
- command output and tool calls are readable in center pane


## Immediate Next Document

After this task breakdown is approved, the next planning artifact should be:

- API contract draft with example payloads
- storage migration note for run/event tables
- frontend component wireframe for the new AI analysis workspace
