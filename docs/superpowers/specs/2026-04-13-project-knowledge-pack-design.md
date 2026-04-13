# Project Knowledge Pack Design

Date: 2026-04-13

## 1. Goal and Scope

Goal
- Improve `runtime diagnosis` and `follow-up analysis` by feeding the agent project-specific knowledge about Logoscope services, key runtime paths, and preferred investigation entrypoints.
- Make diagnosis reasoning less generic and more grounded in the real service topology, data flow, and API boundaries of this repository.
- Build the first version as a reusable foundation so the same knowledge assets can later support broader agent workflows beyond runtime diagnosis.

In scope
- static project knowledge assets for core services and key runtime paths
- a lightweight runtime knowledge selection layer for diagnosis flows
- rules for choosing which service summary and path summary to inject
- tests and observability for what knowledge was selected and why
- documentation source mapping so the pack stays auditable and maintainable

Out of scope
- long-term memory or self-improving skill updates
- vector retrieval, embeddings, or external knowledge stores
- automatic knowledge extraction from every source file in the repository
- redesign of diagnosis prompts unrelated to project grounding
- broad agent-wide rollout beyond `runtime diagnosis` in the first phase

Non-goals
- Do not attempt to encode the entire codebase into runtime context.
- Do not replace existing evidence collection, planning, or command execution logic.
- Do not treat knowledge-pack injection as a substitute for missing fault anchors or missing observations.

## 2. Problem Statement

Current runtime diagnosis has already improved in anchor handling and blocked-reason clarity, but it is still often weaker than it should be in project-specific reasoning.

Confirmed gap
- the model can generate generic troubleshooting ideas, but often lacks a precise understanding of what a given Logoscope service actually does
- it does not consistently know which runtime path is relevant for the current failure, for example:
  - logs ingest path
  - query read path
  - topology generation path
  - AI runtime diagnosis path
- it may recommend reasonable commands without enough project awareness about where evidence should exist, which APIs are read paths versus write paths, or which service should be investigated first

Practical result
- summaries may sound plausible but remain too generic
- suggested commands may not reflect the most likely evidence source in Logoscope
- the system feels “not smart enough” even when the real issue is missing project knowledge rather than weak general reasoning

This phase addresses that gap by supplying a bounded, auditable, project-specific knowledge layer.

## 3. Design Principles

### 3.1 Grounded Before Broad
Project-specific knowledge should sharpen diagnosis only after core runtime reliability is in place.
- fault anchors and execution outcomes still remain the primary truth source
- project knowledge should improve where to look and how to interpret evidence, not replace evidence

### 3.2 Two-Layer Architecture
The first phase should use two layers:
- static knowledge pack as the durable source of truth
- runtime knowledge injection as a thin selector that pulls only the most relevant slices

This prevents prompt sprawl while keeping the knowledge assets reviewable.

### 3.3 Service First, Path Second
For runtime diagnosis, service-level knowledge is the main routing key, but many failures only become clear when mapped onto a key runtime path.
- service summary answers: “what is this service responsible for?”
- path summary answers: “where in the system flow is this failure likely happening?”

Both are required in the first phase.

### 3.4 Prefer Explicit Repository Truth
The knowledge pack must be derived from repository-controlled materials wherever possible.
Preferred sources:
- architecture docs
- API docs
- AGENTS / service READMEs
- implementation files only when docs are insufficient

### 3.5 Small Injection, Strong Selection
Do not inject full documents into runtime diagnosis.
Instead, select:
- one primary service summary
- zero to two related service summaries when needed
- one primary runtime-path summary
- one short block of recommended entrypoints and caution notes

### 3.6 Reusable by Construction
Although phase one serves `runtime diagnosis`, the knowledge assets must be stored in a form that later supports:
- follow-up analysis
- operator-assistant workflows
- future shared agent skills or memory layers

## 4. Knowledge Model

The first version of the project knowledge pack contains two asset types.

### 4.1 Service Knowledge Asset
One document per core service.

Required fields
- service name
- responsibility and boundary
- upstream dependencies
- downstream dependencies
- key APIs or entrypoints
- key data stores, topics, or tables
- common evidence locations
- common failure modes or misinterpretation risks
- diagnosis hints

Initial services in phase one
- `ingest-service`
- `semantic-engine`
- `query-service`
- `topology-service`
- `ai-service`
- `frontend`

### 4.2 Runtime Path Knowledge Asset
One document per key path.

Required fields
- path name
- start and end boundaries
- participating services
- key protocols / queues / tables / APIs
- main failure points
- preferred evidence sources
- common confusion points
- recommended first commands or first checks

Initial paths in phase one
- log ingest and query path
- trace and request correlation path
- topology generation and preview path
- AI runtime diagnosis path

## 5. Source-of-Truth Mapping

The knowledge pack should not be written from memory. Each asset must declare the repository sources it was distilled from.

Primary seed documents identified in this repository
- [AGENTS.md](/root/logoscope/AGENTS.md)
- [SYSTEM_DESIGN.md](/root/logoscope/docs/design/SYSTEM_DESIGN.md)
- [reference.md](/root/logoscope/docs/api/reference.md)
- [service-topology.md](/root/logoscope/docs/architecture/service-topology.md)
- [topology.md](/root/logoscope/docs/api/topology.md)
- [log-ingest-query-runtime-path.zh-CN.md](/root/logoscope/docs/architecture/log-ingest-query-runtime-path.zh-CN.md)

Secondary sources when needed
- service READMEs such as [README.md](/root/logoscope/.worktrees/runtime-diagnosis-reliability/ingest-service/README.md)
- topology highlight docs such as [ENHANCED_TOPOLOGY_HIGHLIGHT.md](/root/logoscope/.worktrees/runtime-diagnosis-reliability/semantic-engine/docs/ENHANCED_TOPOLOGY_HIGHLIGHT.md)
- code entry files only where the documentation does not fully answer runtime responsibilities

Authoring rule
- each knowledge asset must list its source documents explicitly
- if code was needed to fill a gap, the asset should record the file path used

## 6. Storage and Format

### 6.1 Repository Location
Store phase-one assets under a dedicated project knowledge directory within `docs/superpowers` so they remain close to agent-oriented design materials.

Recommended structure
- `docs/superpowers/knowledge/services/<service>.md`
- `docs/superpowers/knowledge/paths/<path>.md`
- `docs/superpowers/knowledge/index.md`

### 6.2 Document Shape
Each knowledge asset should use a stable markdown template with small, clearly labeled sections.

For service assets
- Summary
- Responsibilities
- Boundaries
- Upstream / Downstream
- APIs and Interfaces
- Storage / Topics
- Preferred Evidence Sources
- Common Failures and Cautions
- Diagnosis Entry Hints
- Sources

For path assets
- Summary
- Participating Components
- Step-by-Step Flow
- Failure Surfaces
- Preferred Evidence Sources
- Recommended First Checks
- Common Misreads
- Sources

### 6.3 Why Markdown First
Markdown is sufficient for phase one because it is:
- human-reviewable
- version-controlled
- easy to summarize in runtime code
- compatible with future skill or retrieval systems

A machine-readable index may be added later, but phase one should not require a new storage system.

## 7. Runtime Injection Design

### 7.1 Injection Entry
Project knowledge injection should run only in diagnosis entrypoints where repository-specific grounding materially helps.

Phase-one targets
- `AIAnalysis` follow-up runtime path
- `ai_runtime_lab` diagnosis runtime path
- shared backend follow-up request preparation path if needed for contract consistency

### 7.2 Selection Inputs
The selector should consider:
- `service_name`
- `analysis_type`
- whether this is initial diagnosis or follow-up runtime
- user question text
- detected error keywords or path keywords when available

### 7.3 Selection Outputs
The selector returns a compact runtime payload containing:
- `primary_service_knowledge`
- `related_services`
- `primary_runtime_path`
- `knowledge_entry_hints`
- `knowledge_selection_reason`
- `knowledge_pack_version`

The output should be short enough to inject into prompts or runtime context without overwhelming the rest of the diagnosis payload.

### 7.4 Initial Selection Rules
Service selection
- if `service_name` matches a known asset, choose it as primary
- if no direct service match exists, fall back to a path-first summary based on intent keywords

Path selection
- if query / logs / ClickHouse terms dominate, prefer the log ingest and query path
- if trace / request correlation terms dominate, prefer the trace and request correlation path
- if topology / edge / hybrid terms dominate, prefer the topology path
- if runtime / follow-up / planning / command terms dominate, prefer the AI runtime diagnosis path

Related service expansion
- add at most two related services based on path membership
- do not expand broadly across the whole topology

### 7.5 Prompt Contract
The runtime should inject knowledge as concise summaries, not raw full documents.

Expected structure
- service summary paragraph
- path summary paragraph
- bullet list of 3 to 5 preferred evidence sources or first checks
- bullet list of 1 to 3 caution notes about common misreads

## 8. Integration Boundaries

### 8.1 What This Layer Does
- helps the model understand what the current service is supposed to do
- helps the model choose better first evidence targets
- helps the model interpret errors in the context of actual Logoscope architecture

### 8.2 What This Layer Must Not Do
- it must not override evidence windows or request anchors
- it must not fabricate service relationships not backed by repository truth
- it must not introduce a separate hidden planning policy
- it must not suppress explicit runtime observations in favor of static knowledge

## 9. Observability and Safety

To keep the feature auditable, runtime diagnosis should expose limited metadata about knowledge selection.

Recommended runtime fields
- `knowledge_pack_version`
- `knowledge_primary_service`
- `knowledge_primary_path`
- `knowledge_related_services`
- `knowledge_selection_reason`

These fields should appear in summary or debug metadata, not necessarily in user-facing prose.

Safety expectations
- if no matching knowledge asset is found, runtime diagnosis must continue without failure
- if the selector is uncertain, it should choose fewer assets rather than inject broad context
- missing knowledge is a soft degradation, not a blocked run state

## 10. Rollout Strategy

Phase one should be implemented in this order.

Step 1
- author and review the static knowledge pack assets for core services and paths

Step 2
- build a small selector that maps runtime inputs to relevant assets

Step 3
- inject compact summaries into runtime diagnosis entrypoints

Step 4
- add tests proving selection and fallback behavior

Step 5
- evaluate whether the diagnosis output becomes more precise in service-specific reasoning and command choice

## 11. Acceptance Criteria

The first phase is successful when all are true.

Knowledge assets
- core services and key runtime paths each have reviewed markdown assets
- every asset declares its repository sources

Selection behavior
- runtime diagnosis can select the correct primary service asset for known services
- runtime diagnosis can select the correct primary path summary for common failure intents
- selection degrades safely when no service or path match exists

Runtime usefulness
- diagnosis prompts or summaries show evidence of project-specific grounding
- suggested checks prefer real Logoscope evidence locations more often than generic fallback checks
- no meaningful increase in blocked runs or prompt overload is introduced

## 12. Risks and Mitigations

Risk
- knowledge assets drift away from actual code behavior

Mitigation
- require source lists per asset and keep assets small enough to review during normal development

Risk
- runtime injection becomes too large and dilutes core evidence signals

Mitigation
- inject only summaries, cap related-service expansion, and keep path selection singular by default

Risk
- service summaries become marketing-style rather than diagnosis-grade

Mitigation
- require sections for evidence sources, failure surfaces, and caution notes instead of only descriptive prose

Risk
- this feature is used to mask missing evidence collection

Mitigation
- preserve existing runtime evidence contracts and keep knowledge selection metadata explicit in debug output

## 13. Recommendation

Implement this as a phase-one knowledge foundation for `runtime diagnosis`, with the structure intentionally reusable by future agent workflows.

Reason
- current diagnosis quality is now more often limited by missing project-specific grounding than by missing basic runtime contracts
- a static-first knowledge layer is the fastest way to improve domain precision without introducing new infrastructure
- service-plus-path assets provide enough context to improve first-step reasoning while remaining bounded and reviewable
