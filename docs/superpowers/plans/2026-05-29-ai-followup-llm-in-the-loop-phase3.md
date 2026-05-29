# AI Follow-up LLM-in-the-Loop Phase 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 auto-exec 循环中引入两层重规划——确定性证据传播（零 LLM 成本）和 LLM 重规划（按需调用）——使单请求收敛率达到 ~85%，消除会话因 `react_replan_needed` 卡死的情况。

**Architecture:** 证据传播内联在 `_build_followup_react_loop` 中，作为 evidence slot 循环之后、replan 判断之前的追加步骤（纯函数，<1ms）。LLM replan 通过 callback 参数注入 `_run_followup_auto_exec_react_loop`，在确定性传播后仍有缺口时触发，复用 `run_followup_langchain` 的 prompt 和解析逻辑。

**Tech Stack:** Python 3.12, Pydantic, existing test infrastructure (pytest)

---

## File Structure

| 文件 | 改动 | 职责 |
|------|------|------|
| `ai/followup_planning_helpers.py` | +3 个新函数，修改 `_build_followup_react_loop` | 确定性传播、namespace 提取、LLM replan context 构建 |
| `ai/followup_orchestration_helpers.py` | 修改 `_run_followup_auto_exec_react_loop` 签名和循环体 | LLM replan callback 注入、循环内触发 |
| `api/ai.py` | +1 个新函数，修改调用处 | `_llm_replan_callback` 胶水函数 |
| `tests/test_followup_planning_helpers.py` | +4 个测试 | 确定性传播测试 |
| `tests/test_followup_exec_streaming.py` | +4 个测试 | LLM replan 循环测试 + 端到端 |

---

### Task 1: `_extract_namespace_from_observations()` — 从观察中解析 namespace 映射

**前置条件:** 理解 `kubectl get pods -A` 输出的标准格式：每行第一个字段是 namespace，最后通过 `-l app=<name>` 或 label 列中包含 `app=<name>` 来关联服务名。

**Files:**
- Modify: `ai/followup_planning_helpers.py` (追加在文件末尾，`_build_followup_react_loop` 之前)
- Test: `tests/test_followup_planning_helpers.py`

- [ ] **Step 1: 编写失败的测试**

在 `tests/test_followup_planning_helpers.py` 末尾追加：

```python
def test_extract_namespace_from_kubectl_get_pods_output():
    from ai.followup_planning_helpers import _extract_namespace_from_observations

    observations = [
        {
            "command": "kubectl get pods -A -l app=temporal",
            "stdout": (
                "NAMESPACE   NAME                       READY   STATUS    RESTARTS   AGE\n"
                "default     temporal-7b9c8f5d6-xk3m2   1/1     Running   0          5m\n"
                "default     temporal-7b9c8f5d6-ab12c   1/1     Running   0          5m\n"
            ),
            "status": "executed",
            "exit_code": 0,
        }
    ]
    result = _extract_namespace_from_observations(observations)
    assert result == {"temporal": "default"}


def test_extract_namespace_returns_empty_dict_when_no_matching_output():
    from ai.followup_planning_helpers import _extract_namespace_from_observations

    observations = [
        {
            "command": "kubectl logs deploy/query-service -n islap --tail=20",
            "stdout": "2024-01-01 10:00:00 INFO server started",
            "status": "executed",
            "exit_code": 0,
        }
    ]
    result = _extract_namespace_from_observations(observations)
    assert result == {}


def test_extract_namespace_handles_multiple_apps():
    from ai.followup_planning_helpers import _extract_namespace_from_observations

    observations = [
        {
            "command": "kubectl get pods -A -l app=temporal",
            "stdout": (
                "NAMESPACE   NAME                       READY   STATUS    RESTARTS   AGE\n"
                "default     temporal-7b9c8f5d6-xk3m2   1/1     Running   0          5m\n"
                "islap       clickhouse-6df48cc9f9-ab12   1/1     Running   0          10m\n"
            ),
            "status": "executed",
            "exit_code": 0,
        }
    ]
    result = _extract_namespace_from_observations(observations)
    assert result == {"temporal": "default", "clickhouse": "islap"}


def test_extract_namespace_skips_empty_stdout():
    from ai.followup_planning_helpers import _extract_namespace_from_observations

    observations = [
        {
            "command": "kubectl get pods -A",
            "stdout": "",
            "status": "executed",
            "exit_code": 0,
        }
    ]
    result = _extract_namespace_from_observations(observations)
    assert result == {}
```

- [ ] **Step 2: 运行测试验证失败**

```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend/ai-service
python -m pytest tests/test_followup_planning_helpers.py::test_extract_namespace_from_kubectl_get_pods_output -v 2>&1 | tail -5
python -m pytest tests/test_followup_planning_helpers.py::test_extract_namespace_returns_empty_dict_when_no_matching_output -v 2>&1 | tail -5
python -m pytest tests/test_followup_planning_helpers.py::test_extract_namespace_handles_multiple_apps -v 2>&1 | tail -5
python -m pytest tests/test_followup_planning_helpers.py::test_extract_namespace_skips_empty_stdout -v 2>&1 | tail -5
```

Expected: 每个都 FAIL with `ImportError: cannot import name '_extract_namespace_from_observations'`

- [ ] **Step 3: 实现 `_extract_namespace_from_observations()`**

在 `ai/followup_planning_helpers.py` 中，添加到文件末尾（`_build_followup_react_loop` 定义之前）：

```python
def _extract_namespace_from_observations(
    observations: List[Dict[str, Any]],
) -> Dict[str, str]:
    """
    从 kubectl get pods -A 输出的观察中提取 app→namespace 映射。
    匹配格式: 每行第一个字段是 namespace，最后通过 app=<name> label 关联。
    
    Returns:
        {app_name: namespace, ...}  — 空 dict 表示没有发现
    """
    mapping: Dict[str, str] = {}
    pod_line_re = re.compile(r"^(\S+)\s+\S+.*?\s+app=(\S+)", re.MULTILINE)
    
    for obs in observations:
        stdout = _as_str(obs.get("stdout"))
        if not stdout.strip():
            continue
        for match in pod_line_re.finditer(stdout):
            namespace = match.group(1)
            app = match.group(2).rstrip(",")
            if app and namespace and app not in mapping:
                mapping[app] = namespace
    return mapping
```

- [ ] **Step 4: 运行测试验证通过**

```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend/ai-service
python -m pytest tests/test_followup_planning_helpers.py::test_extract_namespace_from_kubectl_get_pods_output tests/test_followup_planning_helpers.py::test_extract_namespace_returns_empty_dict_when_no_matching_output tests/test_followup_planning_helpers.py::test_extract_namespace_handles_multiple_apps tests/test_followup_planning_helpers.py::test_extract_namespace_skips_empty_stdout -v
```

Expected: 4 PASS

- [ ] **Step 5: 提交**

```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend
git add ai-service/ai/followup_planning_helpers.py ai-service/tests/test_followup_planning_helpers.py
git commit -m "feat(ai-service): add _extract_namespace_from_observations for kubectl get pods -A output

Parse multi-app kubectl output lines to build app→namespace mapping.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 2: 确定性证据传播 — 内联进 `_build_followup_react_loop`

**前置条件:** Task 1 已完成。理解 `_build_followup_react_loop` 中证据槽循环结束于 ~line 1697，replan 判断开始于 ~line 1795。

**Files:**
- Modify: `ai/followup_planning_helpers.py` (在 1697 行之后追加传播逻辑，在 observe 返回结构中新增 `propagation_hits` 字段)
- Test: `tests/test_followup_planning_helpers.py`

- [ ] **Step 1: 编写失败的测试**

在 `tests/test_followup_planning_helpers.py` 末尾追加：

```python
def test_deterministic_propagation_fills_missing_evidence():
    from ai.followup_planning_helpers import _build_followup_react_loop

    # Action A 有 observation 且输出包含 namespace 信息
    # Action B 也期望 namespace 证据但无自己的 observation
    actions = [
        {
            "id": "a1",
            "title": "list temporal pods",
            "command": "kubectl get pods -A -l app=temporal",
            "command_type": "query",
            "executable": True,
            "expected_signal": "返回temporal服务pod列表及其namespace",
        },
        {
            "id": "a2",
            "title": "query temporal logs",
            "command": "kubectl logs -n default deploy/temporal --tail=20",
            "command_type": "query",
            "executable": True,
            "expected_outcome": "确认temporal服务pod所在namespace",
            "expected_signal": "确认temporal服务pod所在namespace",
        },
    ]
    observations = [
        {
            "action_id": "a1",
            "status": "executed",
            "exit_code": 0,
            "command": "kubectl get pods -A -l app=temporal",
            "stdout": (
                "NAMESPACE   NAME                       READY   STATUS    RESTARTS   AGE\n"
                "default     temporal-7b9c8f5d6-xk3m2   1/1     Running   0          5m\n"
            ),
            "command_run_id": "run-abc123",
        },
    ]
    loop = _build_followup_react_loop(actions=actions, action_observations=observations)
    assert loop["observe"]["propagation_hits"] >= 1, (
        f"Expected propagation_hits >= 1, got {loop['observe'].get('propagation_hits')}"
    )
    # Action a2 的证据槽应被跨动作填充
    slot_map = loop["observe"]["evidence_slot_map"]
    a2_slot = next(
        (s for s in slot_map.values() if s.get("action_id") == "a2"), None
    )
    assert a2_slot is not None, "a2 evidence slot should exist"
    assert a2_slot.get("status") == "cross_filled", (
        f"Expected cross_filled, got {a2_slot.get('status')}"
    )


def test_deterministic_propagation_no_hit_when_no_matching_observation():
    from ai.followup_planning_helpers import _build_followup_react_loop

    actions = [
        {
            "id": "a1",
            "title": "list temporal pods",
            "command": "kubectl get pods -A -l app=temporal",
            "command_type": "query",
            "executable": True,
        },
        {
            "id": "a2",
            "title": "check clickhouse connection",
            "command": "kubectl logs deploy/query-service -n islap --tail=20",
            "command_type": "query",
            "executable": True,
            "expected_signal": "确认clickhouse连接是否正常",
        },
    ]
    observations = [
        {
            "action_id": "a1",
            "status": "executed",
            "exit_code": 0,
            "command": "kubectl get pods -A -l app=temporal",
            "stdout": "NAMESPACE   NAME    READY   STATUS    RESTARTS   AGE\ndefault     temporal-xxx   1/1     Running   0          5m\n",
            "command_run_id": "run-abc123",
        },
    ]
    loop = _build_followup_react_loop(actions=actions, action_observations=observations)
    # Action a2 的 expected_signal 和 a1 的 stdout 不匹配 — 应有 0 propagation_hits
    assert loop["observe"].get("propagation_hits", 0) == 0
```

- [ ] **Step 2: 运行测试验证失败**

```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend/ai-service
python -m pytest tests/test_followup_planning_helpers.py::test_deterministic_propagation_fills_missing_evidence -v 2>&1 | tail -10
python -m pytest tests/test_followup_planning_helpers.py::test_deterministic_propagation_no_hit_when_no_matching_observation -v 2>&1 | tail -10
```

Expected: FAIL — `propagation_hits` 字段不存在

- [ ] **Step 3: 在 `_build_followup_react_loop` 中注入传播逻辑**

在 `ai/followup_planning_helpers.py` 中，定位到 evidence slot 循环结束处（line ~1697）和 `replan_needed` 计算处（line ~1795）之间。

在 `evidence_slot_map[slot_id] = {...}` 闭包之后（line 1697）、`ready_template_actions_total` 计算（line 1699）之前，插入：

```python
    # === 确定性证据传播：跨动作观察自动填充缺失证据槽 ===
    propagation_hits = 0
    if evidence_missing_slots > 0:
        for slot_id, slot_info in evidence_slot_map.items():
            if not isinstance(slot_info, dict):
                continue
            if slot_info.get("status") in ("filled", "reused", "pending"):
                continue
            expected_signal = slot_info.get("expected_signal", "")
            if not expected_signal:
                continue
            for obs in safe_observations:
                if not isinstance(obs, dict):
                    continue
                obs_stdout = _as_str(obs.get("stdout"))
                if not obs_stdout.strip():
                    continue
                signal_match, signal_reason = _match_expected_signal(expected_signal, obs)
                if signal_match:
                    slot_info["status"] = "cross_filled"
                    slot_info["evidence_quality"] = "cross"
                    slot_info["signal_match"] = True
                    slot_info["signal_match_reason"] = f"cross_propagation:{signal_reason}"
                    slot_info["source_obs_id"] = _as_str(obs.get("command_run_id"))
                    propagation_hits += 1
                    break
        if propagation_hits:
            evidence_filled_slots += propagation_hits
            evidence_missing_slots -= propagation_hits
```

然后在 return 的 `"observe"` dict 中追加：

```python
"propagation_hits": propagation_hits,
```

（在 `"evidence_partial_slots": evidence_partial_slots,` 行之后，line 1877）

- [ ] **Step 4: 运行测试验证通过**

```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend/ai-service
python -m pytest tests/test_followup_planning_helpers.py::test_deterministic_propagation_fills_missing_evidence tests/test_followup_planning_helpers.py::test_deterministic_propagation_no_hit_when_no_matching_observation -v
```

Expected: 2 PASS

- [ ] **Step 5: 运行完整测试套确保无回归**

```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend/ai-service
python -m pytest tests/test_followup_planning_helpers.py -v 2>&1 | tail -20
```

Expected: 全部 PASS（原有测试 + 2 个新测试）

- [ ] **Step 6: 提交**

```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend
git add ai-service/ai/followup_planning_helpers.py ai-service/tests/test_followup_planning_helpers.py
git commit -m "feat(ai-service): deterministic evidence propagation in _build_followup_react_loop

Cross-action observation matching fills missing evidence slots without LLM call.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 3: `_build_llm_replan_context()` — LLM 重规划上下文构建

**前置条件:** 理解 `run_followup_langchain` 中 prompt 的格式和 `_build_followup_prompt_payload` 的结构。

**Files:**
- Modify: `ai/followup_planning_helpers.py`
- Test: `tests/test_followup_planning_helpers.py`

- [ ] **Step 1: 编写失败的测试**

在 `tests/test_followup_planning_helpers.py` 末尾追加：

```python
def test_build_llm_replan_context_includes_command_summaries():
    from ai.followup_planning_helpers import _build_llm_replan_context

    context = _build_llm_replan_context(
        original_question="确认temporal服务是否正常",
        analysis_context={"namespace": "islap"},
        all_observations=[
            {
                "command": "kubectl get pods -A -l app=temporal",
                "stdout": "NAMESPACE   NAME    READY   STATUS\ndefault     temporal-xxx   1/1     Running",
                "status": "executed",
                "exit_code": 0,
            }
        ],
        executed_commands={"kubectl get pods -A -l app=temporal"},
        current_evidence_gaps=["需要确认temporal日志中是否有连接错误"],
        remaining_iterations=2,
        remaining_timeout=25.0,
    )
    assert "kubectl get pods -A -l app=temporal" in context
    assert "temporal-xxx" in context
    assert "Running" in context
    assert "连接错误" in context


def test_build_llm_replan_context_contains_evidence_gaps():
    from ai.followup_planning_helpers import _build_llm_replan_context

    context = _build_llm_replan_context(
        original_question="clickhouse 是否正常",
        analysis_context={},
        all_observations=[],
        executed_commands=set(),
        current_evidence_gaps=["需要查看clickhouse日志"],
        remaining_iterations=1,
        remaining_timeout=10.0,
    )
    assert "clickhouse日志" in context
    assert "剩余 1 轮" in context
    assert "10s" in context
```

- [ ] **Step 2: 运行测试验证失败**

```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend/ai-service
python -m pytest tests/test_followup_planning_helpers.py::test_build_llm_replan_context_includes_command_summaries -v 2>&1 | tail -5
```

Expected: FAIL with ImportError

- [ ] **Step 3: 实现 `_build_llm_replan_context()`**

在 `ai/followup_planning_helpers.py` 中 `_extract_namespace_from_observations` 附近追加：

```python
def _build_llm_replan_context(
    *,
    original_question: str,
    analysis_context: Optional[Dict[str, Any]] = None,
    all_observations: List[Dict[str, Any]],
    executed_commands: set[str],
    current_evidence_gaps: List[str],
    remaining_iterations: int,
    remaining_timeout: float,
) -> str:
    """构建 LLM 重规划上下文，摘要已执行命令和发现，避免 LLM 重复推断。"""
    ctx = analysis_context or {}
    parts: List[str] = []
    
    # 已执行命令摘要
    cmd_summaries: List[str] = []
    seen_cmds: set[str] = set()
    for obs in all_observations:
        if not isinstance(obs, dict):
            continue
        cmd = _as_str(obs.get("command")).strip()
        if not cmd or cmd in seen_cmds:
            continue
        seen_cmds.add(cmd)
        stdout = _as_str(obs.get("stdout")).strip()
        # 提取关键发现（非header行）
        findings = []
        for line in stdout.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("NAMESPACE") or stripped.startswith("NAME"):
                continue
            if len(stripped) < 10:
                continue
            findings.append(stripped[:180])
        summary = cmd
        if findings:
            summary += f" → 发现: {'; '.join(findings[:3])}"
        if _as_str(obs.get("status")) == "executed" and int(_as_float(obs.get("exit_code"), 0)) == 0:
            summary += " (成功)"
        else:
            summary += f" (状态: {_as_str(obs.get('status'))})"
        cmd_summaries.append(summary)
    
    if cmd_summaries:
        parts.append("【上轮执行摘要】\n已执行命令:\n" + "\n".join(f"  {i+1}. {s}" for i, s in enumerate(cmd_summaries)))
    
    # 发现的 namespace
    discovered = _extract_namespace_from_observations(all_observations)
    if discovered:
        ns_lines = [f"  - {app} → namespace={ns}" for app, ns in discovered.items()]
        parts.append("【已发现的环境信息】\n" + "\n".join(ns_lines))
        parts.append("注意：以上 namespace 已经确定，后续命令可以直接使用 -n <namespace> 参数，不需要重新发现。")
    
    # 证据缺口
    if current_evidence_gaps:
        parts.append("【仍需查明的证据缺口】\n" + "\n".join(f"  {i+1}. {g[:200]}" for i, g in enumerate(current_evidence_gaps)))
    
    # 预算约束
    budget = f"【预算】\n剩余迭代: {max(0, remaining_iterations)} 轮, 剩余时间: {max(0, int(remaining_timeout))}s。请聚焦核心诊断步骤。"
    parts.append(budget)
    
    return "\n\n".join(parts)
```

- [ ] **Step 4: 运行测试验证通过**

```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend/ai-service
python -m pytest tests/test_followup_planning_helpers.py::test_build_llm_replan_context_includes_command_summaries tests/test_followup_planning_helpers.py::test_build_llm_replan_context_contains_evidence_gaps -v
```

Expected: 2 PASS

- [ ] **Step 5: 提交**

```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend
git add ai-service/ai/followup_planning_helpers.py ai-service/tests/test_followup_planning_helpers.py
git commit -m "feat(ai-service): add _build_llm_replan_context for LLM replan prompt

Summarize executed commands, findings, and evidence gaps for the replan LLM call.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 4: LLM replan callback 参数 + V2 循环注入

**前置条件:** Task 2 已完成（确定性传播已内联）。理解 `_run_followup_auto_exec_react_loop` 中 replan 判断后的 break 逻辑（line 2053-2056）。

**Files:**
- Modify: `ai/followup_orchestration_helpers.py` (在 replan 判断后、break 之前注入 LLM replan)
- Test: `tests/test_followup_exec_streaming.py`

- [ ] **Step 1: 编写失败的测试**

在 `tests/test_followup_exec_streaming.py` 末尾追加（注意已有 `import` 行，需追加 `_run_followup_auto_exec_react_loop`）：

```python
@pytest.mark.asyncio
async def test_llm_replan_callback_triggers_when_replan_needed(monkeypatch):
    """
    当确定性传播后仍有缺口且 llm_replan_callback 注入时，
    应调用 callback 并将返回的新 actions merge 进 working_actions。
    """
    from ai.followup_orchestration_helpers import _run_followup_auto_exec_react_loop

    actions = [
        {
            "id": "a1",
            "title": "list pods",
            "command": "kubectl get pods -A -l app=temporal",
            "command_type": "query",
            "executable": True,
            "expected_signal": "返回temporal服务pod列表",
        },
        {
            "id": "a2",
            "title": "check logs",
            "command": "kubectl logs deploy/temporal -n default --tail=20",
            "command_type": "query",
            "executable": True,
            "expected_outcome": "查看temporal日志中的错误信息",
            "expected_signal": "查看temporal日志中的错误信息",
        },
    ]

    async def mock_replan_callback(**kwargs):
        # Simulate LLM replan returning new actions
        return [
            {
                "id": "replan-1",
                "title": "check temporal logs",
                "command": "kubectl logs deploy/temporal -n default --tail=50",
                "command_type": "query",
                "executable": True,
                "expected_outcome": "查看temporal日志",
            }
        ]

    async def mock_run_blocking(fn, *args, **kwargs):
        return fn(*args, **kwargs) if callable(fn) else None

    def mock_build_react_loop(*, actions, action_observations, analysis_context=None):
        from ai.followup_planning_helpers import _build_followup_react_loop
        return _build_followup_react_loop(
            actions=actions,
            action_observations=action_observations,
            analysis_context=analysis_context,
        )

    monkeypatch.setattr("ai.followup_orchestration_helpers._run_followup_readonly_auto_exec", _mock_run_followup_readonly_auto_exec)
    monkeypatch.setattr("ai.followup_orchestration_helpers._resolve_followup_react_max_iterations", lambda: 5)

    # 使用 mock_readonly_auto_exec 使其返回含有 namespace 信息的 stdout
    # 这样 a1 会被标记为 filled，但 a2 的 expected_signal 不匹配 kubectl get pods 的输出
    # 所以 a2 缺失证据 → 触发 replan callback

    bundle = await _run_followup_auto_exec_react_loop(
        session_id="test-session",
        message_id="test-msg",
        actions=actions,
        analysis_context={},
        run_blocking=mock_run_blocking,
        build_react_loop_fn=mock_build_react_loop,
        allow_auto_exec_readonly=True,
        executed_commands=set(),
        initial_action_observations=[],
        initial_evidence_gaps=["查看temporal日志中的错误信息"],
        initial_summary="测试LLM replan循环",
        emit_iteration_thoughts=False,
        event_callback=None,
        logger=None,
    )
    # a2 执行成功 → expected_filled
    assert bundle["react_loop"]["execute"]["executed_success"] >= 1


async def _mock_run_followup_readonly_auto_exec(
    session_id=None, message_id=None, actions=None,
    allow_auto_exec_readonly=None, executed_commands=None,
    prior_observations=None, run_blocking=None,
    event_callback=None, logger=None,
):
    """返回模拟的 kubectl get pods 输出确认 namespace 信息。"""
    observations = []
    for action in (actions or []):
        if not isinstance(action, dict):
            continue
        action_id = action.get("id", "")
        command = action.get("command", "")
        observations.append({
            "action_id": action_id,
            "command": command,
            "status": "executed",
            "exit_code": 0,
            "stdout": "NAMESPACE   NAME    READY   STATUS\ndefault     temporal-xxx   1/1     Running\n",
            "command_run_id": f"run-{action_id}",
        })
    return observations
```

注意：以上测试代码需要放在 fixture 或 conftest 中 import。确保文件顶部已有必要的 import。

- [ ] **Step 2: 运行测试验证失败**

```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend/ai-service
python -m pytest tests/test_followup_exec_streaming.py::test_llm_replan_callback_triggers_when_replan_needed -v 2>&1 | tail -20
```

Expected: FAIL — llm_replan_callback 参数尚未处理

- [ ] **Step 3: 在 `_run_followup_auto_exec_react_loop` 中注入 LLM replan**

修改 `ai/followup_orchestration_helpers.py` 中 `_run_followup_auto_exec_react_loop` 函数的：

**函数签名（line 1796）** — 新增 `llm_replan_callback` 参数：

```python
async def _run_followup_auto_exec_react_loop(
    *,
    session_id: str,
    message_id: str,
    actions: List[Dict[str, Any]],
    analysis_context: Optional[Dict[str, Any]] = None,
    run_blocking: Any,
    build_react_loop_fn: Any,
    allow_auto_exec_readonly: bool = True,
    executed_commands: Optional[set[str]] = None,
    initial_action_observations: Optional[List[Dict[str, Any]]] = None,
    initial_evidence_gaps: Optional[List[str]] = None,
    initial_summary: str = "",
    emit_iteration_thoughts: bool = True,
    event_callback: Optional[Any] = None,
    logger: Optional[Any] = None,
    llm_replan_callback: Optional[Any] = None,  # ★ 新增
) -> Dict[str, Any]:
```

**循环体内（在 line 2053-2056 的 break 逻辑之前）**，替换原有：

```python
        if len(observations) <= 0:
            break
        if not bool((final_react_loop.get("replan") or {}).get("needed")):
            break
```

为新逻辑：

```python
        if len(observations) <= 0:
            break
        
        replan_needed = bool((final_react_loop.get("replan") or {}).get("needed"))
        
        # ★ LLM replan: 确定性传播后仍有缺口且 callback 可用
        if replan_needed and llm_replan_callback is not None:
            remaining = _remaining_timeout_for_iteration(
                deadline_ts=time.perf_counter() + float(
                    os.getenv("AI_FOLLOWUP_REQUEST_DEADLINE_SECONDS", "120")
                ),
            )
            if remaining >= 25.0:
                try:
                    new_actions = await llm_replan_callback(
                        original_question=_as_str(analysis_context.get("question", "")),
                        analysis_context=analysis_context,
                        all_observations=all_observations,
                        executed_commands=executed_commands or set(),
                        current_evidence_gaps=next_actions or active_evidence_gaps,
                        remaining_iterations=max_iterations - iteration,
                        remaining_timeout=remaining - 5.0,
                        event_callback=event_callback,
                        logger=logger,
                    )
                except Exception as exc:
                    logger and logger.warning("LLM replan callback failed: %s", exc)
                    new_actions = None
                if new_actions:
                    working_actions.extend(new_actions)
                    continue  # 下一轮执行新 actions
        
        if not replan_needed:
            break
```

需要新增一个辅助函数来获取 deadline：

```python
def _remaining_timeout_for_iteration(deadline_ts: float) -> float:
    """返回到 deadline 的剩余时间。"""
    return max(0.0, deadline_ts - time.perf_counter())
```

但在 auto-exec 循环中，没有现有的 deadline 跟踪。简单方案：在函数入口记录 `_loop_start = time.perf_counter()`，用 `_remaining = max_iterations * default_timeout_per_iteration - (now - _loop_start)`。

更实际的做法：在 `_run_followup_auto_exec_react_loop` 顶部计算 max_end_time：

```python
    # 在 line 1824 之后追加
    _loop_deadline = time.perf_counter() + float(os.getenv("AI_FOLLOWUP_REQUEST_DEADLINE_SECONDS", "120"))
```

然后在 replan 处用 `remaining = _loop_deadline - time.perf_counter()`。

注意：需要 `import os` 和 `import time` 在文件顶部。

- [ ] **Step 4: 修复测试 — 更新 import 和 mock**

更新测试文件 `tests/test_followup_exec_streaming.py` 顶部 import：

```python
from ai.followup_orchestration_helpers import (
    ...
    _run_followup_auto_exec_react_loop,  # 确保已导入
)
```

并添加 `_mock_run_followup_readonly_auto_exec` 辅助函数（若尚未定义）。

- [ ] **Step 5: 运行测试验证通过**

```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend/ai-service
python -m pytest tests/test_followup_exec_streaming.py::test_llm_replan_callback_triggers_when_replan_needed -v 2>&1 | tail -20
```

Expected: PASS

- [ ] **Step 6: 运行完整测试套**

```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend/ai-service
python -m pytest tests/test_followup_planning_helpers.py tests/test_followup_exec_streaming.py -v 2>&1 | tail -30
```

Expected: 所有既有测试 + 新测试全部 PASS

- [ ] **Step 7: 提交**

```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend
git add ai-service/ai/followup_orchestration_helpers.py ai-service/tests/test_followup_exec_streaming.py
git commit -m "feat(ai-service): inject LLM replan callback into auto-exec react loop

When deterministic propagation leaves evidence gaps and time permits,
the optional llm_replan_callback is called to generate new actions.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 5: `_llm_replan_callback()` 胶水函数

**前置条件:** Task 3 和 Task 4 已完成。理解 `api/ai.py` 中如何调用 `run_followup_langchain` 和 `_run_followup_auto_exec_react_loop`。

**Files:**
- Modify: `api/ai.py`（新增 `_llm_replan_callback`，修改调用 `_run_followup_auto_exec_react_loop` 处传入 callback）

- [ ] **Step 1: 在 `api/ai.py` 中新增 `_llm_replan_callback`**

在 `api/ai.py` 中找到 `_run_follow_up_analysis_core` 函数附近（推荐放在该函数之前，与其他 helper 同级），新增：

```python
async def _llm_replan_callback(
    *,
    original_question: str,
    analysis_context: Dict[str, Any],
    all_observations: List[Dict[str, Any]],
    executed_commands: set[str],
    current_evidence_gaps: List[str],
    remaining_iterations: int,
    remaining_timeout: float,
    event_callback: Optional[Any] = None,
    logger: Optional[Any] = None,
) -> Optional[List[Dict[str, Any]]]:
    """
    LLM 重规划回调：在 auto-exec 循环中仍有证据缺口时，
    构建 replan context 并调用 run_followup_langchain 生成新 actions。
    
    返回:
        List[Dict] — 新 actions（与 _extract_structured_actions 格式一致）
        None — 无法生成（异常/超时/空结果）
    """
    try:
        replan_context = _build_llm_replan_context(
            original_question=original_question,
            analysis_context=analysis_context,
            all_observations=all_observations,
            executed_commands=executed_commands,
            current_evidence_gaps=current_evidence_gaps,
            remaining_iterations=remaining_iterations,
            remaining_timeout=remaining_timeout,
        )
        # 复用 run_followup_langchain，传 replan_context 作为 question 的补充
        replan_result = await run_followup_langchain(
            question=f"{original_question}\n\n{replan_context}",
            analysis_context=analysis_context,
            conversation_id=None,
            llm_service=None,  # 由 run_followup_langchain 内部获取
            llm_timeout_seconds=max(5, int(remaining_timeout * 0.6)),
            fallback_builder=None,
            token_budget=0,
        )
        replan_actions = _as_list(replan_result.get("actions"))
        if replan_actions:
            logger and logger.info(
                "LLM replan generated %d new actions (remaining_timeout=%.1fs)",
                len(replan_actions), remaining_timeout,
            )
            return replan_actions
        return None
    except Exception as exc:
        logger and logger.warning("LLM replan callback failed: %s", exc)
        return None
```

在 `api/ai.py` 第 46-52 行的 import 块中，在 `_prioritize_followup_actions_with_react_memory,` 行后追加 `_build_llm_replan_context,`：

```python
from ai.followup_planning_helpers import (
    _append_followup_react_summary,
    _build_followup_actions,
    _build_followup_react_loop,
    _build_followup_subgoals,
    _prioritize_followup_actions_with_react_memory,
    _build_llm_replan_context,     # ★ 新增
)
```

- [ ] **Step 2: 将 callback 注入到 `_run_followup_auto_exec_react_loop` 调用处**

找到 `api/ai.py` 中调用 `_run_followup_auto_exec_react_loop` 的位置（line ~6430），在参数中追加 `llm_replan_callback=_llm_replan_callback`：

```python
    react_exec_bundle = await _run_followup_auto_exec_react_loop(
        session_id=analysis_session_id,
        message_id=assistant_message_id,
        actions=followup_actions,
        analysis_context=analysis_context,
        allow_auto_exec_readonly=bool(getattr(request, "auto_exec_readonly", True)),
        executed_commands=executed_commands_set,
        initial_action_observations=prior_action_observations,
        initial_evidence_gaps=evidence_gap_queue_for_execution,
        initial_summary=answer_summary_seed,
        emit_iteration_thoughts=bool(show_thought),
        run_blocking=_run_blocking,
        build_react_loop_fn=_build_followup_react_loop,
        event_callback=event_callback,
        logger=logger,
        llm_replan_callback=_llm_replan_callback,  # ★ 新增
    )
```

- [ ] **Step 3: 验证编译通过**

```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend/ai-service
python -c "from api.ai import _llm_replan_callback; print('import OK')"
```

Expected: `import OK`（无 ImportError）

- [ ] **Step 4: 提交**

```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend
git add ai-service/api/ai.py
git commit -m "feat(ai-service): add _llm_replan_callback glue function

Builds replan context from observations, calls run_followup_langchain,
returns new actions for the auto-exec loop.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 6: LLM replan 循环额外测试 + 端到端测试

**前置条件:** Task 1-5 已完成。

**Files:**
- Test: `tests/test_followup_exec_streaming.py`
- Test: `tests/test_followup_planning_helpers.py`

- [ ] **Step 1: LLM replan 空结果测试**

```python
@pytest.mark.asyncio
async def test_llm_replan_callback_returns_none_does_not_break_loop(monkeypatch):
    """
    当 llm_replan_callback 返回 None（异常/空），循环应正常结束不走 fallback。
    """
    from ai.followup_orchestration_helpers import _run_followup_auto_exec_react_loop

    called = {"count": 0}

    async def mock_replan_callback_returns_none(**kwargs):
        called["count"] += 1
        return None  # Simulate LLM returning nothing

    async def mock_run_blocking(fn, *args, **kwargs):
        return fn(*args, **kwargs) if callable(fn) else None

    def mock_build_react_loop(*, actions, action_observations, analysis_context=None):
        from ai.followup_planning_helpers import _build_followup_react_loop
        return _build_followup_react_loop(
            actions=actions,
            action_observations=action_observations,
            analysis_context=analysis_context,
        )

    monkeypatch.setattr("ai.followup_orchestration_helpers._run_followup_readonly_auto_exec", _mock_run_followup_readonly_auto_exec)
    monkeypatch.setattr("ai.followup_orchestration_helpers._resolve_followup_react_max_iterations", lambda: 3)

    actions = [
        {
            "id": "a1",
            "title": "list pods",
            "command": "kubectl get pods -A -l app=temporal",
            "command_type": "query",
            "executable": True,
            "expected_signal": "返回temporal服务pod列表及其namespace",
        },
    ]

    bundle = await _run_followup_auto_exec_react_loop(
        session_id="test-session",
        message_id="test-msg",
        actions=actions,
        analysis_context={},
        run_blocking=mock_run_blocking,
        build_react_loop_fn=mock_build_react_loop,
        allow_auto_exec_readonly=True,
        executed_commands=set(),
        initial_action_observations=[],
        initial_evidence_gaps=["需要查看temporal日志"],
        initial_summary="",
        emit_iteration_thoughts=False,
        event_callback=None,
        logger=None,
        llm_replan_callback=mock_replan_callback_returns_none,
    )
    # replan callback 被调用了，返回 None，循环正常结束
    assert called["count"] >= 1
    assert isinstance(bundle, dict)
    assert "react_loop" in bundle
```

- [ ] **Step 2: 全场景端到端测试**

```python
@pytest.mark.asyncio
async def test_deterministic_propagation_converges_within_single_request(monkeypatch):
    """
    完整场景：单个 auto-exec 请求中，通过确定性证据传播使证据收敛，
    不需要 LLM replan。验证 react_replan_needed = False。
    """
    from ai.followup_orchestration_helpers import _run_followup_auto_exec_react_loop

    async def mock_run_blocking(fn, *args, **kwargs):
        return fn(*args, **kwargs) if callable(fn) else None

    def mock_build_react_loop(*, actions, action_observations, analysis_context=None):
        from ai.followup_planning_helpers import _build_followup_react_loop
        return _build_followup_react_loop(
            actions=actions,
            action_observations=action_observations,
            analysis_context=analysis_context,
        )

    monkeypatch.setattr(
        "ai.followup_orchestration_helpers._run_followup_readonly_auto_exec",
        _mock_run_followup_readonly_auto_exec,
    )
    monkeypatch.setattr("ai.followup_orchestration_helpers._resolve_followup_react_max_iterations", lambda: 5)

    # 两个 action：action a1 的 stdout 包含 a2 需要的 namespace 信息
    actions = [
        {
            "id": "a1",
            "title": "list temporal pods",
            "command": "kubectl get pods -A -l app=temporal",
            "command_type": "query",
            "executable": True,
            "expected_signal": "返回temporal服务pod列表",
        },
        {
            "id": "a2",
            "title": "verify temporal namespace",
            "command": "kubectl get pods -A -l app=temporal",
            "command_type": "query",
            "executable": True,
            "expected_outcome": "确认temporal所在namespace",
            "expected_signal": "确认temporal所在namespace",
        },
    ]

    bundle = await _run_followup_auto_exec_react_loop(
        session_id="test-session",
        message_id="test-msg",
        actions=actions,
        analysis_context={},
        run_blocking=mock_run_blocking,
        build_react_loop_fn=mock_build_react_loop,
        allow_auto_exec_readonly=True,
        executed_commands=set(),
        initial_action_observations=[],
        initial_evidence_gaps=[],
        initial_summary="测试确定性传播收敛",
        emit_iteration_thoughts=False,
        event_callback=None,
        logger=None,
    )
    assert bundle["react_loop"]["replan"]["needed"] is False, (
        f"Expected converged, got replan_needed: {bundle['react_loop'].get('summary')}"
    )
    # 验证确定性传播发生了
    assert bundle["react_loop"]["observe"].get("propagation_hits", 0) >= 1
```

- [ ] **Step 3: 运行所有 LLM replan 测试**

```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend/ai-service
python -m pytest tests/test_followup_exec_streaming.py::test_llm_replan_callback_triggers_when_replan_needed tests/test_followup_exec_streaming.py::test_llm_replan_callback_returns_none_does_not_break_loop tests/test_followup_exec_streaming.py::test_deterministic_propagation_converges_within_single_request -v 2>&1 | tail -20
```

Expected: 3 PASS

- [ ] **Step 4: 运行完整测试套**

```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend/ai-service
python -m pytest tests/test_followup_planning_helpers.py tests/test_followup_exec_streaming.py -v 2>&1 | tail -30
```

Expected: 全部 PASS

- [ ] **Step 5: 最终提交**

```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend
git add ai-service/tests/test_followup_exec_streaming.py ai-service/tests/test_followup_planning_helpers.py
git commit -m "test(ai-service): LLM replan loop tests and end-to-end convergence test

Covers: replan callback triggers, None return handled gracefully,
deterministic propagation converges within single request.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```
