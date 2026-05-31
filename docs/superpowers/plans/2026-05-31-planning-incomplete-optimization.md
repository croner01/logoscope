# AI Analysis Session Blocking Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce `planning_incomplete` session blocking via prompt enhancement + server-side command_spec pre-fill + automatic command repair.

**Architecture:** Three independent layers work together: (1) system prompt tells the AI exactly what valid command_spec looks like, (2) a pre-fill function injects missing `target_kind`/`target_identity` from `analysis_context` before spec compilation, (3) a repair retry catches glued-token and syntax errors and re-attempts compilation.

**Tech Stack:** Python 3.10+, LangChain prompts, FastAPI

---

### Task 1: Enhance FOLLOWUP_SYSTEM_PROMPT with JSON examples and rule 17

**Files:**
- Modify: `ai-service/ai/langchain_runtime/prompts.py:20-21` (rule 5) and `prompts.py:42-43` (after rule 16)

- [ ] **Step 1: Add JSON examples to rule 5**

Current rule 5 says "输出 command_spec（结构化命令）" but provides no concrete example. Append the following after the existing rule 5 text:

Edit `prompts.py` line 20 (`actions 默认优先输出 command_spec...`). Replace the rule 5 text:

```
5) actions 默认优先输出 command_spec（结构化命令），command 仅作兼容字段：能用 command_spec 就不要拼自由文本 shell；但若动作明确对应某个已注册诊断技能，可只输出 skill_name，由系统自动展开为结构化命令链。command_spec 必须使用以下格式：
   generic_exec 示例：
     {"tool": "generic_exec", "args": {"command": "kubectl get pods -n islap", "target_kind": "k8s_cluster", "target_identity": "namespace:islap", "timeout_s": 30}}
   kubectl_clickhouse_query 示例：
     {"tool": "kubectl_clickhouse_query", "args": {"target_kind": "clickhouse_cluster", "target_identity": "database:logs", "query": "SELECT ...", "timeout_s": 45}}
```

- [ ] **Step 2: Add new rule 17**

After rule 16 (line 42, the paragraph starting with `16) 找到 Pod 名称和 Namespace 后...`), add a new line with rule 17:

```
17) 每个 action 必须同时满足以下三个条件才可执行（否则会被标记为 spec_blocked 导致整个计划阻断）：
    - command_spec.tool 必须是 generic_exec 或 kubectl_clickhouse_query
    - command_spec.args.target_kind 不能为空（k8s_cluster / clickhouse_cluster / runtime_node）
    - command_spec.args.target_identity 不能为空（namespace:<ns> / database:<db> / runtime:local）
    缺少任意一项 → 该 action 标记为 spec_blocked → 计划可能被阻断无法继续。
```

- [ ] **Step 3: Verify syntax**

```bash
cd /root/logoscope && python3 -m py_compile ai-service/ai/langchain_runtime/prompts.py
```

Expected: no output (compilation succeeds).

- [ ] **Step 4: Commit**

```bash
cd /root/logoscope && git add ai-service/ai/langchain_runtime/prompts.py && git commit -m "feat: add command_spec JSON examples and rule 17 to FOLLOWUP_SYSTEM_PROMPT"
```

---

### Task 2: Add _prefill_command_spec to followup_planning_helpers.py

**Files:**
- Modify: `ai-service/ai/followup_planning_helpers.py`

Add a new `_prefill_command_spec` function and call it from `_build_followup_react_loop` before the compilation step.

- [ ] **Step 1: Add `_prefill_command_spec` function**

Add this function somewhere before `_build_followup_react_loop` (line ~1147):

```python
def _prefill_command_spec(
    action: Dict[str, Any],
    analysis_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """自动补全 command_spec 中缺失的 target_kind / target_identity / timeout_s。
    
    仅在字段完全为空时注入，不覆盖 AI 已填写的值。
    """
    spec = action.get("command_spec")
    if not isinstance(spec, dict):
        return action
    args = spec.get("args")
    if not isinstance(args, dict):
        args = {}
        spec["args"] = args

    has_target_kind = bool(_as_str(args.get("target_kind") or spec.get("target_kind")).strip())
    has_target_identity = bool(_as_str(args.get("target_identity") or spec.get("target_identity")).strip())

    # 推断 target_kind
    if not has_target_kind:
        command = _as_str(args.get("command") or spec.get("command")).lower()
        if "kubectl" in command:
            args["target_kind"] = "k8s_cluster"
        elif "clickhouse" in command:
            args["target_kind"] = "clickhouse_cluster"
        else:
            args["target_kind"] = "runtime_node"

    resolved_target_kind = _as_str(args.get("target_kind") or spec.get("target_kind")).strip()

    # 推断 target_identity
    if not has_target_identity:
        if resolved_target_kind == "k8s_cluster":
            ctx = analysis_context if isinstance(analysis_context, dict) else {}
            ns = _as_str(ctx.get("namespace") or ctx.get("service_namespace")).strip() or "default"
            args["target_identity"] = f"namespace:{ns}"
        elif resolved_target_kind == "clickhouse_cluster":
            args["target_identity"] = "database:logs"
        else:
            args["target_identity"] = "runtime:local"

    # 补全 timeout_s（默认 30s）
    if not args.get("timeout_s") and not spec.get("timeout_s"):
        args["timeout_s"] = 30

    action["command_spec"] = spec
    return action
```

Also add `_as_str` import if not already at top of file, or ensure it's available (it's defined at line ~54 in the same file).

- [ ] **Step 2: Insert pre-fill call in `_build_followup_react_loop`**

Find the first action iteration loop in `_build_followup_react_loop` (around line 1270, where `plan_total` is being calculated). Before any spec compilation or blocking logic, add pre-fill.

Look for the block starting with:

```python
    plan_total = 0
    query_total = 0
```

Add immediately before this block:

```python
    # Pre-fill missing command_spec fields before compilation
    for action in safe_actions:
        _prefill_command_spec(action, analysis_context)
```

This ensures all actions have their `target_kind`/`target_identity`/`timeout_s` pre-filled before they enter the blocking decision logic.

- [ ] **Step 3: Verify syntax**

```bash
cd /root/logoscope && python3 -m py_compile ai-service/ai/followup_planning_helpers.py
```

Expected: no output.

- [ ] **Step 4: Run existing tests**

```bash
cd /root/logoscope/ai-service && python3 -m pytest tests/ -x -q 2>&1 | tail -10
```

Expected: tests pass (no regressions from pre-fill logic).

- [ ] **Step 5: Commit**

```bash
cd /root/logoscope && git add ai-service/ai/followup_planning_helpers.py && git commit -m "feat: add _prefill_command_spec to auto-inject missing target_kind/target_identity"
```

---

### Task 3: Add repair retry in compile_followup_command_spec

**Files:**
- Modify: `ai-service/ai/followup_command_spec.py`

After compilation failure with repair-eligible reasons (`glued_command_tokens`, `invalid_kubectl_token`), attempt a simple text repair and recompile.

- [ ] **Step 1: Add `_repair_glued_command` function in followup_command_spec.py**

Add this function before `compile_followup_command_spec` (line ~1084):

```python
def _repair_glued_command(command_text: str) -> str:
    """修复常见的命令格式粘连问题。
    
    处理场景：
    - kubectl-nislaplogs → kubectl -n islap logs
    - grep-ierror → grep -i error
    - head-20 → head -20
    - --tail=50 → --tail 50 (在某些上下文中)
    """
    if not command_text or len(command_text) < 5:
        return command_text
    
    repaired = command_text
    # kubectl followed by glued flags: kubectl-nislaplogs...
    repaired = re.sub(
        r'\bkubectl(?=-[a-z])',
        'kubectl ',
        repaired,
    )
    # Known flag patterns: grep-ierror, head-20, tail-100
    repaired = re.sub(
        r'\b(grep|head|tail|sort|awk|xargs)(?=-[a-z0-9])',
        r'\1 ',
        repaired,
    )
    # clickhouse-client--query → clickhouse-client --query
    repaired = re.sub(
        r'(\bclickhouse-client)(?=--)',
        r'\1 ',
        repaired,
    )
    return repaired
```

- [ ] **Step 2: Add the `re` import at the top of the file if not present**

Check if `import re` exists at the top of `followup_command_spec.py`. If not, add it:

```python
import re
```

- [ ] **Step 3: Add repair-retry logic in `compile_followup_command_spec`**

Before the `return` for `glued_command_tokens` (line 1128-1133), add a repair-retry:

Find this block:

```python
        glued_head_prefix = _detect_glued_command_head(head)
        if glued_head_prefix:
            return {
                "ok": False,
                "reason": "glued_command_tokens",
                "detail": f"command head '{head}' should be separated (expected '{glued_head_prefix} ...')",
            }
```

Replace the return with a repair-retry:

```python
        glued_head_prefix = _detect_glued_command_head(head)
        if glued_head_prefix:
            # Attempt repair for glued tokens
            if command_text:
                repaired_text = _repair_glued_command(command_text)
                if repaired_text != command_text:
                    try:
                        repaired_argv = [item for item in shlex.split(repaired_text) if _as_str(item).strip()]
                        if repaired_argv:
                            repaired_head = _as_str(repaired_argv[0]).strip().lower()
                            if not _detect_glued_command_head(repaired_head):
                                # Repair succeeded; rebuild spec with repaired argv
                                args["command"] = repaired_text
                                args["command_argv"] = repaired_argv
                                command_argv = repaired_argv
                                head = repaired_head
                                # Continue with the repaired argv instead of returning failure
                                glued_head_prefix = None
                    except Exception:
                        pass
            if glued_head_prefix:
                return {
                    "ok": False,
                    "reason": "glued_command_tokens",
                    "detail": f"command head '{head}' should be separated (expected '{glued_head_prefix} ...')",
                }
```

Similarly, for the `invalid_kubectl_token` case (line 1163-1167):

Find:

```python
            if any(("(" in token or ")" in token) for token in kubectl_scope_argv[1:]):
                return {
                    "ok": False,
                    "reason": "invalid_kubectl_token",
                    "detail": "kubectl argv contains unsupported token characters",
                }
```

Replace with:

```python
            if any(("(" in token or ")" in token) for token in kubectl_scope_argv[1:]):
                # Attempt repair: remove parentheses
                if command_text:
                    repaired_text = re.sub(r'[()]', '', command_text)
                    if repaired_text != command_text:
                        try:
                            repaired_argv = [item for item in shlex.split(repaired_text) if _as_str(item).strip()]
                            if repaired_argv:
                                args["command"] = repaired_text
                                args["command_argv"] = repaired_argv
                                command_argv = repaired_argv
                                # Continue with repaired argv
                                return compile_followup_command_spec(spec, run_sql_preflight=run_sql_preflight)
                        except Exception:
                            pass
                return {
                    "ok": False,
                    "reason": "invalid_kubectl_token",
                    "detail": "kubectl argv contains unsupported token characters",
                }
```

- [ ] **Step 4: Verify syntax**

```bash
cd /root/logoscope && python3 -m py_compile ai-service/ai/followup_command_spec.py
```

Expected: no output.

- [ ] **Step 5: Run existing tests**

```bash
cd /root/logoscope/ai-service && python3 -m pytest tests/ -x -q 2>&1 | tail -10
```

Expected: tests pass.

- [ ] **Step 6: Commit**

```bash
cd /root/logoscope && git add ai-service/ai/followup_command_spec.py && git commit -m "feat: add repair-retry for glued_command_tokens and invalid_kubectl_token in compile_followup_command_spec"
```

---

### Task 4: Enhance template command hints to cover more scenarios

**Files:**
- Modify: `ai-service/ai/followup_planning_helpers.py` (the `_build_non_executable_query_command_hints` function)

Enhance the template hint generation to cover `kubectl get pods`, `kubectl logs`, `kubectl describe pod` with auto-generated `target_kind`/`target_identity`.

- [ ] **Step 1: Locate and read `_build_non_executable_query_command_hints`**

```bash
grep -n "def _build_non_executable_query_command_hints" /root/logoscope/ai-service/ai/followup_planning_helpers.py
```

Read it to understand the current template generation logic.

- [ ] **Step 2: Add template targeting for kubectl commands**

In the `_infer_query_template_command_spec` function (or equivalent template inference), add logic that when the command starts with `kubectl`, auto-injects `target_kind=k8s_cluster` and `target_identity=namespace:<ns>` from context. Similarly for `clickhouse-client` → `clickhouse_cluster` + `database:logs`.

```python
def _infer_query_template_command_spec(command: str) -> tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Infer command_spec from a raw command string for template generation."""
    safe_command = _as_str(command).strip()
    if not safe_command:
        return None, None
    
    tool = "generic_exec"
    args: Dict[str, Any] = {
        "command": safe_command,
    }
    
    lower_cmd = safe_command.lower()
    if lower_cmd.startswith("kubectl"):
        args["target_kind"] = "k8s_cluster"
        args["target_identity"] = "namespace:default"
    elif "clickhouse" in lower_cmd:
        args["target_kind"] = "clickhouse_cluster"
        args["target_identity"] = "database:logs"
    else:
        args["target_kind"] = "runtime_node"
        args["target_identity"] = "runtime:local"
    
    args["timeout_s"] = 30
    spec: Dict[str, Any] = {
        "tool": tool,
        "args": args,
    }
    return None, spec
```

- [ ] **Step 3: Verify syntax**

```bash
cd /root/logoscope && python3 -m py_compile ai-service/ai/followup_planning_helpers.py
```

Expected: no output.

- [ ] **Step 4: Commit**

```bash
cd /root/logoscope && git add ai-service/ai/followup_planning_helpers.py && git commit -m "feat: enhance template command hints with auto-inferred target_kind/target_identity"
```
