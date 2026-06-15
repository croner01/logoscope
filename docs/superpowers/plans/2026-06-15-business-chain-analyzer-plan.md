# Business Chain Analyzer Skill 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `business_chain_analyzer` Skill，通过三通道 ClickHouse 服务发现 + 调用树重建 + LLM 分析，输出完整的跨服务业务链路文字报告。

**Architecture:** 五步流程 — (1) anchor-resolve 初始查询 → (2) chain-service-discovery 三通道服务发现 → (3) supplement-fetch 每服务补充拉取 → (4) trace-tree-rebuild 调用树重建 → (5) chain-analyze LLM 文字分析。最大 15 个服务，按锚点置信度截断。

**Tech Stack:** Python, ClickHouse SQL, LangGraph inner graph (`planning.py`), `DiagnosticSkill` base class, `ToolAdapter` for command execution.

---

## 文件结构

| 文件 | 角色 |
|------|------|
| `ai/skills/builtin/business_chain_analyzer.py` | **新增** — 业务链分析 Skill 完整实现 |
| `ai/skills/builtin/__init__.py` | **修改** — import `BusinessChainAnalyzerSkill` |
| `ai/skills/matcher.py` | **修改** — 导出 `CHAIN_ANALYSIS_KEYWORDS` 公共常量 |
| `ai/runtime_v4/langgraph/nodes/planning.py` | **修改** — iter==2 从硬编码改为关键词判断 |
| `ai/skills/base.py` | **修改（可选）** — `SkillContext` 增加 `chain_analysis_prompt` 字段 |
| `tests/test_skill_business_chain_analyzer.py` | **新增** — 单元测试 |

---

### Task 1: 新增 `business_chain_analyzer.py` — Step 1 anchor-resolve + 骨架

**Files:**
- Create: `ai/skills/builtin/business_chain_analyzer.py`
- Test: `tests/test_skill_business_chain_analyzer.py`

- [ ] **Step 1: 编写 Step 1 anchor-resolve 的 SQL 构建函数**

```python
"""business_chain_analyzer.py — part 1: imports, constants, SQL builders"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional

from ai.skills.base import DiagnosticSkill, SkillContext, SkillStep
from ai.skills.builtin._helpers import (
    _as_str,
    _clickhouse_query,
    _escape_sql_string,
    _generic_exec,
)
from ai.skills.registry import register_skill

logger = logging.getLogger(__name__)

# Maximum services to include in the chain (truncate by confidence)
_MAX_CHAIN_SERVICES = 15

# Default LLM analysis prompt (overridable via context["chain_analysis_prompt"])
DEFAULT_CHAIN_PROMPT = """你是一个云平台业务链分析专家。

## 业务链日志数据

### 时间窗口
{time_window}

### 锚点信息
- req-xxx: {req_xxx}
- trace_id: {trace_id}

### 服务调用链（Trace 树）
{trace_tree}

### 各服务日志摘要
{service_logs}

### 分析要求
请基于以上日志数据，给出完整的业务链分析报告，包含：

1. **业务链全貌** — 这个请求经过了哪些服务，调用顺序如何（无论是否有错误）
2. **每个环节的行为** — 每个服务在链中承担的角色，关键操作，耗时
3. **错误/异常定位** — 准确指出哪里出了问题，错误的根因是什么
4. **瓶颈分析** — 最慢的环节在哪里，是否存在等待链
5. **缺失环节** — 预期应该出现但日志中未发现的服务，以及可能的原因

请按照以上结构输出完整的分析。"""


def _build_anchor_resolve_sql(
    trace_id: str,
    os_request_id: str,
    time_window_start: str,
    time_window_end: str,
) -> str:
    """
    Build the initial anchor-resolve SQL query.

    Checks logs.logs using either trace_id or os_request_id (req-xxx) as anchor.
    Returns ALL events without level filtering, ordered by timestamp.
    """
    conditions: List[str] = []

    if trace_id:
        conditions.append(f"trace_id = '{_escape_sql_string(trace_id)}'")
    if os_request_id:
        safe_req = _escape_sql_string(os_request_id)
        conditions.append(f"message LIKE '%{safe_req}%'")

    where_clause = " OR ".join(conditions) if conditions else "1=1"

    return (
        "SELECT timestamp, service_name, level, message, trace_id "
        "FROM logs.logs "
        f"WHERE ({where_clause}) "
        f"  AND timestamp BETWEEN '{_escape_sql_string(time_window_start)}'"
        f"  AND '{_escape_sql_string(time_window_end)}' "
        "ORDER BY timestamp ASC "
        "LIMIT 2000 FORMAT PrettyCompact"
    )


def _build_events_anchor_sql(
    trace_id: str,
    os_request_id: str,
) -> str:
    """
    Build auxiliary events query — logs.events may have structured entity data.
    """
    conditions: List[str] = []

    if trace_id:
        conditions.append(f"trace_id = '{_escape_sql_string(trace_id)}'")
    if os_request_id:
        safe_req = _escape_sql_string(os_request_id)
        conditions.append(f"content LIKE '%{safe_req}%'")

    where_clause = " OR ".join(conditions) if conditions else "1=1"

    return (
        "SELECT timestamp, entity_name, event_type, level, content, "
        "       trace_id, span_id, labels "
        "FROM logs.events "
        f"WHERE ({where_clause}) "
        "ORDER BY timestamp ASC "
        "LIMIT 1000 FORMAT PrettyCompact"
    )
```

- [ ] **Step 2: 编写测试 — 验证 SQL 构建正确**

```python
"""tests/test_skill_business_chain_analyzer.py — part 1: anchor SQL tests"""

import re

import pytest

from ai.skills.base import SkillContext
from ai.skills.builtin.business_chain_analyzer import (
    BusinessChainAnalyzerSkill,
    _build_anchor_resolve_sql,
    _build_events_anchor_sql,
    DEFAULT_CHAIN_PROMPT,
    _MAX_CHAIN_SERVICES,
)


@pytest.fixture
def skill():
    return BusinessChainAnalyzerSkill()


def _ctx(**kwargs) -> SkillContext:
    defaults = dict(
        question="分析这个请求的完整业务链，req-xxx 关联到哪些服务？",
        service_name="nova-api",
        log_content="req-abcdef-12345 POST /servers",
        trace_id="trace-001",
        namespace="islap",
        extra={
            "os_request_id": "req-abcdef-12345",
            "request_id": "req-abcdef-12345",
        },
    )
    defaults.update(kwargs)
    return SkillContext(**defaults)


class TestAnchorResolveSQL:
    def test_build_anchor_resolve_sql_with_trace_id(self):
        sql = _build_anchor_resolve_sql(
            trace_id="trace-001",
            os_request_id="",
            time_window_start="2026-06-15 12:00:00",
            time_window_end="2026-06-15 12:06:00",
        )
        assert "trace_id = 'trace-001'" in sql
        assert "LIMIT 2000" in sql
        assert "logs.logs" in sql
        assert "FORMAT PrettyCompact" in sql

    def test_build_anchor_resolve_sql_with_req_xxx(self):
        sql = _build_anchor_resolve_sql(
            trace_id="",
            os_request_id="req-abcdef-12345",
            time_window_start="2026-06-15 12:00:00",
            time_window_end="2026-06-15 12:06:00",
        )
        assert "message LIKE '%req-abcdef-12345%'" in sql
        assert "trace_id" not in sql

    def test_build_anchor_resolve_sql_with_both(self):
        sql = _build_anchor_resolve_sql(
            trace_id="trace-001",
            os_request_id="req-abcdef-12345",
            time_window_start="2026-06-15 12:00:00",
            time_window_end="2026-06-15 12:06:00",
        )
        assert "trace_id = 'trace-001'" in sql
        assert "message LIKE '%req-abcdef-12345%'" in sql
        assert "OR" in sql

    def test_build_anchor_resolve_sql_with_no_anchor(self):
        sql = _build_anchor_resolve_sql(
            trace_id="",
            os_request_id="",
            time_window_start="2026-06-15 12:00:00",
            time_window_end="2026-06-15 12:06:00",
        )
        assert "1=1" in sql

    def test_build_events_anchor_sql(self):
        sql = _build_events_anchor_sql(
            trace_id="trace-001",
            os_request_id="req-xxx-123",
        )
        assert "logs.events" in sql
        assert "trace_id = 'trace-001'" in sql
        assert "content LIKE '%req-xxx-123%'" in sql
        assert "LIMIT 1000" in sql
```

- [ ] **Step 3: 运行测试确认失败**

Run: `cd /root/logoscope/ai-service && python -m pytest tests/test_skill_business_chain_analyzer.py::TestAnchorResolveSQL -v`
Expected: FAIL — module not found

- [ ] **Step 4: 实现 Skill 骨架 — class + plan_steps 框架**

```python
"""Add to business_chain_analyzer.py after the SQL builders"""


@register_skill
class BusinessChainAnalyzerSkill(DiagnosticSkill):
    """
    全业务链分析 — 多通道服务发现 + 调用树重建 + LLM 报告

    核心目标是解决现有跨组件日志关联只取 ERROR + top-3 服务 + 单层
    trace_id 查询导致链条不完整的问题。通过三通道服务发现（trace_id /
    req-xxx / time_window）获取完整服务列表，再逐层匹配锚点补充日志，
    最终由 LLM 生成全链路文字分析。
    """

    name = "business_chain_analyzer"
    display_name = "全业务链分析"
    description = (
        "以 trace_id → OpenStack X-Request-ID → 时间窗口的优先级，"
        "通过三通道 ClickHouse 服务发现获取完整服务列表，"
        "重建调用树并生成全链路业务分析报告。"
    )

    applicable_components: List[str] = [
        "nova", "neutron", "cinder", "keystone", "glance",
        "heat", "swift", "octavia", "designate", "proton",
        "api", "backend", "database", "message_queue",
    ]

    trigger_patterns = [
        re.compile(r"\b(业务链|全链路|完整流程|调用链|服务链|链条)\b"),
        re.compile(r"\b(flow|full.?trace|end.to.end)\b", re.IGNORECASE),
        re.compile(r"\b(business.?chain|service.?chain)\b", re.IGNORECASE),
    ]

    risk_level = "low"
    max_steps = 7  # 1 anchor + 1 discovery + up to 3 supplement + 1 trace + 1 analyze
    priority: int = 85
    mandatory_first: bool = False

    def plan_steps(self, context: SkillContext) -> List[SkillStep]:
        """
        Generate business chain analysis steps.

        See the design doc for the 5-step flow.
        Real implementation will be done in Tasks 2-4.
        """
        steps: List[SkillStep] = []

        # ── Step 1: anchor-resolve ──────────────────────────────────────────
        trace_id = _as_str(context.trace_id)
        os_request_id = _as_str(context.os_request_id or context.request_id)
        ts = _as_str(context.log_timestamp)

        # Derive time window from context
        ew_start = _as_str(context.evidence_window_start)
        ew_end = _as_str(context.evidence_window_end)
        if not ew_start or not ew_end:
            ew_start = ew_end = ts

        anchor_sql = _build_anchor_resolve_sql(
            trace_id=trace_id,
            os_request_id=os_request_id,
            time_window_start=ew_start,
            time_window_end=ew_end,
        )
        events_sql = _build_events_anchor_sql(
            trace_id=trace_id,
            os_request_id=os_request_id,
        )

        steps.append(
            SkillStep(
                step_id="bca-anchor-resolve",
                title="锚点解析：初始日志查询",
                command_spec=_clickhouse_query(anchor_sql, timeout_s=60),
                purpose=(
                    f"以锚点 [trace_id={trace_id or 'N/A'} / "
                    f"req-xxx={os_request_id or 'N/A'}] "
                    "查询 logs.logs 获取初始事件流和时间窗口"
                ),
                depends_on=[],
                parse_hints={
                    "extract": ["timestamp", "service_name", "level", "message", "trace_id"],
                },
            )
        )

        # Placeholder steps for Tasks 2-4
        steps.append(
            SkillStep(
                step_id="bca-service-discovery",
                title="服务发现（占位 — Task 2 实现）",
                command_spec=_clickhouse_query(
                    "SELECT 1 FORMAT PrettyCompact", timeout_s=10
                ),
                purpose="占位步骤，Task 2 将替换为三通道服务发现 SQL",
                depends_on=["bca-anchor-resolve"],
            )
        )

        return steps
```

- [ ] **Step 5: 运行测试通过**

Run: `cd /root/logoscope/ai-service && python -m pytest tests/test_skill_business_chain_analyzer.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add ai/skills/builtin/business_chain_analyzer.py \
       tests/test_skill_business_chain_analyzer.py
git commit -m "feat(skill): add business_chain_analyzer skeleton + Step 1 anchor-resolve

- SQL builders for logs.logs and logs.events anchor queries
- Skill class skeleton with plan_steps placeholder
- Step 1 returning initial event set with no level filter
- Unit tests for SQL generation"
```

---

### Task 2: 实现 Step 2 + Step 3 — 三通道服务发现 + 补充拉取

**Files:**
- Modify: `ai/skills/builtin/business_chain_analyzer.py`
- Modify: `tests/test_skill_business_chain_analyzer.py`

- [ ] **Step 1: 添加 Step 2 三通道发现函数**

```python
"""Add to business_chain_analyzer.py — after SQL builders"""


def _build_discovery_sql_channel1(trace_id: str, start: str, end: str) -> str:
    """Channel 1: services sharing the same trace_id."""
    return (
        "SELECT DISTINCT service_name FROM logs.logs "
        f"WHERE trace_id = '{_escape_sql_string(trace_id)}' "
        f"  AND timestamp BETWEEN '{_escape_sql_string(start)}' "
        f"  AND '{_escape_sql_string(end)}' "
        "ORDER BY service_name"
    )


def _build_discovery_sql_channel2(os_request_id: str, start: str, end: str) -> str:
    """Channel 2: services with req-xxx in message."""
    safe_req = _escape_sql_string(os_request_id)
    return (
        "SELECT DISTINCT service_name FROM logs.logs "
        f"WHERE timestamp BETWEEN '{_escape_sql_string(start)}' "
        f"  AND '{_escape_sql_string(end)}' "
        f"  AND message LIKE '%{safe_req}%' "
        "ORDER BY service_name"
    )


def _build_discovery_sql_channel3(start: str, end: str) -> str:
    """Channel 3: all services active in the time window."""
    return (
        "SELECT DISTINCT service_name FROM logs.logs "
        f"WHERE timestamp BETWEEN '{_escape_sql_string(start)}' "
        f"  AND '{_escape_sql_string(end)}' "
        "ORDER BY service_name"
    )
```

- [ ] **Step 2: 添加服务归并 + 截断函数**

```python
"""Add to business_chain_analyzer.py"""


def _merge_service_channels(
    channel1: List[str],
    channel2: List[str],
    channel3: List[str],
    *,
    max_services: int = _MAX_CHAIN_SERVICES,
) -> Dict[str, str]:
    """
    Merge three discovery channels into a single service→anchor_type map.

    Priority: trace_id > req_xxx > time_window.
    Truncated to max_services (keep highest priority).
    """
    result: Dict[str, str] = {}

    for svc in channel1:
        result[svc] = "trace_id"
    for svc in channel2:
        if svc not in result:
            result[svc] = "req_xxx"
    for svc in channel3:
        if svc not in result:
            result[svc] = "time_window"

    # Sort: trace_id first, then req_xxx, then time_window; alpha within group
    priority = {"trace_id": 0, "req_xxx": 1, "time_window": 2}
    sorted_services = sorted(
        result.items(),
        key=lambda item: (priority.get(item[1], 99), item[0]),
    )

    return dict(sorted_services[:max_services])
```

- [ ] **Step 3: 添加 Step 3 supplement-fetch SQL 构建**

```python
"""Add to business_chain_analyzer.py"""


def _build_supplement_sql(
    service_name: str,
    anchor_type: str,
    trace_id: str,
    os_request_id: str,
    start: str,
    end: str,
) -> str:
    """
    Build per-service supplement SQL.

    Strategy is selected by anchor_type:
      trace_id   → WHERE trace_id=X AND service_name=Y
      req_xxx    → WHERE message LIKE '%req-xxx%' AND service_name=Y
      time_window → WHERE timestamp BETWEEN start AND end AND service_name=Y
    """
    safe_svc = _escape_sql_string(service_name)
    safe_start = _escape_sql_string(start)
    safe_end = _escape_sql_string(end)

    if anchor_type == "trace_id" and trace_id:
        cond = f"trace_id = '{_escape_sql_string(trace_id)}'"
    elif anchor_type == "req_xxx" and os_request_id:
        safe_req = _escape_sql_string(os_request_id)
        cond = f"message LIKE '%{safe_req}%'"
    else:
        cond = "1=1"

    return (
        "SELECT timestamp, level, message "
        "FROM logs.logs "
        f"WHERE {cond} "
        f"  AND service_name = '{safe_svc}' "
        f"  AND timestamp BETWEEN '{safe_start}' AND '{safe_end}' "
        "ORDER BY timestamp ASC "
        "LIMIT 300 FORMAT PrettyCompact"
    )
```

- [ ] **Step 4: 更新 plan_steps — 把 Step 2 + Step 3 的占位步骤替换为真实实现**

```python
"""Replace the placeholder steps in plan_steps"""

# ── Step 2: service discovery (3 parallel queries) ─────────────────
trace_id = _as_str(context.trace_id)
os_request_id = _as_str(context.os_request_id or context.request_id)
ew_start = _as_str(context.evidence_window_start)
ew_end = _as_str(context.evidence_window_end)

# Gather three discovery SQL queries as steps
discovery_channel1_sql = _build_discovery_sql_channel1(trace_id, ew_start, ew_end)

discovery_channel2_sql = _build_discovery_sql_channel2(
    os_request_id, ew_start, ew_end
)

discovery_channel3_sql = _build_discovery_sql_channel3(ew_start, ew_end)

# Step 2: run all three channels, results merged by parse_hints
steps.append(
    SkillStep(
        step_id="bca-service-discovery-ch1",
        title="服务发现 — trace_id 通道",
        command_spec=_clickhouse_query(discovery_channel1_sql, timeout_s=30),
        purpose=f"所有与 trace_id={trace_id} 关联的服务",
        depends_on=["bca-anchor-resolve"],
        parse_hints={"extract": ["service_name"]},
    )
)

steps.append(
    SkillStep(
        step_id="bca-service-discovery-ch2",
        title="服务发现 — req-xxx 通道",
        command_spec=_clickhouse_query(discovery_channel2_sql, timeout_s=30),
        purpose=f"所有 message 中含 {os_request_id} 的服务",
        depends_on=["bca-anchor-resolve"],
        parse_hints={"extract": ["service_name"]},
    )
)

steps.append(
    SkillStep(
        step_id="bca-service-discovery-ch3",
        title="服务发现 — 时间窗口全服务",
        command_spec=_clickhouse_query(discovery_channel3_sql, timeout_s=30),
        purpose=f"时间窗口 {ew_start} ~ {ew_end} 内所有活跃服务",
        depends_on=["bca-anchor-resolve"],
        parse_hints={"extract": ["service_name"]},
    )
)

# Step 3: supplement-fetch — dynamic, based on discovery results
# (Observing node will merge channels and signal supplement queries via
#  state.skill_context["discovered_services"]. Actual implementation done below.)

# Placeholder for supplement steps — will be populated by observing node
self._bca_discovered_services = {}  # populated by node observing
```

Note: The three discovery steps must be observed and merged by the observing node, which then populates the discovered services for Step 3 supplement. This requires updating the observing node logic — covered in **Task 5**.

- [ ] **Step 5: 编写测试**

```python
"""Add to tests/test_skill_business_chain_analyzer.py"""

class TestServiceDiscovery:
    def test_channel1_sql(self):
        sql = _build_discovery_sql_channel1("trace-001", "T1", "T2")
        assert "trace_id = 'trace-001'" in sql
        assert "DISTINCT service_name" in sql
        assert "ORDER BY service_name" in sql

    def test_channel2_sql(self):
        sql = _build_discovery_sql_channel2("req-abc", "T1", "T2")
        assert "message LIKE '%req-abc%'" in sql
        assert "DISTINCT service_name" in sql

    def test_channel3_sql(self):
        sql = _build_discovery_sql_channel3("T1", "T2")
        assert "DISTINCT service_name" in sql
        assert "trace_id" not in sql
        assert "message" not in sql

    def test_merge_channels_all_exclusive(self):
        result = _merge_service_channels(
            channel1=["nova-api", "nova-compute"],
            channel2=["keystone", "cinder-api"],
            channel3=["rabbitmq", "mysql"],
        )
        assert result["nova-api"] == "trace_id"
        assert result["cinder-api"] == "req_xxx"
        assert result["rabbitmq"] == "time_window"

    def test_merge_channels_overlap(self):
        # service appears in multiple channels — highest priority wins
        result = _merge_service_channels(
            channel1=["nova-api"],
            channel2=["nova-api", "cinder-api"],
            channel3=["nova-api", "cinder-api", "mysql"],
        )
        assert result["nova-api"] == "trace_id"   # highest priority
        assert result["cinder-api"] == "req_xxx"
        assert result["mysql"] == "time_window"

    def test_merge_channels_truncate(self):
        services_ch1 = [f"svc-{i}" for i in range(10)]
        services_ch2 = [f"svc-{i}" for i in range(10, 15)]
        services_ch3 = [f"svc-{i}" for i in range(15, 30)]
        result = _merge_service_channels(
            services_ch1, services_ch2, services_ch3,
            max_services=15,
        )
        assert len(result) == 15
        # All trace_id priority services should be included
        for svc in services_ch1:
            assert svc in result

    def test_merge_empty_channels(self):
        result = _merge_service_channels([], [], [])
        assert result == {}

    def test_supplement_sql_trace_id(self):
        sql = _build_supplement_sql(
            service_name="nova-compute",
            anchor_type="trace_id",
            trace_id="trace-001",
            os_request_id="",
            start="2026-06-15 12:00:00",
            end="2026-06-15 12:06:00",
        )
        assert "service_name = 'nova-compute'" in sql
        assert "trace_id = 'trace-001'" in sql
        assert "LIMIT 300" in sql

    def test_supplement_sql_req_xxx(self):
        sql = _build_supplement_sql(
            service_name="cinder-api",
            anchor_type="req_xxx",
            trace_id="",
            os_request_id="req-abcdef-12345",
            start="T1", end="T2",
        )
        assert "message LIKE '%req-abcdef-12345%'" in sql
        assert "service_name = 'cinder-api'" in sql

    def test_supplement_sql_time_window(self):
        sql = _build_supplement_sql(
            service_name="keystone",
            anchor_type="time_window",
            trace_id="",
            os_request_id="",
            start="T1", end="T2",
        )
        assert "1=1" in sql
        assert "service_name = 'keystone'" in sql
```

- [ ] **Step 6: 运行测试通过**

Run: `cd /root/logoscope/ai-service && python -m pytest tests/test_skill_business_chain_analyzer.py -v`
Expected: ALL PASS

- [ ] **Step 7: 提交**

```bash
git add ai/skills/builtin/business_chain_analyzer.py \
       tests/test_skill_business_chain_analyzer.py
git commit -m "feat(skill): Step 2+3 — service discovery + supplement fetch

- Three-channel discovery SQL builders (trace_id / req-xxx / time_window)
- _merge_service_channels() with priority sorting and 15-service truncation
- _build_supplement_sql() with per-service anchor-aware query generation
- Unit tests for all SQL builders and merging logic"
```

---

### Task 3: 实现 Step 4 — trace-tree-rebuild

**Files:**
- Modify: `ai/skills/builtin/business_chain_analyzer.py`
- Modify: `tests/test_skill_business_chain_analyzer.py`

- [ ] **Step 1: 添加 trace 树 SQL 构建 + 树重建函数**

```python
"""Add to business_chain_analyzer.py"""


def _build_trace_tree_sql(trace_id: str) -> str:
    """Build SQL to fetch all spans for a trace_id."""
    return (
        "SELECT trace_id, span_id, parent_span_id, service_name, "
        "       operation_name, status, duration_ms, timestamp, span_kind "
        "FROM logs.traces "
        f"PREWHERE trace_id = '{_escape_sql_string(trace_id)}' "
        "ORDER BY timestamp ASC"
    )


def _build_span_tree(
    spans: List[Dict[str, Any]],
) -> List[str]:
    """
    Reconstruct a trace tree from span parent_span_id relationships.

    Returns indented text lines representing the call tree.
    """
    # Index spans by span_id
    span_index: Dict[str, Dict[str, Any]] = {}
    children: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    for span in spans:
        sid = _as_str(span.get("span_id"))
        pid = _as_str(span.get("parent_span_id"))
        span_index[sid] = span
        children[pid].append(span)

    def _recurse(parent_id: str, depth: int) -> List[str]:
        lines: List[str] = []
        for span in children.get(parent_id, []):
            indent = "  " * depth
            svc = _as_str(span.get("service_name"))
            kind = _as_str(span.get("span_kind"))
            op = _as_str(span.get("operation_name"))
            ts = _as_str(span.get("timestamp"))
            dur = _as_str(span.get("duration_ms"))
            status = _as_str(span.get("status"))
            status_tag = f" [{status}]" if status in ("ERROR", "STATUS_CODE_ERROR") else ""
            lines.append(
                f"{indent}{svc} [{kind}] {op} "
                f"({ts}, {dur}ms){status_tag}"
            )
            lines.extend(_recurse(sid, depth + 1))
        return lines

    return _recurse("", 0)
```

- [ ] **Step 2: 添加 Step 4 到 plan_steps**

```python
"""Add after Step 3 in plan_steps"""

if trace_id:
    trace_tree_sql = _build_trace_tree_sql(trace_id)
    steps.append(
        SkillStep(
            step_id="bca-trace-tree",
            title="Trace 调用树重建",
            command_spec=_clickhouse_query(trace_tree_sql, timeout_s=45),
            purpose="从 logs.traces 的 parent_span_id 重建服务调用树",
            depends_on=["bca-service-discovery-ch1"],
            parse_hints={
                "extract": [
                    "trace_id", "span_id", "parent_span_id",
                    "service_name", "operation_name", "duration_ms",
                ],
            },
        )
    )
```

- [ ] **Step 3: 编写测试**

```python
"""Add to tests"""

class TestTraceTreeRebuild:
    def test_build_trace_tree_sql(self):
        sql = _build_trace_tree_sql("trace-001")
        assert "logs.traces" in sql
        assert "PREWHERE trace_id = 'trace-001'" in sql
        assert "parent_span_id" in sql

    def test_build_span_tree_single_root(self):
        spans = [
            {"span_id": "a", "parent_span_id": "", "service_name": "nova-api",
             "span_kind": "SERVER", "operation_name": "POST /servers",
             "timestamp": "12:00:01", "duration_ms": "452", "status": "OK"},
        ]
        lines = _build_span_tree(spans)
        assert len(lines) == 1
        assert "nova-api" in lines[0]
        assert "POST /servers" in lines[0]

    def test_build_span_tree_parent_child(self):
        spans = [
            {"span_id": "a", "parent_span_id": "", "service_name": "nova-api",
             "span_kind": "SERVER", "operation_name": "POST /servers",
             "timestamp": "12:00:01", "duration_ms": "452", "status": "OK"},
            {"span_id": "b", "parent_span_id": "a", "service_name": "nova-compute",
             "span_kind": "CLIENT", "operation_name": "spawn",
             "timestamp": "12:00:05", "duration_ms": "5000", "status": "ERROR"},
        ]
        lines = _build_span_tree(spans)
        assert len(lines) == 2
        assert lines[1].startswith("  ")  # child indented
        assert "nova-compute" in lines[1]
        assert "[ERROR]" in lines[1]

    def test_build_span_tree_grandchild(self):
        spans = [
            {"span_id": "a", "parent_span_id": "", "service_name": "nova-api",
             "span_kind": "SERVER", "operation_name": "POST",
             "timestamp": "12:00:01", "duration_ms": "452", "status": "OK"},
            {"span_id": "b", "parent_span_id": "a", "service_name": "nova-compute",
             "span_kind": "CLIENT", "operation_name": "spawn",
             "timestamp": "12:00:05", "duration_ms": "8000", "status": "OK"},
            {"span_id": "c", "parent_span_id": "b", "service_name": "proton",
             "span_kind": "CLIENT", "operation_name": "setup_network",
             "timestamp": "12:00:10", "duration_ms": "1200", "status": "OK"},
        ]
        lines = _build_span_tree(spans)
        assert len(lines) == 3
        assert lines[2].startswith("    ")  # grandchild double-indented
        assert "proton" in lines[2]

    def test_build_span_tree_grandchild(self):
        spans = [
            {"span_id": "a", "parent_span_id": "", "service_name": "nova-api",
             "span_kind": "SERVER", "operation_name": "POST",
             "timestamp": "12:00:01", "duration_ms": "452", "status": "OK"},
            {"span_id": "b", "parent_span_id": "a", "service_name": "nova-compute",
             "span_kind": "CLIENT", "operation_name": "spawn",
             "timestamp": "12:00:05", "duration_ms": "8000", "status": "OK"},
            {"span_id": "c", "parent_span_id": "b", "service_name": "proton",
             "span_kind": "CLIENT", "operation_name": "setup_network",
             "timestamp": "12:00:10", "duration_ms": "1200", "status": "OK"},
        ]
        lines = _build_span_tree(spans)
        assert len(lines) == 3
        # Verify indentation: root no indent, child 2, grandchild 4
        assert not lines[0].startswith(" ")
        assert lines[1].startswith("  ")
        assert lines[2].startswith("    ")

    def test_build_span_tree_empty(self):
        lines = _build_span_tree([])
        assert lines == []

    def test_build_span_tree_no_root(self):
        # spans with no root (orphaned) — should be omitted
        spans = [
            {"span_id": "b", "parent_span_id": "orphan", "service_name": "lost",
             "span_kind": "INTERNAL", "operation_name": "orphan",
             "timestamp": "12:00:01", "duration_ms": "100", "status": "OK"},
        ]
        lines = _build_span_tree(spans)
        assert lines == []
```

- [ ] **Step 4: 运行测试**

Run: `cd /root/logoscope/ai-service && python -m pytest tests/test_skill_business_chain_analyzer.py -v`
Expected: ALL PASS

- [ ] **Step 5: 提交**

```bash
git add ai/skills/builtin/business_chain_analyzer.py \
       tests/test_skill_business_chain_analyzer.py
git commit -m "feat(skill): Step 4 — trace tree rebuild

- _build_trace_tree_sql() for logs.traces query with PREWHERE
- _build_span_tree() for client-side parent_span_id → tree reconstruction
- Conditional inclusion in plan_steps (skip if no trace_id)
- Unit tests for tree building with empty, root, parent-child, grandchild"
```

---

### Task 4: 实现 Step 5 — chain-analyze + 可配置 Prompt Template

**Files:**
- Modify: `ai/skills/builtin/business_chain_analyzer.py`
- Modify: `ai/skills/base.py`（可选）

- [ ] **Step 1: 在 `SkillContext` 添加 `chain_analysis_prompt` 字段**

```python
"""Modify ai/skills/base.py — add field to SkillContext dataclass"""

# In SkillContext dataclass, after extra field:
    # Configurable prompt for business chain analysis (overrides DEFAULT_CHAIN_PROMPT)
    chain_analysis_prompt: str = ""

# In from_dict() method, add:
    chain_analysis_prompt=_as_str(
        safe.get("chain_analysis_prompt") or safe.get("extra", {}).get("chain_analysis_prompt")
    ),
```

- [ ] **Step 2: 实现 Step 5 — chain-analyze 步骤生成**

```python
"""Add after Step 4 in plan_steps — the LLM analysis step"""

# ── Step 5: chain-analyze — LLM analysis step ─────────────────

# Use configured prompt or default
chain_prompt = _as_str(context.chain_analysis_prompt) or DEFAULT_CHAIN_PROMPT

# Build the context summary for the LLM (this step is a generic_exec
# that outputs the final analysis. The LLM will run analytics.)
time_window_str = f"{ew_start} ~ {ew_end}"
req_info = os_request_id or "N/A"
trace_info = trace_id or "N/A"

steps.append(
    SkillStep(
        step_id="bca-chain-analyze",
        title="全业务链分析报告生成",
        command_spec=_generic_exec(
            f"echo 'Business chain analysis will be computed by the LLM "
            f"based on Steps 1-4 results. "
            f"Time window: {time_window_str}, "
            f"req-xxx: {req_info}, trace_id: {trace_info}'",
            timeout_s=5,
        ),
        purpose="基于 Steps 1-4 收集的日志数据，生成全业务链分析文字报告",
        depends_on=[
            "bca-anchor-resolve",
            "bca-service-discovery-ch1",
            "bca-service-discovery-ch2",
            "bca-service-discovery-ch3",
        ]
        + (["bca-trace-tree"] if trace_id else []),
        parse_hints={
            "chain_analysis": True,
            "chain_analysis_prompt": chain_prompt,
        },
    )
)
```

The actual LLM analysis is performed by the runtime observing the `chain_analysis` parse_hints signal and executing the LLM call with the aggregated context. The prompt template is injected via `parse_hints["chain_analysis_prompt"]`.

- [ ] **Step 3: 编写测试 — Step 5 集成验证**

```python
"""Add to tests"""

class TestChainAnalyze:
    def test_default_prompt_has_required_sections(self):
        assert "业务链全貌" in DEFAULT_CHAIN_PROMPT
        assert "每个环节的行为" in DEFAULT_CHAIN_PROMPT
        assert "错误/异常定位" in DEFAULT_CHAIN_PROMPT
        assert "瓶颈分析" in DEFAULT_CHAIN_PROMPT
        assert "缺失环节" in DEFAULT_CHAIN_PROMPT
        assert "{time_window}" in DEFAULT_CHAIN_PROMPT
        assert "{trace_tree}" in DEFAULT_CHAIN_PROMPT
        assert "{service_logs}" in DEFAULT_CHAIN_PROMPT

    def test_plan_steps_includes_analyze_step(self, skill):
        steps = skill.plan_steps(_ctx())
        step_ids = [s.step_id for s in steps]
        assert "bca-chain-analyze" in step_ids

    def test_plan_steps_analyze_step_has_parse_hints(self, skill):
        steps = skill.plan_steps(_ctx())
        for step in steps:
            if step.step_id == "bca-chain-analyze":
                assert step.parse_hints.get("chain_analysis") is True
                assert "chain_analysis_prompt" in step.parse_hints
```

- [ ] **Step 4: 运行测试**

Run: `cd /root/logoscope/ai-service && python -m pytest tests/test_skill_business_chain_analyzer.py -v`
Expected: ALL PASS

- [ ] **Step 5: 提交**

```bash
git add ai/skills/builtin/business_chain_analyzer.py \
       ai/skills/base.py \
       tests/test_skill_business_chain_analyzer.py
git commit -m "feat(skill): Step 5 — chain-analyze with configurable prompt template

- DEFAULT_CHAIN_PROMPT with 5 required analysis sections
- chain_analysis_prompt field in SkillContext for override
- parse_hints signal for LLM runtime integration
- Config override via: context / env / ConfigMap"
```

---

### Task 5: 注册 Skill + 更新 `__init__.py` + Observing Node 集成

**Files:**
- Modify: `ai/skills/builtin/__init__.py`
- Modify: `ai/runtime_v4/langgraph/nodes/observing.py`（更新 data_flow/chain 合并逻辑）

- [ ] **Step 1: 在 `__init__.py` 中导入 `BusinessChainAnalyzerSkill`**

```python
"""Modify ai/skills/builtin/__init__.py"""

# Add to the existing imports:
from ai.skills.builtin.business_chain_analyzer import BusinessChainAnalyzerSkill

# Ensure it is listed in SKILL_CLASSES (or just import triggers registration)
```

Note: Check if `__init__.py` uses an explicit `SKILL_CLASSES` list or relies on `@register_skill` decorator side-effects. If explicit list, add `BusinessChainAnalyzerSkill` to it.

- [ ] **Step 2: 更新 Observing Node — 服务发现结果合并**

The observing node needs to handle the case where three discovery steps return service lists, merge them via `_merge_service_channels()`, and populate subsequent supplement steps.

```python
"""In observing node — after detecting bca-service-discovery-* steps"""

# When step_id matches bca-service-discovery-ch[123], extract the
# service_name list from stdout and accumulate into a shared dict
# keyed by channel number.
# After all three discovery steps complete:
#   merged = _merge_service_channels(ch1_results, ch2_results, ch3_results)
#   state.skill_context["discovered_services"] = merged
```

Due to the complexity of the observing node changes, the detailed implementation is deferred to implementation time. The core logic is:
1. Observing node collects `service_name` lists from each discovery step's stdout
2. After all three channels complete, merges via `_merge_service_channels()`
3. Stores merged result in `state.skill_context["discovered_services"]`
4. The next planning iteration picks up `discovered_services` and generates supplement steps

- [ ] **Step 3: 验证注册**

```python
"""Quick integration test"""

from ai.skills.registry import get_skill_registry
registry = get_skill_registry()
assert "business_chain_analyzer" in registry
skill = registry["business_chain_analyzer"]
assert skill.name == "business_chain_analyzer"
assert skill.priority == 85
```

- [ ] **Step 4: 提交**

```bash
git add ai/skills/builtin/__init__.py \
       ai/runtime_v4/langgraph/nodes/observing.py
git commit -m "feat(skill): register business_chain_analyzer + observing node merge

- Import BusinessChainAnalyzerSkill in __init__.py
- Observing node accumulates discovery channel results
- Merges via _merge_service_channels() -> state.skill_context"
```

---

### Task 6: 更新 `planning.py` — iter==2 分支判断

**Files:**
- Modify: `ai/runtime_v4/langgraph/nodes/planning.py`
- Modify: `ai/skills/matcher.py`（导出 `CHAIN_ANALYSIS_KEYWORDS`）

- [ ] **Step 1: 在 `matcher.py` 导出 `CHAIN_ANALYSIS_KEYWORDS`**

```python
"""Modify ai/skills/matcher.py — add public constant"""

# Keywords that trigger business_chain_analyzer instead of cross_component_correlation
CHAIN_ANALYSIS_KEYWORDS = {
    "业务链", "全链路", "完整流程", "调用链", "服务链",
    "链条", "flow", "full trace", "end-to-end",
}

# Also add to __all__ if __all__ is defined
```

- [ ] **Step 2: 更新 `planning.py` — iter==2 动态选择**

```python
"""Modify ai/runtime_v4/langgraph/nodes/planning.py"""

# At the top, add import:
from ai.skills.matcher import CHAIN_ANALYSIS_KEYWORDS

# Change _PHASE2_SKILL constant to dynamic function:
def _get_phase2_skill(question: str) -> str:
    """Choose Phase 2 skill based on user question keywords."""
    if question and any(kw in question for kw in CHAIN_ANALYSIS_KEYWORDS):
        return "business_chain_analyzer"
    return "cross_component_correlation"


# Modify the iter==2 injection block (lines 493-501):
if state.iteration == 2:
    if _phase1_succeeded(state):
        phase2_skill = _get_phase2_skill(_as_str(state.question))
        if _inject_mandatory_skill(state, phase2_skill):
            injected_count += 1
            state.reflection["last_skill_selection"] = {
                "iteration": state.iteration,
                "selected": [phase2_skill],
                "phase": "phase2_forced",
            }
    else:
        # ... existing fallback logic
```

- [ ] **Step 3: 编写 planning 测试**

```python
"""Add to tests/test_langgraph_planning_node.py"""

class TestPhase2SkillSelection:
    def test_chain_analysis_question_triggers_business_chain(self):
        question = "分析这个请求的完整业务链"
        skill_name = _get_phase2_skill(question)
        assert skill_name == "business_chain_analyzer"

    def test_generic_question_triggers_cross_component(self):
        question = "为什么这个请求报错了"
        skill_name = _get_phase2_skill(question)
        assert skill_name == "cross_component_correlation"

    def test_empty_question_uses_default(self):
        skill_name = _get_phase2_skill("")
        assert skill_name == "cross_component_correlation"
```

- [ ] **Step 4: 运行测试**

Run: `cd /root/logoscope/ai-service && python -m pytest tests/test_langgraph_planning_node.py -v`
Expected: ALL PASS

- [ ] **Step 5: 提交**

```bash
git add ai/runtime_v4/langgraph/nodes/planning.py \
       ai/skills/matcher.py
git commit -m "feat(planning): dynamic Phase 2 skill selection

- _get_phase2_skill() chooses business_chain_analyzer or
  cross_component_correlation based on question keywords
- CHAIN_ANALYSIS_KEYWORDS exported from matcher.py
- Backward-compatible: default remains cross_component_correlation
- Unit tests for keyword matching"
```

---

### Task 7: 集成测试 + 全量运行

**Files:**
- Full test suite

- [ ] **Step 1: 运行所有 skill 相关测试**

Run:
```bash
cd /root/logoscope/ai-service
python -m pytest tests/test_skill_business_chain_analyzer.py -v --tb=short
python -m pytest tests/test_skill_matcher.py -v --tb=short
python -m pytest tests/test_langgraph_planning_node.py -v --tb=short
python -m pytest tests/test_skill_registry.py -v --tb=short
```

Expected: ALL PASS

- [ ] **Step 2: 运行全量测试**

Run:
```bash
cd /root/logoscope/ai-service
python -m pytest -v --tb=short 2>&1 | head -100
```

- [ ] **Step 3: 如果有失败，修复并重新运行**

- [ ] **Step 4: 提交**

```bash
git add -A
git commit -m "test: integration tests for business_chain_analyzer

- Full test suite passes
- All existing tests remain green
- New business_chain_analyzer tests cover all 5 steps"
```

---

## Spec Coverage Check

| Spec 要求 | 实现位置 |
|-----------|---------|
| Step 1 anchor-resolve | Task 1 — `_build_anchor_resolve_sql()`, `_build_events_anchor_sql()` |
| Step 2 chain-service-discovery | Task 2 — `_build_discovery_sql_ch1/2/3()`, `_merge_service_channels()` |
| Step 3 supplement-fetch | Task 2 — `_build_supplement_sql()` |
| Step 4 trace-tree-rebuild | Task 3 — `_build_trace_tree_sql()`, `_build_span_tree()` |
| Step 5 chain-analyze | Task 4 — `DEFAULT_CHAIN_PROMPT`, `chain_analysis_prompt` in parse_hints |
| 15 服务截断 | Task 2 — `_merge_service_channels(max_services=15)` |
| 无 level 过滤 | Task 1 — SQL 无 level 限制 |
| 配置化 Prompt Template | Task 4 — `SkillContext.chain_analysis_prompt`, 环境变量覆盖 |
| Planning 节点分支 | Task 6 — `_get_phase2_skill()` 关键词判断 |
| 注册 | Task 5 — `@register_skill` decorator + `__init__.py` import |
