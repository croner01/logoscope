# Observability Skills Design

Date: 2026-04-14

## 1. Goal and Scope

Goal
- Add two observability-focused diagnostic skills that improve log and runtime troubleshooting for the two highest-friction scenarios in this repository:
  - read-path latency / slow-query incidents
  - correlation-anchor gaps in log diagnosis
- Resolve the remaining reliability risks in the current skill layer so prompt guidance, knowledge guidance, and structured execution all point in the same direction.

In scope
- two new built-in diagnostic skills
- focused tests for skill registration, matching, and step generation
- cleanup of remaining builtin-skill command-shape risks
- knowledge-pack version bump for observability prompt changes

Out of scope
- runtime execution engine redesign
- frontend UI changes
- new storage engines or schema migrations
- replacing existing builtin skills that already work well for Pod, network, or resource failures

Non-goals
- Do not create one giant “万能日志排障” skill.
- Do not duplicate `runtime_diagnosis_orchestrator` or `clickhouse_log_query` without adding clearer scenario boundaries.
- Do not weaken the structured-command restrictions already enforced by runtime execution.

## 2. Problem Statement

The current runtime diagnosis stack is better grounded than before, but two observability troubleshooting gaps remain.

### 2.1 Read-Path Latency / Slow Query
Current behavior can still spread across:
- generic runtime diagnosis
- ClickHouse log querying
- prompt-level guidance

This works, but it does not provide a single high-confidence entry skill for:
- query-service slow reads
- ClickHouse-backed timeouts
- large result scans
- slow preview / aggregation read paths

The result is that the model can still spend too much effort deciding which evidence layer to inspect first.

### 2.2 Correlation-Anchor Gap
We already improved prompt and knowledge behavior around `trace_id`, `request_id`, and time windows, but there is still no dedicated skill for the case:
- diagnosis needs to reconstruct a request path from incomplete anchors
- raw logs still contain enough information to continue
- the system should decide whether to narrow by request, by trace, or by explicit window

Without a dedicated skill, this logic is spread across prompt wording and project knowledge, which is weaker than an explicit diagnostic playbook.

### 2.3 Existing Remaining Risks
Two concrete risks still remain in the current implementation:
- some builtin skills still use chained shell forms that do not align well with the stricter structured-command guidance
- the project knowledge pack content changed, but its version marker still reflects the older revision

## 3. Design Principles

### 3.1 Narrow Skills, Clear Ownership
Each new skill should own one operational decision surface:
- one for read-path latency and slow-query evidence
- one for correlation-anchor reconstruction

This is preferable to a broad skill that becomes hard to trigger, explain, and maintain.

### 3.2 Complement Existing Skills, Do Not Replace Them
Existing builtin skills already cover:
- Pod lifecycle
- network connectivity
- resource usage
- generic runtime cross-layer diagnosis
- ClickHouse log deep queries

The new skills should sit above or beside those skills as stronger scenario-specific entrypoints, not as overlapping clones.

### 3.3 Strong Structured-Command Discipline
All new skill steps must produce structured, read-only, single-purpose commands compatible with the current runtime command policy.

### 3.4 Fault Layer Before Generic Expansion
The read-path skill should bias toward direct execution/resource evidence once the fault surface is known.
The correlation skill should bias toward the strongest available anchor without pretending correlation completeness is always required.

## 4. Proposed Skills

### 4.1 `observability_read_path_latency`

Purpose
- Handle read-path latency and slow-query incidents where symptoms already point to query-service / ClickHouse backed reads.

Representative scenarios
- query-service timeout
- slow `/api/v1/logs`, `/api/v1/traces`, preview, or aggregation routes
- ClickHouse read amplification symptoms
- large result-set or heavy filter latency

Primary evidence order
1. query-service logs near the fault window
2. ClickHouse `system.query_log`
3. ClickHouse `system.processes`
4. ClickHouse `system.metrics`
5. optional read-only `EXPLAIN` when query shape clarification is needed

Boundaries
- not for missing-ingest incidents unless read-path evidence already shows rows exist but reads are slow
- not for generic network failures
- not for Pod crash / restart diagnosis

Relationship to existing skills
- more scenario-specific than `runtime_diagnosis_orchestrator`
- more execution/resource-oriented than `clickhouse_log_query`

### 4.2 `observability_log_correlation_gap`

Purpose
- Handle cases where log diagnosis is blocked or degraded because `trace_id`, `request_id`, or time-window anchors are incomplete or unevenly available.

Representative scenarios
- no `trace_id`, but `request_id` exists
- request anchor exists in raw log text, but not in normalized context
- diagnosis needs explicit time-window narrowing before broader evidence collection
- related-log anchor timestamps exist but are not being used effectively

Primary evidence order
1. raw anchor extraction from available log text or context
2. explicit window confirmation / narrowing
3. request-vs-trace anchor choice
4. query-side confirmation that the selected anchor can still retrieve useful evidence

Boundaries
- not for clearly established database execution bottlenecks
- not for topology-only anomalies
- not for resource-only failures

Relationship to existing skills
- complements prompt/knowledge routing by making anchor reconstruction an explicit diagnostic playbook
- should reduce false blocking on missing `trace_id`

## 5. Remaining Risk Cleanup

### 5.1 Builtin Skill Command Cleanup
Current builtin skills that still deserve cleanup:
- `network_check`
- `resource_usage`

Desired outcome
- reduce shell chaining where feasible
- keep commands single-purpose and friendlier to the runtime command compiler / policy model

### 5.2 Knowledge Pack Versioning
The project knowledge pack should receive a version bump so runtime summaries and future replay analysis clearly distinguish the new observability guidance from the older revision.

## 6. Validation Strategy

For each new skill:
- registration test
- trigger-pattern sanity test
- match-score behavior for positive and negative scenarios
- `plan_steps()` shape test
- max-step bound test
- command-spec safety / structured intent review

For cleanup work:
- targeted tests for updated builtin skill outputs if command shapes materially change
- knowledge-pack version assertions updated to the new version

## 7. Expected Outcome

After this change:
- query/read latency incidents should enter a more direct, execution-layer-first workflow
- correlation-gap incidents should stop relying on prompt wording alone
- builtin skill outputs should align better with structured-command runtime expectations
- runtime metadata should clearly show that the newer observability guidance pack is active
