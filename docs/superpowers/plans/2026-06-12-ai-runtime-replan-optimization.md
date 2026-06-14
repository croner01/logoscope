# AI 运行时 LLM 重规划鲁棒性优化 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 解决命令执行失败后 LLM 重规划无法生成后续排查动作的问题。核心在两个修复点：(1) 失败命令的 stderr 传递给 LLM 重规划上下文；(2) LLM 返回空动作列表时重试一次并附带反馈。

**Architecture:** 仅修改两个函数，不涉及核心数据流变更。`_build_llm_replan_context` 负责构建 LLM 看到的上下文（新增失败信息），`_llm_replan_callback` 负责调用 LLM 并处理结果（新增空动作重试）。

**Tech Stack:** Python 3.10, pytest

---

## 问题分析

### 根因 1：失败命令的 stderr 未传递给 LLM

**文件：** `ai-service/ai/followup_planning_helpers.py:_build_llm_replan_context`

当前代码对失败命令只输出 `(状态: executed)`，不包含 stderr。LLM 没有足够信息诊断失败原因，无法制定修正计划。

### 根因 2：空动作无重试

**文件：** `ai-service/api/ai.py:_llm_replan_callback`

当 `run_followup_langchain` 返回的 `langchain_actions` 为空列表时，直接 `return None`。不通知 LLM 为什么、不重试。

---

## 文件变更清单

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `ai-service/ai/followup_planning_helpers.py` | 修改 | `_build_llm_replan_context`：失败命令附带 stderr |
| `ai-service/api/ai.py` | 修改 | `_llm_replan_callback`：空动作时重试一次 |
| `ai-service/tests/test_followup_planning_helpers.py` | 追加测试 | 验证 stderr 包含在上下文中（3 个新测试） |

---

### Task 1: _build_llm_replan_context 附带失败 stderr

**File:** `ai-service/ai/followup_planning_helpers.py`
**Function:** `_build_llm_replan_context`
**Lines:** 2351-2354

- [ ] **Step 1: 修改失败命令状态标注逻辑**

当前代码：
```python
        if _as_str(obs.get("status")) == "executed" and int(_as_float(obs.get("exit_code"), 0)) == 0:
            summary += " (成功)"
        else:
            summary += f" (状态: {_as_str(obs.get('status'))})"
```

替换为：
```python
        if _as_str(obs.get("status")) == "executed" and int(_as_float(obs.get("exit_code"), 0)) == 0:
            summary += " (成功)"
        else:
            _stderr = _as_str(obs.get("stderr")).strip()
            _msg = _as_str(obs.get("message")).strip()
            _exit_code = int(_as_float(obs.get("exit_code"), 0))
            _error_hint = _stderr or _msg or ""
            if _error_hint:
                summary += f" (失败, exit={_exit_code}: {_error_hint[:500]})"
            else:
                summary += f" (状态: {_as_str(obs.get('status'))}, exit={_exit_code})"
```

**变更说明：**
- stderr 优先于 message（stderr 是原始错误输出，message 是内部包装文本）
- 限制 500 字符防止上下文撑爆
- `_error_hint` 为空时仍显示状态和 exit_code，不丢失信息
- 不修改函数签名，不影响现有调用者

- [ ] **Step 2: 验证单元测试**

Run:
```bash
cd ai-service && python -m pytest tests/test_followup_planning_helpers.py -x -v
```

Expected: 所有已有测试通过（行为变更仅影响失败命令的上下文文本格式，不影响上层逻辑判断）

- [ ] **Step 3: 提交**

```bash
git add ai-service/ai/followup_planning_helpers.py
git commit -m "fix: include stderr in LLM replan context for failed commands"
```

---

### Task 2: _llm_replan_callback 空动作重试

**File:** `ai-service/api/ai.py`
**Function:** `_llm_replan_callback`（`_run_follow_up_analysis_core` 内部闭包）
**Lines:** 6664-6712

- [ ] **Step 1: 重构 LLM 调用逻辑，增加空动作重试**

当前代码（第 6664-6712 行）：
```python
        replan_context = _build_llm_replan_context(
            original_question=original_question,
            analysis_context=analysis_context,
            all_observations=all_observations,
            executed_commands=executed_commands,
            current_evidence_gaps=current_evidence_gaps,
            remaining_iterations=remaining_iterations,
            remaining_timeout=remaining_timeout,
        )
        augmented_context = dict(analysis_context) if analysis_context else {}
        augmented_context["_llm_replan_context"] = replan_context
        try:
            replan_llm_service = get_llm_service()
        except Exception as exc:
            logger and logger.warning("LLM replan: failed to get llm service: %s", exc)
            return None
        try:
            replan_bundle = await asyncio.wait_for(
                run_followup_langchain(
                    question=f"[重规划] {original_question[:200]}",
                    analysis_context=augmented_context,
                    compacted_history=[],
                    compacted_summary="",
                    references=[],
                    subgoals=[],
                    reflection={},
                    long_term_memory={"enabled": False, "hits": 0, "summary": "", "items": []},
                    llm_enabled=llm_enabled,
                    llm_requested=True,
                    token_budget=min(token_budget, 4000),
                    token_warning=False,
                    llm_timeout_seconds=_replan_llm_timeout,
                    llm_first_token_timeout_seconds=20,
                    llm_service=replan_llm_service,
                    fallback_builder=lambda *args, **kwargs: _build_followup_fallback_answer(*args, **kwargs),
                    stream_token_callback=None,
                ),
                timeout=_replan_llm_timeout,
            )
        except asyncio.TimeoutError:
            logger and logger.warning("LLM replan timed out after %ss", _replan_llm_timeout)
            return None
        except Exception as exc:
            logger and logger.warning("LLM replan failed: %s", exc)
            return None
        new_actions_raw = _as_list(replan_bundle.get("langchain_actions"))
        if not new_actions_raw:
            return None
        return new_actions_raw
```

替换为：
```python
        replan_context = _build_llm_replan_context(
            original_question=original_question,
            analysis_context=analysis_context,
            all_observations=all_observations,
            executed_commands=executed_commands,
            current_evidence_gaps=current_evidence_gaps,
            remaining_iterations=remaining_iterations,
            remaining_timeout=remaining_timeout,
        )
        try:
            replan_llm_service = get_llm_service()
        except Exception as exc:
            logger and logger.warning("LLM replan: failed to get llm service: %s", exc)
            return None

        # ── 首次调用 ──────────────────────────────────────────────────
        augmented_context = dict(analysis_context) if analysis_context else {}
        augmented_context["_llm_replan_context"] = replan_context
        try:
            replan_bundle = await asyncio.wait_for(
                run_followup_langchain(
                    question=f"[重规划] {original_question[:200]}",
                    analysis_context=augmented_context,
                    compacted_history=[],
                    compacted_summary="",
                    references=[],
                    subgoals=[],
                    reflection={},
                    long_term_memory={"enabled": False, "hits": 0, "summary": "", "items": []},
                    llm_enabled=llm_enabled,
                    llm_requested=True,
                    token_budget=min(token_budget, 4000),
                    token_warning=False,
                    llm_timeout_seconds=_replan_llm_timeout,
                    llm_first_token_timeout_seconds=20,
                    llm_service=replan_llm_service,
                    fallback_builder=lambda *args, **kwargs: _build_followup_fallback_answer(*args, **kwargs),
                    stream_token_callback=None,
                ),
                timeout=_replan_llm_timeout,
            )
        except asyncio.TimeoutError:
            logger and logger.warning("LLM replan timed out after %ss", _replan_llm_timeout)
            return None
        except Exception as exc:
            logger and logger.warning("LLM replan failed: %s", exc)
            return None

        new_actions_raw = _as_list(replan_bundle.get("langchain_actions"))
        if new_actions_raw:
            return new_actions_raw

        # ── 重试：空动作时附带反馈再次调用 ────────────────────────────
        logger and logger.warning(
            "LLM replan returned empty actions (question=%s), retrying with feedback",
            original_question[:80],
        )
        retry_context = replan_context + (
            "\n\n【反馈】\n"
            "上一轮你返回了空动作列表。请基于已执行命令的失败信息，"
            "生成具体的下一步诊断命令（ClickHouse 查询或 kubectl 命令）。"
            "不要输出空列表。"
        )
        augmented_context_retry = dict(analysis_context) if analysis_context else {}
        augmented_context_retry["_llm_replan_context"] = retry_context
        _retry_timeout = min(_replan_llm_timeout, 25)
        try:
            replan_bundle_retry = await asyncio.wait_for(
                run_followup_langchain(
                    question=f"[重规划] {original_question[:200]}",
                    analysis_context=augmented_context_retry,
                    compacted_history=[],
                    compacted_summary="",
                    references=[],
                    subgoals=[],
                    reflection={},
                    long_term_memory={"enabled": False, "hits": 0, "summary": "", "items": []},
                    llm_enabled=llm_enabled,
                    llm_requested=True,
                    token_budget=min(token_budget, 4000),
                    token_warning=False,
                    llm_timeout_seconds=_retry_timeout,
                    llm_first_token_timeout_seconds=15,
                    llm_service=replan_llm_service,
                    fallback_builder=lambda *args, **kwargs: _build_followup_fallback_answer(*args, **kwargs),
                    stream_token_callback=None,
                ),
                timeout=_retry_timeout,
            )
        except (asyncio.TimeoutError, Exception):
            logger and logger.warning("LLM replan retry also failed")
            return None

        new_actions_retry = _as_list(replan_bundle_retry.get("langchain_actions"))
        if not new_actions_retry:
            logger and logger.warning("LLM replan retry also returned empty actions")
            return None
        return new_actions_retry
```

**变更说明：**
- **首次调用逻辑不变**：超时/异常 → return None（和原来一致）
- **仅在 `langchain_actions` 为空时重试**：超时和异常不重试（系统性问题重试无意义）
- **重试超时降至 25s**：`min(_replan_llm_timeout, 25)`，避免消耗过多总预算
- **`augmented_context_retry` 独立创建**：不修改第一轮的 context，无副作用
- **反馈文本明确具体**：告诉 LLM 需要生成命令类型，以及有失败信息可用
- **重试后不再递归**：最多 2 次 LLM 调用，不会死循环
- **重试仍失败则明确日志**：区分"首次失败"和"重试也失败"

- [ ] **Step 2: 验证编译**

```bash
cd ai-service && python -c "import ai.api; print('import ok')"
```
Expected: 无语法错误（闭包内变量 `run_followup_langchain`、`llm_enabled`、`token_budget`、`_replan_llm_timeout` 均来自外部作用域，保持不变）

- [ ] **Step 3: 提交**

```bash
git add ai-service/api/ai.py
git commit -m "fix: retry LLM replan with feedback on empty actions"
```

---

### Task 3: 测试

**Files:**
- Modify: `ai-service/tests/test_followup_planning_helpers.py`（`_build_llm_replan_context` 已有 2 个测试在 line 1298，采用相同风格追加新测试）

- [ ] **Step 1: 追加失败命令 stderr 测试（与现有测试风格一致）**

在现有 `test_build_llm_replan_context_contains_evidence_gaps`（line 1329）之后追加以下 3 个测试函数：

```python
def test_build_llm_replan_context_failed_command_includes_stderr():
    """失败命令的 stderr 应出现在重规划上下文中。"""
    from ai.followup_planning_helpers import _build_llm_replan_context
    observations = [
        {
            "command": "SELECT * FROM logs.logs WHERE level = 'ERROR'",
            "status": "executed",
            "exit_code": 1,
            "stdout": "",
            "stderr": "Code: 60. DB::Exception: Table logs.logs does not exist.",
            "message": "query failed",
        }
    ]
    context = _build_llm_replan_context(
        original_question="排查服务错误",
        analysis_context=None,
        all_observations=observations,
        executed_commands=set(),
        current_evidence_gaps=[],
        remaining_iterations=2,
        remaining_timeout=60,
    )
    assert "Table logs.logs does not exist" in context, \
        "stderr 内容应出现在重规划上下文中"
    assert "exit=1" in context, "exit_code 应出现在重规划上下文中"
    assert "失败" in context, "失败命令应标注为失败"


def test_build_llm_replan_context_success_command_no_failure_marker():
    """成功命令不应包含失败标记。"""
    from ai.followup_planning_helpers import _build_llm_replan_context
    observations = [
        {
            "command": "SELECT count() FROM logs.logs",
            "status": "executed",
            "exit_code": 0,
            "stdout": "1000\n",
            "stderr": "",
        }
    ]
    context = _build_llm_replan_context(
        original_question="test",
        analysis_context=None,
        all_observations=observations,
        executed_commands=set(),
        current_evidence_gaps=[],
        remaining_iterations=2,
        remaining_timeout=60,
    )
    assert "成功" in context, "成功命令应标注为成功"
    assert "失败" not in context, "成功命令不应包含失败标记"


def test_build_llm_replan_context_fallback_when_no_stderr():
    """当 stderr 和 message 都为空时，至少显示状态和 exit_code。"""
    from ai.followup_planning_helpers import _build_llm_replan_context
    observations = [
        {
            "command": "kubectl get pods",
            "status": "failed",
            "exit_code": 127,
            "stdout": "",
            "stderr": "",
            "message": "",
        }
    ]
    context = _build_llm_replan_context(
        original_question="test",
        analysis_context=None,
        all_observations=observations,
        executed_commands=set(),
        current_evidence_gaps=[],
        remaining_iterations=2,
        remaining_timeout=60,
    )
    assert "exit=127" in context, "无错误信息时至少显示 exit_code"
    assert "failed" in context or "失败" in context, "应有状态指示"
```

- [ ] **Step 2: 运行测试验证通过**

```bash
cd ai-service && python -m pytest tests/test_followup_planning_helpers.py \
  -k "test_build_llm_replan_context" -x -v
```
Expected: 5 passed（2 个已有 + 3 个新增）

- [ ] **Step 3: 全量测试**

```bash
cd ai-service && python -m pytest -x --tb=short
```
Expected: 全部测试通过（新增测试不影响已有测试行为）

- [ ] **Step 4: 提交**

```bash
git add ai-service/tests/test_followup_planning_helpers.py
git commit -m "test: add stderr test cases for replan context"
```

> **注意：** `_llm_replan_callback` 重试逻辑的测试因深度嵌套闭包无法直接 import，暂通过 Task 2 的代码审查和 `_build_llm_replan_context` 的测试间接覆盖。后续可通过集成测试框架（mock `run_followup_langchain`）补充。

---

### Task 4: 回归验证

**Files:** 全部已修改文件

- [ ] **Step 1: 运行所有后端测试**

```bash
cd ai-service && python -m pytest -x --tb=short
```
Expected: All tests pass

- [ ] **Step 2: 最终提交**

```bash
git add -A
git commit -m "chore: replan optimization implementation complete"
```

---

## 风险评估

| 风险 | 概率 | 防护 |
|------|------|------|
| stderr 包含敏感信息 | 低 | 上游 `_mask_sensitive_text` 已处理，且只在 LLM 重规划内部使用 |
| stderr 体积过大撑爆上下文 | 低 | 限制 500 字符，仅对失败命令生效 |
| 重试导致总耗时翻倍 | 中 | 重试超时降至 25s，单次重试上限 25s |
| 重试反馈误导 LLM | 低 | 反馈文本明确指定"基于失败信息生成诊断命令" |
| 第二次重试仍返回空 | 低 | 返回 None，与原行为一致，不重复重试 |
| `run_followup_langchain` 有副作用 | 低 | 每次调用创建全新的 dict/list 参数 |
| 闭包变量访问错误 | 低 | 不使用新闭包变量，仅复用外层已捕获的 `run_followup_langchain`、`llm_enabled` 等 |
| 日志中暴露敏感信息 | 低 | `original_question[:80]` 截断，且外部已 `_mask_sensitive_text` |
