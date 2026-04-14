# Observability Skills Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two observability-focused diagnostic skills for read-path latency and correlation-anchor gaps, while cleaning up remaining builtin skill command-shape risks and versioning the refreshed knowledge pack.

**Architecture:** Keep the skill layer narrow and scenario-specific. Add one skill for query/read-path latency and one for correlation-anchor reconstruction, instead of a single catch-all logging skill. Reuse the existing `DiagnosticSkill` / `SkillStep` model and keep all command output compatible with the current structured-command runtime policy. In the same pass, tighten `network_check` and `resource_usage` command shapes and bump `project_knowledge_pack` version so prompt evolution is observable.

**Tech Stack:** Python, pytest, markdown knowledge assets

---

### Task 1: Version the Updated Knowledge Pack

**Files:**
- Modify: `ai-service/ai/project_knowledge_pack.py`
- Modify: `ai-service/tests/test_project_knowledge_pack.py`
- Modify: `ai-service/tests/test_langchain_runtime_service.py`
- Modify: `ai-service/tests/test_agent_runtime_api.py`

- [ ] **Step 1: Write the failing assertions for the new knowledge-pack version**

Update the tests that currently expect `2026-04-13.v1` so they expect the new version string instead.

- [ ] **Step 2: Run the focused tests to verify they fail for version mismatch**

Run: `python3 -m pytest --no-cov -q tests/test_project_knowledge_pack.py -k knowledge_pack_version`
Expected: FAIL because the implementation still returns the old version.

- [ ] **Step 3: Update the version constant**

Change `PROJECT_KNOWLEDGE_PACK_VERSION` in `ai-service/ai/project_knowledge_pack.py` from `2026-04-13.v1` to `2026-04-14.v2`.

- [ ] **Step 4: Re-run the focused tests**

Run: `python3 -m pytest --no-cov -q tests/test_project_knowledge_pack.py`
Expected: PASS

### Task 2: Add `observability_read_path_latency`

**Files:**
- Create: `ai-service/ai/skills/builtin/observability_read_path_latency.py`
- Modify: `ai-service/ai/skills/builtin/__init__.py`
- Create: `ai-service/tests/test_skill_observability_read_path_latency.py`

- [ ] **Step 1: Write the failing test file**

Cover these behaviors:
- skill has required metadata
- trigger patterns match slow-query / timeout / preview / aggregation read-path text
- unrelated network-only content does not strongly match
- `plan_steps()` returns <= `max_steps`
- steps produce only read-only structured commands
- steps include query-service logs, `system.query_log`, `system.processes`, `system.metrics`

- [ ] **Step 2: Run the new skill test file and verify failure**

Run: `python3 -m pytest --no-cov -q tests/test_skill_observability_read_path_latency.py`
Expected: FAIL because the skill file does not exist yet.

- [ ] **Step 3: Implement the skill**

Create a new builtin skill with:
- `name = "observability_read_path_latency"`
- focused trigger patterns for query/read latency, slow query, timeout, aggregation slowness, preview slowness
- ordered steps:
  1. query-service log window
  2. ClickHouse `system.query_log`
  3. ClickHouse `system.processes`
  4. ClickHouse `system.metrics`
  5. optional read-only `EXPLAIN` only when context clearly points to query-shape ambiguity

All commands must compile to `generic_exec` or `kubectl_clickhouse_query`.

- [ ] **Step 4: Register the new skill**

Import the new skill module from `ai-service/ai/skills/builtin/__init__.py`.

- [ ] **Step 5: Re-run the skill test**

Run: `python3 -m pytest --no-cov -q tests/test_skill_observability_read_path_latency.py`
Expected: PASS

### Task 3: Add `observability_log_correlation_gap`

**Files:**
- Create: `ai-service/ai/skills/builtin/observability_log_correlation_gap.py`
- Modify: `ai-service/ai/skills/builtin/__init__.py`
- Create: `ai-service/tests/test_skill_observability_log_correlation_gap.py`

- [ ] **Step 1: Write the failing test file**

Cover these behaviors:
- skill has required metadata
- trigger patterns match missing `trace_id`, available `request_id`, time-window narrowing, anchor mismatch cases
- clearly established database slow-query symptoms do not become a strong positive match here
- `plan_steps()` returns <= `max_steps`
- steps focus on anchor extraction, explicit window use, request-vs-trace narrowing, and query-side anchor confirmation

- [ ] **Step 2: Run the new skill test file and verify failure**

Run: `python3 -m pytest --no-cov -q tests/test_skill_observability_log_correlation_gap.py`
Expected: FAIL because the skill file does not exist yet.

- [ ] **Step 3: Implement the skill**

Create a new builtin skill with:
- `name = "observability_log_correlation_gap"`
- trigger patterns for missing trace/request anchors, window ambiguity, correlation degradation
- ordered steps:
  1. inspect raw anchor text / context
  2. confirm explicit window or derive narrow search window
  3. check whether request-based correlation is available even without `trace_id`
  4. confirm query-side evidence retrieval using the strongest remaining anchor

Keep all steps read-only and structured.

- [ ] **Step 4: Register the new skill**

Import the new skill module from `ai-service/ai/skills/builtin/__init__.py`.

- [ ] **Step 5: Re-run the skill test**

Run: `python3 -m pytest --no-cov -q tests/test_skill_observability_log_correlation_gap.py`
Expected: PASS

### Task 4: Clean Up `network_check` Command Shapes

**Files:**
- Modify: `ai-service/ai/skills/builtin/network_check.py`
- Modify: `ai-service/tests/test_skill_network_check.py`

- [ ] **Step 1: Add a failing test for command-shape constraints**

Add assertions that generated commands:
- avoid shell pipes
- avoid shell chaining with `&&`
- avoid inline redirection
- remain single-purpose and read-only

- [ ] **Step 2: Run the focused network-skill test and verify failure**

Run: `python3 -m pytest --no-cov -q tests/test_skill_network_check.py -k command_shape`
Expected: FAIL because current commands still contain shell chaining / piping.

- [ ] **Step 3: Refactor the skill commands**

Reshape steps into cleaner, single-purpose commands while preserving diagnosis order:
- health check
- DNS resolve
- service endpoints

- [ ] **Step 4: Re-run the full network skill test file**

Run: `python3 -m pytest --no-cov -q tests/test_skill_network_check.py`
Expected: PASS

### Task 5: Clean Up `resource_usage` Command Shapes

**Files:**
- Modify: `ai-service/ai/skills/builtin/resource_usage.py`
- Modify: `ai-service/tests/test_skill_resource_usage.py`

- [ ] **Step 1: Add a failing test for command-shape constraints**

Add assertions that generated commands:
- avoid shell chaining with `&&`
- avoid redirection
- stay focused on one evidence source per step where feasible

- [ ] **Step 2: Run the focused resource-skill test and verify failure**

Run: `python3 -m pytest --no-cov -q tests/test_skill_resource_usage.py -k command_shape`
Expected: FAIL because current commands still contain chained shell forms.

- [ ] **Step 3: Refactor the skill commands**

Split or simplify steps so they still gather:
- node usage
- pod usage
- quota / limit evidence

without shell chaining.

- [ ] **Step 4: Re-run the full resource skill test file**

Run: `python3 -m pytest --no-cov -q tests/test_skill_resource_usage.py`
Expected: PASS

### Task 6: Verify Skill Registry and Prompt Integration

**Files:**
- Modify if needed: `ai-service/tests/test_skill_matcher.py`
- Modify if needed: `ai-service/tests/test_langchain_runtime_service.py`

- [ ] **Step 1: Inspect existing matcher tests**

Confirm whether the new skills need explicit matcher coverage beyond their individual skill tests.

- [ ] **Step 2: Add focused matcher assertions if coverage is missing**

Prefer small tests that prove:
- the new skills can appear in the catalog for matching contexts
- they do not replace unrelated network/resource/topology routing

- [ ] **Step 3: Run the focused matcher/prompt tests**

Run:
- `python3 -m pytest --no-cov -q tests/test_skill_matcher.py`
- `python3 -m pytest --no-cov -q tests/test_langchain_runtime_service.py -k project_knowledge`

Expected: PASS

### Task 7: Final Focused Verification

**Files:**
- Review: all modified files

- [ ] **Step 1: Read the final diff**

Check for:
- duplicate skill responsibilities
- too-broad trigger patterns
- commands that violate current runtime safety direction
- version mismatches in tests

- [ ] **Step 2: Run the final focused verification bundle**

Run:
- `python3 -m pytest --no-cov -q tests/test_project_knowledge_pack.py`
- `python3 -m pytest --no-cov -q tests/test_skill_observability_read_path_latency.py`
- `python3 -m pytest --no-cov -q tests/test_skill_observability_log_correlation_gap.py`
- `python3 -m pytest --no-cov -q tests/test_skill_network_check.py`
- `python3 -m pytest --no-cov -q tests/test_skill_resource_usage.py`

Expected: PASS

- [ ] **Step 3: Summarize residual risk**

Document whether:
- the new skills should remain builtin or later move into a richer observability skill pack
- `runtime_diagnosis_orchestrator` still needs coordination tuning after the specialized skills exist
