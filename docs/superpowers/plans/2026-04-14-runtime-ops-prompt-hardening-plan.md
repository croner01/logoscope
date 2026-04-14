# Runtime Ops Prompt Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden runtime diagnosis prompt guidance so query/read-path slow-query incidents prefer direct execution/resource evidence while other operational scenarios keep their own correct diagnosis entrypoints.

**Architecture:** Use a two-layer change. The global follow-up prompt gets only narrow, general-purpose guidance about fault-layer evidence and non-universal correlation anchors. Scenario-specific routing remains in project knowledge assets for services and runtime paths, which keeps slow-query guidance from bleeding into unrelated cases.

**Tech Stack:** Python prompt templates, markdown knowledge assets, pytest

---

### Task 1: Update Design Documents

**Files:**
- Create: `docs/superpowers/specs/2026-04-14-runtime-ops-prompt-hardening-design.md`
- Create: `docs/superpowers/plans/2026-04-14-runtime-ops-prompt-hardening-plan.md`

- [ ] **Step 1: Save the approved design**

Write the spec with:
- global prompt should only set diagnosis discipline
- scenario routing belongs in knowledge assets
- read-path slow-query incidents should pivot to execution/resource evidence
- ingest, semantic, topology, and frontend scenarios keep their own first checks

- [ ] **Step 2: Save the implementation plan**

Write this plan file with explicit file targets, testing steps, and review steps.

### Task 2: Tighten the Global Follow-Up Prompt

**Files:**
- Modify: `ai-service/ai/langchain_runtime/prompts.py`

- [ ] **Step 1: Add the failing expectation as a review target**

Expectation:
- the system prompt must say correlation anchors are important but not universal prerequisites
- the system prompt must say that once a stronger fault-layer symptom is established, direct evidence from that layer takes priority
- the system prompt must remain generic and must not name only query-service / ClickHouse

- [ ] **Step 2: Edit the system prompt minimally**

Update the numbered rules to add:
- a boundary around `trace_id` / `request_id` / time windows
- a boundary around selecting evidence from the dominant fault layer

- [ ] **Step 3: Re-read the full prompt block**

Confirm it still reads like a general SRE runtime prompt rather than a single-incident prompt.

### Task 3: Strengthen Query and Correlation Knowledge Assets

**Files:**
- Modify: `docs/superpowers/knowledge/services/query-service.md`
- Modify: `docs/superpowers/knowledge/paths/log-ingest-query.md`
- Modify: `docs/superpowers/knowledge/paths/trace-request-correlation.md`
- Modify: `docs/superpowers/knowledge/paths/ai-runtime-diagnosis.md`

- [ ] **Step 1: Update query-service guidance**

Add wording that:
- read latency may be a ClickHouse root cause
- once read-path slow-query symptoms are established, prefer `system.query_log`, `system.processes`, `system.metrics`, and necessary SQL-plan evidence

- [ ] **Step 2: Update log-ingest-query path guidance**

Add wording that:
- distinguishes ingest absence from read-path latency
- explicitly tells diagnosis to pivot to storage/read execution evidence when the issue is slow query rather than missing ingest

- [ ] **Step 3: Update trace-request-correlation guidance**

Add wording that:
- keeps correlation anchors first-class
- explicitly says they are not mandatory blockers when the direct fault layer is already known

- [ ] **Step 4: Update ai-runtime-diagnosis guidance**

Add wording that:
- instructs blocked/low-confidence analysis to distinguish anchor insufficiency from wrong evidence-layer choice

### Task 4: Tighten Other Operational Knowledge Assets

**Files:**
- Modify: `docs/superpowers/knowledge/services/ingest-service.md`
- Modify: `docs/superpowers/knowledge/services/semantic-engine.md`
- Modify: `docs/superpowers/knowledge/services/topology-service.md`
- Modify: `docs/superpowers/knowledge/services/frontend.md`

- [ ] **Step 1: Update ingest-service wording**

Make the entry hints and cautions more explicit that query-side symptoms are not proof of ingest failure, and that queue/envelope progression should be checked first.

- [ ] **Step 2: Update semantic-engine wording**

Make it clearer that malformed service names or missing fields should route to normalization inspection before query/UI blame.

- [ ] **Step 3: Update topology-service wording**

Make it clearer that empty or odd topology can be an upstream graph-input problem, and that missing trace IDs do not end topology diagnosis.

- [ ] **Step 4: Update frontend wording**

Make it clearer that diagnosis weakness can begin with missing backend context caused by frontend context assembly or transport loss.

### Task 5: Verify Selection Logic Still Routes Correctly

**Files:**
- Review: `ai-service/ai/project_knowledge_pack.py`
- Test: `ai-service/tests/test_project_knowledge_pack.py`

- [ ] **Step 1: Inspect current selector assumptions**

Confirm whether wording-only changes are enough. Do not change selector heuristics unless tests show a real routing gap.

- [ ] **Step 2: Add or update focused tests if needed**

Target behaviors:
- query failure still selects query-service plus log-ingest-query
- path-only fallback still works
- no scenario loses knowledge when service metadata is absent

- [ ] **Step 3: Run focused tests**

Run: `python3 -m pytest --no-cov -q tests/test_project_knowledge_pack.py`
Expected: PASS

### Task 6: Final Review

**Files:**
- Review: all modified files

- [ ] **Step 1: Read the resulting diff**

Check for:
- slow-query-specific language leaking into unrelated services
- duplicated or contradictory guidance
- over-strong wording that turns anchors into “never needed”

- [ ] **Step 2: Run final focused verification**

Run:
- `python3 -m pytest --no-cov -q tests/test_project_knowledge_pack.py`

Expected: PASS

- [ ] **Step 3: Summarize residual risk**

Document whether the selector still relies mostly on keywords and whether deeper routing heuristics should remain out of scope for this pass.
