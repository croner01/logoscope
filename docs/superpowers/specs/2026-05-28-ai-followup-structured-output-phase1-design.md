# Phase 1: LLM Structured Output Enforcement for AI Followup

**Date:** 2026-05-28
**Status:** Approved Design

## Problem

Followup analysis runs (`followup_analysis` mode) frequently end in `planning_incomplete`
blocked state because the LLM (deepseek-chat) returns plain natural language text instead of
structured JSON with `command_spec`. The system prompt already contains JSON format
instructions (`FOLLOWUP_SYSTEM_PROMPT` rule 4: "输出 JSON，不要 markdown"), but
deepseek-chat routinely ignores them and outputs diagnostic prose.

## Root Cause

Two missing layers compared to industry standard AI SRE agent architecture:

1. **No API-level format enforcement** — the code relies solely on prompt instructions
   without enabling `response_format` at the LLM API level. DeepSeek supports
   `response_format: {"type": "json_object"}`, which guarantees valid JSON output.
2. **No retry on parse failure** — when the LLM returns non-JSON text, the system
   immediately falls back to `actions=[]` without attempting to re-prompt the LLM
   with a format correction.

## Scope

Phase 1 addresses these two gaps only. Covers the `langchain` followup engine path
(`run_followup_langchain`). Claude provider and other engines are out of scope.

## Design

### Layer 0: API-level JSON enforcement

**Call chain to modify:**

```
langchain_runtime/service.py          llm_stream_helpers.py          llm_service.py
run_followup_langchain()  ─►  collect_chat_response()      ─►  LLMService.chat()
  add response_format           accept & forward                 ─►  OpenAIProvider.generate()
                                                                     pass to SDK client.chat.completions.create()
```

#### 1. `ai/llm_service.py`

`LLMConfig` dataclass: add optional `response_format` field.

`LLMService.chat()` and `chat_stream()`: accept optional `response_format` parameter,
forward it to `self._provider.generate()` / `generate_stream()` via kwargs.

`OpenAIProvider.generate()` and `generate_stream()`: extract `response_format` from
kwargs and pass to `client.chat.completions.create()`. DeepSeekProvider inherits
this automatically.

#### 2. `ai/llm_stream_helpers.py` `collect_chat_response()`

Add optional `response_format` parameter to signature. Forward to all three
`llm_service.chat()` / `stream_fn()` call sites (lines 41, 46, 93).

#### 3. `ai/langchain_runtime/service.py` `run_followup_langchain()`

Before calling `collect_chat_response()`, check `llm_service.config.provider`.
If provider is `deepseek` or `openai`, set
`response_format={"type": "json_object"}`.

#### 4. `ai/langchain_runtime/prompts.py` `FOLLOWUP_SYSTEM_PROMPT`

Move JSON requirement to the first sentence. Add stronger negative instruction:
"你必须只输出 JSON，不要输出任何非 JSON 内容，不要解释，不要 markdown 标记。"

This is also required by DeepSeek's `response_format: json_object` — the prompt must
contain the word "json".

#### 5. `ai/langchain_runtime/service.py` `_build_format_instructions()`

Append a concrete JSON example to the static schema fallback to help the LLM
understand the expected shape.

### Layer 1: Retry on parse failure

Inserted in `run_followup_langchain()` between `_parse_structured_answer()` failure
and the existing fallback return (langchain_runtime/service.py ~line 1005-1040).

**Logic:**

```
raw LLM response
  ↓
_parse_structured_answer()
  ↓
success → continue (no change)
  ↓ failure AND text is not JSON-like
retry once with correction prompt appended
  ↓
collect_chat_response() (no streaming, same timeout)
  ↓
_parse_structured_answer() again
  ↓
success → use retry result
  ↓ failure → original fallback path (unchanged)
```

**Retry message format:**

```
{original_full_prompt}

【格式纠正】你之前的回答没有使用要求的 JSON 格式。
请严格按照输出格式要求重新生成，只输出合法 JSON，不要多余文字。
之前的回答（仅供参考，不要重复）：
{previous_answer_text[:2000]}
```

**Rules:**
- Max 1 retry per LLM call.
- Retry uses `on_token=None` (no streaming) to avoid confusing the frontend with
  two separate response streams.
- Retry uses the same `response_format` setting.
- Retry share the original timeout budget (no extension).
- Retry is skipped if the original response looks like JSON but failed schema
  validation — that's a content/quality issue, not a format issue.

### Files Changed

| File | Change | Risk |
|------|--------|------|
| `ai/llm_service.py` | `LLMConfig.response_format`, `chat()` / `chat_stream()` pass-through | Low |
| `ai/llm_stream_helpers.py` | `collect_chat_response()` add + forward param | Low |
| `ai/langchain_runtime/service.py` | Enable `response_format` for deepseek/openai; add retry loop | Medium |
| `ai/langchain_runtime/prompts.py` | Strengthen JSON instruction in system prompt | Low |

### Out of Scope (Phase 2+)

- Claude provider structured output (requires tool_use path)
- Command inference from natural language (`build_command_spec_self_repair_payload` enhancement)
- `planning_incomplete` gate relaxation
- `waiting_user_input` downgrade path

## Testing

- Existing `test_followup_planning_helpers.py`, `test_agent_runtime_api.py`,
  `test_llm_stream_helpers.py` must pass unchanged.
- Manual: verify `run_followup_langchain` with deepseek-chat returns JSON
  when `response_format` is enabled.
- Manual: verify retry path by temporarily corrupting the format instructions.
