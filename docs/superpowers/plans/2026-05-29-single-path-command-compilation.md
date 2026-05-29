# Single-Path Command Compilation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate session blocks caused by LLM-generated malformed shell commands by routing all commands through `compile_followup_command_spec`.

**Architecture:** Add `_compact_command_normalizer()` to `followup_command_spec.py` that pre-processes command text before `shlex.split` to fix common compact patterns (e.g., `kubectlgetpods` → `kubectl get pods`). In `_extract_structured_actions`, auto-wrap free-text commands as `generic_exec` command_spec so they go through the compiler. Fix LLM repair fallthrough when it returns unchanged commands.

**Tech Stack:** Python 3.11+, re, shlex, pytest

---

### Task 1: Add `_compact_command_normalizer` to `followup_command_spec.py`

**Files:**
- Modify: `ai-service/ai/followup_command_spec.py` (add new function)
- Test: `ai-service/tests/test_followup_command_spec.py` (add test class)

- [ ] **Step 1: Write tests for the normalizer**

```python
# Add to end of ai-service/tests/test_followup_command_spec.py

class TestCompactCommandNormalizer:
    """Test _compact_command_normalizer across all known compact patterns."""

    def test_kubectl_verb_glued_to_getpods(self):
        from ai.followup_command_spec import _compact_command_normalizer
        raw = "kubectlgetpods-A-lapp=temporal"
        result = _compact_command_normalizer(raw)
        assert result == "kubectl get pods -A -l app=temporal", f"got: {result}"

    def test_kubectl_verb_glued_to_logs_with_flags(self):
        from ai.followup_command_spec import _compact_command_normalizer
        raw = "kubectllogs-nislap-lapp=temporal--since-time=2026-05-29T06:44:00Z--tail=100"
        result = _compact_command_normalizer(raw)
        assert "kubectl" in result
        assert "logs" in result.split()
        assert "-n" in result.split()
        assert "islap" in result.split()
        assert "-l" in result.split()
        assert "app=temporal" in result.split()
        assert "--since-time=2026-05-29T06:44:00Z" in result.split()
        assert "--tail=100" in result.split()

    def test_already_correct_command_unchanged(self):
        from ai.followup_command_spec import _compact_command_normalizer
        raw = "kubectl get pods -A -l app=temporal"
        result = _compact_command_normalizer(raw)
        assert result == raw

    def test_non_kubectl_command_unchanged(self):
        from ai.followup_command_spec import _compact_command_normalizer
        raw = "echo hello world"
        result = _compact_command_normalizer(raw)
        assert result == raw

    def test_kubectl_describe_pod_expansion(self):
        from ai.followup_command_spec import _compact_command_normalizer
        raw = "kubectldescribepodnginx-X"
        result = _compact_command_normalizer(raw)
        assert "describe" in result.split()
        assert "pod" in result.split()

    def test_stage6_short_flag_concatenation(self):
        from ai.followup_command_spec import _compact_command_normalizer
        raw = "kubectl get pods -A-lapp=temporal"
        result = _compact_command_normalizer(raw)
        assert "-A" in result.split()
        assert "-l" in result.split()

    def test_empty_string_returns_empty(self):
        from ai.followup_command_spec import _compact_command_normalizer
        assert _compact_command_normalizer("") == ""
        assert _compact_command_normalizer(None) == ""
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend/ai-service
pip install -r requirements-runtime.txt -r requirements-test.txt -q 2>/dev/null
pytest tests/test_followup_command_spec.py::TestCompactCommandNormalizer -v --no-header 2>&1 | head -20
```
Expected: ModuleNotFoundError or AttributeError for _compact_command_normalizer

- [ ] **Step 3: Implement `_compact_command_normalizer`**

Add to `ai-service/ai/followup_command_spec.py`, near line 590 (after `_detect_glued_command_head`):

```python
def _compact_command_normalizer(text: Any) -> str:
    """Pre-process command text before shlex.split to insert missing spaces
    in LLM-generated compact commands (e.g. 'kubectlgetpods' → 'kubectl get pods').

    Uses entirely deterministic regex — no LLM call.
    Order matters: flag expansion must happen after value--flag split.
    """
    raw = _as_str(text)
    if not raw:
        return ""

    result = raw

    # Stage 1: kubectl<verb> → kubectl <verb>
    # Matches "kubectl" directly followed by a known verb (no space)
    result = re.sub(
        r"(?i)^kubectl(?=(?:get|describe|logs|exec|top|rollout|apply|delete|patch|edit|replace|scale|set|annotate|label|create|expose|autoscale|cordon|uncordon|drain|taint|auth|config|cluster-info|version|api-resources|api-versions|explain|options|plugin|completion)\b)",
        "kubectl ",
        result,
    )
    # Stage 1 also handles "kubectl" mid-command (after space or pipe)
    result = re.sub(
        r"(?i)(?<=\s)kubectl(?=(?:get|describe|logs|exec|top|rollout|apply|delete|patch|edit|replace|scale|set|annotate|label|create|expose|autoscale|cordon|uncordon|drain|taint|auth|config|cluster-info)\b)",
        "kubectl ",
        result,
    )

    # Stage 2: expand compact verbs (getpods → get pods, etc.)
    for compact, expanded in _KUBECTL_COMPACT_VERB_EXPANSIONS.items():
        result = re.sub(rf"(?i)(?<!\w){compact}(?!\w)", " ".join(expanded), result)

    # Stage 3: <known_verb>-<attached_flag> → <known_verb> -<attached_flag>
    _kubectl_verbs = "get|describe|logs|exec|top|rollout|apply|delete|patch|edit|replace|scale|set|annotate|label|create|expose"
    result = re.sub(
        rf"(?i)(\b(?:{_kubectl_verbs})\b)(?=-[a-zA-Z])",
        r"\1 ",
        result,
    )

    # Stage 4: <value>--<flag> → <value> --<flag>
    # Lookbehind: -- preceded by non-whitespace means missing space
    result = re.sub(r"(?<=\S)--", " --", result)

    # Stage 5: short flag value glue: -n<ns>, -l<sel>, -o<fmt>
    # Only when the value starts immediately after the flag letter
    result = re.sub(r"(?i)(?<!\S)-n([a-z0-9](?:[-a-z0-9]*[a-z0-9])?)(?=\s|$)", r"-n \1", result)
    result = re.sub(r"(?i)(?<!\S)-l([a-z0-9_.-]+=[a-z0-9_.:/-]+)(?=\s|$)", r"-l \1", result)
    result = re.sub(r"(?i)(?<!\S)-o(jsonpath=[^\s]+|json|yaml|wide|name)(?=\s|$)", r"-o \1", result)

    # Stage 5b: handle -l<key>=<value> where -l is NOT followed by space
    # e.g. "-lapp=temporal" → "-l app=temporal", "-lapp=temporal--since" → "-l app=temporal --since"
    result = re.sub(r"(?i)(?<!\S)-l(?=[a-z][a-z0-9_.-]*=)", "-l ", result)
    result = re.sub(r"(?i)-n([a-z0-9][-a-z0-9]*)(?!\S)", r"-n \1", result)

    # Stage 6: short flag concatenation -A-l → -A -l
    reordered = re.sub(r"(-[A-Za-z])(?=-[A-Za-z])", r"\1 ", result)

    # Clean up any double spaces introduced
    result = re.sub(r" {2,}", " ", result).strip()

    return result
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend/ai-service
pytest tests/test_followup_command_spec.py::TestCompactCommandNormalizer -v --no-header 2>&1
```
Expected: all 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add ai-service/ai/followup_command_spec.py ai-service/tests/test_followup_command_spec.py
git commit -m "feat(ai-service): add _compact_command_normalizer for compact kubectl commands

Adds deterministic pre-shlex normalizer to fix LLM-generated commands
with missing spaces (e.g. kubectlgetpods → kubectl get pods).

6-stage pipeline: kubectl-verb split, compact verb expansion, verb-flag split,
value--flag split, short flag value glue fix, short flag concatenation.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 2: Integrate normalizer into `compile_followup_command_spec`

**Files:**
- Modify: `ai-service/ai/followup_command_spec.py` (modify `compile_followup_command_spec`)
- Test: `ai-service/tests/test_followup_command_spec.py` (add test)

- [ ] **Step 1: Write test verifying compiler accepts normalized commands**

```python
# Add to TestCompactCommandNormalizer class

def test_compiler_accepts_normalized_glued_command(self):
    from ai.followup_command_spec import compile_followup_command_spec
    # This is what _extract_structured_actions will produce after auto-wrap
    spec = {
        "tool": "generic_exec",
        "args": {
            "command": "kubectlgetpods-A-lapp=temporal",
            "target_kind": "k8s_namespace",
            "target_identity": "namespace:islap",
            "timeout_s": 30,
        }
    }
    result = compile_followup_command_spec(spec)
    assert result.get("ok") is True, f"compilation failed: {result.get('reason')}"
    assert "kubectl" in result.get("command", "")
    assert "get" in result.get("command", "")
    assert "pods" in result.get("command", "")
    assert "-l" in result.get("command", "")

def test_compiler_accepts_already_correct_command(self):
    from ai.followup_command_spec import compile_followup_command_spec
    spec = {
        "tool": "generic_exec",
        "args": {
            "command": "kubectl get pods -A -l app=temporal",
            "target_kind": "k8s_namespace",
            "target_identity": "namespace:islap",
            "timeout_s": 30,
        }
    }
    result = compile_followup_command_spec(spec)
    assert result.get("ok") is True
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend/ai-service
pytest tests/test_followup_command_spec.py::TestCompactCommandNormalizer -v --no-header -k "compiler"
```
Expected: FAIL — glued command should currently be rejected by `_detect_glued_command_head`

- [ ] **Step 3: Add normalizer call in `compile_followup_command_spec`**

In `ai-service/ai/followup_command_spec.py`, locate the `generic_exec` branch (~line 948-954) and modify:

```python
# BEFORE (lines 948-954):
        if not command_argv:
            if not command_text:
                return {"ok": False, "reason": "command is required in command_spec.args"}
            try:
                command_argv = [item for item in shlex.split(command_text) if _as_str(item).strip()]
            except Exception:
                return {"ok": False, "reason": "invalid command text in command_spec.args.command"}

# AFTER:
        if not command_argv:
            if not command_text:
                return {"ok": False, "reason": "command is required in command_spec.args"}
            command_text = _compact_command_normalizer(command_text)  # NEW
            try:
                command_argv = [item for item in shlex.split(command_text) if _as_str(item).strip()]
            except Exception:
                return {"ok": False, "reason": "invalid command text in command_spec.args.command"}
```

- [ ] **Step 4: Run tests to verify both old and new tests pass**

```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend/ai-service
pytest tests/test_followup_command_spec.py -v --no-header 2>&1 | tail -20
```
Expected: all original tests pass AND new compiler tests PASS

Also run full test suite for the module:
```bash
pytest tests/test_followup_command_spec.py -v 2>&1 | tail -30
```

- [ ] **Step 5: Commit**

```bash
git add ai-service/ai/followup_command_spec.py ai-service/tests/test_followup_command_spec.py
git commit -m "feat(ai-service): integrate _compact_command_normalizer into compile_followup_command_spec

The normalizer runs before shlex.split in the generic_exec branch, so
glued commands like 'kubectlgetpods-...' are now automatically fixed
instead of rejected by _detect_glued_command_head.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 3: Auto-wrap free-text commands in `_extract_structured_actions`

**Files:**
- Modify: `ai-service/ai/langchain_runtime/service.py` (modify `_extract_structured_actions`)
- Test: `ai-service/tests/test_followup_exec_streaming.py` or `ai-service/tests/test_langchain_runtime.py`

- [ ] **Step 1: Write test for auto-wrap behavior**

```python
# Add to an existing test file, e.g. tests/test_followup_exec_streaming.py
# or create a new test for the extraction

def test_extract_structured_actions_wraps_free_text_command():
    """Actions with command but no command_spec should be auto-wrapped as generic_exec."""
    from ai.langchain_runtime.service import _extract_structured_actions
    from ai.langchain_runtime.models import StructuredAnswer, ActionItem

    answer = StructuredAnswer(
        conclusion="test",
        summary="",
        actions=[
            ActionItem(
                title="check pods",
                command="kubectlgetpods-A-lapp=temporal",
                executable=True,
            )
        ],
    )
    result = _extract_structured_actions(answer)
    assert len(result) == 1
    action = result[0]
    # Even with glued command, the executable should remain True
    # because the compiler will fix it
    # (Or executable could be False if compilation fails — verify behavior)
    assert action["command_spec"] is not None
    if action.get("command_spec"):
        assert action["command_spec"].get("tool") == "generic_exec"

def test_extract_structured_actions_keeps_skill_name_untouched():
    """Actions with skill_name should NOT be wrapped even without command_spec."""
    from ai.langchain_runtime.service import _extract_structured_actions
    from ai.langchain_runtime.models import StructuredAnswer, ActionItem

    answer = StructuredAnswer(
        conclusion="test",
        summary="",
        actions=[
            ActionItem(
                title="run skill",
                skill_name="diagnose_pod",
                executable=True,
            )
        ],
    )
    result = _extract_structured_actions(answer)
    assert len(result) == 1
    assert result[0]["skill_name"] == "diagnose_pod"
    # command_spec should be empty dict, no auto-wrap
    assert result[0]["command_spec"] == {}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend/ai-service
pytest tests/ -k "test_extract_structured_actions_wraps_free_text_command or test_extract_structured_actions_keeps_skill_name" -v --no-header 2>&1 | head -20
```
Expected: FAIL (function behavior hasn't changed yet)

- [ ] **Step 3: Add auto-wrap logic in `_extract_structured_actions`**

In `ai-service/ai/langchain_runtime/service.py`, find the `_extract_structured_actions` function (~line 870-880). Locate the section where `command_spec` is checked:

```python
# Current code around line 870-880:
        raw_command_spec = getattr(action, "command_spec", None)
        raw_command_spec = _model_to_dict(raw_command_spec)
        command_spec = normalize_followup_command_spec(raw_command_spec)
        command = ""
        command_display = _normalize_action_command(getattr(action, "command", ""))
        command_spec_compile_reason = ""
        if skill_name and not command_spec:
            command_spec_compile_reason = ""
        elif not command_spec:
            command_spec_compile_reason = "missing_structured_spec"
```

Change the `elif not command_spec:` branch:

```python
        elif not command_spec:
            # Auto-wrap free-text command into generic_exec for compilation
            raw_command = _as_str(getattr(action, "command", "")).strip()
            if raw_command:
                command_spec = {
                    "tool": "generic_exec",
                    "args": {
                        "command": raw_command,
                        "target_kind": "k8s_cluster",
                        "target_identity": "runtime:local",
                        "timeout_s": 30,
                    },
                }
                command_spec = normalize_followup_command_spec(command_spec)
            if not command_spec:
                command_spec_compile_reason = "missing_structured_spec"
```

- [ ] **Step 4: Run tests to verify they pass and no regressions**

```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend/ai-service
pytest tests/ -k "test_extract_structured_actions_wraps_free_text_command or test_extract_structured_actions_keeps_skill_name" -v --no-header 2>&1
```
Expected: both tests PASS

```bash
pytest tests/test_langchain_runtime.py -v --no-header 2>&1 | tail -10
```
Expected: no regressions

- [ ] **Step 5: Commit**

```bash
git add ai-service/ai/langchain_runtime/service.py ai-service/tests/
git commit -m "feat(ai-service): auto-wrap free-text commands into command_spec in _extract_structured_actions

Actions with free-text command but no command_spec now get auto-wrapped
as generic_exec tool calls, ensuring they go through compile_followup_command_spec
validation. Skill-name-only actions are unaffected.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 4: Fix LLM repair fallthrough when `fixed == original`

**Files:**
- Modify: `ai-service/ai/langchain_runtime/service.py` (modify `_repair_malformed_action_commands`)
- Test: `ai-service/tests/test_followup_exec_streaming.py`

- [ ] **Step 1: Write test for the repair fallthrough**

```python
# Add to a new or existing test class in test_followup_exec_streaming.py

def test_repair_falls_through_to_heuristic_when_llm_returns_same():
    """When LLM repair returns fixed == original, the heuristic should be tried."""
    from ai.langchain_runtime.service import _repair_malformed_action_commands
    from ai.langchain_runtime.models import StructuredAnswer, ActionItem

    answer = StructuredAnswer(
        conclusion="test",
        summary="",
        actions=[
            ActionItem(
                title="check pods",
                command="kubectlgetpods-A-lapp=temporal",
                executable=True,
            )
        ],
    )
    # Mock LLM service that returns the same command as "fixed"
    class MockLLM:
        class config:
            provider = "deepseek"
        async def generate(self, *, messages, **kwargs):
            # Return JSON with original==fixed (simulating LLM failure)
            return '[{"original": "kubectlgetpods-A-lapp=temporal", "fixed": "kubectlgetpods-A-lapp=temporal"}]'
        async def generate_stream(self, **kwargs):
            class StreamMock:
                async def __aiter__(self):
                    yield self
                full_content = ""
            return StreamMock()

    import types
    mock_llm_service = types.SimpleNamespace()
    mock_llm_service.config = types.SimpleNamespace()
    mock_llm_service.config.provider = "deepseek"
    mock_llm_service.generate = MockLLM.generate
    mock_llm_service.generate_stream = MockLLM().generate_stream()

    import asyncio
    result = asyncio.run(
        _repair_malformed_action_commands(
            llm_service=mock_llm_service,
            structured=answer,
            timeout_seconds=5,
        )
    )
    # Should return True because heuristic fixed the command
    assert result is True
    # The command should now be properly spaced
    assert answer.actions[0].command == "kubectl get pods -A -l app=temporal"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend/ai-service
pytest tests/test_followup_exec_streaming.py -k "repair_falls_through" -v --no-header 2>&1 | head -20
```
Expected: FAIL (current code accepts the LLM's unchanged result)

- [ ] **Step 3: Modify `_repair_malformed_action_commands` fallthrough logic**

In `ai-service/ai/langchain_runtime/service.py`, find the repair_map application section (~line 661-676):

```python
# Current code:
        for item in malformed:
            original = item["command"]
            if original in repair_map:
                repair = repair_map[original]
                _set_action_command(
                    structured.actions[item["index"]],
                    repair["fixed"],
                    repair.get("fixed_argv") or [],
                )
                logger.info(
                    "Repaired malformed command: %s -> %s",
                    original[:120],
                    repair["fixed"][:120],
                )
                updated += 1
            else:
                # Heuristic fallback
                import shlex
                fixed = _normalize_action_command(original)
                ...
```

Replace with:

```python
        for item in malformed:
            original = item["command"]
            if original in repair_map:
                repair = repair_map[original]
                if repair["fixed"] != original:
                    # LLM produced a valid fix — use it
                    _set_action_command(
                        structured.actions[item["index"]],
                        repair["fixed"],
                        repair.get("fixed_argv") or [],
                    )
                    logger.info(
                        "Repaired malformed command: %s -> %s",
                        original[:120],
                        repair["fixed"][:120],
                    )
                    updated += 1
                    continue
                # LLM returned same string — fall through to heuristic
                logger.info(
                    "LLM repair returned unchanged command for '%s', trying heuristic",
                    original[:80],
                )
            # Heuristic fallback (was in the else branch)
            import shlex
            fixed = _normalize_action_command(original)
            if fixed and fixed != original:
                try:
                    fixed_argv = shlex.split(fixed)
                except Exception:
                    fixed_argv = fixed.split()
                _set_action_command(
                    structured.actions[item["index"]],
                    fixed,
                    fixed_argv,
                )
                logger.info(
                    "Heuristic-repaired malformed command: %s -> %s",
                    original[:120],
                    fixed[:120],
                )
                updated += 1
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend/ai-service
pytest tests/test_followup_exec_streaming.py -k "repair_falls_through" -v --no-header 2>&1
```
Expected: PASS

```bash
pytest tests/test_followup_exec_streaming.py -v --no-header 2>&1 | tail -15
```
Expected: no regressions

- [ ] **Step 5: Commit**

```bash
git add ai-service/ai/langchain_runtime/service.py ai-service/tests/
git commit -m "fix(ai-service): fall through to heuristic when LLM repair returns unchanged command

When _repair_malformed_action_commands gets a 'fixed' command identical
to the original from the LLM, the heuristic _normalize_action_command is
now tried instead of silently accepting the unchanged command.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 5: Run integration verification

**Files:** (no changes)

- [ ] **Step 1: Run the full test suite**

```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend/ai-service
pytest -v --tb=short 2>&1 | tail -40
```
Expected: all tests pass. Note any failures and fix before proceeding.

- [ ] **Step 2: Verify the end-to-end flow conceptually**

```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend/ai-service
python -c "
from ai.followup_command_spec import _compact_command_normalizer, compile_followup_command_spec

# Test all production patterns from the spec
cases = [
    ('kubectlgetpods-A-lapp=temporal', 'kubectl get pods -A -l app=temporal'),
    ('kubectllogs-nislap-lapp=temporal--since-time=2026-05-29T06:44:00Z--tail=100', None),  # just verify it parses
    ('echo hello', 'echo hello'),
    ('kubectl get pods -A', 'kubectl get pods -A'),
]

for raw, expected in cases:
    result = _compact_command_normalizer(raw)
    tokens = result.split()
    if expected:
        assert result == expected, f'normalize({raw!r}) = {result!r}, expected {expected!r}'
    print(f'  OK: {raw!r} -> {result!r} ({len(tokens)} tokens)')

# Verify compiler accepts normalized form
spec = {
    'tool': 'generic_exec',
    'args': {
        'command': 'kubectlgetpods-A-lapp=temporal',
        'target_kind': 'k8s_namespace',
        'target_identity': 'namespace:islap',
        'timeout_s': 30,
    }
}
compiled = compile_followup_command_spec(spec)
assert compiled.get('ok'), f'compiler rejected: {compiled.get(\"reason\")}'
print(f'  Compiler OK: {compiled[\"command\"]}')
"
```
Expected: all assertions pass, output shows correct normalization

- [ ] **Step 3: Final commit of any test adjustments**

```bash
git add . && git status
```
If any changes, commit them. Otherwise, no commit needed.
