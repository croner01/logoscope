# AI Analysis — Metadata-Driven Remote Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Inject log metadata (pod/namespace/node/labels) as execution targets into the AI agent, route commands through a dual-channel system (query-service for local log lookups, exec-service for remote pod/node commands), add cost preflight gating, and implement session-level command dedup with result memory.

**Architecture:** Five new focused files in `ai-service/ai/agent_runtime/` — `command_router.py` (dual-channel routing), `query_client.py` (query-service HTTP client), `cost_preflight.py` (cost estimation + gate), `execution_journal.py` (fingerprint dedup + memory). Modified files: `SkillContext` gains `source_target`, frontend injects metadata, `service.py` integrates the new pipeline stages.

**Tech Stack:** Python 3.11+ (ai-service), TypeScript/React (frontend), HTTP to query-service + exec-service

---

### Task 1: Metadata Injection — Frontend

**Files:**
- Modify: `frontend/src/utils/runtimeFollowUpContext.ts:50-76`

- [ ] **Step 1: Add `sourceTarget` to the function params interface**

In `buildRuntimeFollowUpContext()`, add the new param to the interface (after `followupRelatedMeta`):

```typescript
  sourceTarget?: {
    pod_name?: string | null;
    namespace?: string | null;
    node_name?: string | null;
    host_ip?: string | null;
    container_name?: string | null;
    labels?: Record<string, string> | null;
    service_name?: string | null;
  } | null;
```

- [ ] **Step 2: Add `source_target` to the returned `baseContext`**

In the `baseContext` object (after `request_id`), add:

```typescript
    source_target: params.sourceTarget && (
      params.sourceTarget.pod_name
      || params.sourceTarget.namespace
      || params.sourceTarget.node_name
    ) ? compactRecord({
      pod_name: firstText(params.sourceTarget.pod_name),
      namespace: firstText(params.sourceTarget.namespace),
      node_name: firstText(params.sourceTarget.node_name),
      host_ip: firstText(params.sourceTarget.host_ip),
      container_name: firstText(params.sourceTarget.container_name),
      labels: params.sourceTarget.labels && typeof params.sourceTarget.labels === 'object'
        ? params.sourceTarget.labels
        : undefined,
      service_name: firstText(params.sourceTarget.service_name),
    }) : undefined,
```

- [ ] **Step 3: Run TypeScript type check**

```bash
cd frontend && npx tsc --noEmit src/utils/runtimeFollowUpContext.ts
```
Expected: PASS (no new type errors in this file)

- [ ] **Step 4: Commit**

```bash
git add frontend/src/utils/runtimeFollowUpContext.ts
git commit -m "feat: add source_target injection to runtimeFollowUpContext

Extracts pod_name, namespace, node_name, host_ip, container_name,
labels, and service_name from log metadata into analysis_context
so the AI agent can target diagnostics at the specific pod/node.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Metadata Injection — AIAnalysis.tsx Call Sites

**Files:**
- Modify: `frontend/src/pages/AIAnalysis.tsx` (around `buildLogAnalysisInput` and `buildRuntimeFollowUpContext` call sites)

- [ ] **Step 1: Find and update `buildRuntimeFollowUpContext` call sites**

Search `AIAnalysis.tsx` for calls to `buildRuntimeFollowUpContext`. Each call site needs the new `sourceTarget` param.

Run: `grep -n "buildRuntimeFollowUpContext" /root/logoscope/frontend/src/pages/AIAnalysis.tsx`

Read each call site context (~10 lines around each match) and identify where `location.state.logData` or similar log metadata is in scope.

- [ ] **Step 2: Pass `sourceTarget` at each call site**

At each `buildRuntimeFollowUpContext({...})` call, add:

```typescript
  sourceTarget: logData ? {
    pod_name: logData.pod_name,
    namespace: logData.namespace,
    node_name: logData.node_name,
    host_ip: logData.host_ip,
    container_name: logData.container_name,
    labels: logData.labels,
    service_name: logData.service_name,
  } : null,
```

Where `logData` is the variable name used at that call site for the source log entry (may be `location.state.logData`, `sourceLog`, etc.).

- [ ] **Step 3: Run full TypeScript check and lint**

```bash
cd frontend && npx tsc --noEmit && npm run lint
```
Expected: PASS (no new errors)

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/AIAnalysis.tsx
git commit -m "feat: pass source_target metadata from AIAnalysis page to follow-up context

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Metadata Injection — SkillContext

**Files:**
- Modify: `ai-service/ai/skills/base.py:29-61`

- [ ] **Step 1: Add `source_target` field to `SkillContext` dataclass**

Add after the `evidence_window_end` field (line 61):

```python
    # ── Execution target from source log metadata ────────────────────────────
    source_target: Dict[str, Any] = field(default_factory=dict)
```

- [ ] **Step 2: Populate `source_target` in `SkillContext.from_dict()`**

In `from_dict()`, add after the `evidence_window_end` line:

```python
            source_target=(
                safe.get("source_target")
                if isinstance(safe.get("source_target"), dict)
                else {}
            ),
```

- [ ] **Step 3: Add `source_target_text()` helper method on `SkillContext`**

After `combined_text()`, add:

```python
    def source_target_text(self) -> str:
        """Render source_target as searchable text for pattern matching."""
        st = self.source_target if isinstance(self.source_target, dict) else {}
        parts = [
            _as_str(st.get("pod_name")),
            _as_str(st.get("namespace")),
            _as_str(st.get("node_name")),
            _as_str(st.get("service_name")),
        ]
        labels = st.get("labels")
        if isinstance(labels, dict):
            parts.extend(_as_str(v) for v in labels.values())
        return " ".join(p for p in parts if p)
```

- [ ] **Step 4: Run existing skill tests**

```bash
cd ai-service && python -m pytest tests/ -k "skill" -v --timeout=30 2>&1 | tail -20
```
Expected: all existing tests pass (new field has default, backward compatible)

- [ ] **Step 5: Commit**

```bash
git add ai-service/ai/skills/base.py
git commit -m "feat: add source_target field to SkillContext

Populated from analysis_context.source_target, enables skills to
target diagnostics at the specific pod/namespace/node from the
source log entry.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: QueryServiceClient — new module

**Files:**
- Create: `ai-service/ai/agent_runtime/query_client.py`
- Create: `ai-service/tests/test_query_client.py`

- [ ] **Step 1: Write the failing test**

Create `ai-service/tests/test_query_client.py`:

```python
"""Tests for QueryServiceClient."""
from __future__ import annotations

import json
from unittest.mock import patch, MagicMock
from ai.agent_runtime.query_client import QueryServiceClient, QueryServiceClientError


class TestQueryServiceClient:
    def test_builds_log_query_params_from_metadata(self):
        client = QueryServiceClient(base_url="http://query-service:8092")
        params = client._build_log_params(
            service_name="semantic-engine",
            namespace="islap",
            pod_name="semantic-engine-abc123",
            trace_id=None,
            start_time="2026-06-05T10:00:00Z",
            end_time="2026-06-05T10:30:00Z",
            level="ERROR",
            search=None,
            limit=200,
        )
        assert params["service_name"] == "semantic-engine"
        assert params["namespace"] == "islap"
        assert params["pod_name"] == "semantic-engine-abc123"
        assert params["start_time"] == "2026-06-05T10:00:00Z"
        assert params["end_time"] == "2026-06-05T10:30:00Z"
        assert params["level"] == "ERROR"
        assert params["limit"] == 200

    def test_builds_minimal_params_without_optionals(self):
        client = QueryServiceClient()
        params = client._build_log_params(limit=100)
        assert params["limit"] == 100
        assert "service_name" not in params
        assert "pod_name" not in params

    def test_translates_simple_select_to_query_params(self):
        client = QueryServiceClient()
        spec = {
            "tool": "kubectl_clickhouse_query",
            "args": {
                "query": "SELECT * FROM logs.events WHERE service_name='api-gateway' AND level='ERROR' ORDER BY timestamp DESC LIMIT 50",
                "target_kind": "clickhouse_cluster",
                "target_identity": "database:logs",
            },
        }
        params = client.translate_clickhouse_spec(spec)
        assert params is not None
        assert params["service_name"] == "api-gateway"
        assert params["level"] == "ERROR"
        assert params["limit"] == 50

    def test_returns_none_for_complex_sql(self):
        client = QueryServiceClient()
        spec = {
            "tool": "kubectl_clickhouse_query",
            "args": {
                "query": "SELECT service_name, COUNT(*) as cnt FROM logs.events GROUP BY service_name HAVING cnt > 100",
                "target_kind": "clickhouse_cluster",
            },
        }
        params = client.translate_clickhouse_spec(spec)
        assert params is None  # complex aggregation → route to remote

    def test_unified_result_matches_exec_service_shape(self):
        client = QueryServiceClient()
        result = client._to_command_result(
            events=[{"id": "a1", "message": "error", "timestamp": "2026-06-05T10:00:00Z"}],
            total_count=1,
            duration_ms=45,
        )
        assert result["status"] == "completed"
        assert result["exit_code"] == 0
        assert "events" in result["stdout"] or result["total_count"] == 1
        assert "duration_ms" in result
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ai-service && python -m pytest tests/test_query_client.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'ai.agent_runtime.query_client'`

- [ ] **Step 3: Write the `QueryServiceClient` implementation**

Create `ai-service/ai/agent_runtime/query_client.py`:

```python
"""
Query-service HTTP client for local log lookups.

Routes simple ClickHouse log queries to query-service /api/v1/logs
instead of going through kubectl exec into ClickHouse pods.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any, Dict, List, Optional

import requests


class QueryServiceClientError(RuntimeError):
    """Raised when query-service returns an invalid or failed response."""


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


_SIMPLE_SELECT_RE = re.compile(
    r"^\s*SELECT\s+(?!.*\bGROUP\s+BY\b)(?!.*\bJOIN\b)(?!.*\bUNION\b)(?!.*\bHAVING\b)"
    r"(?!.*\bOVER\s*\()"
    r".*?\bFROM\s+logs\.events\b",
    re.IGNORECASE | re.DOTALL,
)

_CONDITION_RE = re.compile(
    r"(?:WHERE|AND)\s+(\w+)\s*=\s*'([^']*)'",
    re.IGNORECASE,
)

_LIMIT_RE = re.compile(r"LIMIT\s+(\d+)", re.IGNORECASE)
_ORDER_BY_RE = re.compile(r"ORDER\s+BY\s+(\w+)\s*(DESC|ASC)?", re.IGNORECASE)


class QueryServiceClient:
    """Calls query-service /api/v1/logs for local log lookups.

    Translates simple ClickHouse SELECT queries from ``kubectl_clickhouse_query``
    command_specs into query-service filter parameters, avoiding the overhead
    of kubectl exec into ClickHouse pods.
    """

    def __init__(self, base_url: str | None = None):
        self._base_url = (base_url or os.getenv("QUERY_SERVICE_BASE_URL", "http://query-service:8092")).rstrip("/")

    # ── public API ──────────────────────────────────────────────────────────

    def translate_clickhouse_spec(self, command_spec: Dict[str, Any]) -> Dict[str, Any] | None:
        """Try to translate a kubectl_clickhouse_query spec to query-service params.

        Returns None if the SQL is too complex for query-service (needs remote).
        """
        args = command_spec.get("args") if isinstance(command_spec.get("args"), dict) else {}
        query = _as_str(args.get("query") or command_spec.get("query"))
        if not query:
            return None

        if not _SIMPLE_SELECT_RE.search(query):
            return None

        params: Dict[str, Any] = {}

        # Extract WHERE conditions
        for match in _CONDITION_RE.finditer(query):
            col = match.group(1).lower()
            val = match.group(2)
            if col == "service_name":
                params["service_name"] = val
            elif col == "namespace":
                params["namespace"] = val
            elif col == "pod_name":
                params["pod_name"] = val
            elif col == "trace_id":
                params["trace_id"] = val
            elif col == "level" or col == "level_norm":
                params["level"] = val.upper()
            elif col == "container_name":
                params["container_name"] = val

        # Extract LIMIT
        limit_match = _LIMIT_RE.search(query)
        if limit_match:
            params["limit"] = int(limit_match.group(1))

        # Extract ORDER BY for cursor direction
        order_match = _ORDER_BY_RE.search(query)
        if order_match:
            params["order_by"] = order_match.group(1)
            if order_match.group(2):
                params["order_dir"] = order_match.group(2).upper()

        return params if len(params) > 1 else None  # need more than just limit

    async def query_logs(
        self,
        *,
        service_name: str | None = None,
        namespace: str | None = None,
        pod_name: str | None = None,
        trace_id: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        level: str | None = None,
        search: str | None = None,
        container_name: str | None = None,
        limit: int = 200,
        timeout_seconds: int = 30,
    ) -> Dict[str, Any]:
        """Query logs via query-service /api/v1/logs.

        Returns a dict matching exec-service's command result shape so callers
        don't care which channel was used.
        """
        params = self._build_log_params(
            service_name=service_name,
            namespace=namespace,
            pod_name=pod_name,
            trace_id=trace_id,
            start_time=start_time,
            end_time=end_time,
            level=level,
            search=search,
            container_name=container_name,
            limit=limit,
        )
        started_at = time.monotonic()
        result = await self._request("GET", "/api/v1/logs", params=params, timeout_seconds=timeout_seconds)
        duration_ms = int((time.monotonic() - started_at) * 1000)

        events = result.get("events") if isinstance(result, dict) else []
        if not isinstance(events, list):
            events = []
        total_count = _as_int(result.get("total") or result.get("count") or len(events))

        return self._to_command_result(
            events=events,
            total_count=total_count,
            duration_ms=duration_ms,
        )

    # ── internal helpers ────────────────────────────────────────────────────

    def _build_log_params(
        self,
        *,
        service_name: str | None = None,
        namespace: str | None = None,
        pod_name: str | None = None,
        trace_id: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        level: str | None = None,
        search: str | None = None,
        container_name: str | None = None,
        limit: int = 200,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": max(1, min(int(limit or 200), 1000))}
        for key, val in (
            ("service_name", service_name),
            ("namespace", namespace),
            ("pod_name", pod_name),
            ("trace_id", trace_id),
            ("start_time", start_time),
            ("end_time", end_time),
            ("container_name", container_name),
        ):
            text = _as_str(val).strip()
            if text:
                params[key] = text
        level_text = _as_str(level).strip().upper()
        if level_text in {"TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"}:
            params["level"] = level_text
        search_text = _as_str(search).strip()
        if search_text:
            params["search"] = search_text
        return params

    def _to_command_result(
        self,
        *,
        events: List[Dict[str, Any]],
        total_count: int,
        duration_ms: int,
    ) -> Dict[str, Any]:
        return {
            "status": "completed",
            "exit_code": 0,
            "stdout": json.dumps({"events": events, "total_count": total_count}, ensure_ascii=False),
            "stderr": "",
            "duration_ms": duration_ms,
            "total_count": total_count,
            "output_truncated": len(events) < total_count,
            "command_type": "query",
            "risk_level": "low",
            "command_family": "clickhouse",
            "executor_type": "query_service",
            "executor_profile": "query-service-readonly",
            "target_kind": "clickhouse_cluster",
            "target_identity": "database:logs",
            "timed_out": False,
            "error_code": "",
            "error_detail": "",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Dict[str, Any] | None = None,
        timeout_seconds: int = 30,
    ) -> Dict[str, Any]:
        endpoint = f"{self._base_url}{path}"

        def _do_request() -> Dict[str, Any]:
            try:
                response = requests.request(
                    method=method.upper(),
                    url=endpoint,
                    params=params if isinstance(params, dict) else None,
                    timeout=(3, max(3, int(timeout_seconds))),
                )
            except Exception as exc:
                raise QueryServiceClientError(f"query-service unavailable: {exc}") from exc

            if int(response.status_code) >= 400:
                raise QueryServiceClientError(
                    f"query-service request failed status={response.status_code} path={path}"
                )
            try:
                body = response.json()
            except Exception:
                return {}
            return body if isinstance(body, dict) else {}

        if os.environ.get("PYTEST_CURRENT_TEST") is not None:
            return _do_request()
        return await asyncio.to_thread(_do_request)


__all__ = [
    "QueryServiceClient",
    "QueryServiceClientError",
]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd ai-service && python -m pytest tests/test_query_client.py -v
```
Expected: all 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add ai-service/ai/agent_runtime/query_client.py ai-service/tests/test_query_client.py
git commit -m "feat: add QueryServiceClient for local log lookups via query-service

Translates simple ClickHouse SELECT queries to query-service /api/v1/logs
filter parameters. Complex SQL (JOIN/UNION/GROUP BY/HAVING/window functions)
returns None to signal fallback to remote exec channel.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: CommandRouter — new module

**Files:**
- Create: `ai-service/ai/agent_runtime/command_router.py`
- Create: `ai-service/tests/test_command_router.py`

- [ ] **Step 1: Write the failing test**

Create `ai-service/tests/test_command_router.py`:

```python
"""Tests for CommandRouter."""
from __future__ import annotations

import pytest
from ai.agent_runtime.command_router import CommandRouter


class TestCommandRouter:
    @pytest.fixture
    def router(self):
        return CommandRouter()

    def test_routes_simple_clickhouse_select_to_local(self, router):
        spec = {
            "tool": "kubectl_clickhouse_query",
            "args": {
                "query": "SELECT * FROM logs.events WHERE service_name='api' LIMIT 10",
                "target_kind": "clickhouse_cluster",
            },
        }
        channel, reason = router.route(spec)
        assert channel == "local"
        assert "simple" in reason.lower() or "query-service" in reason.lower()

    def test_routes_complex_clickhouse_to_remote(self, router):
        spec = {
            "tool": "kubectl_clickhouse_query",
            "args": {
                "query": "SELECT service_name, COUNT(*) FROM logs.events GROUP BY service_name",
                "target_kind": "clickhouse_cluster",
            },
        }
        channel, reason = router.route(spec)
        assert channel == "remote"

    def test_routes_kubectl_to_remote(self, router):
        spec = {
            "tool": "generic_exec",
            "args": {
                "command": "kubectl logs some-pod -n islap --tail=50",
                "target_kind": "k8s_cluster",
            },
        }
        channel, reason = router.route(spec)
        assert channel == "remote"

    def test_routes_shell_to_remote(self, router):
        spec = {
            "tool": "generic_exec",
            "args": {
                "command": "cat /etc/hosts",
                "target_kind": "runtime_workspace",
            },
        }
        channel, reason = router.route(spec)
        assert channel == "remote"

    def test_routes_host_control_to_remote(self, router):
        spec = {
            "tool": "generic_exec",
            "args": {
                "command": "systemctl status kubelet",
                "target_kind": "host_node",
            },
        }
        channel, reason = router.route(spec)
        assert channel == "remote"

    def test_unknown_tool_defaults_to_remote(self, router):
        spec = {"tool": "unknown_tool", "args": {}}
        channel, reason = router.route(spec)
        assert channel == "remote"
        assert "unknown" in reason.lower() or "default" in reason.lower()

    def test_empty_spec_defaults_to_remote(self, router):
        channel, reason = router.route({})
        assert channel == "remote"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ai-service && python -m pytest tests/test_command_router.py -v
```
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write the `CommandRouter` implementation**

Create `ai-service/ai/agent_runtime/command_router.py`:

```python
"""
Dual-channel command router.

Routes command_specs to the appropriate execution channel:
- local: query-service API for simple log queries
- remote: exec-service for kubectl, pod exec, complex SQL, host commands
"""
from __future__ import annotations

import re
from typing import Any, Dict, Tuple


_ROUTE_LOCAL = "local"
_ROUTE_REMOTE = "remote"

_CLICKHOUSE_TOOLS = {"kubectl_clickhouse_query", "clickhouse_query", "k8s_clickhouse_query"}

_SIMPLE_SELECT_RE = re.compile(
    r"^\s*SELECT\s+(?!.*\bGROUP\s+BY\b)(?!.*\bJOIN\b)(?!.*\bUNION\b)"
    r"(?!.*\bHAVING\b)(?!.*\bOVER\s*\()"
    r".*?\bFROM\s+logs\.events\b",
    re.IGNORECASE | re.DOTALL,
)

_K8S_TOOLS = {"generic_exec"}
_K8S_FAMILIES = {"kubernetes", "helm"}

_SHELL_FAMILIES = {"shell"}

_HOST_CONTROL_FAMILIES = {"host_control"}


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


class CommandRouter:
    """Routes command_spec to the appropriate execution channel."""

    ROUTE_LOCAL = _ROUTE_LOCAL
    ROUTE_REMOTE = _ROUTE_REMOTE

    def route(self, command_spec: Dict[str, Any]) -> Tuple[str, str]:
        """Return (channel, reason) for the given command_spec.

        ``channel`` is ``"local"`` (query-service) or ``"remote"`` (exec-service).
        ``reason`` is a human-readable explanation of the routing decision.
        """
        if not isinstance(command_spec, dict) or not command_spec:
            return (_ROUTE_REMOTE, "empty or invalid command_spec, defaulting to remote")

        tool = _as_str(command_spec.get("tool")).strip().lower()
        if not tool:
            return (_ROUTE_REMOTE, "no tool specified, defaulting to remote")

        args = command_spec.get("args") if isinstance(command_spec.get("args"), dict) else {}
        query = _as_str(args.get("query") or command_spec.get("query"))

        # ClickHouse tool → check SQL complexity
        if tool in _CLICKHOUSE_TOOLS:
            if query and _SIMPLE_SELECT_RE.search(query):
                return (_ROUTE_LOCAL, "simple ClickHouse log query → query-service")
            return (_ROUTE_REMOTE, "complex ClickHouse SQL → exec-service kubectl exec")

        # All other tools go remote
        if tool in _K8S_TOOLS:
            command = _as_str(args.get("command") or command_spec.get("command")).strip().lower()
            target_kind = _as_str(args.get("target_kind") or command_spec.get("target_kind")).strip().lower()
            if target_kind in {"host_node"} or command.startswith("systemctl") or command.startswith("service "):
                return (_ROUTE_REMOTE, "host-level command → exec-service ssh-gateway")
            return (_ROUTE_REMOTE, "shell/k8s command → exec-service toolbox-gateway")

        return (_ROUTE_REMOTE, f"unknown tool '{tool}', defaulting to remote")


__all__ = ["CommandRouter"]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd ai-service && python -m pytest tests/test_command_router.py -v
```
Expected: all 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add ai-service/ai/agent_runtime/command_router.py ai-service/tests/test_command_router.py
git commit -m "feat: add CommandRouter for dual-channel command dispatch

Routes simple ClickHouse SELECT queries to query-service (local channel)
and kubectl/shell/host commands to exec-service (remote channel).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: ExecutionJournal — new module

**Files:**
- Create: `ai-service/ai/agent_runtime/execution_journal.py`
- Create: `ai-service/tests/test_execution_journal.py`

- [ ] **Step 1: Write the failing test**

Create `ai-service/tests/test_execution_journal.py`:

```python
"""Tests for ExecutionJournal."""
from __future__ import annotations

from ai.agent_runtime.execution_journal import ExecutionJournal


class TestExecutionJournal:
    def test_fingerprint_is_deterministic(self):
        journal = ExecutionJournal()
        spec_a = {
            "tool": "generic_exec",
            "args": {"command": "kubectl logs pod-abc -n islap --tail=100", "target_kind": "k8s_cluster", "target_identity": "pod:pod-abc/namespace:islap"},
        }
        spec_b = {
            "tool": "generic_exec",
            "args": {"command": "kubectl logs pod-abc -n islap --tail=100", "target_kind": "k8s_cluster", "target_identity": "pod:pod-abc/namespace:islap"},
        }
        assert journal.fingerprint(spec_a) == journal.fingerprint(spec_b)

    def test_different_pods_produce_different_fingerprints(self):
        journal = ExecutionJournal()
        spec_a = {
            "tool": "generic_exec",
            "args": {"command": "kubectl logs pod-abc", "target_kind": "k8s_cluster", "target_identity": "pod:pod-abc"},
        }
        spec_b = {
            "tool": "generic_exec",
            "args": {"command": "kubectl logs pod-xyz", "target_kind": "k8s_cluster", "target_identity": "pod:pod-xyz"},
        }
        assert journal.fingerprint(spec_a) != journal.fingerprint(spec_b)

    def test_lookup_returns_none_for_unknown_fingerprint(self):
        journal = ExecutionJournal()
        assert journal.lookup("nonexistent") is None

    def test_record_and_lookup_roundtrip(self):
        journal = ExecutionJournal()
        fp = "abc123"
        journal.record(
            fingerprint=fp,
            command="kubectl get pods",
            target_kind="k8s_cluster",
            target_identity="namespace:islap",
            exit_code=0,
            summary="found 3 pods running",
            output_preview="NAME READY STATUS\npod-1 1/1 Running",
        )
        entry = journal.lookup(fp)
        assert entry is not None
        assert entry["fingerprint"] == "abc123"
        assert entry["exit_code"] == 0
        assert entry["summary"] == "found 3 pods running"
        assert "output_truncated_preview" in entry

    def test_duplicate_record_overwrites(self):
        journal = ExecutionJournal()
        fp = "dup123"
        journal.record(fp, "cmd1", "k8s_cluster", "ns:default", 1, "failed", "error output")
        journal.record(fp, "cmd2", "k8s_cluster", "ns:default", 0, "retry ok", "good output")
        entry = journal.lookup(fp)
        assert entry["exit_code"] == 0
        assert entry["summary"] == "retry ok"

    def test_context_for_llm_formats_correctly(self):
        journal = ExecutionJournal()
        journal.record("fp1", "kubectl logs pod-a", "k8s_cluster", "pod:pod-a", 0, "3 errors found", "...")
        journal.record("fp2", "kubectl describe pod pod-a", "k8s_cluster", "pod:pod-a", 0, "OOMKilled", "...")
        context = journal.context_for_llm()
        assert "kubectl logs pod-a" in context
        assert "3 errors found" in context
        assert "OOMKilled" in context
        assert "fp1" not in context  # fingerprints are internal

    def test_empty_journal_context_is_empty_string(self):
        journal = ExecutionJournal()
        assert journal.context_for_llm() == ""
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ai-service && python -m pytest tests/test_execution_journal.py -v
```
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write the `ExecutionJournal` implementation**

Create `ai-service/ai/agent_runtime/execution_journal.py`:

```python
"""
Session-level command dedup and result memory.

Each ExecutionJournal lives for the duration of one agent run.
It prevents re-executing identical commands and provides past
results to the LLM for context-aware planning.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from ai.agent_runtime.models import utc_now_iso


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


class ExecutionJournal:
    """Session-level command dedup and result memory."""

    MAX_ENTRIES = 50

    def __init__(self, entries: List[Dict[str, Any]] | None = None):
        self._entries: Dict[str, Dict[str, Any]] = {}
        if entries:
            for entry in entries:
                fp = _as_str(entry.get("fingerprint")).strip()
                if fp:
                    self._entries[fp] = dict(entry)

    # ── public API ──────────────────────────────────────────────────────────

    def fingerprint(self, command_spec: Dict[str, Any]) -> str:
        """Compute a stable fingerprint from a command_spec.

        Hash of (tool, target_kind, target_identity, normalized command text).
        Parameter values ARE part of the fingerprint — we only skip exact duplicates.
        """
        if not isinstance(command_spec, dict):
            return hashlib.sha1(b"empty").hexdigest()[:16]

        tool = _as_str(command_spec.get("tool")).strip().lower()
        args = command_spec.get("args") if isinstance(command_spec.get("args"), dict) else {}

        command = _as_str(args.get("command") or command_spec.get("command")).strip()
        query = _as_str(args.get("query") or command_spec.get("query")).strip()
        action = command or query

        target_kind = _as_str(
            args.get("target_kind") or command_spec.get("target_kind")
        ).strip()
        target_identity = _as_str(
            args.get("target_identity") or command_spec.get("target_identity")
        ).strip()

        payload = {
            "tool": tool,
            "action": " ".join(action.split()),  # normalize whitespace
            "target_kind": target_kind,
            "target_identity": target_identity,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def lookup(self, fingerprint: str) -> Dict[str, Any] | None:
        """Return cached journal entry or None."""
        fp = _as_str(fingerprint).strip()
        if not fp:
            return None
        return self._entries.get(fp)

    def record(
        self,
        fingerprint: str,
        command: str,
        target_kind: str,
        target_identity: str,
        exit_code: int,
        summary: str,
        output_preview: str,
        *,
        channel: str = "remote",
    ) -> None:
        """Record a command execution in the journal."""
        fp = _as_str(fingerprint).strip()
        if not fp:
            return

        self._entries[fp] = {
            "fingerprint": fp,
            "command": _as_str(command).strip(),
            "target_kind": _as_str(target_kind).strip(),
            "target_identity": _as_str(target_identity).strip(),
            "executed_at": utc_now_iso(),
            "exit_code": int(exit_code or 0),
            "summary": _as_str(summary).strip(),
            "output_truncated_preview": _as_str(output_preview)[:2000],
            "channel": _as_str(channel).strip() or "remote",
        }

        # Cap entries
        if len(self._entries) > self.MAX_ENTRIES:
            sorted_entries = sorted(
                self._entries.values(),
                key=lambda e: _as_str(e.get("executed_at", "")),
            )
            self._entries = {
                e["fingerprint"]: e
                for e in sorted_entries[-self.MAX_ENTRIES:]
            }

    def context_for_llm(self, max_chars: int = 4000) -> str:
        """Build a summary of all executed commands for LLM context injection.

        Returns a compact text block listing each command and its outcome.
        """
        if not self._entries:
            return ""

        lines = ["## 已执行的诊断命令 (本次会话)", ""]
        total = 0
        for entry in sorted(
            self._entries.values(),
            key=lambda e: _as_str(e.get("executed_at", "")),
        ):
            cmd = _as_str(entry.get("command", "")).strip()
            summary = _as_str(entry.get("summary", "")).strip()
            exit_code = entry.get("exit_code", 0)
            status = "✓" if exit_code == 0 else "✗"
            line = f"- {status} `{cmd}`"
            if summary:
                line += f" — {summary}"
            if total + len(line) > max_chars:
                lines.append(f"  ... (还有 {len(self._entries) - len(lines) + 2} 条已省略)")
                break
            lines.append(line)
            total += len(line) + 1

        return "\n".join(lines)

    def to_list(self) -> List[Dict[str, Any]]:
        """Serialize entries for storage in AgentRun.summary_json."""
        return sorted(
            self._entries.values(),
            key=lambda e: _as_str(e.get("executed_at", "")),
        )

    @classmethod
    def from_summary(cls, summary_json: Dict[str, Any]) -> "ExecutionJournal":
        """Restore journal from AgentRun.summary_json."""
        entries = summary_json.get("execution_journal") if isinstance(summary_json, dict) else None
        if isinstance(entries, list):
            return cls(entries=entries)
        return cls()


__all__ = ["ExecutionJournal"]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd ai-service && python -m pytest tests/test_execution_journal.py -v
```
Expected: all 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add ai-service/ai/agent_runtime/execution_journal.py ai-service/tests/test_execution_journal.py
git commit -m "feat: add ExecutionJournal for session-level command dedup and memory

Fingerprints commands by (tool, action, target_kind, target_identity),
skips exact duplicates, and generates LLM context from prior results.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: CostPreflight — new module

**Files:**
- Create: `ai-service/ai/agent_runtime/cost_preflight.py`
- Create: `ai-service/tests/test_cost_preflight.py`

- [ ] **Step 1: Write the failing test**

Create `ai-service/tests/test_cost_preflight.py`:

```python
"""Tests for CostPreflight."""
from __future__ import annotations

import pytest
from ai.agent_runtime.cost_preflight import CostPreflight, Decision


class TestCostPreflight:
    @pytest.fixture
    def preflight(self):
        return CostPreflight()

    def test_low_cost_command_returns_auto(self, preflight):
        spec = {
            "tool": "generic_exec",
            "args": {"command": "kubectl logs pod-a -n islap --tail=100", "target_kind": "k8s_cluster"},
        }
        tracker = {"commands_executed": 1, "estimated_rows_scanned": 0, "targets_touched": {"pod": 1, "node": 0}}
        result = preflight.evaluate(spec, tracker)
        assert result.decision == Decision.AUTO

    def test_all_namespaces_triggers_block(self, preflight):
        spec = {
            "tool": "generic_exec",
            "args": {"command": "kubectl get pods -A", "target_kind": "k8s_cluster"},
        }
        tracker = {"commands_executed": 1, "estimated_rows_scanned": 0, "targets_touched": {"pod": 0, "node": 0}}
        result = preflight.evaluate(spec, tracker)
        assert result.decision == Decision.BLOCK

    def test_session_command_limit_triggers_block(self, preflight):
        spec = {
            "tool": "generic_exec",
            "args": {"command": "kubectl get pods", "target_kind": "k8s_cluster"},
        }
        tracker = {"commands_executed": 11, "estimated_rows_scanned": 0, "targets_touched": {"pod": 0, "node": 0}}
        result = preflight.evaluate(spec, tracker)
        assert result.decision == Decision.BLOCK

    def test_high_estimated_rows_triggers_block(self, preflight):
        spec = {
            "tool": "kubectl_clickhouse_query",
            "args": {"query": "SELECT * FROM logs.events WHERE timestamp > now() - INTERVAL 7 DAY", "target_kind": "clickhouse_cluster"},
        }
        tracker = {"commands_executed": 1, "estimated_rows_scanned": 0, "targets_touched": {"pod": 0, "node": 0}}
        result = preflight.evaluate(spec, tracker)
        assert result.decision == Decision.BLOCK

    def test_many_target_nodes_triggers_warn(self, preflight):
        spec = {
            "tool": "generic_exec",
            "args": {"command": "kubectl describe nodes", "target_kind": "k8s_cluster"},
        }
        tracker = {"commands_executed": 2, "estimated_rows_scanned": 0, "targets_touched": {"pod": 0, "node": 0}}
        result = preflight.evaluate(spec, tracker)
        assert result.decision in {Decision.WARN, Decision.BLOCK}

    def test_normal_pod_command_within_limits_is_auto(self, preflight):
        spec = {
            "tool": "generic_exec",
            "args": {"command": "kubectl describe pod my-pod -n islap", "target_kind": "k8s_cluster", "target_identity": "pod:my-pod/namespace:islap"},
        }
        tracker = {"commands_executed": 5, "estimated_rows_scanned": 1000, "targets_touched": {"pod": 3, "node": 1}}
        result = preflight.evaluate(spec, tracker)
        assert result.decision == Decision.AUTO
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ai-service && python -m pytest tests/test_cost_preflight.py -v
```
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write the `CostPreflight` implementation**

Create `ai-service/ai/agent_runtime/cost_preflight.py`:

```python
"""
Cost preflight gate for AI agent commands.

Estimates command cost and decides whether to auto-execute,
warn, or block pending manual approval.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict


class Decision(Enum):
    AUTO = "auto"
    WARN = "warn"
    BLOCK = "block"


@dataclass
class PreflightResult:
    decision: Decision
    reason: str = ""
    estimated_cost: Dict[str, Any] | None = None


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


_ALL_NAMESPACES_PATTERN = re.compile(r"(?:\s|^)-(?:-all-namespaces|A)(?:\s|$)")
_LARGE_TIME_WINDOW_RE = re.compile(
    r"(?:INTERVAL\s+'?\s*(\d+)\s*(?:DAY|MONTH|WEEK))",
    re.IGNORECASE,
)
_SCAN_COUNT_RE = re.compile(r"(?:COUNT|count)\(\*\)", re.IGNORECASE)


class CostPreflight:
    """Estimates command cost and decides auto/warn/block."""

    # Default thresholds (configurable via kwargs)
    DEFAULT_THRESHOLDS: Dict[str, Any] = {
        "estimated_rows": 100_000,
        "time_window_days": 1,
        "target_nodes": 3,
        "session_command_limit": 10,
        "all_namespaces_block": True,
    }

    def __init__(self, **thresholds: Any):
        self.thresholds = {**self.DEFAULT_THRESHOLDS, **thresholds}

    def evaluate(self, command_spec: Dict[str, Any], cost_tracker: Dict[str, Any]) -> PreflightResult:
        """Evaluate a command_spec against cost thresholds.

        Args:
            command_spec: Normalized command spec dict.
            cost_tracker: Session cost state from AgentRun.summary_json.cost_tracker.

        Returns:
            PreflightResult with decision and reason.
        """
        safe_spec = command_spec if isinstance(command_spec, dict) else {}
        safe_tracker = cost_tracker if isinstance(cost_tracker, dict) else {}

        args = safe_spec.get("args") if isinstance(safe_spec.get("args"), dict) else {}
        command = _as_str(args.get("command") or safe_spec.get("command")).strip()
        query = _as_str(args.get("query") or safe_spec.get("query")).strip()

        checks: list[tuple[bool, str]] = []

        # ── Session command limit ──────────────────────────────────────────
        executed = _as_int(safe_tracker.get("commands_executed"), 0)
        limit = _as_int(self.thresholds.get("session_command_limit"), 10)
        if executed >= limit:
            checks.append((True, f"会话已执行 {executed} 条命令，达到上限 {limit}"))

        # ── All-namespaces check ───────────────────────────────────────────
        if command and self.thresholds.get("all_namespaces_block"):
            if _ALL_NAMESPACES_PATTERN.search(command):
                checks.append((True, "命令包含 --all-namespaces/-A，范围过大"))

        # ── Large time window check ────────────────────────────────────────
        time_text = command + " " + query
        time_match = _LARGE_TIME_WINDOW_RE.search(time_text)
        if time_match:
            days = int(time_match.group(1))
            max_days = _as_int(self.thresholds.get("time_window_days"), 1)
            if days > max_days:
                checks.append((True, f"查询时间窗口 {days} 天超过限制 {max_days} 天"))

        # ── Full scan check ────────────────────────────────────────────────
        if query and _SCAN_COUNT_RE.search(query) and "WHERE" not in query.upper():
            checks.append((True, "全表 COUNT 扫描可能代价较高"))

        # ── Node scope check ───────────────────────────────────────────────
        if command and ("describe nodes" in command.lower() or "get nodes" in command.lower()):
            target_kind = _as_str(
                args.get("target_kind") or safe_spec.get("target_kind")
            ).strip()
            if target_kind == "k8s_cluster":
                checks.append((True, "全集群节点查询范围过大"))

        # ── Combine results ─────────────────────────────────────────────────
        if checks:
            reasons = [reason for triggered, reason in checks if triggered]
            return PreflightResult(
                decision=Decision.BLOCK,
                reason="; ".join(reasons),
                estimated_cost={"triggers": len(checks)},
            )

        return PreflightResult(
            decision=Decision.AUTO,
            reason="代价在阈值以内",
        )


__all__ = ["CostPreflight", "Decision", "PreflightResult"]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd ai-service && python -m pytest tests/test_cost_preflight.py -v
```
Expected: all 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add ai-service/ai/agent_runtime/cost_preflight.py ai-service/tests/test_cost_preflight.py
git commit -m "feat: add CostPreflight for command cost estimation and gating

Evaluates commands against thresholds (session limit, all-namespaces,
time window, full scan) and returns auto/warn/block decision.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: Integration — Wire into AgentRuntimeService

**Files:**
- Modify: `ai-service/ai/agent_runtime/service.py`
- Modify: `ai-service/ai/agent_runtime/models.py`

- [ ] **Step 1: Add new fields to `AgentRun` model**

In `ai-service/ai/agent_runtime/models.py`, update the `AgentRun` dataclass. No changes needed to the dataclass fields themselves — `summary_json` already holds arbitrary dict data. Instead, add a helper property.

After the `to_dict` method, add:

```python
    def get_execution_journal_entries(self) -> list:
        """Return execution journal entries from summary_json."""
        entries = self.summary_json.get("execution_journal") if isinstance(self.summary_json, dict) else None
        return entries if isinstance(entries, list) else []

    def get_cost_tracker(self) -> dict:
        """Return cost tracker from summary_json."""
        tracker = self.summary_json.get("cost_tracker") if isinstance(self.summary_json, dict) else None
        if isinstance(tracker, dict):
            return tracker
        return {
            "commands_executed": 0,
            "estimated_rows_scanned": 0,
            "targets_touched": {"pod": 0, "node": 0},
            "session_command_limit": 10,
        }
```

- [ ] **Step 2: Run existing model tests**

```bash
cd ai-service && python -m pytest tests/ -k "model" -v --timeout=30 2>&1 | tail -10
```
Expected: PASS (backward-compatible additions)

- [ ] **Step 3: Integrate CommandRouter, ExecutionJournal, CostPreflight into `execute_command_tool`**

In `ai-service/ai/agent_runtime/service.py`, add imports at the top:

```python
from ai.agent_runtime.command_router import CommandRouter
from ai.agent_runtime.cost_preflight import CostPreflight, Decision
from ai.agent_runtime.execution_journal import ExecutionJournal
from ai.agent_runtime.query_client import QueryServiceClient, QueryServiceClientError
```

In `AgentRuntimeService.__init__`, add after `self._pending_action_timers`:

```python
        self._command_router = CommandRouter()
        self._cost_preflight = CostPreflight()
        self._query_client = QueryServiceClient()
```

In `execute_command_tool`, between `_stage_apply_command_gates` and `_stage_check_command_idempotency`, insert a new stage call:

```python
        # Stage 2.5: Cost preflight gate
        cost_result = self._stage_cost_preflight_gate(
            run_id=run_id,
            tool_name=tool_name,
            context=context,
        )
        if cost_result is not None:
            return cost_result
```

Then add the new method after `_stage_apply_command_gates`:

```python
    def _stage_cost_preflight_gate(
        self,
        *,
        run_id: str,
        tool_name: str,
        context: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Evaluate command cost and optionally block for approval."""
        run = context["run"]
        safe_tool_call_id = context["safe_tool_call_id"]
        safe_title = context["safe_title"]
        safe_action_id = context["safe_action_id"]
        safe_purpose = context["safe_purpose"]
        safe_command = context.get("safe_command", "")
        safe_command_spec = context.get("safe_command_spec", {})

        cost_tracker = run.get_cost_tracker()
        result = self._cost_preflight.evaluate(
            safe_command_spec if isinstance(safe_command_spec, dict) else {},
            cost_tracker,
        )

        if result.decision == Decision.AUTO:
            return None

        # BLOCK or WARN → emit approval_required and wait
        approval_payload = build_approval_required_payload(
            tool_call_id=safe_tool_call_id,
            action_id=safe_action_id,
            command=safe_command,
            purpose=f"[Cost Gate] {result.reason}",
            precheck={
                "status": "confirmation_required",
                "message": result.reason,
                "command_type": "query",
                "risk_level": "medium",
                "command_family": "unknown",
                "approval_policy": "cost_gate",
                "target_kind": _as_str(context.get("target_kind", "")),
                "target_identity": _as_str(context.get("target_identity", "")),
                "requires_confirmation": True,
                "requires_elevation": False,
                "confirmation_ticket": "",
            },
        )
        self._append_approval_context(run, approval_payload)
        self._update_run_summary(
            run,
            pending_action_kind="cost_gate_approval",
            pending_action_id=safe_action_id,
        )
        self.append_event(
            run.run_id,
            event_protocol.APPROVAL_REQUIRED,
            approval_payload,
        )
        self.append_event(
            run.run_id,
            event_protocol.ACTION_WAITING_APPROVAL,
            {
                "tool_call_id": safe_tool_call_id,
                "action_id": safe_action_id,
                "command": safe_command,
                "purpose": safe_purpose,
                "reason": result.reason,
                "gate": "cost_preflight",
            },
        )
        return {
            "status": "waiting_approval",
            "tool_call_id": safe_tool_call_id,
            "run": run,
            "reason": result.reason,
        }
```

- [ ] **Step 4: Integrate ExecutionJournal into idempotency stage**

In `_stage_check_command_idempotency`, add a check against the ExecutionJournal BEFORE the existing fingerprint checks. After the `active_command_run_id` check and before the `command_run_index` lookup:

```python
        # Check ExecutionJournal for cached results with summaries
        journal = ExecutionJournal.from_summary(summary)
        journal_fp = journal.fingerprint(safe_command_spec if isinstance(safe_command_spec, dict) else {})
        cached = journal.lookup(journal_fp)
        if cached is not None:
            self.append_event(
                run.run_id,
                event_protocol.TOOL_CALL_SKIPPED_DUPLICATE,
                {
                    "tool_call_id": safe_tool_call_id,
                    "tool_name": _as_str(tool_name, "command.exec"),
                    "title": safe_title,
                    "status": "skipped_duplicate",
                    "reason_code": "journal_cache_hit",
                    "action_id": safe_action_id,
                    "command": safe_command,
                    "purpose": safe_purpose,
                    "message": f"已执行过相同命令: {_as_str(cached.get('summary')).strip()}",
                    "cached_summary": _as_str(cached.get("summary")).strip(),
                    "evidence_reuse": True,
                    "evidence_outcome": "reused",
                },
            )
            return {
                "status": "skipped_duplicate",
                "tool_call_id": safe_tool_call_id,
                "run": run,
                "cached_summary": _as_str(cached.get("summary")).strip(),
            }
```

Also in `execute_command_tool`, after the idempotency check passes (returns None), add the journal fingerprint to context:

```python
        # Add journal fingerprint to context for later recording
        context["journal_fingerprint"] = journal_fp
```

- [ ] **Step 5: Record to ExecutionJournal after successful command execution**

In `_stage_execute_command_run`, after the `bridge_exec_run_stream_to_runtime` call (or after the command completes), record to the journal. Find the section where the command run index is updated, and add:

```python
            # Record to ExecutionJournal
            journal_fp = context.get("journal_fingerprint", "")
            if journal_fp:
                journal = ExecutionJournal.from_summary(dict(run.summary_json or {}))
                exec_result = command_run if isinstance(command_run, dict) else {}
                journal.record(
                    fingerprint=journal_fp,
                    command=safe_command,
                    target_kind=target_kind,
                    target_identity=target_identity,
                    exit_code=_as_int(exec_result.get("exit_code"), 0),
                    summary=self._summarize_command_result(exec_result, safe_purpose),
                    output_preview=_as_str(exec_result.get("stdout", ""))[:2000],
                )
                self._update_run_summary(run, execution_journal=journal.to_list(), **{
                    f"cost_tracker.commands_executed": (run.get_cost_tracker().get("commands_executed", 0) + 1),
                })
```

Add a helper method:

```python
    def _summarize_command_result(self, exec_result: Dict[str, Any], purpose: str) -> str:
        """Generate a one-line summary of a command result."""
        status = _as_str(exec_result.get("status", "completed")).strip().lower()
        exit_code = _as_int(exec_result.get("exit_code"), 0)
        stdout = _as_str(exec_result.get("stdout", "")).strip()
        stderr = _as_str(exec_result.get("stderr", "")).strip()

        if status in {"failed", "cancelled", "timed_out"}:
            err = stderr or _as_str(exec_result.get("error_detail", ""))
            return f"失败 ({status}): {err[:120]}" if err else f"失败 ({status})"

        if exit_code != 0:
            return f"退出码 {exit_code}: {stderr[:120]}" if stderr else f"退出码 {exit_code}"

        # Success — extract key info from output
        lines = [l for l in stdout.split("\n") if l.strip()]
        if not lines:
            return f"完成: {purpose[:120]}"
        return f"输出 {len(lines)} 行: {lines[0][:120]}"
```

- [ ] **Step 6: Inject journal context into LLM prompts**

Find where the LLM system prompt is built for the react loop. Add the journal context. The key method is likely `_request_business_question` or wherever the prompt assembly happens. Add:

```python
        # Inject execution journal context so the LLM knows what has been checked
        journal = ExecutionJournal.from_summary(dict(run.summary_json or {}))
        journal_context = journal.context_for_llm()
        if journal_context:
            # Append to the system prompt or context
            ...
```

(Exact injection point depends on which prompt builder is used — this step requires reading the prompt assembly code at implementation time to find the right place.)

- [ ] **Step 7: Run all existing agent runtime tests**

```bash
cd ai-service && python -m pytest tests/ -k "agent_runtime or runtime" -v --timeout=30 2>&1 | tail -30
```
Expected: existing tests pass; new code is backward compatible

- [ ] **Step 8: Run the full ai-service test suite**

```bash
cd ai-service && python -m pytest tests/ -v --timeout=60 2>&1 | tail -20
```
Expected: all tests pass (including new ones from Tasks 4-7)

- [ ] **Step 9: Commit**

```bash
git add ai-service/ai/agent_runtime/service.py ai-service/ai/agent_runtime/models.py
git commit -m "feat: integrate CommandRouter, ExecutionJournal, CostPreflight into agent runtime

- CommandRouter routes simple ClickHouse queries to query-service
- ExecutionJournal prevents duplicate command execution within session
- CostPreflight gates expensive/wide-scope commands for approval
- Journal results injected into LLM context for informed planning

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 9: Frontend — Cost Gate Approval UI

**Files:**
- Modify: `frontend/src/pages/AIAnalysis.tsx` (approval card section)
- Modify: `frontend/src/features/ai-runtime/` (if approval components are extracted)

- [ ] **Step 1: Identify existing approval UI**

Run: `grep -n "approval_required\|APPROVAL_REQUIRED\|approvalRequired\|cost_gate\|waiting_approval" /root/logoscope/frontend/src/pages/AIAnalysis.tsx | head -20`

Read the surrounding code for how approval cards are rendered.

- [ ] **Step 2: Extend approval card to display cost gate reason**

At the existing approval card rendering location, check if the `approval_policy === "cost_gate"` or if the gate field exists. When present, render:

```tsx
{approvalData.gate === 'cost_gate' && (
  <div className="cost-gate-notice">
    <AlertTriangle size={16} />
    <span>代价评估拦截: {approvalData.reason || '该命令代价较高，需要手动批准'}</span>
  </div>
)}
```

- [ ] **Step 3: Handle cost gate confirm/deny events**

Reuse the existing approval confirm/deny handlers. The backend expects the same `resolve_approval` call — no new API needed.

- [ ] **Step 4: Run lint and typecheck**

```bash
cd frontend && npx tsc --noEmit && npm run lint
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/AIAnalysis.tsx
git commit -m "feat: add cost gate approval UI to AI analysis page

Displays cost preflight block reason in approval cards when
approval_policy is 'cost_gate'.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 10: End-to-End Integration Test

**Files:**
- Create: `ai-service/tests/test_metadata_driven_exec_e2e.py`

- [ ] **Step 1: Write the end-to-end integration test**

Create `ai-service/tests/test_metadata_driven_exec_e2e.py`:

```python
"""End-to-end integration tests for metadata-driven execution pipeline."""
from __future__ import annotations

import pytest
from ai.agent_runtime.command_router import CommandRouter
from ai.agent_runtime.cost_preflight import CostPreflight, Decision
from ai.agent_runtime.execution_journal import ExecutionJournal
from ai.skills.base import SkillContext


class TestMetadataDrivenE2E:
    """Integration tests covering the full pipeline from context → route → dedup → gate."""

    def test_skill_context_receives_source_target(self):
        ctx = SkillContext.from_dict({
            "service_name": "api-gateway",
            "namespace": "islap",
            "source_target": {
                "pod_name": "api-gateway-abc123",
                "namespace": "islap",
                "node_name": "node-2",
                "host_ip": "10.0.1.10",
                "container_name": "api-gateway",
                "labels": {"app": "api-gateway", "version": "v1.2"},
                "service_name": "api-gateway",
            },
        })
        assert ctx.source_target["pod_name"] == "api-gateway-abc123"
        assert ctx.source_target["namespace"] == "islap"
        assert ctx.source_target["node_name"] == "node-2"
        assert ctx.source_target["labels"]["app"] == "api-gateway"
        assert "api-gateway-abc123" in ctx.source_target_text()
        assert "node-2" in ctx.source_target_text()

    def test_router_journal_preflight_pipeline(self):
        """Simulate the full command execution pipeline."""
        router = CommandRouter()
        journal = ExecutionJournal()
        preflight = CostPreflight()

        # Step 1: Route a simple log query
        spec = {
            "tool": "kubectl_clickhouse_query",
            "args": {
                "query": "SELECT * FROM logs.events WHERE pod_name='api-gateway-abc123' LIMIT 50",
                "target_kind": "clickhouse_cluster",
                "target_identity": "database:logs",
            },
        }
        channel, reason = router.route(spec)
        assert channel == "local"

        # Step 2: Check dedup (first time → no cache)
        fp = journal.fingerprint(spec)
        assert journal.lookup(fp) is None

        # Step 3: Record execution
        journal.record(fp, "SELECT ... LIMIT 50", "clickhouse_cluster", "database:logs", 0, "找到 12 条相关日志", "...")

        # Step 4: Cost preflight should pass (small query)
        tracker = {"commands_executed": 1, "estimated_rows_scanned": 12, "targets_touched": {"pod": 0, "node": 0}}
        result = preflight.evaluate(spec, tracker)
        assert result.decision == Decision.AUTO

        # Step 5: Second attempt → dedup hit
        cached = journal.lookup(fp)
        assert cached is not None
        assert cached["summary"] == "找到 12 条相关日志"

        # Step 6: Remote command (kubectl logs on target pod)
        remote_spec = {
            "tool": "generic_exec",
            "args": {
                "command": "kubectl logs api-gateway-abc123 -n islap --tail=200",
                "target_kind": "k8s_cluster",
                "target_identity": "pod:api-gateway-abc123/namespace:islap",
            },
        }
        channel2, _ = router.route(remote_spec)
        assert channel2 == "remote"

        # Step 7: -A flag triggers cost gate block
        wide_spec = {
            "tool": "generic_exec",
            "args": {
                "command": "kubectl get pods -A",
                "target_kind": "k8s_cluster",
            },
        }
        result2 = preflight.evaluate(wide_spec, tracker)
        assert result2.decision == Decision.BLOCK

    def test_journal_llm_context_includes_source_target_info(self):
        journal = ExecutionJournal()
        journal.record("fp1", "kubectl logs api-gateway-abc123 -n islap --tail=100", "k8s_cluster", "pod:api-gateway-abc123/namespace:islap", 0, "发现 3 条连接超时错误", "2026-06-05T10:28:15Z ERROR connection timeout...")
        ctx = journal.context_for_llm()
        assert "kubectl logs api-gateway-abc123" in ctx
        assert "连接超时" in ctx
```

- [ ] **Step 2: Run the E2E test**

```bash
cd ai-service && python -m pytest tests/test_metadata_driven_exec_e2e.py -v
```
Expected: all 3 test cases PASS

- [ ] **Step 3: Commit**

```bash
git add ai-service/tests/test_metadata_driven_exec_e2e.py
git commit -m "test: add e2e integration tests for metadata-driven exec pipeline

Covers context → router → journal → preflight pipeline, verifies
source_target propagation, channel routing, dedup behavior, and
cost gate decisions.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Implementation Order Summary

| Order | Task | Dependencies |
|-------|------|-------------|
| 1 | Metadata Injection — Frontend (runtimeFollowUpContext.ts) | None |
| 2 | Metadata Injection — AIAnalysis.tsx Call Sites | Task 1 |
| 3 | Metadata Injection — SkillContext | None |
| 4 | QueryServiceClient | None |
| 5 | CommandRouter | None |
| 6 | ExecutionJournal | None |
| 7 | CostPreflight | None |
| 8 | Integration — AgentRuntimeService | Tasks 3-7 |
| 9 | Frontend — Cost Gate UI | Task 8 |
| 10 | E2E Integration Test | Tasks 3-8 |

Tasks 3-7 can run in parallel (all create independent new files).
Tasks 1-2 can run in parallel with Tasks 3-7 (frontend vs backend).
Task 8 is the integration step that wires everything together.
Tasks 9-10 are final steps.
