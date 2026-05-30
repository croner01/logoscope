# AI Followup Single-Path Command Compilation

**Date**: 2026-05-29
**Status**: Draft
**Author**: AI-assisted design

## Problem

The AI followup system has a dual-path architecture for command specification that causes
sessions to get blocked when the LLM outputs malformed shell commands.

### Dual-Path Architecture (Current)

```
LLM JSON Response
  ├→ has command_spec (structured) → compile_followup_command_spec() → validation + compilation → executable
  └→ no command_spec (free-text)  → shlex.split → NO validation → fragile → blocked
```

The free-text path bypasses all validation in `compile_followup_command_spec`:
- Glued command detection (`_detect_glued_command_head`)
- kubectl canonicalization (`_canonicalize_kubectl_command_argv`)
- Namespace/selector validation
- Blocked operator detection

### Symptoms in Production

DeepSeek consistently generates commands without proper spacing:

| LLM Output | Expected |
|------------|----------|
| `kubectlgetpods-A-lapp=temporal` | `kubectl get pods -A -l app=temporal` |
| `kubectllogs-nislap-lapp=temporal--since-time=X--tail=100` | `kubectl logs -n islap -l app=temporal --since-time=X --tail=100` |

Repair pipeline (also DeepSeek) can't fix its own malformed output, and the heuristic
fallback (`_normalize_action_command`) is skipped due to a code bug where LLM repair
returning `fixed == original` still counts as a successful repair.

### Industry Context

All major AI coding tools (Claude Code, GitHub Copilot CLI, OpenAI Function Calling,
AutoGPT) use a single-path architecture:

```
LLM → structured tool call (typed params) → schema validation → execute
```

No free-text shell command generation. Parameters are separated at the JSON level,
not parsed from a string via `shlex.split`.

## Design: Single-Path Command Compilation

### Architecture Change

**Before:**
```
LLM command: "kubectlgetpods-A-lapp=temporal"
  → _repair_malformed_action_commands (LLM repair → fails)
  → _extract_structured_actions (no command_spec → skips compiler)
  → shlex.split → 1 token → command_type="unknown"
  → max_unknown_retries=1 → blocked
```

**After:**
```
LLM command: "kubectlgetpods-A-lapp=temporal"
  → _extract_structured_actions (no command_spec → auto-wrap as generic_exec)
  → compile_followup_command_spec
    → _compact_command_normalizer (deterministic, local)
    → shlex.split → ["kubectl", "get", "pods", "-A", "-l", "app=temporal"]
    → _canonicalize_kubectl_command_argv
  → command = "kubectl get pods -A -l app=temporal"
  → normal execution
```

### Component Design

#### 1. `_extract_structured_actions` — Auto-wrap free-text commands

**File**: `ai/langchain_runtime/service.py`

When an action has `command` but no `command_spec` and no `skill_name`:

```python
if not command_spec and not skill_name and command:
    command_spec = {"tool": "generic_exec", "args": {"command": command}}
```

Then pass through normal compilation. Compilation failure → `executable=false` + `reason`.

#### 2. `_compact_command_normalizer` — Deterministic command pre-processing

**File**: `ai/followup_command_spec.py` (new function)

Pre-processes command text BEFORE `shlex.split` to fix common compact patterns.

**Pipeline (6 stages):**

```
Input: kubectllogs-nislap-lapp=temporal--since-time=X--tail=100

Stage 1 — kubectl<verb> → kubectl <verb>
  Pattern: ^kubectl(?=<known_verb>\b)
  Input:  kubectllogs-nislap-lapp=temporal--since-time=X--tail=100
  Output: kubectl logs-nislap-lapp=temporal--since-time=X--tail=100

Stage 2 — compact verb expansion
  Table: _KUBECTL_COMPACT_VERB_EXPANSIONS
  Input:  kubectl logs-nislap-lapp=temporal--since-time=X--tail=100
  Output: kubectl logs-nislap-lapp=temporal--since-time=X--tail=100
  (no compact verb in this example)

Stage 3 — <verb>-<flag> → <verb> -<flag>
  Pattern: split known verb from any attached short flags
  Verb list: reuse the kubectl verb pattern from _normalize_action_command (service.py:285-289) to keep in sync
  Input:  kubectl logs-nislap-lapp=temporal--since-time=X--tail=100
  Output: kubectl logs -nislap-lapp=temporal--since-time=X--tail=100

Stage 4 — <value>--<flag> → <value> --<flag>
  Pattern: (?<=\S)--
  Input:  kubectl logs -nislap-lapp=temporal--since-time=X--tail=100
  Output: kubectl logs -nislap-lapp=temporal --since-time=X --tail=100

Stage 5 — short flag glue -n<ns>, -l<sel>, -o<fmt>
  Pattern: -n| -l| -o followed by value without space
  Input:  kubectl logs -nislap-lapp=temporal --since-time=X --tail=100
  Output: kubectl logs -n islap -l app=temporal --since-time=X --tail=100

Stage 6 — short flag concatenation -A-lapp → -A -lapp
  Pattern: (-[A-Za-z])(?=-[A-Za-z])
  Input:  kubectl get pods -A-lapp=temporal
  Output: kubectl get pods -A -l app=temporal

Output: kubectl logs -n islap -l app=temporal --since-time=X --tail=100
```

**Key design decisions:**
- All regexes are contextual (word boundaries, known verbs) to avoid false positives
- Order matters: flag expansion (Stage 5/6) must happen after value--flag split (Stage 4)
- No LLM dependency — fully deterministic
- Existing `_repair_generic_exec_command_for_suggestion` (followup_command_spec.py:668)
  provides validated regex patterns that can be reused
- `_normalize_action_command` (service.py:251) already handles similar patterns but
  at a different pipeline stage — its regexes should be extracted into
  `followup_command_spec.py` as shared utilities

#### 3. `compile_followup_command_spec` integration

**File**: `ai/followup_command_spec.py` — in `generic_exec` branch (~L948-954)

```python
# BEFORE:
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

#### 4. LLM Repair fallthrough fix

**File**: `ai/langchain_runtime/service.py` — `_repair_malformed_action_commands` (~L664)

When LLM repair returns `fixed == original`, treat it as a failed repair and fall through
to the heuristic `_normalize_action_command`, instead of accepting the unchanged command.

```python
if original in repair_map:
    repair = repair_map[original]
    if repair["fixed"] == original:
        # LLM repair returned same string — treat as failure, try heuristic
        fixed = _normalize_action_command(original)
        if fixed and fixed != original:
            try:
                fixed_argv = shlex.split(fixed)
            except Exception:
                fixed_argv = fixed.split()
            _set_action_command(
                structured.actions[item["index"]],
                fixed, fixed_argv,
            )
            updated += 1
    else:
        # Use LLM's repair result
        _set_action_command(
            structured.actions[item["index"]],
            repair["fixed"],
            repair.get("fixed_argv") or [],
        )
        updated += 1
```

Note: After Phase B stabilizes, the entire LLM repair path can be removed since
`_compact_command_normalizer` in the compiler handles all common patterns.

### File Changes Summary

| File | Change | Risk |
|------|--------|------|
| `ai/followup_command_spec.py` | Add `_compact_command_normalizer()`, integrate into `compile_followup_command_spec` | Low — new code, existing paths unchanged |
| `ai/langchain_runtime/service.py` | Add auto-wrap in `_extract_structured_actions` | Low — only affects actions without command_spec |
| `ai/langchain_runtime/service.py` | Fix LLM repair fallthrough in `_repair_malformed_action_commands` | Low — enhances existing logic |

### Test Scenarios

| # | Input | Expected Output | Validation |
|---|-------|-----------------|------------|
| 1 | `kubectlgetpods-A-lapp=temporal` | `kubectl get pods -A -l app=temporal` | shlex.split → 7 tokens |
| 2 | `kubectllogs-nislap-lapp=temporal--since-time=X--tail=100` | `kubectl logs -n islap -l app=temporal --since-time=X --tail=100` | shlex.split → 11 tokens |
| 3 | `kubectl get pods -A` (already correct) | unchanged | no-op |
| 4 | `echo hello` (non-kubectl) | unchanged | no-op |
| 5 | `kubectldescribepodnginx-X` | `kubectl describe pod nginx-X` | verify expansion |
| 6 | action with `skill_name`, no `command_spec` | not wrapped, `executable=true` | unchanged behavior |
| 7 | `curl -X POST http://...` (blocked) | unchanged, caught by existing blocked operator check | compiler rejects |

### Migration Plan

**Phase A** (implement first, deploy immediately):
- Add `_compact_command_normalizer` to `followup_command_spec.py`
- Integrate into `compile_followup_command_spec` for `generic_exec`
- Add auto-wrap in `_extract_structured_actions`
- Fix LLM repair fallthrough

**Phase B** (after Phase A stabilizes, ~1 week):
- Verify no new session blocks from malformed commands in production
- Remove `_repair_malformed_action_commands` LLM call
- Remove `COMMAND_REPAIR_PROMPT`

**Phase C** (cleanup):
- Deduplicate regex logic between `_normalize_action_command` and `_compact_command_normalizer`
- Consider removing `_repair_generic_exec_command_for_suggestion` if fully replaced

### Rollback

Each phase is an independent commit. Rollback by reverting the specific phase commit.
Phase A changes are additive — no existing behavior is removed, so rollback is safe.
