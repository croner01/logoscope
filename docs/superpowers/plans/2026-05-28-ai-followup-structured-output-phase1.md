# AI Followup Structured Output Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `response_format` API enforcement and retry-on-parse-failure to the followup langchain LLM path so deepseek-chat reliably outputs JSON with structured actions instead of plain natural language text.

**Architecture:** Four incremental layers: (1) add `response_format` field to `LLMConfig` and thread it through `chat()` → provider `generate()` → SDK; (2) thread it through `collect_chat_response()`; (3) enable it for deepseek/openai providers in `run_followup_langchain()`; (4) add a single-attempt retry loop when the LLM returns non-JSON text. The prompt is also strengthened so the JSON instruction is the very first sentence.

**Tech Stack:** Python 3.10+, openai SDK, deepseek-chat (OpenAI-compatible), Anthropic SDK (Claude, out of scope)

---

### Task 1: Add `response_format` to `LLMConfig` and thread through `LLMService.chat()`/`chat_stream()`

**Files:**
- Modify: `ai/llm_service.py:173-186` (LLMConfig)
- Modify: `ai/llm_service.py:827-857` (chat/chat_stream)

- [ ] **Step 1: Add `response_format` field to `LLMConfig`**

Insert after `rate_limit`:

```python
@dataclass
class LLMConfig:
    ...
    rate_limit: int = 60  # requests per minute
    response_format: Optional[Dict[str, Any]] = None  # {"type": "json_object"} etc.
```

- [ ] **Step 2: Add `response_format` parameter to `LLMService.chat()`**

```python
async def chat(
    self,
    message: str,
    context: Dict[str, Any] = None,
    response_format: Optional[Dict[str, Any]] = None,  # new
) -> str:
```

Inside `chat()`, forward `response_format` to `self._provider.generate()` via kwargs:

```python
response = await self._provider.generate(
    prompt, system_prompt, response_format=response_format or self.config.response_format,
)
```

- [ ] **Step 3: Add `response_format` parameter to `LLMService.chat_stream()`**

```python
async def chat_stream(
    self,
    message: str,
    context: Dict[str, Any] = None,
    response_format: Optional[Dict[str, Any]] = None,  # new
) -> AsyncIterator[str]:
```

Inside `chat_stream()`, forward the same way:

```python
async for chunk in self._provider.generate_stream(
    prompt, system_prompt, response_format=response_format or self.config.response_format,
):
```

- [ ] **Step 4: Run existing tests**

```bash
cd ai-service && pytest tests/test_llm_service.py -v
```
Expected: all pass (the new parameter is optional, defaults to None, no behavioral change).

- [ ] **Step 5: Commit**

```bash
git add ai-service/ai/llm_service.py
git commit -m "feat(ai-service): add response_format to LLMConfig and chat()/chat_stream()

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 2: Thread `response_format` through `OpenAIProvider.generate()`/`generate_stream()`

**Files:**
- Modify: `ai/llm_service.py:292-346` (OpenAIProvider.generate)
- Modify: `ai/llm_service.py:348-398` (OpenAIProvider.generate_stream)

- [ ] **Step 1: Extract `response_format` from kwargs in `generate()` and pass to SDK**

Current code at lines 319-324:
```python
response = await client.chat.completions.create(
    model=kwargs.get("model", self.config.model),
    messages=messages,
    max_tokens=kwargs.get("max_tokens", self.config.max_tokens),
    temperature=kwargs.get("temperature", self.config.temperature),
)
```

Change to:
```python
create_kwargs = dict(
    model=kwargs.get("model", self.config.model),
    messages=messages,
    max_tokens=kwargs.get("max_tokens", self.config.max_tokens),
    temperature=kwargs.get("temperature", self.config.temperature),
)
response_format = kwargs.get("response_format") or getattr(self.config, 'response_format', None)
if response_format is not None:
    create_kwargs["response_format"] = response_format
response = await client.chat.completions.create(**create_kwargs)
```

- [ ] **Step 2: Same change for `generate_stream()`**

Current code at lines 363-369:
```python
stream = await client.chat.completions.create(
    model=kwargs.get("model", self.config.model),
    messages=messages,
    max_tokens=kwargs.get("max_tokens", self.config.max_tokens),
    temperature=kwargs.get("temperature", self.config.temperature),
    stream=True,
)
```

Change to:
```python
create_kwargs = dict(
    model=kwargs.get("model", self.config.model),
    messages=messages,
    max_tokens=kwargs.get("max_tokens", self.config.max_tokens),
    temperature=kwargs.get("temperature", self.config.temperature),
    stream=True,
)
response_format = kwargs.get("response_format") or getattr(self.config, 'response_format', None)
if response_format is not None:
    create_kwargs["response_format"] = response_format
stream = await client.chat.completions.create(**create_kwargs)
```

- [ ] **Step 3: Run existing tests**

```bash
cd ai-service && pytest tests/test_llm_service.py -v
```
Expected: all pass (response_format is only added when non-None, default config has it as None).

- [ ] **Step 4: Commit**

```bash
git add ai-service/ai/llm_service.py
git commit -m "feat(ai-service): pass response_format to OpenAI SDK in generate()/generate_stream()

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 3: Thread `response_format` through `collect_chat_response()`

**Files:**
- Modify: `ai/llm_stream_helpers.py:21-96`
- Test: `tests/test_llm_stream_helpers.py`
- Test: `tests/test_langchain_runtime_service.py` (DummyStreamingLLM needs **kwargs)

- [ ] **Step 1: Add `response_format` parameter and forward to all call sites**

```python
async def collect_chat_response(
    *,
    llm_service: Any,
    message: str,
    context: Optional[dict],
    total_timeout_seconds: int,
    first_token_timeout_seconds: int,
    on_token: Optional[Callable[[str], Any]] = None,
    response_format: Optional[Dict[str, Any]] = None,  # new
) -> str:
```

Forward to all 4 `llm_service.chat()`/`chat_stream()` call sites:

Line 41:
```python
llm_service.chat(message=message, context=context, response_format=response_format),
```

Line 46:
```python
stream_obj = stream_fn(message=message, context=context, response_format=response_format)
```

Line 50 (non-stream fallback inside streaming path):
```python
llm_service.chat(message=message, context=context, response_format=response_format),
```

Line 93 (stream error fallback):
```python
llm_service.chat(message=message, context=context, response_format=response_format),
```

- [ ] **Step 2: Update mock LLM classes in tests to accept `response_format`**

In `tests/test_llm_stream_helpers.py`, the mock classes (`_BrokenAfterFirstChunkLLM`, `_SlowStreamLLM`, `_FailFastStreamLLM`) define `chat()` and `chat_stream()` without a `response_format` parameter. Since Python accepts extra keyword args via `**kwargs`, add a catch-all to each:

```python
class _BrokenAfterFirstChunkLLM:
    ...
    async def chat(self, message, context=None, **kwargs):  # add **kwargs
        ...
    async def chat_stream(self, message, context=None, **kwargs):  # add **kwargs
        ...
```

Same for `_SlowStreamLLM` and `_FailFastStreamLLM`.

Also update `DummyStreamingLLM` in `tests/test_langchain_runtime_service.py`:
```python
class DummyStreamingLLM:
    def __init__(self, chunks):
        self._chunks = chunks

    async def chat_stream(self, message, context=None, **kwargs):  # add **kwargs
        for chunk in self._chunks:
            yield chunk

    async def chat(self, message, context=None, **kwargs):  # add **kwargs
        return "".join(self._chunks)
```

- [ ] **Step 3: Run existing tests**

```bash
cd ai-service && pytest tests/test_llm_stream_helpers.py -v
```
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add ai-service/ai/llm_stream_helpers.py ai-service/tests/test_llm_stream_helpers.py
git commit -m "feat(ai-service): add response_format parameter to collect_chat_response()

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 4: Strengthen system prompt for JSON output

**Files:**
- Modify: `ai/langchain_runtime/prompts.py:13-27`

- [ ] **Step 1: Move JSON requirement to first sentence**

Current:
```python
FOLLOWUP_SYSTEM_PROMPT = """你是 SRE/可观测性专家，回答必须严格遵守：
1) 只基于已给证据...
...
4) 输出 JSON，不要 markdown；
..."""
```

Change to:
```python
FOLLOWUP_SYSTEM_PROMPT = """你必须只输出 JSON，不要输出任何非 JSON 内容，不要解释，不要 markdown 标记。

你是 SRE/可观测性专家，回答必须严格遵守：
1) 只基于已给证据，不编造日志、trace、调用链；
2) 优先输出"结论 → 请求流程 → 根因 → 修复步骤 → 验证/回滚"；
3) 若证据不足，必须明确 missing_evidence；
4) 输出 JSON，不要 markdown；
5) actions 默认优先输出 command_spec（结构化命令），command 仅作兼容字段：能用 command_spec 就不要拼自由文本 shell；但若动作明确对应某个已注册诊断技能，可只输出 skill_name，由系统自动展开为结构化命令链；
6) actions 在未使用 skill_name 时必须提供 command_spec（tool + args）。SQL 查询优先用 kubectl_clickhouse_query（默认提供 target_kind=clickhouse_cluster、target_identity=database:<db>、query、timeout_s；仅旧链路兼容时才提供 pod_selector）；非 SQL 的系统查询命令用 generic_exec（必须提供 command 或 command_argv、target_kind、target_identity、timeout_s）。由系统编译成可执行命令，禁止自行压缩空格或拼接紧凑 shell；
7) command 需可执行且安全：默认优先使用 kubectl/rg/grep/cat/tail/head/jq/ls/echo/pwd 等当前自动执行链路稳定支持的只读命令；只有明确需要 HTTP/数据库直接取证时，再使用 curl（仅 GET/HEAD 或 -G 查询）或 clickhouse-client/clickhouse（仅 SELECT/SHOW/DESCRIBE/EXPLAIN 只读查询）；禁止脚本化链式拼接（| && || ;）与重定向（> >> < <<）及后台执行（&）；每个 action 只允许一条单步命令，pipeline_steps 最多 2-3 步；命令必须保留标准空格分词（命令、flag、参数之间要有空格），禁止输出 logs--tail / grep-ierror / head-20 / -it$(...) 这类紧凑写法；
8) 不能给可执行命令时，明确 executable=false 与 reason，不要伪造命令；禁止用 echo/printf 把人工说明、页面操作提示、监控检查建议包装成"伪命令"。
9) `trace_id`、`request_id`、时间窗是重要诊断锚点，但不是所有场景继续排障的硬前置；当上下文已经显示更强的故障层信号时，应先使用当前最强锚点继续取证，而不是机械要求补齐全部锚点。
10) 若症状已明显落在某一故障层，优先收集该层直接证据：读路径/慢查询优先执行与资源证据，网络问题优先连通性与端点证据，Pod 生命周期问题优先 describe/events/logs，资源问题优先 CPU/内存/配额证据，拓扑问题优先图构建与预览契约证据；不要把通用相关性补全当成默认下一步。
11) 必须遵守闭环顺序：先给"当前总结"，再基于 missing_evidence 生成命令；命令观察后再总结是否收敛；若未收敛继续补证据，直到可以给出最终结论。
12) 若上下文中列出了可用诊断技能（Diagnostic Skills），优先在 actions 中通过 skill_name 字段引用技能；使用 skill_name 时无需重复手写该技能的 command_spec，系统会自动展开为结构化命令链；仅在技能不覆盖时才手动构造 command_spec。
13) 必须使用「事件时间窗」中给出的具体时间戳: kubectl logs 用 --since-time= 而非 --since=15m；ClickHouse 查询用 toDateTime64 具体时间条件而非 now() - INTERVAL N MINUTE。如果事件时间窗未给出精确时间，从问题/日志文本中自行提取首条时间戳。

【重要】你的输出将被程序自动解析。如果输出不是合法 JSON，系统将无法处理你的诊断结果，必须重试。请确保输出是严格的 JSON 格式。"""
```

Key changes:
- Added `"你必须只输出 JSON，不要输出任何非 JSON 内容，不要解释，不要 markdown 标记。"` as first sentence
- Added closing paragraph reinforcing that non-JSON output will be rejected
- Rest of rules unchanged

- [ ] **Step 2: Run existing tests**

```bash
cd ai-service && pytest tests/test_langchain_runtime_service.py -v
```
Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add ai-service/ai/langchain_runtime/prompts.py
git commit -m "feat(ai-service): strengthen JSON output requirement in system prompt

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 5: Enable `response_format` for deepseek/openai and add retry loop in `run_followup_langchain()`

**Files:**
- Modify: `ai/langchain_runtime/service.py:975-1040`
- Test: `tests/test_langchain_runtime_service.py`

- [ ] **Step 1: Enable `response_format` for deepseek/openai providers**

Before the `collect_chat_response()` call at line 980, detect provider:

```python
prompt = build_followup_prompt(prompt_payload)
message = f"{FOLLOWUP_SYSTEM_PROMPT}\n\n{prompt}"

# NEW: enable response_format for OpenAI-compatible providers
llm_provider_name = getattr(llm_service, 'config', None) and getattr(llm_service.config, 'provider', None)
use_json_response_format = llm_provider_name in ('deepseek', 'openai', 'local')

llm_timeout_fallback = False
raw_token_stream_enabled = _should_stream_raw_tokens()
try:
    result = await collect_chat_response(
        llm_service=llm_service,
        message=message,
        context={...},
        total_timeout_seconds=...,
        first_token_timeout_seconds=...,
        on_token=...,
        response_format={"type": "json_object"} if use_json_response_format else None,  # new
    )
```

- [ ] **Step 2: Add retry loop on non-JSON response**

After `_parse_structured_answer` returns None and the response is not JSON-like, insert a retry before the existing fallback returns:

```python
structured = _parse_structured_answer(answer_text)
if structured is None:
    # === Retry: only when response is not JSON-like (format issue, not content issue) ===
    if not _looks_like_json_payload(answer_text):
        retry_message = (
            f"{message}\n\n"
            "【格式纠正】你之前的回答没有使用要求的 JSON 格式。"
            "请严格按照输出格式要求重新生成，只输出合法 JSON，不要多余文字。\n"
            "之前的回答（仅供参考，不要重复）：\n"
            f"{answer_text[:2000]}"
        )
        try:
            retry_result = await collect_chat_response(
                llm_service=llm_service,
                message=retry_message,
                context={
                    "engine": "langchain",
                    "token_budget": token_budget,
                    "token_warning": token_warning,
                    "analysis_context": analysis_context,
                    "conversation_history": compacted_history[-10:],
                    "conversation_summary": compacted_summary,
                    "long_term_memory": long_term_memory,
                    "references": references,
                    "subgoals": subgoals,
                    "reflection": reflection,
                    "tool_observations": tool_observations,
                    "raw_token_stream_enabled": False,
                },
                total_timeout_seconds=max(5, int(llm_timeout_seconds)),
                first_token_timeout_seconds=max(1, int(llm_first_token_timeout_seconds)),
                on_token=None,  # no streaming during retry
                response_format={"type": "json_object"} if use_json_response_format else None,
            )
            retry_text = _as_str(retry_result)
            if retry_text:
                answer_text = retry_text
                structured = _parse_structured_answer(answer_text)
        except Exception:
            logger.warning("LLM format retry failed, using original response", exc_info=True)

    if structured is None:
        # existing fallback (unchanged)
        if _looks_like_json_payload(answer_text):
            ...
```

Note: The `context` dict in the retry call is the same as the original at lines 983-996, except `raw_token_stream_enabled` is set to `False` to avoid streaming during retry.

- [ ] **Step 3: Run existing tests**

```bash
cd ai-service && pytest tests/test_langchain_runtime_service.py -v
```
Expected: all pass (new code only activates on non-JSON response, default mock behavior unchanged).

- [ ] **Step 4: Commit**

```bash
git add ai-service/ai/langchain_runtime/service.py
git commit -m "feat(ai-service): enable response_format for deepseek/openai and add retry on parse failure

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 6: Write tests for the retry logic

**Files:**
- Create: `tests/test_langchain_runtime_service.py` (or add to existing)

- [ ] **Step 1: Add test for non-JSON LLM response triggers retry**

Add to `tests/test_langchain_runtime_service.py`:

```python
class _RetryLLMService:
    """Mock LLM service that returns non-JSON on first call, JSON on retry."""
    def __init__(self):
        self.call_count = 0
        self.config = type('obj', (object,), {'provider': 'deepseek'})

    async def chat(self, message, context=None, response_format=None):
        self.call_count += 1
        if self.call_count == 1:
            return "这是纯文本诊断建议，没有 JSON 格式。"
        return json.dumps({
            "conclusion": "诊断结论",
            "actions": [],
            "missing_evidence": [],
            "summary": "",
        })

    async def chat_stream(self, message, context=None, response_format=None):
        self.call_count += 1
        if self.call_count == 1:
            yield "这是纯文本诊断建议，没有 JSON 格式。"
        else:
            yield json.dumps({
                "conclusion": "诊断结论",
                "actions": [],
                "missing_evidence": [],
                "summary": "",
            })


@pytest.mark.asyncio
async def test_run_followup_langchain_retry_on_non_json():
    """When LLM returns non-JSON text, retry should produce a valid answer."""
    from ai.langchain_runtime.service import run_followup_langchain

    llm_service = _RetryLLMService()

    result = await run_followup_langchain(
        question="测试问题",
        analysis_context={},
        compacted_history=[],
        compacted_summary="",
        references=[],
        subgoals=[],
        reflection={},
        long_term_memory={},
        llm_enabled=True,
        llm_requested=True,
        token_budget=0,
        token_warning=False,
        llm_timeout_seconds=30,
        llm_service=llm_service,
        fallback_builder=lambda q, ac, **kw: "fallback",
    )

    assert result.get("analysis_method") == "langchain"
    assert llm_service.call_count == 2  # original + retry
```

- [ ] **Step 2: Run the test**

```bash
cd ai-service && pytest tests/test_langchain_runtime_service.py::test_run_followup_langchain_retry_on_non_json -v
```
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add ai-service/tests/test_langchain_runtime_service.py
git commit -m "test(ai-service): add test for retry on non-JSON LLM response

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 7: Run full test suite and verify

- [ ] **Step 1: Run all ai-service tests**

```bash
cd ai-service && pytest -v 2>&1 | tail -40
```
Expected: no regressions.

- [ ] **Step 2: Commit any remaining changes**

```bash
git add -A
git commit -m "chore(ai-service): finalize structured output Phase 1 changes

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```
