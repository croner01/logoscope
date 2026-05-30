# Phase 2: Natural Language Command Extraction for AI Followup

**Date:** 2026-05-28
**Status:** Approved Design

## Problem

Even after Phase 1's `response_format: json_object` enforcement + retry on parse failure,
the LLM (deepseek-chat) sometimes still returns natural language diagnostic prose instead of
structured JSON with `command_spec`. When this happens:

1. `_parse_structured_answer()` returns `None`
2. The text is not JSON-like, so `_sanitize_json_like_answer()` is skipped
3. The code falls through to `fallback_builder()` which returns empty actions
4. `planning_incomplete` gate blocks plan progression (50%+ spec-blocked, zero executable)

The NL diagnostic text often contains useful commands (kubectl, clickhouse-client, rg, grep)
embedded in prose, but the system discards them because they are not in structured form.

## Scope

Phase 2 adds a lightweight LLM-based command extraction step between Phase 1's retry
failure and the existing fallback path. It only triggers when the LLM returns pure natural
language (non-JSON) text after Phase 1 retry.

**In scope:**
- New `NL_COMMAND_EXTRACTION_PROMPT` in `prompts.py`
- New `_extract_commands_from_nl()` function in `service.py`
- Integration hook in `run_followup_langchain()` after retry failure
- Tests for extraction success, empty result, and exception paths

**Out of scope (future):**
- Pattern/regex-based command extraction (Phase 2+ fallback)
- `planning_incomplete` gate relaxation
- `waiting_user_input` downgrade path
- Claude provider structured output

## Design

### Data Flow

```
LLM primary call (response_format=json_object)
  ↓ returns NL text
Phase 1 retry (same response_format)
  ↓ returns NL text again (or looks-like-JSON but parse fails)
_parse_structured_answer() → None
  ↓
_looks_like_json_payload()? → No → skip sanitize
  ↓
┌─────────────────────────────────────────────────┐
│  Phase 2: _extract_commands_from_nl()           │
│  ┌───────────────────────────────────────────┐  │
│  │ NL_COMMAND_EXTRACTION_PROMPT + llm call   │  │
│  │ (non-streaming, short timeout, no json    │  │
│  │  response_format to avoid constraining    │  │
│  │  the extraction model)                    │  │
│  └───────────────────────────────────────────┘  │
│  ↓ success → build StructuredAnswer from result │
│  ↓ failure → None → existing fallback path      │
└─────────────────────────────────────────────────┘
```

### 1. `NL_COMMAND_EXTRACTION_PROMPT` (in `prompts.py`)

A self-contained extraction prompt designed to convert LLM diagnostic prose into
structured command entries. Minimal, focused, no followup context overhead.

```
你是一个 SRE 诊断命令提取器。以下是一段 AI 对可观测性问题的自然语言分析文本，
请从中提取可用于进一步诊断的命令。

要求：
1. 只基于文本中明确提到的命令，不编造
2. 输出 JSON 数组，每个元素包含：
   - title: 命令的简短标题
   - action: 动作描述
   - command_spec: {"tool": "generic_exec", "args": {"command": "...",
     "target_kind": "k8s_cluster", "target_identity": "namespace:islap",
     "timeout_s": 30}}
   - expected_outcome: 预期结果
3. 文本中没有明确命令时返回 [] 
4. 不要包含任何非 JSON 内容

分析文本：
{nl_text}

原始问题：
{original_question}
```

Key design decisions:
- **No `response_format: json_object`** — the extraction model needs flexibility to return
  `[]` (empty array), which is valid JSON but not an object. `json_object` mode forces `{}`.
- **Short context** — only `nl_text` (up to 4000 chars) and `original_question` (up to 500
  chars), not the full followup prompt.
- **Explicit `generic_exec` usage** — extraction commands are always `generic_exec` since
  they are ad-hoc diagnostic commands, not ClickHouse queries.

### 2. `_extract_commands_from_nl()` (in `service.py`)

```
async def _extract_commands_from_nl(
    *,
    llm_service: Any,
    nl_text: str,
    original_question: str,
    timeout_seconds: int,
) -> Optional[List[ActionItem]]:
```

**Logic:**
1. Build extraction prompt from template
2. Call `collect_chat_response()` (non-streaming, short timeout)
3. Parse response as JSON array
4. Convert each entry to `ActionItem` with `command_spec`
5. If no entries or parse failure → return `None`

**Error handling:**
- LLM timeout → catch exception, return `None`
- JSON parse failure → log warning, return `None`
- Empty result (`[]`) → return `None` (no commands found)

### 3. Integration in `run_followup_langchain()`

Inserted after Phase 1 retry block, before existing fallback:

```
if structured is None:
    if not _looks_like_json_payload(answer_text):
        try:
            nl_actions = await _extract_commands_from_nl(...)
        except Exception:
            nl_actions = None
        
        if nl_actions:
            structured = StructuredAnswer(
                conclusion="从自然语言分析中提取诊断命令",
                summary="LLM 返回了自然语言分析，已提取可执行命令",
                actions=nl_actions,
            )
    
    if structured is None:
        # 原有 fallback 路径 (unmodified)
```

**Guards:**
- Only fires when `not _looks_like_json_payload(answer_text)` — avoids interfering with
  JSON-like responses that failed schema validation (content issue, not format issue)
- Any exception in extraction → logged, `None` returned, original fallback unaffected
- Extraction call uses `on_token=None` (non-streaming) to avoid confusing upstream

### Files Changed

| File | Change | Risk |
|------|--------|------|
| `ai/langchain_runtime/prompts.py` | Add `NL_COMMAND_EXTRACTION_PROMPT` constant | Low |
| `ai/langchain_runtime/service.py` | Add `_extract_commands_from_nl()`, hook in retry block | Medium |
| `tests/test_langchain_runtime_service.py` | Add test for NL extraction path | Low |

### Out of Scope (Phase 2+)

- Pattern/regex-based command extraction from NL text
- `planning_incomplete` gate relaxation
- `waiting_user_input` downgrade path
- Claude provider structured output

## Testing

### Test 1: NL extraction success path

Mock LLM that:
1. First `chat_stream()` returns NL text (not JSON)
2. NL extraction call returns valid JSON array with command entries

Asserts:
- `structured` is not `None`
- `actions` contains entries with `command_spec`
- Actions have `executable` based on valid spec compilation

### Test 2: NL extraction returns empty

Mock LLM where NL extraction returns `[]`.

Asserts:
- `structured` is `None`
- Original fallback path is taken

### Test 3: NL extraction LLM exception

Mock LLM that raises exception on extraction call.

Asserts:
- Exception is caught
- `structured` is `None`
- No crash, original fallback path is taken
