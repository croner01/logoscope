# Runtime Diagnosis Reliability Design

Date: 2026-04-12

## 1. Goal and Scope

Goal
- Unify all runtime diagnosis entrypoints so they carry complete fault anchors into planning and execution.
- Stop misclassifying runnable template-command scenarios as `planning_incomplete`.
- Make readonly auto-exec behavior explicit, explainable, and observable.
- Ensure failed runtime runs can be quickly attributed to one of four causes: context missing, planning incomplete, execution disabled, backend unavailable.

In scope
- `ai_runtime_lab` entry
- `AIAnalysis` follow-up runtime entry
- follow-up planning helpers
- follow-up readonly auto-exec policy
- runtime summary / blocked reason output contract
- regression tests and run-level observability fields

Out of scope
- project knowledge pack / skill system / long-term memory
- new external data sources
- new storage engines or schema migrations
- full orchestration rewrite

Non-goals
- Do not redesign diagnosis conclusion templates in this phase.
- Do not change the product meaning of confidence/coverage beyond making their causes easier to interpret.

## 2. Problem Statement

Two failure classes are already confirmed in production-like runs.

### 2.1 Context Loss
- Runtime entries do not consistently carry the fault timestamp, evidence window, and request anchor into `analysis_context`.
- Some runs degrade to generic `--since=15m` queries because `request_flow_window_start/end` are absent.
- Follow-up logic still over-relies on `trace_id`; this is incorrect for services that only expose `request_id` or only expose timestamp-correlated logs.

### 2.2 Planning / Execution Misclassification
- The planner can already generate low-risk readonly template commands.
- Despite this, the run may still end with `blocked_reason=planning_incomplete`.
- In other runs, readonly auto-exec is disabled but the outcome is surfaced as generic “evidence insufficient”, hiding the actual reason: no command was executed.

The result is a system that appears unintelligent for the wrong reason: not because the model lacks ideas, but because the runtime chain drops anchors, mislabels plan state, and hides execution policy outcomes.

## 3. Design Principles

### 3.1 Anchor First
Diagnosis runtime must always prefer stable fault anchors over heuristic widening.
- Preferred anchors: explicit evidence window > related-log anchor > source log timestamp > parsed timestamp from text.
- Preferred correlation key: `request_id` and `trace_id` are peers; absence of `trace_id` must not downgrade the run into weak mode if `request_id` and time window exist.

### 3.2 Runnable Beats Incomplete
If a readonly template command already exists and is structurally executable, the run is not `planning_incomplete`.
- It may instead be `readonly_auto_exec_disabled`, `backend_unready`, or `observation_missing`.

### 3.3 Policy Must Be Visible
When readonly auto-exec is disabled, the runtime must explicitly tell the user:
- commands were generated
- commands were not executed
- `observed_actions=0` is a policy outcome, not a planning failure

### 3.4 Uniform Contracts Across Entrypoints
`ai_runtime_lab` and `AIAnalysis` follow-up runtime must emit the same diagnosis contract fields and summary fields. Entry-specific UI may differ, but runtime semantics must not.

## 4. Architecture Overview

Spec A standardizes the runtime chain into four contracts.

### 4.1 Context Contract
Defines what fault anchors enter runtime.

### 4.2 Planning Contract
Defines what qualifies as incomplete planning vs ready-to-run templates.

### 4.3 Execution Policy Contract
Defines when readonly template commands auto-run and how the runtime explains disabled execution.

### 4.4 Outcome Contract
Defines how the run summarizes blocked reasons, coverage, template readiness, and anchor quality.

## 5. Context Contract

### 5.1 Required Runtime Context Fields
All runtime diagnosis entries must normalize these fields when available:
- `analysis_type`
- `service_name`
- `trace_id`
- `request_id`
- `source_log_timestamp`
- `related_log_anchor_timestamp`
- `request_flow_window_start`
- `request_flow_window_end`
- `source_trace_id`
- `source_request_id`
- `analysis_type_original`
- `analysis_type_downgraded`
- `analysis_type_downgrade_reason`

### 5.2 Field Priority Rules

#### `request_id`
Priority:
1. explicit result payload (`result.request_id`, `result.agent.request_id`)
2. follow-up related metadata (`followup_related_request_id`)
3. extracted value from current user question / input text
4. source log attributes (`source_request_id`)

#### `trace_id`
Priority:
1. explicit result payload
2. follow-up related metadata
3. extracted value from current input
4. source log trace id

#### Anchor timestamp
Priority:
1. `related_log_anchor_timestamp`
2. `followup_related_anchor_utc`
3. `source_log_timestamp`
4. parsed timestamp from result / context

#### Evidence window
Priority:
1. `request_flow_window_start/end`
2. `followup_related_start_time/end_time`
3. `evidence_window_start/end`
4. derived window around anchor timestamp

### 5.3 Alias Compatibility
To preserve compatibility with existing UI and helper outputs, the resolver must accept legacy aliases:
- `followup_related_anchor_utc`
- `followup_related_start_time`
- `followup_related_end_time`
- `evidence_window_start`
- `evidence_window_end`

### 5.4 Derived Window Rule
If no explicit window exists but an anchor timestamp exists:
- derive `request_flow_window_start/end` using configured window minutes
- clamp to a bounded range, e.g. 1 to 120 minutes
- record that the window was derived, not explicit

### 5.5 Entrypoint Coverage

#### `AIAnalysis` follow-up runtime
Must build context through a single helper instead of assembling partial fields inline.

#### `ai_runtime_lab`
Must use the same normalized context helper for runtime creation and no longer enforce harder evidence gates than the main runtime path.

## 6. Planning Contract

### 6.1 Canonical Planning States
A follow-up runtime may be in one of these planning states:
- `planning_incomplete`
- `template_ready`
- `ready_to_execute`
- `observation_missing`
- `backend_unready`
- `readonly_auto_exec_disabled`

These states are not all terminal run statuses. They are planning/execution interpretation states used in summary and blocked reason mapping.

### 6.2 `planning_incomplete` Definition
A run may be labeled `planning_incomplete` only when all are true:
- no existing executable actions are present
- no executable template commands can be generated from current plan gaps
- unresolved actions are still dominated by spec/semantic gaps
- no observation has been collected for any executable action

This explicitly excludes runs where template commands already exist.

### 6.3 `template_ready` Definition
A run is `template_ready` when:
- readonly template commands are structurally valid
- at least one template command is executable
- execution may or may not have happened yet

This state can coexist with:
- `readonly_auto_exec_disabled`
- `backend_unready`
- `observation_missing`

### 6.4 Planning Quality Fields
`plan_quality` must include:
- `planning_blocked`
- `planning_blocked_reason`
- `spec_blocked_ratio`
- `ready_template_actions`
- `template_ready_reason`

### 6.5 Template Command Semantics
Low-risk readonly templates should be treated as first-class execution candidates when they map to supported structured specs, especially for:
- `kubectl logs`
- ClickHouse `system.query_log`
- ClickHouse `system.processes`
- ClickHouse metrics queries
- safe read-only cluster inspection commands already supported by command spec validation

## 7. Execution Policy Contract

### 7.1 Default Policy
Recommended product default:
- readonly template commands auto-execute when all are true:
  - query action
  - supported structured command spec
  - low risk
  - per-request `auto_exec_readonly=true`
  - global readonly auto-exec feature enabled
  - backend execution path ready

### 7.2 Policy-Off Behavior
If per-request `auto_exec_readonly=false`:
- do not execute template commands
- preserve generated template actions
- summary must say execution was skipped by policy
- `observed_actions=0` must not be interpreted as evidence failure alone

### 7.3 Backend-Unready Behavior
If execution backend is unavailable:
- generated commands remain `template_ready`
- blocked reason should distinguish backend readiness from planning quality
- runtime should propose next-best commands without collapsing to `planning_incomplete`

### 7.4 User-Facing Messaging
UI and run transcript should expose one of these explicit notices:
- readonly auto-exec disabled for this run
- readonly auto-exec globally disabled
- execution backend unavailable
- templates generated and execution started

## 8. Outcome Contract

### 8.1 Summary Fields
Every runtime run summary should expose, when relevant:
- `blocked_reason`
- `blocked_reason_detail`
- `gate_decision.reason`
- `anchor_quality`
- `window_quality`
- `plan_quality.ready_template_actions`
- `template_ready_actions`
- `executed_template_actions`
- `observed_actions`
- `exec_coverage`
- `evidence_coverage`

### 8.2 Blocked Reason Mapping

#### Use `planning_incomplete` only when
- no executable actions
- no template-ready actions
- planning genuinely cannot continue

#### Use `readonly_auto_exec_disabled` when
- template-ready actions exist
- execution skipped due to request policy

#### Use `backend_unready` when
- template-ready actions exist
- execution path unavailable or degraded

#### Use `observation_missing` when
- executable actions exist or were issued
- evidence slots remain unresolved because no successful observation arrived

### 8.3 Anchor Quality
Suggested values:
- `explicit_window`
- `derived_window_from_related_anchor`
- `derived_window_from_source_log`
- `text_timestamp_only`
- `no_anchor`

### 8.4 Window Quality
Suggested values:
- `explicit`
- `derived`
- `fallback_default`

These fields make post-run analysis operational instead of interpretive.

## 9. Module and File Responsibilities

### Frontend
- `frontend/src/pages/AIAnalysis.tsx`
  - build follow-up runtime context through a shared helper
  - merge source anchors and related-log window metadata
- `frontend/src/pages/AIRuntimePlayground.tsx`
  - align lab entry behavior with main runtime semantics
  - surface readonly auto-exec policy notice
- `frontend/src/utils/runtimeAnalysisMode.ts`
  - resolve analysis type downgrade and normalized runtime context
- `frontend/src/utils/runtimeFollowUpContext.ts`
  - own anchor/window/request/trace merge logic
- `frontend/src/features/ai-runtime/hooks/useAgentRuntimeCommandFlow.ts`
  - ensure command runtime sessions inherit normalized analysis context

### Backend
- `ai-service/ai/followup_planning_helpers.py`
  - own evidence window resolution and planning state interpretation
  - define `planning_incomplete` vs `template_ready`
- `ai-service/ai/followup_orchestration_helpers.py`
  - own readonly auto-exec runtime behavior and template-action generation
  - preserve anchor/window fields in generated commands
- `ai-service/api/ai.py`
  - map internal planning/execution outcomes into stable run summary / blocked reason fields
- `ai-service/ai/request_flow_agent.py`
  - remains the canonical producer of request-flow windows and related evidence context

## 10. Testing Strategy

### 10.1 Frontend
Must verify:
- trace mode without `trace_id` auto-downgrades and clears dirty trace id
- follow-up runtime context contains `source_log_timestamp`, request id, anchor timestamp, and evidence window aliases
- `ai_runtime_lab` allows log-mode start without `trace_id`
- lab surfaces policy notice when `auto_exec_readonly=false`

### 10.2 Backend
Must verify:
- alias window fields resolve to explicit evidence window
- template-ready actions do not trigger `planning_incomplete`
- `planning_incomplete` still triggers when no runnable or template-ready action exists
- readonly auto-exec disabled cases surface policy-oriented summary and do not masquerade as planning failure
- backend-unready cases keep template-ready state visible

### 10.3 Replay / Regression Acceptance
Use known failing run classes like:
- `run-a170121c4c7b`
- similar log follow-up runs where `observed_actions=0`

Expected outcome:
- no hard stop on missing `trace_id` in log mode
- generated commands contain fault time window
- blocked reason is specific to policy/backend/planning cause
- template-ready scenarios do not end as `planning_incomplete`

## 11. Observability and Rollout

### 11.1 Run-Level Metrics
Track at least:
- ratio of runs with explicit vs derived windows
- ratio of runs with `request_id` present but no `trace_id`
- ratio of `planning_incomplete` runs with `ready_template_actions > 0` (must trend to zero)
- ratio of `readonly_auto_exec_disabled` outcomes
- ratio of `backend_unready` outcomes

### 11.2 Success Criteria
Spec A is successful when:
- fault windows are preserved through runtime creation and template generation
- template-ready runs are not mislabeled as planning failures
- users can tell why no commands were executed
- runtime failure attribution becomes operationally actionable

## 12. Risks and Tradeoffs

Risk
- exposing more blocked-reason detail may require transcript/UI wording changes

Mitigation
- keep the outcome taxonomy compact and stable

Risk
- over-broad fallback windows can still dilute evidence quality

Mitigation
- preserve explicit/derived quality labels and keep fallback windows bounded

Risk
- runtime entries may continue diverging if helpers are bypassed later

Mitigation
- centralize context normalization in shared helpers and cover with targeted tests

## 13. Recommendation

Implement Spec A before any project knowledge-pack work.

Reason
- if anchors are missing and runnable templates are mislabeled, adding more project knowledge will increase sophistication of reasoning without increasing the chance of grounded evidence collection.
- Spec A establishes the minimum reliable substrate on which project-specific diagnosis knowledge can later compound.
