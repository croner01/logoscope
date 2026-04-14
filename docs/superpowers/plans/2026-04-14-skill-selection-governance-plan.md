# Runtime Skill Selection Governance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 收紧全局 runtime skill 自动选择，避免低置信度场景误注入诊断步骤，同时保留 prompt catalog 的宽松候选发现能力。

**Architecture:** 将“候选发现”和“自动注入”拆成两条策略。`matcher` 继续提供较宽的候选集合给 prompt/catalog 使用；`agent_runtime` 与 `langgraph planning` 的自动注入则改为读取带明细的匹配结果，并要求满足更强证据门槛，例如存在正则命中而不是只靠 `component_type` 加分。这样既不牺牲诊断主动性，也不会让普通 `service/time window` 场景被误拉进专项 skill。

**Tech Stack:** Python, pytest, FastAPI runtime API, LangGraph planning flow

---

### Task 1: 固化误选回归用例

**Files:**
- Modify: `ai-service/tests/test_skill_matcher.py`
- Modify: `ai-service/tests/test_agent_runtime_api.py`
- Modify: `ai-service/tests/test_langgraph_planning_node.py`

- [ ] **Step 1: 在 matcher 测试里先写失败用例，描述“只有组件命中不应自动选中”**

```python
def test_extract_auto_selected_skills_rejects_component_only_match():
    register_skill(_NetworkSkill)
    ctx = SkillContext(
        question="service health check looks unstable",
        service_name="query-service",
        log_content="health endpoint flaps intermittently",
        component_type="service",
    )

    skills = extract_auto_selected_skills(ctx, threshold=0.35)

    assert skills == []
```

- [ ] **Step 2: 运行单测确认它先失败，证明当前实现确实存在误选问题**

Run: `python3 -m pytest --no-cov -q tests/test_skill_matcher.py -k "component_only_match"`
Expected: FAIL，提示 `extract_auto_selected_skills` 尚不存在或当前逻辑仍返回技能

- [ ] **Step 3: 在 runtime API 测试里补一个“普通 service 场景不应发出 skill_matched”用例**

```python
def test_create_ai_run_does_not_auto_select_low_confidence_service_skill(monkeypatch):
    runtime_service = _build_runtime_service()
    monkeypatch.setattr("api.ai.get_agent_runtime_service", lambda *_args, **_kwargs: runtime_service)

    async def _run():
        created = await create_ai_run(
            AIRunCreateRequest(
                session_id="sess-skill-gating-001",
                question="service health check looks unstable",
                analysis_context={"analysis_type": "log", "service_name": "query-service", "component_type": "service"},
            )
        )
        return created["run"]["run_id"]

    run_id = asyncio.run(_run())
    events = runtime_service.list_events(run_id, after_seq=0, limit=50)
    assert "skill_matched" not in [item.event_type for item in events]
```

- [ ] **Step 4: 运行 API 测试确认它先失败或暴露当前误选行为**

Run: `python3 -m pytest --no-cov -q tests/test_agent_runtime_api.py -k "low_confidence_service_skill"`
Expected: FAIL，当前代码仍会在低阈值下自动选择 skill

- [ ] **Step 5: 在 planning 节点测试里补一个“低分技能不会进入 actions”用例**

```python
def test_run_planning_skips_low_confidence_skill_pairs():
    state = _make_state(
        question="service health check looks unstable",
        skill_context={
            "question": "service health check looks unstable",
            "log_content": "health endpoint flaps intermittently",
            "component_type": "service",
            "namespace": "islap",
        },
    )

    with patch(
        "ai.runtime_v4.langgraph.nodes.planning._select_skills_by_rules",
        return_value=[],
    ):
        new_state = run_planning(state)

    assert new_state.actions == []
```

- [ ] **Step 6: 运行 planning 测试确认红灯建立完成**

Run: `python3 -m pytest --no-cov -q tests/test_langgraph_planning_node.py -k "low_confidence_skill"`
Expected: FAIL 或至少在实现前无法表达新门槛语义


### Task 2: 为匹配过程补充“可判责”的分数明细

**Files:**
- Modify: `ai-service/ai/skills/base.py`
- Modify: `ai-service/ai/skills/matcher.py`
- Test: `ai-service/tests/test_skill_matcher.py`

- [ ] **Step 1: 在 `DiagnosticSkill` 中新增返回明细的评分方法，先保留 `match_score()` 兼容旧调用**

```python
@dataclass
class SkillMatchDetails:
    pattern_hits: int
    pattern_score: float
    component_bonus: float
    total_score: float


class DiagnosticSkill(ABC):
    def match_details(self, context: SkillContext) -> SkillMatchDetails:
        text = context.combined_text().lower()
        if not text:
            return SkillMatchDetails(0, 0.0, 0.0, 0.0)

        pattern_hits = 0
        for pattern in self.trigger_patterns:
            if isinstance(pattern, str):
                if _re.search(pattern, text, _re.IGNORECASE):
                    pattern_hits += 1
            elif hasattr(pattern, "search") and pattern.search(text):
                pattern_hits += 1

        pattern_score = min(1.0, pattern_hits / max(1, len(self.trigger_patterns)))
        component_bonus = self._component_bonus(context)
        total_score = min(1.0, pattern_score + component_bonus)
        return SkillMatchDetails(pattern_hits, pattern_score, component_bonus, total_score)

    def match_score(self, context: SkillContext) -> float:
        return self.match_details(context).total_score
```

- [ ] **Step 2: 在 matcher 中新增自动注入专用入口，而不是复用宽松的 `match_skills_by_rules()`**

```python
def extract_auto_selected_skills(
    context: SkillContext,
    *,
    threshold: float = 0.35,
    max_skills: int = 3,
) -> List[DiagnosticSkill]:
    candidates: List[Tuple[DiagnosticSkill, SkillMatchDetails]] = []
    for skill in get_skill_registry().values():
        details = skill.match_details(context)
        if details.total_score < threshold:
            continue
        if details.pattern_hits <= 0:
            continue
        candidates.append((skill, details))

    candidates.sort(key=lambda pair: pair[1].total_score, reverse=True)
    return [skill for skill, _details in candidates[:max_skills]]
```

- [ ] **Step 3: 在 matcher 测试中补断言，确保“catalog 可见”与“auto-select 可执行”被正式分离**

```python
def test_catalog_can_include_low_score_skill_without_auto_selecting_it():
    register_skill(_NetworkSkill)
    ctx = SkillContext(
        question="service health check looks unstable",
        service_name="query-service",
        log_content="health endpoint flaps intermittently",
        component_type="service",
    )

    catalog = build_skill_catalog_for_prompt(ctx)
    skills = extract_auto_selected_skills(ctx, threshold=0.35)

    assert "network_skill" in catalog
    assert skills == []
```

- [ ] **Step 4: 跑 matcher 测试，确认明细分数和新选择入口都通过**

Run: `python3 -m pytest --no-cov -q tests/test_skill_matcher.py`
Expected: PASS


### Task 3: 让 runtime API 和 LangGraph planning 使用新的自动注入门槛

**Files:**
- Modify: `ai-service/ai/agent_runtime/service.py`
- Modify: `ai-service/ai/runtime_v4/langgraph/nodes/planning.py`
- Modify: `ai-service/tests/test_agent_runtime_api.py`
- Modify: `ai-service/tests/test_langgraph_planning_node.py`

- [ ] **Step 1: 将 runtime API 中的自动选择从 `extract_high_confidence_skills(..., threshold=0.1)` 改为新入口**

```python
from ai.skills.matcher import (
    build_skill_catalog_for_prompt,
    extract_auto_selected_skills,
    get_skill_selection_summary,
)

matched_skills = extract_auto_selected_skills(
    skill_ctx,
    threshold=0.35,
    max_skills=3,
)
```

- [ ] **Step 2: 将 planning 节点的 `_PLANNING_MIN_SCORE` 从裸常量改为自动注入门槛，并统一走新入口**

```python
_PLANNING_AUTOSELECT_MIN_SCORE = 0.35


def _select_skills_by_rules(state: InnerGraphState) -> List[Any]:
    from ai.skills.matcher import extract_auto_selected_skills

    context = _build_skill_context_from_state(state)
    selected = extract_auto_selected_skills(
        context,
        threshold=_PLANNING_AUTOSELECT_MIN_SCORE,
        max_skills=_PLANNING_MAX_SKILLS,
    )
    return [(skill, skill.match_score(context)) for skill in selected]
```

- [ ] **Step 3: 在 runtime 事件或 reflection 中追加最小必要的诊断说明，避免后续再出现“为什么选了它”的黑盒问题**

```python
summary_json["selected_skills"] = skill_names
summary_json["skill_selection_policy"] = "pattern_hits_required+threshold_0_35"
summary_json["skill_step_count"] = step_seq - 1
```

- [ ] **Step 4: 跑 API 与 planning 相关测试，确认低置信度技能不再被自动注入**

Run: `python3 -m pytest --no-cov -q tests/test_agent_runtime_api.py -k "skill"`
Expected: PASS

Run: `python3 -m pytest --no-cov -q tests/test_langgraph_planning_node.py`
Expected: PASS


### Task 4: 回归 observability 与现有 builtin skill 场景

**Files:**
- Modify: `ai-service/tests/test_skill_observability_read_path_latency.py`
- Modify: `ai-service/tests/test_skill_observability_log_correlation_gap.py`
- Modify: `ai-service/tests/test_skill_network_check.py`
- Modify: `ai-service/tests/test_skill_resource_usage.py`
- Test: `ai-service/tests/test_langchain_runtime_service.py`

- [ ] **Step 1: 给 observability skill 增加“仍可被真实强信号选中”的回归断言**

```python
def test_correlation_gap_still_matches_missing_trace_with_request_id(skill):
    ctx = _ctx(log_content="missing trace_id but request_id=req-001 is present")
    assert skill.match_score(ctx) > 0.0


def test_read_path_latency_still_matches_slow_query_timeout(skill):
    ctx = _ctx(log_content="clickhouse slow query timeout on logs endpoint")
    assert skill.match_score(ctx) > 0.0
```

- [ ] **Step 2: 对现有 `network_check` / `resource_usage` 保留回归，确保全局门槛调整不会误伤原有显性场景**

```python
def test_network_skill_still_matches_connection_refused():
    ctx = _ctx(log_content="connection refused ECONNREFUSED")
    assert skill.match_score(ctx) > 0.0
```

- [ ] **Step 3: 运行聚焦回归集，确认 prompt 收紧与全局 skill 收紧可以同时成立**

Run: `python3 -m pytest --no-cov -q tests/test_skill_observability_read_path_latency.py tests/test_skill_observability_log_correlation_gap.py tests/test_skill_network_check.py tests/test_skill_resource_usage.py`
Expected: PASS

Run: `python3 -m pytest --no-cov -q tests/test_langchain_runtime_service.py -k "stable_readonly_commands or fault_layer_rule_generic"`
Expected: PASS


### Task 5: 最终核验与变更说明

**Files:**
- Modify: `docs/superpowers/specs/2026-04-14-observability-skills-design.md`
- Modify: `docs/superpowers/plans/2026-04-14-observability-skills-plan.md`
- Create: `docs/superpowers/specs/2026-04-14-skill-selection-governance-design.md`

- [ ] **Step 1: 在设计文档里补一段“为什么之前会误选”的根因总结**

```markdown
根因不是单一 prompt，而是评分模型把“pattern 命中”和“component 加分”混在一起，
同时 runtime 自动选择使用了 `0.1` 的低阈值，导致普通 `service` 场景也可能进入专项 skill。
```

- [ ] **Step 2: 新增治理设计文档，固定本次策略边界**

```markdown
- prompt/catalog 发现允许宽松
- 自动注入要求 `pattern_hits > 0`
- 自动注入阈值独立于 catalog 阈值
- 选中原因需要可追踪
```

- [ ] **Step 3: 跑最终验证集合并检查工作区差异**

Run: `python3 -m pytest --no-cov -q tests/test_skill_matcher.py tests/test_langgraph_planning_node.py tests/test_agent_runtime_api.py -k "skill or knowledge_pack_version"`
Expected: PASS

Run: `git diff -- ai-service/ai/skills/base.py ai-service/ai/skills/matcher.py ai-service/ai/agent_runtime/service.py ai-service/ai/runtime_v4/langgraph/nodes/planning.py ai-service/tests/test_skill_matcher.py ai-service/tests/test_langgraph_planning_node.py ai-service/tests/test_agent_runtime_api.py`
Expected: diff 只包含 skill 选择治理相关改动

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-04-14-skill-selection-governance-design.md docs/superpowers/plans/2026-04-14-skill-selection-governance-plan.md ai-service/ai/skills/base.py ai-service/ai/skills/matcher.py ai-service/ai/agent_runtime/service.py ai-service/ai/runtime_v4/langgraph/nodes/planning.py ai-service/tests/test_skill_matcher.py ai-service/tests/test_langgraph_planning_node.py ai-service/tests/test_agent_runtime_api.py
git commit -m "fix: gate runtime skill auto-selection by evidence"
```

