# AI Agent Runtime Refactor Draft

## Background

The current AI analysis page is trying to deliver a Trae-like "working agent" experience:

- the user interacts in natural language
- the model keeps thinking across steps
- the system can query logs/traces, inspect context, execute commands, observe results, and continue
- the UI shows a coherent live run instead of a single QA answer

The current implementation does not match that product model. It is closer to:

- one follow-up request
- one LLM generation
- optional derived action list
- limited readonly auto-exec
- a streamed UI shell wrapped around a non-stream-native runtime

This mismatch is the root cause behind:

- dialogue behavior still feeling wrong
- streamed output appearing to stop midway
- command execution not feeling continuous
- thought/action/approval state becoming hard to reconcile
- incremental fixes repeatedly addressing symptoms instead of the execution model


## Diagnosis

### 1. Runtime model does not match the product requirement

The current follow-up flow is still centered around a single answer generation stage. The system prepares context, runs LLM or fallback, derives actions, optionally runs a few readonly actions, then emits final history.

This is not a true agent loop.

What the product needs is:

1. plan
2. select tool/command
3. precheck
4. execute
5. receive live observation
6. think again
7. continue or stop

### 2. Frontend renders a local placeholder instead of a durable assistant run

The AI analysis page creates a local placeholder assistant message during streaming and later merges the final backend history into the page state.

This creates several failure modes:

- streamed state and persisted state are not the same object
- approvals can be attached to temporary message state
- action observations can appear before the final message identity exists
- reconnect/recovery becomes merge logic instead of state recovery

### 3. Streaming is only partial

The current system streams some plan/thought/action events, but the core response path is not designed as a native event log.

Important limitations:

- `langchain` mode suppresses raw token streaming by default
- command execution is not truly streamed; it waits for process completion
- the frontend expects a final payload to reconcile the run

This means the user sees a shell of progress, not a continuous working session.

### 4. `agent_v2` is only an event wrapper

`agent_v2` currently maps legacy events into a v2 event vocabulary, but it is not an independent orchestrator with:

- durable state
- resumable execution
- structured tool lifecycle
- command output subscription
- interrupt/resume semantics

### 5. LangChain is being used as a structured answer layer, not an agent runtime

Current `langchain_runtime` behavior is useful for:

- prompt assembly
- structured output parsing
- helper observation packaging

It is not currently providing:

- multi-step tool calling
- resumable state graph
- persisted checkpoints
- long-running session orchestration

Therefore it should not be treated as the core runtime for this module in its current form.


## Refactor Goals

The refactor should target the following product outcomes.

### Product goals

- The user can ask the system to investigate a problem in natural language.
- The system can perform multiple rounds of thinking and acting inside one run.
- The UI can show live progress that remains coherent after completion or reconnect.
- The system can request approval before risky actions.
- Command execution output is streamed continuously when appropriate.
- The final answer is the result of a run, not the only thing that exists.

### Technical goals

- Replace placeholder message merging with durable run/event state.
- Separate runtime orchestration from answer formatting.
- Introduce a single event protocol used by backend and frontend.
- Make command execution truly stream-native.
- Support interruption, cancellation, retry, and recovery.
- Keep existing storage and policy controls compatible where possible.

### Non-goals for phase 1

- Exposing raw chain-of-thought to users
- Fully autonomous write actions without approval
- General internet browsing
- Multi-agent collaboration inside one user run


## Target Architecture

### Core principle

The system should move from a "request returns answer" model to a "run emits events" model.

### Proposed layers

1. Session layer
   Maintains long-lived conversation identity, user intent, and historical context.

2. Run layer
   Each user turn creates a run. A run owns its event stream, intermediate state, tool calls, and final result.

3. Agent runtime
   Executes the agent loop:
   plan -> select tool -> precheck -> execute -> observe -> summarize -> continue/stop

4. Tool adapters
   Query logs, query traces, query topology, KB lookup, command precheck, command execute, command output subscribe.

5. Event store and stream transport
   Persists run events and streams them to the frontend over one protocol.

6. Presentation layer
   Frontend renders messages, steps, approvals, tool output, and final answer from the event log.


## Proposed Data Model

### Conversation session

Represents a durable user-facing dialogue container.

Suggested fields:

- `session_id`
- `source`
- `analysis_type`
- `title`
- `service_name`
- `trace_id`
- `context_json`
- `summary_text`
- `created_at`
- `updated_at`
- `archived`

### Agent run

Represents one investigation turn.

Suggested fields:

- `run_id`
- `session_id`
- `status`
- `user_message_id`
- `assistant_message_id`
- `runtime_version`
- `engine`
- `input_json`
- `context_json`
- `summary_json`
- `started_at`
- `ended_at`
- `cancelled_at`
- `error_code`
- `error_detail`

Status values:

- `queued`
- `running`
- `waiting_approval`
- `completed`
- `failed`
- `cancelled`

### Run event

Represents the source of truth for live UI rendering and recovery.

Suggested fields:

- `event_id`
- `run_id`
- `seq`
- `event_type`
- `payload_json`
- `created_at`

### Tool invocation

Tracks each tool call independently.

Suggested fields:

- `tool_call_id`
- `run_id`
- `step_id`
- `tool_name`
- `input_json`
- `status`
- `result_json`
- `started_at`
- `ended_at`

### Command run

Tracks OS command execution separately from the agent message layer.

Suggested fields:

- `command_run_id`
- `run_id`
- `tool_call_id`
- `message_id`
- `action_id`
- `command`
- `command_type`
- `risk_level`
- `status`
- `requires_confirmation`
- `requires_elevation`
- `exit_code`
- `started_at`
- `ended_at`


## Event Protocol

The current legacy/v2 mapping should be replaced with one canonical event protocol.

### Required event types

- `run_started`
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
- `run_status_changed`
- `run_finished`
- `run_failed`
- `run_cancelled`

### Event contract rules

- Every event must contain `run_id`.
- Every event must contain a monotonic `seq`.
- Tool-related events must include `tool_call_id`.
- Command-related events must include `command_run_id` when execution has started.
- `assistant_delta` and `assistant_message_finalized` must refer to a durable `assistant_message_id`.
- The frontend must not invent synthetic assistant message identity for streamed runs.

### Reasoning exposure policy

Do not stream raw private chain-of-thought.

Instead, expose bounded summaries such as:

- "正在定位首个失败节点"
- "准备查看 query-service 最近 50 行日志"
- "日志显示连接池错误，继续检查数据库依赖"

This satisfies product transparency without binding the protocol to raw model internals.


## Backend Runtime Design

### Runtime responsibilities

The runtime should own:

- session/run initialization
- step planning
- tool selection
- safety policy checks
- command execution lifecycle
- observation summarization
- continuation or termination decision
- event persistence and streaming

### Suggested runtime state machine

- `initializing`
- `planning`
- `acting`
- `observing`
- `waiting_approval`
- `answering`
- `finalizing`
- `completed`
- `failed`
- `cancelled`

### Suggested control loop

1. Create run and emit `run_started`.
2. Create assistant message shell and emit `message_started`.
3. Build bounded context bundle.
4. Ask model for next step only, not final full answer.
5. If next step is a tool call, execute it and stream observations.
6. Summarize observation back into runtime memory.
7. Repeat until stop criteria are met.
8. Ask model to produce final user-facing answer.
9. Emit `assistant_message_finalized` and `run_finished`.

### Stop criteria

- sufficient evidence collected
- user question answered
- no safe next action available
- approval required and waiting
- iteration/time/token budget reached
- explicit cancel

### Memory model

Use three bounded memories:

- conversation memory
- run scratchpad
- long-term historical hints

The run scratchpad should be internal structured state, not a user-visible transcript dump.


## Tooling Model

### Tool categories

Phase 1 should keep a small, high-signal tool set.

- `logs.query`
- `traces.query`
- `topology.query`
- `kb.search`
- `command.precheck`
- `command.execute`
- `command.stream`

### Tool contracts

Each tool should define:

- name
- input schema
- output schema
- timeout
- retry policy
- safety classification
- whether output can be streamed

### Command execution path

The command path should be:

1. runtime proposes command
2. precheck evaluates policy
3. if readonly and allowed, runtime may execute directly
4. if write/elevated, runtime emits `approval_required`
5. once approved, runtime launches streaming command run
6. stdout/stderr are emitted as `tool_call_output_delta`
7. completion becomes `tool_call_finished`


## Exec Service Refactor

The exec service is a critical dependency for this product.

### Problems in current exec behavior

- process execution waits to finish before returning output
- stream endpoint replays stored output instead of streaming live output
- command run lifecycle is not integrated into a durable agent run

### Required changes

- replace blocking `subprocess.run` with async process execution
- stream stdout/stderr line or chunk deltas while the process is alive
- publish command lifecycle events with durable run identifiers
- support cancel and timeout semantics
- preserve audit records and policy metadata

### Recommended execution API

- `POST /api/v1/exec/runs`
  Creates a command run and starts execution.

- `GET /api/v1/exec/runs/{command_run_id}`
  Returns current snapshot.

- `GET /api/v1/exec/runs/{command_run_id}/events`
  Streams or paginates command output events.

- `POST /api/v1/exec/runs/{command_run_id}/cancel`
  Cancels a running command.

### Output event types

- `command_started`
- `command_output_delta`
- `command_finished`
- `command_failed`
- `command_cancelled`


## Frontend Redesign

### Main principle

The frontend should render an event log, not reconstruct a conversation from partial local state.

### Suggested frontend structure

- `useAgentSession`
  Manages session identity and top-level page state.

- `useAgentRun`
  Starts a run, subscribes to its event stream, handles reconnect.

- `useRunEventStore`
  Normalizes event log into renderable state.

- `AgentConversationView`
  Renders user and assistant messages.

- `AgentRunTimeline`
  Renders reasoning summaries, tool calls, approvals, and command output.

- `ToolCallCard`
  Renders one tool call, including command output stream.

- `ApprovalPanel`
  Handles confirm/reject for risky actions.

### UI behavior requirements

- Assistant message identity is assigned by backend before text delta starts.
- Tool calls appear as first-class items in the run timeline.
- Command output expands inline and updates live.
- If stream disconnects, the UI can recover by reloading run events from `run_id`.
- Final answer is a terminal event, not a separate merge phase.

### Avoid in the new design

- local placeholder assistant IDs
- post-hoc merging of stream placeholder into history
- dual rendering paths for streamed and non-streamed state
- deriving approvals from ad hoc message metadata arrays


## LangChain Evaluation

### What LangChain can still do well here

- prompt templates
- structured output parsing
- message formatting helpers
- model abstraction wrappers

### What it should not own in the current codebase

- core run orchestration
- event persistence
- approval lifecycle
- command output streaming
- frontend protocol design

### Recommendation

Phase 1 should not depend on LangChain as the central runtime.

Recommended approach:

- keep the existing LLM service abstraction
- keep LangChain utilities only where they simplify structured output
- build the agent runtime in project-native code

### Optional future evaluation

If later the team wants graph-based orchestration, evaluate LangGraph separately after:

- event protocol is stable
- run persistence exists
- command streaming exists
- approval lifecycle is settled

Do not introduce LangGraph before those foundations exist.


## Compatibility Strategy

### Reuse where reasonable

- existing AI session/history storage concepts
- existing command policy and ticket logic
- existing LLM provider abstraction
- existing request-flow context extraction logic

### Replace or isolate

- monolithic `_run_follow_up_analysis_core`
- frontend placeholder stream merge path
- legacy/v2 event translation layering
- non-stream-native exec implementation


## Migration Plan

### Phase 0. Design freeze and instrumentation

- stop expanding current follow-up UX behavior
- add logs/metrics around stream aborts, timeout reasons, and run durations
- define canonical event schema

Deliverable:

- approved protocol and runtime boundaries

### Phase 1. Backend run/event foundation

- add run and event persistence
- introduce canonical event emitter
- create assistant message shell at run start
- keep existing follow-up logic behind the new run envelope

Deliverable:

- frontend can subscribe to durable run events

### Phase 2. Exec streaming foundation

- refactor exec service to real-time output streaming
- connect command lifecycle to run events
- support cancel and approval resume

Deliverable:

- command output no longer appears as a one-shot block

### Phase 3. Native agent loop

- replace one-shot answer-first flow with iterative tool execution loop
- keep readonly tools in phase 3 scope
- gate write actions behind approval

Deliverable:

- one run can perform multiple act/observe rounds

### Phase 4. Frontend rewrite of AI analysis conversation shell

- build run-event-driven UI
- remove placeholder merge logic
- expose tool cards, approvals, and run recovery

Deliverable:

- stable Trae-like interactive workflow

### Phase 5. Cleanup and deprecation

- remove old stream/v2 compatibility mapping
- delete dead metadata merge logic
- simplify tests around one protocol only


## Risks

### 1. Scope creep

Risk:
Trying to solve agent UX, run persistence, command safety, and frontend redesign all at once.

Mitigation:
Sequence by foundations. Do not start frontend redesign before the event protocol and run model are fixed.

### 2. Unsafe autonomous actions

Risk:
Pushing toward "agent really works" without enforcing strict command policy.

Mitigation:
Keep readonly auto-exec narrow. Require approval for any write/elevated action.

### 3. Token and latency blow-up

Risk:
Multi-step loops can become expensive and slow.

Mitigation:
Use bounded context, iteration caps, summarized observations, and explicit stop criteria.

### 4. Recovery complexity

Risk:
Reconnections and page refreshes become brittle.

Mitigation:
Persist events and reconstruct UI from event history, not component memory.


## Success Criteria

The refactor is successful when:

- a run can survive UI reconnect without losing state
- assistant output is attached to a durable backend message from the start
- command output is visibly streamed during execution
- approvals can pause and resume the same run
- the model can perform multiple act/observe rounds in one run
- the frontend no longer needs placeholder-to-history merge logic


## Immediate Next Steps

1. Approve the canonical run/event protocol.
2. Approve the backend split: session layer, run layer, runtime layer, tool adapters.
3. Approve exec-service refactor into true streaming command runs.
4. Approve frontend rewrite around run-event rendering.
5. After approval, produce a concrete implementation plan with file/module breakdown.
