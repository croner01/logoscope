# Project Knowledge Pack Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a phase-one project knowledge pack for Logoscope so runtime diagnosis can inject service-specific and runtime-path-specific repository knowledge before planning and evidence collection.

**Architecture:** Phase one uses a two-layer design. First, create static markdown knowledge assets for core services and key runtime paths under `docs/superpowers/knowledge/`. Second, add a lightweight backend selector that loads those assets, chooses the most relevant service/path pair from `analysis_context`, injects a compact prompt section, and records selection metadata in runtime summaries.

**Tech Stack:** Markdown docs, Python/FastAPI, pytest, existing LangChain follow-up prompt pipeline.

---

## File Structure / Ownership

**Static Knowledge Assets**
- Create: `docs/superpowers/knowledge/index.md`
  - Human entrypoint for all phase-one project knowledge assets.
- Create: `docs/superpowers/knowledge/services/ai-service.md`
- Create: `docs/superpowers/knowledge/services/frontend.md`
- Create: `docs/superpowers/knowledge/services/ingest-service.md`
- Create: `docs/superpowers/knowledge/services/query-service.md`
- Create: `docs/superpowers/knowledge/services/semantic-engine.md`
- Create: `docs/superpowers/knowledge/services/topology-service.md`
  - One markdown asset per core service.
- Create: `docs/superpowers/knowledge/paths/ai-runtime-diagnosis.md`
- Create: `docs/superpowers/knowledge/paths/log-ingest-query.md`
- Create: `docs/superpowers/knowledge/paths/topology-generation-preview.md`
- Create: `docs/superpowers/knowledge/paths/trace-request-correlation.md`
  - One markdown asset per key runtime path.

**Backend Selector / Injection**
- Create: `ai-service/ai/project_knowledge_pack.py`
  - Loads markdown assets, extracts bounded sections, selects the best service/path assets, and builds compact runtime injection payloads.
- Modify: `ai-service/api/ai.py`
  - Enriches follow-up runtime `analysis_context` with project knowledge and persists selection metadata into run summary/context.
- Modify: `ai-service/ai/langchain_runtime/prompts.py`
  - Injects a dedicated project-knowledge section into follow-up prompts.

**Tests**
- Create: `ai-service/tests/test_project_knowledge_pack.py`
  - Verifies asset loading, section extraction, service/path selection, and safe fallback.
- Modify: `ai-service/tests/test_langchain_runtime_service.py`
  - Verifies project-knowledge prompt section is injected when context contains knowledge metadata.
- Modify: `ai-service/tests/test_agent_runtime_api.py`
  - Verifies follow-up runtime stores `knowledge_pack_version`, primary service/path, and selection reason in run metadata.

---

### Task 0: Prepare the isolated worktree

**Files:** none

- [ ] **Step 1: Confirm the implementation worktree exists and is clean**

Run:
```bash
git -C /root/logoscope/.worktrees/runtime-diagnosis-reliability status -sb
```
Expected: branch `runtime-diagnosis-reliability` with no unrelated edits.

- [ ] **Step 2: Enter the worktree**

Run:
```bash
cd /root/logoscope/.worktrees/runtime-diagnosis-reliability
```
Expected: subsequent paths in this plan are resolved from this worktree.

---

### Task 1: Author the phase-one knowledge assets

**Files:**
- Create: `docs/superpowers/knowledge/index.md`
- Create: `docs/superpowers/knowledge/services/ai-service.md`
- Create: `docs/superpowers/knowledge/services/frontend.md`
- Create: `docs/superpowers/knowledge/services/ingest-service.md`
- Create: `docs/superpowers/knowledge/services/query-service.md`
- Create: `docs/superpowers/knowledge/services/semantic-engine.md`
- Create: `docs/superpowers/knowledge/services/topology-service.md`
- Create: `docs/superpowers/knowledge/paths/ai-runtime-diagnosis.md`
- Create: `docs/superpowers/knowledge/paths/log-ingest-query.md`
- Create: `docs/superpowers/knowledge/paths/topology-generation-preview.md`
- Create: `docs/superpowers/knowledge/paths/trace-request-correlation.md`

- [ ] **Step 1: Create the knowledge-pack directory structure and index**

Run:
```bash
mkdir -p docs/superpowers/knowledge/services docs/superpowers/knowledge/paths
```

Create `docs/superpowers/knowledge/index.md` with:
```markdown
# Project Knowledge Pack Index

Phase-one knowledge assets for runtime diagnosis.

## Services
- [ai-service](./services/ai-service.md)
- [frontend](./services/frontend.md)
- [ingest-service](./services/ingest-service.md)
- [query-service](./services/query-service.md)
- [semantic-engine](./services/semantic-engine.md)
- [topology-service](./services/topology-service.md)

## Runtime Paths
- [ai-runtime-diagnosis](./paths/ai-runtime-diagnosis.md)
- [log-ingest-query](./paths/log-ingest-query.md)
- [topology-generation-preview](./paths/topology-generation-preview.md)
- [trace-request-correlation](./paths/trace-request-correlation.md)

## Authoring Rules
- Every asset must include `## Summary`, `## Sources`, and diagnosis-oriented sections.
- `## Sources` must list repository document paths used to derive the asset.
- Assets are written for diagnosis grounding, not for marketing copy.
```

- [ ] **Step 2: Create the six service knowledge assets**

Create `docs/superpowers/knowledge/services/ingest-service.md` with:
```markdown
# ingest-service

## Summary
`ingest-service` is the OTLP ingest entry for logs, metrics, and traces. It adapts request formats and writes normalized envelopes into the queueing layer rather than serving as the primary long-term query surface.

## Responsibilities
- Accept `/v1/logs`, `/v1/metrics`, `/v1/traces`
- Normalize incoming payload metadata
- Forward records into Kafka-backed ingest flow

## Boundaries
- Owns protocol adaptation and queue write
- Does not own query-time diagnosis or topology rendering

## Upstream / Downstream
- Upstream: Fluent Bit, OTel Collector, OTLP clients
- Downstream: Kafka topics such as `logs.raw`; semantic-engine worker consumes resulting envelopes

## APIs and Interfaces
- `POST /v1/logs`
- `POST /v1/metrics`
- `POST /v1/traces`

## Storage / Topics
- Writes to queue topics rather than directly serving user queries
- Key log topic: `logs.raw`

## Preferred Evidence Sources
- ingest-service pod logs
- queue writer / transform logic
- upstream collector payload shape

## Common Failures and Cautions
- Do not confuse successful ingest acceptance with downstream persistence success
- Request-format adaptation issues often appear before queue or storage failures

## Diagnosis Entry Hints
- Check whether payloads reached Kafka before blaming query-side services
- When logs are missing downstream, verify `/v1/logs` path and envelope transformation first

## Sources
- `/root/logoscope/AGENTS.md`
- `/root/logoscope/docs/api/reference.md`
- `/root/logoscope/docs/architecture/log-ingest-query-runtime-path.zh-CN.md`
- `/root/logoscope/ingest-service/README.md`
```

Create `docs/superpowers/knowledge/services/semantic-engine.md` with:
```markdown
# semantic-engine

## Summary
`semantic-engine` is the normalization and intelligence layer. It transforms raw envelopes into structured events, performs correlation and classification, and contributes topology-building inputs.

## Responsibilities
- Normalize logs/traces/metrics into structured events
- Classify and correlate events
- Provide AI-analysis-adjacent intelligence and topology inputs

## Boundaries
- Owns semantic processing and enrichment
- Does not serve as the primary end-user query API for logs

## Upstream / Downstream
- Upstream: Kafka raw topics from ingest path
- Downstream: ClickHouse, Neo4j, AI-facing structured context

## APIs and Interfaces
- Internal worker consumption flow
- semantic analysis APIs exposed by semantic-engine service

## Storage / Topics
- Reads queue topics such as `logs.raw`
- Writes normalized data to ClickHouse / Neo4j paths

## Preferred Evidence Sources
- semantic-engine worker logs
- normalization output fields
- classification / correlation traces in structured results

## Common Failures and Cautions
- Missing fields downstream may come from normalization loss, not user query bugs
- Topology symptoms can start in semantic processing rather than topology-service presentation

## Diagnosis Entry Hints
- When service names or trace fields look malformed, inspect normalization before blaming query rendering
- For topology anomalies, verify whether semantic-engine produced the expected graph inputs

## Sources
- `/root/logoscope/AGENTS.md`
- `/root/logoscope/docs/design/SYSTEM_DESIGN.md`
- `/root/logoscope/docs/architecture/log-ingest-query-runtime-path.zh-CN.md`
- `/root/logoscope/docs/architecture/service-topology.md`
```

Create `docs/superpowers/knowledge/services/query-service.md` with:
```markdown
# query-service

## Summary
`query-service` is the primary read-path API for logs, traces, previews, and derived observability views. It is the main diagnosis surface when users are reading existing evidence rather than ingesting new data.

## Responsibilities
- Serve log and trace queries
- Provide preview and derived query APIs
- Support realtime and filtered read paths for frontend exploration

## Boundaries
- Owns read/query interfaces
- Does not own OTLP ingest or topology graph construction

## Upstream / Downstream
- Upstream: ClickHouse-backed persisted observability data
- Downstream: frontend explorers, AI analysis flows, operator queries

## APIs and Interfaces
- `GET /api/v1/logs`
- query-service trace/log preview endpoints
- realtime log WebSocket path

## Storage / Topics
- Reads ClickHouse tables such as `logs.logs`
- Uses query-side preview routes for topology/log correlation

## Preferred Evidence Sources
- query-service logs
- ClickHouse `system.query_log`
- frontend request parameters hitting query-service APIs

## Common Failures and Cautions
- Slow reads may be query-service symptoms but ClickHouse root causes
- Missing topology preview data may reflect upstream topology generation issues rather than query-service-only bugs

## Diagnosis Entry Hints
- For user-facing read failures, start with query-service logs plus ClickHouse query evidence
- Prefer request window + request_id correlation before assuming trace-only visibility

## Sources
- `/root/logoscope/AGENTS.md`
- `/root/logoscope/docs/api/reference.md`
- `/root/logoscope/docs/architecture/log-ingest-query-runtime-path.zh-CN.md`
- `/root/logoscope/docs/api/topology.md`
```

Create `docs/superpowers/knowledge/services/topology-service.md` with:
```markdown
# topology-service

## Summary
`topology-service` serves topology query, manual adjustment, and realtime topology update APIs. It presents graph views built from traces, logs, metrics, and manual configuration inputs.

## Responsibilities
- Serve topology APIs and WebSocket updates
- Support manual node/edge adjustments
- Expose hybrid / enhanced / stats topology views

## Boundaries
- Owns topology presentation and adjustment APIs
- Does not own raw log ingest or generic query-service log exploration

## Upstream / Downstream
- Upstream: topology graph inputs from traces, logs, metrics, and manual config
- Downstream: frontend topology pages, preview and graph consumers

## APIs and Interfaces
- `GET /api/v1/topology/hybrid`
- `GET /api/v1/topology/enhanced`
- `GET /api/v1/topology/stats`
- `POST /api/v1/topology/edges/manual`
- `WS /ws/topology`

## Storage / Topics
- Serves graph data backed by Neo4j / hybrid topology logic
- Reads topology snapshots and graph metadata paths

## Preferred Evidence Sources
- topology-service logs
- hybrid topology responses
- edge preview contracts and topology metadata

## Common Failures and Cautions
- Empty topology can come from upstream graph-input loss, not only topology-service API bugs
- Trace ID absence does not imply topology generation is impossible because hybrid topology also uses logs and metrics

## Diagnosis Entry Hints
- For topology anomalies, compare topology API output with upstream graph-building expectations
- Use hybrid topology and edge preview together before concluding graph corruption

## Sources
- `/root/logoscope/AGENTS.md`
- `/root/logoscope/docs/api/reference.md`
- `/root/logoscope/docs/api/topology.md`
- `/root/logoscope/docs/architecture/service-topology.md`
```

Create `docs/superpowers/knowledge/services/ai-service.md` with:
```markdown
# ai-service

## Summary
`ai-service` is the diagnosis orchestration layer for AI analysis, follow-up reasoning, runtime command planning, and blocked-reason reporting. It turns evidence plus runtime context into investigation guidance.

## Responsibilities
- Serve AI analysis and follow-up diagnosis APIs
- Orchestrate runtime planning, execution policy, and summaries
- Maintain diagnosis contracts, runtime history, and command-approval flow

## Boundaries
- Owns diagnosis orchestration and runtime summaries
- Does not own raw observability storage or frontend rendering

## Upstream / Downstream
- Upstream: user question, analysis context, follow-up evidence, related logs, runtime metadata
- Downstream: runtime runs, summaries, action plans, prompt injection, operator guidance

## APIs and Interfaces
- AI runtime run APIs
- follow-up analysis entrypoints
- runtime streaming / event endpoints

## Storage / Topics
- runtime store / session history
- uses upstream logs, trace results, and related references as context rather than primary storage ownership

## Preferred Evidence Sources
- ai-service runtime events
- follow-up planning payloads
- blocked reason / gate decision metadata

## Common Failures and Cautions
- Generic reasoning quality problems may be context-loss issues rather than LLM capability issues
- `planning_incomplete` must not be used when runnable template commands already exist

## Diagnosis Entry Hints
- Inspect runtime summary, selected actions, and gate decision before changing prompt wording
- Separate context-missing, planning, execution-policy, and backend-readiness failures

## Sources
- `/root/logoscope/docs/superpowers/specs/2026-04-12-runtime-diagnosis-reliability-design.md`
- `/root/logoscope/docs/superpowers/plans/2026-04-12-runtime-diagnosis-reliability-plan.md`
- `/root/logoscope/docs/design/ai-agent-runtime-implementation-v1.md`
```

Create `docs/superpowers/knowledge/services/frontend.md` with:
```markdown
# frontend

## Summary
`frontend` is the user interaction layer. It launches AI analysis, runtime follow-up, logs exploration, trace exploration, and topology views, and is responsible for carrying the right diagnosis anchors into backend runtime entrypoints.

## Responsibilities
- Provide AIAnalysis and runtime-lab user flows
- Render logs, traces, topology, and runtime thought streams
- Build frontend-side analysis context and follow-up context

## Boundaries
- Owns user interaction and context assembly
- Does not execute diagnosis logic or persist runtime evidence itself

## Upstream / Downstream
- Upstream: user input, selected logs, UI state
- Downstream: query-service, topology-service, ai-service runtime APIs

## APIs and Interfaces
- AI analysis pages and runtime hooks
- logs / traces / topology explorers
- runtime SSE / polling clients

## Storage / Topics
- No primary observability storage ownership
- Transports user-selected context to backend services

## Preferred Evidence Sources
- browser-visible runtime state
- request payloads sent to ai-service
- follow-up context construction helpers

## Common Failures and Cautions
- Missing backend evidence can begin with frontend context loss
- Over-strict frontend gating can block legitimate diagnosis flows before backend logic runs

## Diagnosis Entry Hints
- Verify what `analysis_context` the page actually sends before assuming backend reasoning failure
- When trace IDs are absent, confirm whether log-mode + time-window fallback still carries request anchors

## Sources
- `/root/logoscope/AGENTS.md`
- `/root/logoscope/docs/design/SYSTEM_DESIGN.md`
- `/root/logoscope/docs/superpowers/specs/2026-04-12-runtime-diagnosis-reliability-design.md`
```

Create `docs/superpowers/knowledge/services/ai-service.md` and the five service files above exactly as shown.

- [ ] **Step 3: Create the four runtime-path knowledge assets**

Create `docs/superpowers/knowledge/paths/log-ingest-query.md` with:
```markdown
# log-ingest-query

## Summary
The log ingest and query path explains how a log enters Logoscope, is normalized and stored, and is later queried by users and runtime diagnosis.

## Participating Components
- Fluent Bit
- OTel Collector / Gateway
- ingest-service
- Kafka
- semantic-engine worker
- ClickHouse
- query-service
- frontend LogsExplorer

## Step-by-Step Flow
1. Upstream agents send logs into `ingest-service /v1/logs`
2. ingest-service transforms payloads and writes queue envelopes
3. semantic-engine worker consumes raw log envelopes and normalizes them
4. normalized log data lands in ClickHouse tables such as `logs.logs`
5. query-service serves read APIs and realtime query views
6. frontend explorers and diagnosis flows read from query-side surfaces

## Failure Surfaces
- malformed ingest payloads
- queue delivery gaps
- normalization field loss
- ClickHouse persistence or slow query issues
- query-service read path failures

## Preferred Evidence Sources
- ingest-service logs
- semantic-engine worker logs
- ClickHouse `logs.logs`
- ClickHouse `system.query_log`
- query-service logs

## Recommended First Checks
- confirm whether the event was accepted by ingest-service
- confirm whether normalized rows exist in ClickHouse
- confirm whether query-service failures are storage-driven or API-driven

## Common Misreads
- missing log results in UI do not automatically mean ingest failed
- slow query symptoms often come from ClickHouse even when surfaced by query-service

## Sources
- `/root/logoscope/docs/architecture/log-ingest-query-runtime-path.zh-CN.md`
- `/root/logoscope/docs/api/reference.md`
- `/root/logoscope/docs/design/SYSTEM_DESIGN.md`
```

Create `docs/superpowers/knowledge/paths/trace-request-correlation.md` with:
```markdown
# trace-request-correlation

## Summary
This path explains how diagnosis should correlate a fault across `trace_id`, `request_id`, and timestamp windows when some correlation keys are missing.

## Participating Components
- frontend AIAnalysis context builder
- ai-service follow-up context logic
- query-service log and trace read surfaces
- semantic-engine normalized fields

## Step-by-Step Flow
1. user input or prior analysis provides log text, trace ID, request ID, or timestamp anchors
2. frontend and backend normalize analysis context
3. diagnosis chooses the strongest available anchor set
4. evidence collection prefers explicit windows and request correlation over weak heuristic widening

## Failure Surfaces
- trace-only assumptions in services that expose only request IDs
- dropped timestamps or missing request windows
- over-broad fallback queries like `--since=15m`

## Preferred Evidence Sources
- raw log lines containing request IDs or timestamps
- normalized `analysis_context` fields
- follow-up related log windows and anchor timestamps

## Recommended First Checks
- confirm whether `request_id` is present even when `trace_id` is not
- confirm whether `request_flow_window_start/end` were preserved
- confirm whether the generated commands use explicit windows rather than broad defaults

## Common Misreads
- absence of `trace_id` does not imply diagnosis must stop
- request correlation and time windows can be first-class anchors

## Sources
- `/root/logoscope/docs/superpowers/specs/2026-04-12-runtime-diagnosis-reliability-design.md`
- `/root/logoscope/docs/architecture/log-ingest-query-runtime-path.zh-CN.md`
```

Create `docs/superpowers/knowledge/paths/topology-generation-preview.md` with:
```markdown
# topology-generation-preview

## Summary
This path explains how topology views are built and previewed, and where diagnosis should inspect graph-generation versus graph-serving failures.

## Participating Components
- semantic-engine topology builders
- topology-service APIs
- query-service preview routes
- frontend topology pages
- ClickHouse / Neo4j graph data

## Step-by-Step Flow
1. traces, logs, metrics, and manual config contribute graph inputs
2. topology builders create hybrid or enhanced graph structures
3. topology-service serves graph and update APIs
4. query preview routes may join topology edges with log evidence
5. frontend renders graph and edge-preview views

## Failure Surfaces
- upstream graph-input loss
- confidence threshold filtering
- preview contract mismatch
- topology-service response issues

## Preferred Evidence Sources
- hybrid topology API output
- topology-service logs
- edge preview responses
- graph metadata or snapshot state

## Recommended First Checks
- compare topology API output with preview output
- verify whether missing edges are filtered, absent upstream, or broken in serving layer
- inspect confidence threshold and manual suppression effects

## Common Misreads
- empty topology is not always a topology-service-only fault
- preview failures do not always imply graph-construction failures

## Sources
- `/root/logoscope/docs/architecture/service-topology.md`
- `/root/logoscope/docs/api/topology.md`
- `/root/logoscope/docs/api/reference.md`
```

Create `docs/superpowers/knowledge/paths/ai-runtime-diagnosis.md` with:
```markdown
# ai-runtime-diagnosis

## Summary
This path explains how runtime diagnosis builds context, plans evidence collection, enforces execution policy, and reports blocked reasons.

## Participating Components
- frontend AIAnalysis / runtime lab
- ai-service runtime API
- follow-up session / planning / orchestration helpers
- command execution backend

## Step-by-Step Flow
1. frontend builds diagnosis context from selected logs, trace IDs, request IDs, and time windows
2. ai-service creates a runtime run and normalizes context
3. follow-up logic plans actions and may generate template commands
4. readonly execution policy decides whether commands auto-run
5. runtime summary reports blocked or completed status with explicit reason taxonomy

## Failure Surfaces
- missing fault anchors at entry
- template-ready actions mislabeled as planning failures
- readonly auto-exec disabled but not surfaced clearly
- backend-unready execution path

## Preferred Evidence Sources
- runtime event stream
- gate decision metadata
- blocked reason detail
- action and observation counts

## Recommended First Checks
- inspect `analysis_context` first
- inspect `ready_template_actions` and `observed_actions`
- inspect whether blocked reason is planning, policy, backend, or evidence related

## Common Misreads
- low confidence may come from missing observations, not weak reasoning alone
- a blocked run does not always mean the planner failed

## Sources
- `/root/logoscope/docs/superpowers/specs/2026-04-12-runtime-diagnosis-reliability-design.md`
- `/root/logoscope/docs/superpowers/plans/2026-04-12-runtime-diagnosis-reliability-plan.md`
```

Create `docs/superpowers/knowledge/paths/ai-runtime-diagnosis.md` and the three path files above exactly as shown.

- [ ] **Step 4: Validate the assets contain the required headings and sources**

Run:
```bash
rg -n "^## (Summary|Sources|Preferred Evidence Sources|Recommended First Checks|Common Misreads|Common Failures and Cautions)$" docs/superpowers/knowledge/services docs/superpowers/knowledge/paths
```
Expected: every asset returns the required sections and no asset is missing a `Sources` block.

- [ ] **Step 5: Commit the static knowledge assets**

Run:
```bash
git add docs/superpowers/knowledge
git commit -m "docs(ai-runtime): add phase-one project knowledge assets"
```
Expected: one commit containing only the knowledge markdown assets.

---

### Task 2: Build the knowledge-pack loader and selector

**Files:**
- Create: `ai-service/ai/project_knowledge_pack.py`
- Create: `ai-service/tests/test_project_knowledge_pack.py`

- [ ] **Step 1: Write failing tests for loading and selection**

Create `ai-service/tests/test_project_knowledge_pack.py` with:
```python
"""Tests for ai.project_knowledge_pack."""

from pathlib import Path

from ai.project_knowledge_pack import (
    extract_markdown_sections,
    load_project_knowledge_registry,
    select_project_knowledge,
)


def test_extract_markdown_sections_reads_summary_and_sources():
    content = """# query-service

## Summary
query read path

## Preferred Evidence Sources
- query-service logs
- ClickHouse system.query_log

## Common Failures and Cautions
- do not confuse query-service symptoms with ClickHouse root cause

## Sources
- /root/logoscope/docs/api/reference.md
"""
    sections = extract_markdown_sections(content)

    assert sections["Summary"] == "query read path"
    assert "ClickHouse system.query_log" in sections["Preferred Evidence Sources"]
    assert "/root/logoscope/docs/api/reference.md" in sections["Sources"]


def test_load_project_knowledge_registry_loads_expected_assets():
    root = Path(__file__).resolve().parents[2]
    registry = load_project_knowledge_registry(root / "docs" / "superpowers" / "knowledge")

    assert "query-service" in registry["services"]
    assert "ai-runtime-diagnosis" in registry["paths"]
    assert registry["services"]["query-service"]["summary"]
    assert registry["paths"]["log-ingest-query"]["summary"]


def test_select_project_knowledge_prefers_service_and_log_path_for_query_failures():
    root = Path(__file__).resolve().parents[2]
    selection = select_project_knowledge(
        {
            "service_name": "query-service",
            "analysis_type": "log",
            "question": "query-service Code:241 clickhouse 慢查询怎么排查",
            "input_text": "ERROR query-service Code:241 request failed",
        },
        knowledge_root=root / "docs" / "superpowers" / "knowledge",
    )

    assert selection["knowledge_primary_service"] == "query-service"
    assert selection["knowledge_primary_path"] == "log-ingest-query"
    assert selection["knowledge_pack_version"] == "2026-04-13.v1"
    assert "ClickHouse" in selection["project_knowledge_prompt"]


def test_select_project_knowledge_can_fallback_to_path_when_service_missing():
    root = Path(__file__).resolve().parents[2]
    selection = select_project_knowledge(
        {
            "analysis_type": "log",
            "question": "topology edge preview 返回空结果",
            "input_text": "preview topology edge empty",
        },
        knowledge_root=root / "docs" / "superpowers" / "knowledge",
    )

    assert selection["knowledge_primary_service"] == ""
    assert selection["knowledge_primary_path"] == "topology-generation-preview"
    assert selection["project_knowledge_prompt"]
```

- [ ] **Step 2: Run the selector tests to verify failure**

Run:
```bash
cd ai-service && /root/logoscope/.venv/bin/pytest tests/test_project_knowledge_pack.py -q --no-cov
```
Expected: FAIL because `ai.project_knowledge_pack` does not exist yet.

- [ ] **Step 3: Implement the loader, manifest, and selector**

Create `ai-service/ai/project_knowledge_pack.py` with:
```python
"""Project knowledge pack loader and runtime selector."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re
from typing import Any, Dict, List, Optional


PROJECT_KNOWLEDGE_PACK_VERSION = "2026-04-13.v1"

_SERVICE_MANIFEST = {
    "ai-service": {"asset": "services/ai-service.md", "aliases": ["ai-service"]},
    "frontend": {"asset": "services/frontend.md", "aliases": ["frontend"]},
    "ingest-service": {"asset": "services/ingest-service.md", "aliases": ["ingest-service"]},
    "query-service": {"asset": "services/query-service.md", "aliases": ["query-service"]},
    "semantic-engine": {"asset": "services/semantic-engine.md", "aliases": ["semantic-engine"]},
    "topology-service": {"asset": "services/topology-service.md", "aliases": ["topology-service"]},
}

_PATH_MANIFEST = {
    "ai-runtime-diagnosis": {
        "asset": "paths/ai-runtime-diagnosis.md",
        "keywords": ["runtime", "follow-up", "followup", "blocked_reason", "planning", "command"],
        "related_services": ["ai-service", "frontend"],
    },
    "log-ingest-query": {
        "asset": "paths/log-ingest-query.md",
        "keywords": ["log", "logs", "clickhouse", "query", "query_log", "code:241"],
        "related_services": ["ingest-service", "semantic-engine", "query-service"],
    },
    "topology-generation-preview": {
        "asset": "paths/topology-generation-preview.md",
        "keywords": ["topology", "edge", "hybrid", "preview", "graph"],
        "related_services": ["semantic-engine", "topology-service", "query-service"],
    },
    "trace-request-correlation": {
        "asset": "paths/trace-request-correlation.md",
        "keywords": ["trace", "request_id", "request id", "time window", "timestamp"],
        "related_services": ["frontend", "ai-service", "query-service"],
    },
}


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def extract_markdown_sections(content: str) -> Dict[str, str]:
    sections: Dict[str, List[str]] = {}
    current = ""
    for raw_line in str(content or "").splitlines():
        line = raw_line.rstrip()
        heading = re.match(r"^##\s+(.+?)\s*$", line)
        if heading:
            current = heading.group(1).strip()
            sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(line)
    return {key: "\n".join(value).strip() for key, value in sections.items()}


@lru_cache(maxsize=4)
def load_project_knowledge_registry(knowledge_root: Path) -> Dict[str, Dict[str, Dict[str, Any]]]:
    root = Path(knowledge_root)
    registry = {"services": {}, "paths": {}}

    for service_name, meta in _SERVICE_MANIFEST.items():
        asset_path = root / meta["asset"]
        content = asset_path.read_text(encoding="utf-8")
        sections = extract_markdown_sections(content)
        registry["services"][service_name] = {
            "name": service_name,
            "asset_path": str(asset_path),
            "summary": sections.get("Summary", ""),
            "preferred_evidence": sections.get("Preferred Evidence Sources", ""),
            "cautions": sections.get("Common Failures and Cautions", ""),
            "entry_hints": sections.get("Diagnosis Entry Hints", ""),
            "sources": sections.get("Sources", ""),
        }

    for path_name, meta in _PATH_MANIFEST.items():
        asset_path = root / meta["asset"]
        content = asset_path.read_text(encoding="utf-8")
        sections = extract_markdown_sections(content)
        registry["paths"][path_name] = {
            "name": path_name,
            "asset_path": str(asset_path),
            "summary": sections.get("Summary", ""),
            "preferred_evidence": sections.get("Preferred Evidence Sources", ""),
            "first_checks": sections.get("Recommended First Checks", ""),
            "misreads": sections.get("Common Misreads", ""),
            "sources": sections.get("Sources", ""),
            "keywords": list(meta["keywords"]),
            "related_services": list(meta["related_services"]),
        }

    return registry


def _default_knowledge_root() -> Path:
    return Path(__file__).resolve().parents[2] / "docs" / "superpowers" / "knowledge"


def _normalize_lines(text: str, *, max_lines: int = 3) -> List[str]:
    lines = []
    for raw in str(text or "").splitlines():
        line = raw.strip().lstrip("- ").strip()
        if not line:
            continue
        if line in lines:
            continue
        lines.append(line)
        if len(lines) >= max_lines:
            break
    return lines


def select_project_knowledge(
    analysis_context: Dict[str, Any],
    *,
    knowledge_root: Optional[Path] = None,
) -> Dict[str, Any]:
    safe_context = analysis_context if isinstance(analysis_context, dict) else {}
    registry = load_project_knowledge_registry(Path(knowledge_root or _default_knowledge_root()))
    safe_service = _as_str(safe_context.get("service_name")).lower()
    search_text = " ".join(
        item for item in [
            _as_str(safe_context.get("question")),
            _as_str(safe_context.get("input_text")),
            _as_str(safe_context.get("analysis_type")),
            safe_service,
        ] if item
    ).lower()

    primary_service = registry["services"].get(safe_service, {})
    selected_path_name = ""
    selected_path = {}
    best_score = 0
    for path_name, asset in registry["paths"].items():
        score = sum(1 for keyword in asset.get("keywords", []) if keyword.lower() in search_text)
        if safe_service and safe_service in asset.get("related_services", []):
            score += 1
        if score > best_score:
            best_score = score
            selected_path_name = path_name
            selected_path = asset

    related_services: List[str] = []
    for service_name in selected_path.get("related_services", []):
        if not service_name or service_name == safe_service:
            continue
        related_services.append(service_name)
        if len(related_services) >= 2:
            break

    entry_hints = []
    entry_hints.extend(_normalize_lines(primary_service.get("entry_hints", ""), max_lines=2))
    entry_hints.extend(_normalize_lines(selected_path.get("first_checks", ""), max_lines=3))
    entry_hints = entry_hints[:5]

    cautions = []
    cautions.extend(_normalize_lines(primary_service.get("cautions", ""), max_lines=2))
    cautions.extend(_normalize_lines(selected_path.get("misreads", ""), max_lines=2))
    cautions = cautions[:3]

    prompt_lines: List[str] = []
    if primary_service:
        prompt_lines.append(f"服务摘要: {primary_service.get('summary', '')}")
    if selected_path:
        prompt_lines.append(f"链路摘要: {selected_path.get('summary', '')}")
    if entry_hints:
        prompt_lines.append("优先排查入口:")
        prompt_lines.extend(f"- {item}" for item in entry_hints)
    if cautions:
        prompt_lines.append("注意误判:")
        prompt_lines.extend(f"- {item}" for item in cautions)

    selection_reason_parts = []
    if primary_service:
        selection_reason_parts.append(f"service={safe_service}")
    if selected_path_name:
        selection_reason_parts.append(f"path={selected_path_name}")
    if not selection_reason_parts:
        selection_reason_parts.append("fallback=minimal")

    return {
        "knowledge_pack_version": PROJECT_KNOWLEDGE_PACK_VERSION,
        "knowledge_primary_service": safe_service if primary_service else "",
        "knowledge_primary_path": selected_path_name,
        "knowledge_related_services": related_services,
        "knowledge_selection_reason": ",".join(selection_reason_parts),
        "knowledge_entry_hints": entry_hints,
        "project_knowledge_prompt": "\n".join(item for item in prompt_lines if item).strip(),
    }
```

- [ ] **Step 4: Run the selector tests again**

Run:
```bash
cd ai-service && /root/logoscope/.venv/bin/pytest tests/test_project_knowledge_pack.py -q --no-cov
```
Expected: PASS.

- [ ] **Step 5: Commit the selector module**

Run:
```bash
git add ai-service/ai/project_knowledge_pack.py ai-service/tests/test_project_knowledge_pack.py
git commit -m "feat(ai-runtime): add project knowledge pack selector"
```
Expected: one focused commit for selector logic and tests.

---

### Task 3: Inject project knowledge into diagnosis runtime and summary metadata

**Files:**
- Modify: `ai-service/api/ai.py`
- Modify: `ai-service/ai/langchain_runtime/prompts.py`
- Modify: `ai-service/tests/test_langchain_runtime_service.py`
- Modify: `ai-service/tests/test_agent_runtime_api.py`

- [ ] **Step 1: Add failing tests for prompt injection and runtime metadata**

Add to `ai-service/tests/test_langchain_runtime_service.py`:
```python
def test_run_followup_langchain_prompt_injects_project_knowledge_section(monkeypatch):
    captured = {}

    async def _fake_collect_chat_response(**kwargs):
        captured["message"] = kwargs.get("message", "")
        return '{"conclusion":"ok","summary":"done","actions":[]}'

    monkeypatch.setattr("ai.langchain_runtime.service.collect_chat_response", _fake_collect_chat_response)

    async def _run():
        return await run_followup_langchain(
            **_build_runtime_kwargs(DummyStreamingLLM([])),
            analysis_context={
                "service_name": "query-service",
                "knowledge_pack_version": "2026-04-13.v1",
                "knowledge_primary_service": "query-service",
                "knowledge_primary_path": "log-ingest-query",
                "project_knowledge_prompt": "服务摘要: query-service 是主读路径\n链路摘要: log ingest -> clickhouse -> query-service",
            },
            stream_token_callback=None,
        )

    result = asyncio.run(_run())

    assert result["analysis_method"] == "langchain"
    message = str(captured.get("message") or "")
    assert "## 项目知识（Project Knowledge）" in message
    assert "query-service 是主读路径" in message
    assert "log ingest -> clickhouse -> query-service" in message
```

Add to `ai-service/tests/test_agent_runtime_api.py`:
```python
def test_build_followup_request_from_ai_run_enriches_project_knowledge_metadata():
    run = SimpleNamespace(
        question="继续分析 query-service Code:241",
        session_id="sess-knowledge-001",
        conversation_id="conv-knowledge-001",
        context_json={
            "analysis_type": "log",
            "service_name": "query-service",
            "input_text": "ERROR query-service Code:241",
        },
        input_json={"question": "继续分析 query-service Code:241"},
    )

    request = _build_followup_request_from_ai_run(
        run,
        {
            "mode": "followup_analysis",
            "conversation_id": "conv-knowledge-001",
            "history": [{"role": "user", "content": "继续分析 query-service Code:241"}],
        },
    )

    analysis_context = request.analysis_context
    assert analysis_context["knowledge_pack_version"] == "2026-04-13.v1"
    assert analysis_context["knowledge_primary_service"] == "query-service"
    assert analysis_context["knowledge_primary_path"] == "log-ingest-query"
    assert analysis_context["project_knowledge_prompt"]
```

- [ ] **Step 2: Run the targeted tests to verify failure**

Run:
```bash
cd ai-service && /root/logoscope/.venv/bin/pytest tests/test_langchain_runtime_service.py tests/test_agent_runtime_api.py -q --no-cov -k "project_knowledge or build_followup_request_from_ai_run_enriches"
```
Expected: FAIL because prompt injection and runtime enrichment are not implemented yet.

- [ ] **Step 3: Enrich follow-up runtime context with selected project knowledge**

In `ai-service/api/ai.py`, add imports near the other helper imports:
```python
from ai.project_knowledge_pack import select_project_knowledge
```

Then update `_build_followup_request_from_ai_run(...)` to enrich `analysis_context` before constructing `FollowUpRequest`:
```python
    analysis_context = run.context_json if isinstance(getattr(run, "context_json", None), dict) else {}
    analysis_context = dict(analysis_context)
    analysis_context.setdefault("question", question)
    knowledge_selection = select_project_knowledge(analysis_context)
    analysis_context.update(knowledge_selection)
    run.context_json = analysis_context
```

Still in `ai-service/api/ai.py`, after the run is created inside `_create_ai_run_impl(...)`, persist selection metadata into the run summary when present:
```python
        ctx = run.context_json if isinstance(getattr(run, "context_json", None), dict) else {}
        knowledge_updates = {
            "knowledge_pack_version": _as_str(ctx.get("knowledge_pack_version")),
            "knowledge_primary_service": _as_str(ctx.get("knowledge_primary_service")),
            "knowledge_primary_path": _as_str(ctx.get("knowledge_primary_path")),
            "knowledge_related_services": _as_list(ctx.get("knowledge_related_services"))[:2],
            "knowledge_selection_reason": _as_str(ctx.get("knowledge_selection_reason")),
        }
        knowledge_updates = {key: value for key, value in knowledge_updates.items() if value not in {"", [], None}}
        if knowledge_updates:
            runtime_service._update_run_summary(run, **knowledge_updates)  # noqa: SLF001
```

- [ ] **Step 4: Inject a dedicated project-knowledge prompt section**

In `ai-service/ai/langchain_runtime/prompts.py`, add a new helper and template slot.

Replace `FOLLOWUP_USER_TEMPLATE` with:
```python
FOLLOWUP_USER_TEMPLATE = """问题：
{question}

会话记忆摘要：
{memory_summary}

跨会话历史记忆（长期）：
{long_term_memory_summary}

最近对话：
{recent_history}

任务拆解：
{subgoals_json}

反思结果：
{reflection_json}

可用工具观测：
{tool_observations_json}

证据片段：
{references_json}

{skill_catalog}{project_knowledge}输出格式要求：
{format_instructions}
"""
```

Add helper:
```python
def build_project_knowledge_section(analysis_context: Dict[str, Any]) -> str:
    safe_context = analysis_context if isinstance(analysis_context, dict) else {}
    prompt_block = str(safe_context.get("project_knowledge_prompt") or "").strip()
    if not prompt_block:
        return ""
    return f"## 项目知识（Project Knowledge）\n{prompt_block}\n\n"
```

Update `build_followup_prompt(...)`:
```python
    if "project_knowledge" not in safe_payload:
        analysis_context = safe_payload.get("analysis_context") or {}
        safe_payload["project_knowledge"] = build_project_knowledge_section(analysis_context)
```

- [ ] **Step 5: Run the targeted tests again**

Run:
```bash
cd ai-service && /root/logoscope/.venv/bin/pytest tests/test_project_knowledge_pack.py tests/test_langchain_runtime_service.py tests/test_agent_runtime_api.py -q --no-cov -k "project_knowledge or build_followup_request_from_ai_run_enriches"
```
Expected: PASS.

- [ ] **Step 6: Commit the runtime integration**

Run:
```bash
git add ai-service/api/ai.py ai-service/ai/langchain_runtime/prompts.py ai-service/tests/test_langchain_runtime_service.py ai-service/tests/test_agent_runtime_api.py
git commit -m "feat(ai-runtime): inject project knowledge into diagnosis runtime"
```
Expected: one focused commit for runtime injection and metadata.

---

### Task 4: Full verification and implementation handoff

**Files:**
- Verify: `docs/superpowers/knowledge/**`
- Verify: `ai-service/ai/project_knowledge_pack.py`
- Verify: `ai-service/api/ai.py`
- Verify: `ai-service/ai/langchain_runtime/prompts.py`

- [ ] **Step 1: Run the focused backend verification suite**

Run:
```bash
cd ai-service && /root/logoscope/.venv/bin/pytest \
  tests/test_project_knowledge_pack.py \
  tests/test_langchain_runtime_service.py \
  tests/test_agent_runtime_api.py \
  tests/test_followup_session_helpers.py \
  -q
```
Expected:
- all tests pass
- no regression in follow-up runtime session setup

- [ ] **Step 2: Validate the knowledge assets remain discoverable and source-backed**

Run:
```bash
rg -n "^## Sources$|^# (ai-service|frontend|ingest-service|query-service|semantic-engine|topology-service|ai-runtime-diagnosis|log-ingest-query|topology-generation-preview|trace-request-correlation)$" docs/superpowers/knowledge
```
Expected: all phase-one assets are present and have a `Sources` section.

- [ ] **Step 3: Inspect diff scope**

Run:
```bash
git diff --stat HEAD~3..HEAD
```
Expected: diff is limited to knowledge assets, selector/injection code, and their tests.

- [ ] **Step 4: Final verification-only commit if needed**

If any wording-only or test-only tweaks were required during Task 4, run:
```bash
git add docs/superpowers/knowledge ai-service
git commit -m "test(ai-runtime): finalize project knowledge pack verification"
```
Expected: only use this commit when Task 4 required a narrow follow-up fix.

---

## Plan Self-Review

**Spec coverage:**
- Static knowledge assets for services and paths: Task 1.
- Lightweight selector and runtime payload generation: Task 2.
- Runtime diagnosis injection and metadata observability: Task 3.
- Safe fallback and bounded verification: Task 4.
- No long-term memory, embeddings, or external retrieval are introduced.

**Placeholder scan:**
- No `TBD`, `TODO`, or “implement later” placeholders remain.
- Every file path and command in the plan is explicit.

**Type consistency:**
- Runtime metadata keys are consistently named:
  - `knowledge_pack_version`
  - `knowledge_primary_service`
  - `knowledge_primary_path`
  - `knowledge_related_services`
  - `knowledge_selection_reason`
  - `project_knowledge_prompt`
