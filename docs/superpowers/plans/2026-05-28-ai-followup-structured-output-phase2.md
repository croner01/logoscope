# Phase 2: NL 命令提取 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Phase 1 的 retry + sanitize 都失败后，用轻量 LLM 调用将自然语言诊断文本转换为结构化 `command_spec`，避免 `planning_incomplete` blocked。

**Architecture:** 在 `run_followup_langchain()` 的 Phase 1 retry 块之后、`fallback_builder` 之前，插入 `_extract_commands_from_nl()`。该函数用精简 prompt 调用同 `llm_service`（非流式、短超时），将 NL 文本中的诊断命令提取为 `generic_exec` 的 `command_spec` 条目。成功则构建 `StructuredAnswer`，失败则走原有 fallback（无回归）。

**Tech Stack:** Python 3.10+, asyncio, Pydantic v2

---

### Task 1: 新增 `NL_COMMAND_EXTRACTION_PROMPT` 常量

**Files:**
- Modify: `ai/langchain_runtime/prompts.py` — 新增 prompt 常量

- [ ] **Step 1: 在 prompts.py 中追加常量定义**

在 `FOLLOWUP_USER_TEMPLATE` 和辅助函数之后，添加新常量：

```python
NL_COMMAND_EXTRACTION_PROMPT = """你是一个 SRE 诊断命令提取器。以下是一段 AI 对可观测性问题的自然语言分析文本，请从中提取可用于进一步诊断的命令。

要求：
1. 只基于文本中明确提到的命令，不编造
2. 输出 JSON 数组，每个元素包含：
   - title: 命令的简短标题
   - action: 动作描述
   - command_spec: {"tool": "generic_exec", "args": {"command": "...", "target_kind": "k8s_cluster", "target_identity": "namespace:islap", "timeout_s": 30}}
   - expected_outcome: 预期结果
3. 文本中没有明确命令时返回 []
4. 不要包含任何非 JSON 内容

分析文本：
{nl_text}

原始问题：
{original_question}"""
```

- [ ] **Step 2: 验证文件语法**

```bash
cd ai-service && python -c "from ai.langchain_runtime.prompts import NL_COMMAND_EXTRACTION_PROMPT; print('OK:', NL_COMMAND_EXTRACTION_PROMPT[:80])"
```
Expected: `OK: 你是一个 SRE 诊断命令提取器...`

- [ ] **Step 3: 提交**

```bash
git add ai/langchain_runtime/prompts.py
git commit -m "feat(ai-service): add NL_COMMAND_EXTRACTION_PROMPT constant"
```

---

### Task 2: 新增 `_extract_commands_from_nl()` 函数

**Files:**
- Modify: `ai/langchain_runtime/service.py` — 在 `_looks_like_json_payload` 附近新增函数
- Test: `tests/test_langchain_runtime_service.py` — 新增 `_ExtractionMockLLM` 和测试

- [ ] **Step 1: 在 service.py 中添加函数**

在 `_looks_like_json_payload()` 函数之后（约 line 443），新增：

```python
async def _extract_commands_from_nl(
    *,
    llm_service: Any,
    nl_text: str,
    original_question: str,
    timeout_seconds: int,
) -> Optional[List[Dict[str, Any]]]:
    """
    从 LLM 返回的自然语言文本中提取可执行诊断命令。
    返回 list[dict]（可直接用于构建 ActionItem）或 None。
    """
    from ai.langchain_runtime.prompts import NL_COMMAND_EXTRACTION_PROMPT

    extraction_prompt = NL_COMMAND_EXTRACTION_PROMPT.format(
        nl_text=nl_text[:4000],
        original_question=original_question[:500],
    )

    try:
        result = await collect_chat_response(
            llm_service=llm_service,
            message=extraction_prompt,
            context={"engine": "langchain_nl_extraction"},
            total_timeout_seconds=timeout_seconds,
            first_token_timeout_seconds=max(1, timeout_seconds // 2),
            on_token=None,
        )
    except Exception:
        logger.warning("NL extraction LLM call failed", exc_info=True)
        return None

    raw = _as_str(result).strip()
    if not raw:
        return None
    if raw.startswith("```"):
        # 去掉可能的 markdown 代码块标记
        raw = raw.strip("`").strip()
        if raw.startswith("json"):
            raw = raw[4:].strip()

    try:
        entries = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("NL extraction result is not valid JSON")
        return None

    if not isinstance(entries, list) or not entries:
        return None

    # 转换为 ActionItem 兼容的 dict 列表
    actions = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        command_spec = entry.get("command_spec") or {}
        action = {
            "priority": int(entry.get("priority", 1)),
            "title": entry.get("title", ""),
            "action": entry.get("action", ""),
            "command_spec": command_spec,
            "expected_outcome": entry.get("expected_outcome", ""),
        }
        if command_spec.get("command") or (command_spec.get("args") or {}).get("command"):
            actions.append(action)

    return actions if actions else None
```

- [ ] **Step 2: 验证语法**

```bash
cd ai-service && python -c "
from ai.langchain_runtime.service import _extract_commands_from_nl
import inspect
print('OK: function exists')
print(inspect.signature(_extract_commands_from_nl))
"
```
Expected: 无导入错误，打印函数签名

- [ ] **Step 3: 提交**

```bash
git add ai/langchain_runtime/service.py
git commit -m "feat(ai-service): add _extract_commands_from_nl() function"
```

---

### Task 3: 在 `run_followup_langchain()` 中插入 NL 提取钩子

**Files:**
- Modify: `ai/langchain_runtime/service.py` — 在 retry 块之后、fallback 之前插入

- [ ] **Step 1: 定位钩入点**

在 Phase 1 retry 块之后、`if structured is None:` 内部，约 line 1048，找到：

```python
    if structured is None:
        # === Retry: 仅当 response 不是 JSON 时重试 ===
        ...
        if structured is None:
            # === 这里插入 NL 提取 ===
            ...
        
        if structured is None:
            # 原有 fallback 路径
```

- [ ] **Step 2: 插入 NL 提取代码**

在 retry 块之后、fallback 之前添加：

```python
if structured is None:
    # === NL 命令提取 (Phase 2): 仅当文本是非 JSON 时 ===
    if not _looks_like_json_payload(answer_text):
        try:
            nl_actions = await _extract_commands_from_nl(
                llm_service=llm_service,
                nl_text=answer_text,
                original_question=question,
                timeout_seconds=max(5, int(llm_timeout_seconds * 0.4)),
            )
        except Exception:
            logger.warning("NL command extraction failed", exc_info=True)
            nl_actions = None

        if nl_actions:
            structured = StructuredAnswer(
                conclusion="从自然语言分析中提取诊断命令",
                summary="LLM 返回了自然语言分析，已提取可执行命令",
                actions=[ActionItem(**a) for a in nl_actions],
            )

if structured is None:
    # 原有 fallback 路径 (不变)
```

注意：`timeout_seconds` 用的是 `llm_timeout_seconds` 变量，需确认该变量在此作用域可用。在 `run_followup_langchain()` 中找到该变量名（Phase 1 代码中为 `llm_timeout_seconds`）。

- [ ] **Step 3: 确认变量作用域**

```bash
cd ai-service && grep -n "llm_timeout_seconds" ai/langchain_runtime/service.py | head -10
```
Expected: 确认变量在主函数作用域中定义

- [ ] **Step 4: 提交**

```bash
git add ai/langchain_runtime/service.py
git commit -m "feat(ai-service): integrate NL command extraction hook in run_followup_langchain"
```

---

### Task 4: 编写测试用例

**Files:**
- Modify: `tests/test_langchain_runtime_service.py`

需新增 3 个测试 + 1 个 mock LLM 类。

- [ ] **Step 1: 添加 mock LLM 类**

在文件末尾 `_RetryLLMService` class 之后、最后一个测试之前，添加 3 个 mock 类：

```python
class _NLCommandExtractionMockLLM:
    """主调用返回 NL，Phase 1 重试返回 NL，NL 提取返回合法 JSON actions。"""
    def __init__(self):
        self.call_count = 0
        self.extraction_inputs = []

    async def chat(self, message, context=None, **kwargs):
        return '{"conclusion":"fallback","summary":"fallback","actions":[]}'

    async def chat_stream(self, message, context=None, **kwargs):
        self.call_count += 1
        if self.call_count == 1:
            yield "这个问题需要先查看 query-service 的日志。建议使用 kubectl logs 命令。"
        elif self.call_count == 2:
            yield "还需要进一步确认问题，建议查看详细的错误信息。"
        elif self.call_count == 3:
            self.extraction_inputs.append(str(message))
            yield (
                '[{"title":"查看日志","action":"kubectl logs",'
                '"command_spec":{"tool":"generic_exec","args":{'
                '"command":"kubectl logs deploy/query-service -n islap --tail=20",'
                '"target_kind":"k8s_cluster","target_identity":"namespace:islap","timeout_s":30'
                '}},'
                '"expected_outcome":"确认是否持续报错"}]'
            )


class _NLExtractionEmptyMockLLM:
    """主调用返回 NL，NL 提取返回空数组。"""
    def __init__(self):
        self.call_count = 0

    async def chat(self, message, context=None, **kwargs):
        return '{"conclusion":"fallback","summary":"fallback","actions":[]}'

    async def chat_stream(self, message, context=None, **kwargs):
        self.call_count += 1
        if self.call_count == 1:
            yield "这个问题需要进一步排查。"
        elif self.call_count == 2:
            yield "需要先查看日志才能确定问题。"
        elif self.call_count == 3:
            yield "[]"
        else:
            yield ""


class _NLExtractionFailMockLLM:
    """主调用返回 NL，NL 提取调用抛出异常。"""
    def __init__(self):
        self.call_count = 0

    async def chat(self, message, context=None, **kwargs):
        return '{"conclusion":"fallback","summary":"fallback","actions":[]}'

    async def chat_stream(self, message, context=None, **kwargs):
        self.call_count += 1
        if self.call_count == 1:
            yield "这个问题需要先看日志。"
        elif self.call_count == 2:
            yield "需要进一步排查原因。"
        elif self.call_count == 3:
            raise RuntimeError("NL extraction mock failure")
        else:
            yield ""
```

- [ ] **Step 2: 测试 1 — NL 提取成功路径**

```python
def test_run_followup_langchain_nl_extraction_success():
    """NL 提取成功时，actions 包含提取的命令。"""
    llm = _NLCommandExtractionMockLLM()

    async def _run():
        return await run_followup_langchain(
            **_build_runtime_kwargs(llm),
            stream_token_callback=None,
        )

    result = asyncio.run(_run())

    assert llm.call_count == 3  # 主调用 + retry + NL 提取
    assert result["analysis_method"] == "langchain"
    assert len(result.get("actions") or []) == 1
    assert result["actions"][0]["command_spec"]["tool"] == "generic_exec"
```

- [ ] **Step 3: 测试 2 — NL 提取返回空数组**

```python
def test_run_followup_langchain_nl_extraction_empty():
    """NL 提取返回空数组时，走原有 fallback 路径。"""
    llm = _NLExtractionEmptyMockLLM()

    async def _run():
        return await run_followup_langchain(
            **_build_runtime_kwargs(llm),
            stream_token_callback=None,
        )

    result = asyncio.run(_run())

    assert result["analysis_method"] == "langchain"
    # 走 fallback 路径，answer 为 "fallback"
    assert result["answer"] == "fallback"
```

- [ ] **Step 4: 运行测试确认失败**

先跑现有测试确保环境正常：

```bash
cd ai-service && python -m pytest tests/test_langchain_runtime_service.py -x -v 2>&1 | tail -30
```

- [ ] **Step 5: 提交测试文件**

```bash
git add tests/test_langchain_runtime_service.py
git commit -m "test(ai-service): add NL extraction tests for Phase 2"
```

---

### Task 5: 运行完整测试套件 + 最终提交

- [ ] **Step 1: 运行完整测试套件**

```bash
cd ai-service && python -m pytest tests/ -x --timeout=60 2>&1 | tail -40
```
Expected: 无新增失败（可接受与 Phase 1 相同的预存失败）

- [ ] **Step 2: 检查是否有遗漏的 import 或类型错误**

```bash
cd ai-service && python -c "from ai.langchain_runtime.service import run_followup_langchain, _extract_commands_from_nl; print('OK')"
```

- [ ] **Step 3: 最终提交（包含所有未提交更改）**

```bash
git add -u
git commit -m "feat(ai-service): complete Phase 2 NL command extraction"
```
