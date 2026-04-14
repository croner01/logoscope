# Runtime Ops Prompt Hardening Design

Date: 2026-04-14

## 1. Goal and Scope

Goal
- Harden runtime diagnosis prompt guidance for operational troubleshooting without pushing unrelated scenarios into a slow-query-specific workflow.
- Make query/read-path performance incidents prefer execution- and resource-level evidence once the fault surface is already known.
- Preserve good behavior for ingest, semantic processing, topology, network, resource, and frontend-context diagnosis.

In scope
- follow-up system prompt guidance
- project knowledge assets for core services and runtime paths
- prompt-facing wording that shapes investigation priority
- focused regression coverage for knowledge selection behavior

Out of scope
- runtime execution engine changes
- skill orchestration rewrite
- new diagnostic skills
- UI wording changes unrelated to runtime diagnosis guidance

Non-goals
- Do not turn the system prompt into a large routing matrix for every operational scenario.
- Do not make `trace_id` or `request_id` optional in all contexts; only remove the incorrect assumption that they are universal prerequisites.
- Do not weaken evidence-first behavior.

## 2. Problem Statement

The current runtime diagnosis prompt stack is directionally correct but still has a routing bias.

Confirmed issue
- In query-service / ClickHouse read-path incidents, the model can continue prioritizing `trace_id` / `request_id` completion even after the failure surface has clearly shifted to slow query, storage execution, or resource pressure.

Why this happens
- The system prompt strongly enforces anchor completeness and evidence collection discipline.
- The project knowledge assets already mention query-service and ClickHouse evidence, but they do not explicitly say when correlation anchors stop being the main task and execution/resource evidence becomes the primary task.
- The trace/request correlation path is helpful, but its relative weight can make the model treat correlation completion like a prerequisite rather than a tool.

Risk of a naive fix
- If the global prompt is changed to always prefer system metrics or SQL evidence first, unrelated scenarios such as ingest loss, topology anomalies, network failures, Pod restarts, or frontend context loss could be misrouted.

## 3. Design Principles

### 3.1 Global Prompt Sets Discipline, Not Scenario Routing
The global prompt should define:
- evidence-first reasoning
- structured command requirements
- avoiding fake prerequisites
- selecting the strongest fault-layer evidence once the fault surface is known

It should not encode a full troubleshooting decision tree for every service.

### 3.2 Scenario Routing Lives in Knowledge Assets
Service and path knowledge should carry:
- preferred first evidence
- when to switch layers
- common misreads
- what not to blame first

This keeps routing specific but locally scoped.

### 3.3 Correlation Anchors Are Tools, Not Universal Gates
`trace_id`, `request_id`, and time windows remain first-class anchors for:
- request reconstruction
- narrowing time windows
- joining evidence across services

But once the dominant symptom is clearly:
- storage execution latency
- read-path slow query
- resource saturation
- network reachability
- Pod lifecycle failure

the diagnosis must prioritize direct evidence from that layer.

### 3.4 Tightening Must Add Boundaries, Not Volume
The fix should primarily add explicit boundaries such as:
- “do not require X before continuing when Y is already known”
- “when symptom family is Z, prefer direct evidence from layer Z”

This is more robust than adding many new examples or large prompt blocks.

## 4. Target Prompt Behavior

### 4.1 Read-Path / Slow-Query Incidents
When the incident already points to query-service / ClickHouse read-path latency:
- prefer query-service logs, `system.query_log`, `system.processes`, `system.metrics`, and when necessary `EXPLAIN`
- treat `trace_id` / `request_id` as accelerators for narrowing scope, not blockers for continuing diagnosis
- do not repeatedly replan toward correlation completion if execution-layer evidence is already the stronger next step

### 4.2 Ingest Incidents
When the symptom is missing downstream data:
- first confirm ingest acceptance and queue/envelope progression
- do not jump directly to query-side blame

### 4.3 Semantic / Normalization Incidents
When fields or service names look malformed:
- first inspect normalization and semantic output
- do not prematurely blame UI or query rendering

### 4.4 Topology Incidents
When topology looks empty or wrong:
- compare topology API output with upstream graph-building expectations
- do not treat missing trace IDs as proof topology diagnosis cannot proceed

### 4.5 Frontend Runtime Context Incidents
When runtime diagnosis appears weak:
- first verify what `analysis_context` actually reached backend runtime
- do not assume backend reasoning failed before checking frontend context loss

## 5. Proposed Changes

### 5.1 Global Prompt
Update `ai-service/ai/langchain_runtime/prompts.py` to add two narrowly-scoped rules:
- correlation anchors are important but not universal prerequisites once a stronger fault-layer symptom is established
- when the dominant symptom clearly belongs to one operational layer, prefer direct evidence from that layer before asking for more generic anchors

### 5.2 Knowledge Assets
Update these files to sharpen routing:
- `docs/superpowers/knowledge/services/query-service.md`
- `docs/superpowers/knowledge/paths/log-ingest-query.md`
- `docs/superpowers/knowledge/paths/trace-request-correlation.md`
- `docs/superpowers/knowledge/paths/ai-runtime-diagnosis.md`
- `docs/superpowers/knowledge/services/ingest-service.md`
- `docs/superpowers/knowledge/services/semantic-engine.md`
- `docs/superpowers/knowledge/services/topology-service.md`
- `docs/superpowers/knowledge/services/frontend.md`

### 5.3 Selection Logic Coverage
Keep the current project-knowledge selector simple unless a test shows it is materially misrouting scenarios. Prefer updating asset wording over adding more selection heuristics in this pass.

## 6. Validation Strategy

We should verify:
- the global prompt still reads as general operational guidance rather than a slow-query-only prompt
- query-service read-path incidents explicitly push toward execution/resource evidence
- correlation guidance still says `trace_id` / `request_id` are valuable, but no longer sounds like a hard stop
- ingest / semantic / topology / frontend knowledge still points to the correct first fault layer

Focused regression should include:
- project knowledge selection tests that still choose query-service + log-ingest-query for query failures
- manual review of generated prompt blocks for representative scenarios

## 7. Expected Outcome

After this hardening:
- slow-query and read-path incidents should pivot earlier to ClickHouse execution/resource evidence
- runtime diagnosis should stop sounding blocked on correlation completion when correlation is no longer the strongest next move
- other operational scenarios should remain correctly routed by their own service/path knowledge instead of inheriting read-path-specific behavior
