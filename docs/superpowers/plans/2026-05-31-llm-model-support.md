# LLM Model Support Enhancement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add model catalog API, frontend model selection UI, and fix session model downgrade issue.

**Architecture:** Three independent layers: (1) backend model catalog API serves known models per provider, (2) frontend uses it to show suggestions, (3) follow-up session model inheritance prefers current config over history.

**Tech Stack:** Python 3.10+, FastAPI, React 18 + TypeScript + TailwindCSS

---

### Task 1: Add PROVIDER_MODELS and requested_model to LLMResponse

**Files:**
- Modify: `ai-service/ai/llm_service.py` (around line 172 `LLMResponse` dataclass, line 293 `OpenAIProvider.generate()`, line 119 `_resolve_llm_model()`)

- [ ] **Step 1: Add `PROVIDER_MODELS` dict**

Add after the imports and helper functions (before `LLMConfig` at line 172):

```python
# Known models per provider, used by GET /api/v1/ai/llm/models
PROVIDER_MODELS: Dict[str, List[str]] = {
    "deepseek": [
        "deepseek-chat",
        "deepseek-reasoner",
        "deepseek-v4-flash",
        "deepseek-v4-pro",
    ],
    "openai": [
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-4-turbo",
        "gpt-3.5-turbo",
    ],
    "claude": [
        "claude-sonnet-4-6",
        "claude-opus-4-7",
        "claude-haiku-4-5",
        "claude-sonnet-4-20250514",
    ],
    "local": [],
}

def get_provider_models(provider: str) -> List[str]:
    """Return known models for the given provider (case-insensitive)."""
    return PROVIDER_MODELS.get(provider.strip().lower(), [])
```

- [ ] **Step 2: Add `requested_model` field to LLMResponse**

```python
@dataclass
class LLMResponse:
    content: str
    model: str
    requested_model: str = ""  # 实际请求的模型名，用于追踪降级
    provider: str
    usage: Dict[str, int] = field(default_factory=dict)
    cached: bool = False
    latency_ms: int = 0
    error: Optional[str] = None
```

- [ ] **Step 3: Track requested_model in OpenAIProvider.generate()**

In `OpenAIProvider.generate()` (around line 329-341), add `requested_model` to the return:

```python
            response = await client.chat.completions.create(**create_kwargs)

            content = response.choices[0].message.content
            latency_ms = int((datetime.now() - start_time).total_seconds() * 1000)

            self._set_cache(cache_key, content)

            return LLMResponse(
                content=content,
                model=response.model,
                requested_model=kwargs.get("model", self.config.model),
                provider=self.provider_name,
                usage=_usage_to_dict(getattr(response, "usage", None)),
                latency_ms=latency_ms,
            )
```

- [ ] **Step 4: Also add requested_model to error return and cached return paths**

```python
            # Cached path (around line 302-308)
            return LLMResponse(
                content=cached,
                model=self.config.model,
                requested_model=self.config.model,
                provider=self.provider_name,
                cached=True,
            )

            # Error path (around line 346-351)
            return LLMResponse(
                content="",
                model=self.config.model,
                requested_model=self.config.model,
                provider=self.provider_name,
                error=str(e),
            )
```

- [ ] **Step 5: Track requested_model in LLMService.analyze_log()**

In `llm_service.py:analyze_log()` around line 751-754:

```python
        result = _parse_llm_json(response.content)
        if result is not None:
            result["cached"] = response.cached
            result["latency_ms"] = response.latency_ms
            result["model"] = response.model
            result["requested_model"] = response.requested_model  # 新增
            return result
```

- [ ] **Step 6: Verify syntax**

```bash
cd /root/logoscope && python3 -m py_compile ai-service/ai/llm_service.py
```

Expected: no output.

- [ ] **Step 7: Commit**

```bash
cd /root/logoscope && git add ai-service/ai/llm_service.py && git commit -m "feat: add PROVIDER_MODELS dict and requested_model tracking to LLMResponse"
```

---

### Task 2: Add GET /llm/models endpoint

**Files:**
- Modify: `ai-service/api/ai.py`

- [ ] **Step 1: Add import for get_provider_models at the top of api/ai.py**

```python
from ai.llm_service import get_provider_models, PROVIDER_MODELS
```

Check existing imports to find the right section.

- [ ] **Step 2: Add the GET /llm/models endpoint**

Find the existing llm runtime endpoints (around line 4364), add a new endpoint after `GET /llm/runtime`:

```python
@router.get("/llm/models")
async def get_llm_available_models(provider: str = "") -> Dict[str, Any]:
    """
    获取指定 provider 的可用模型列表。
    若未指定 provider，返回所有 provider 的模型字典。
    """
    if provider:
        models = get_provider_models(provider)
        return {"provider": provider.lower(), "models": models}
    return {"models": PROVIDER_MODELS}
```

- [ ] **Step 3: Verify syntax**

```bash
cd /root/logoscope && python3 -m py_compile ai-service/api/ai.py
```

Expected: no output.

- [ ] **Step 4: Commit**

```bash
cd /root/logoscope && git add ai-service/api/ai.py && git commit -m "feat: add GET /api/v1/ai/llm/models endpoint for model catalog"
```

---

### Task 3: Fix follow-up session model inheritance

**Files:**
- Modify: `ai-service/ai/followup_session_helpers.py` (around line 72 `_build_followup_session_seed`)

- [ ] **Step 1: Add import at the top**

```python
import os
```
(Check if already imported.)

- [ ] **Step 2: Modify `_build_followup_session_seed`**

Replace the `"llm_model"` line:

```python
    # 优先使用当前运行时配置的模型，避免从历史会话继承旧模型名
    inherited_model = _as_str((analysis_context.get("llm_info") or {}).get("model"))
    current_model = _as_str(os.getenv("LLM_MODEL")).strip()
    effective_model = current_model or inherited_model

    return {
        # ... (all other fields unchanged)
        "llm_model": effective_model,
        # ...
    }
```

Full function after change:

```python
def _build_followup_session_seed(
    analysis_context: Dict[str, Any],
    question: str,
    *,
    extract_overview_summary: Callable[[Dict[str, Any]], str],
    llm_provider: str,
) -> Dict[str, Any]:
    result_payload = analysis_context.get("result")
    normalized_result = result_payload if isinstance(result_payload, dict) else {}
    inherited_model = _as_str((analysis_context.get("llm_info") or {}).get("model"))
    current_model = _as_str(os.getenv("LLM_MODEL")).strip()
    effective_model = current_model or inherited_model
    return {
        "analysis_type": _as_str(analysis_context.get("analysis_type"), "log"),
        "service_name": _as_str(analysis_context.get("service_name")),
        "input_text": _as_str(analysis_context.get("input_text"), question),
        "trace_id": _as_str(analysis_context.get("trace_id")),
        "context": analysis_context,
        "result": {
            "summary": extract_overview_summary(normalized_result),
            "raw": normalized_result,
        },
        "analysis_method": _as_str((analysis_context.get("llm_info") or {}).get("method")),
        "llm_model": effective_model,
        "llm_provider": _as_str(llm_provider),
    }
```

- [ ] **Step 3: Verify syntax**

```bash
cd /root/logoscope && python3 -m py_compile ai-service/ai/followup_session_helpers.py
```

- [ ] **Step 4: Run existing tests**

```bash
cd /root/logoscope/ai-service && python3 -m pytest tests/test_followup_session_helpers.py -x -q --no-cov 2>&1 | tail -10
```

Expected: tests pass.

- [ ] **Step 5: Commit**

```bash
cd /root/logoscope && git add ai-service/ai/followup_session_helpers.py && git commit -m "fix: prioritize current LLM_MODEL config over inherited analysis_context.llm_info.model"
```

---

### Task 4: Add frontend model selection UI

**Files:**
- Modify: `frontend/src/pages/Settings.tsx`

- [ ] **Step 1: Add loadModels state and fetch function**

Find the existing LLM runtime state declarations (around line 1346), add:

```typescript
const [availableModels, setAvailableModels] = useState<string[]>([]);
const [loadingModels, setLoadingModels] = useState(false);
```

Add a fetch function:

```typescript
const loadModelList = useCallback(async (provider: string) => {
  if (!provider) {
    setAvailableModels([]);
    return;
  }
  setLoadingModels(true);
  try {
    const data = await api.getLLMAvailableModels(provider);
    if (data?.models && Array.isArray(data.models)) {
      setAvailableModels(data.models);
    } else {
      setAvailableModels([]);
    }
  } catch {
    setAvailableModels([]);
  } finally {
    setLoadingModels(false);
  }
}, []);
```

- [ ] **Step 2: Add API call to the frontend api.ts**

Find the LLM runtime functions in `frontend/src/utils/api.ts`, add:

```typescript
async getLLMAvailableModels(provider: string): Promise<{ provider: string; models: string[] }> {
  const params = new URLSearchParams({ provider });
  const resp = await this.get(`/api/v1/ai/llm/models?${params.toString()}`);
  return resp.data as { provider: string; models: string[] };
}
```

- [ ] **Step 3: Trigger model list load when provider changes**

In the provider `<select>` onChange handler, add a call to `loadModelList`:

```typescript
const handleProviderChange = (newProvider: string) => {
  setForm(prev => ({ ...prev, provider: newProvider }));
  loadModelList(newProvider);
};
```

- [ ] **Step 4: Replace model <input> with input + datalist**

Find the model input section (around line 1418-1428), replace:

```tsx
{/* Before: pure text input */}
<input
  type="text"
  value={form.model}
  onChange={(e) => setForm(prev => ({ ...prev, model: e.target.value }))}
  placeholder="例如: gpt-4o-mini / claude-3-5-sonnet"
  className="..."
/>
```

With:

```tsx
{/* After: input with datalist suggestions */}
<input
  type="text"
  value={form.model}
  onChange={(e) => setForm(prev => ({ ...prev, model: e.target.value }))}
  placeholder="例如: gpt-4o-mini / claude-3-5-sonnet"
  list="llm-model-suggestions"
  className="..."
/>
<datalist id="llm-model-suggestions">
  {availableModels.map((m) => (
    <option key={m} value={m} />
  ))}
</datalist>
{loadingModels && <span className="text-xs text-gray-400 ml-2">加载中...</span>}
```

- [ ] **Step 5: Verify frontend builds**

```bash
cd /root/logoscope/frontend && npm run build 2>&1 | tail -5
```

Expected: build succeeds.

- [ ] **Step 6: Commit**

```bash
cd /root/logoscope && git add frontend/src/pages/Settings.tsx frontend/src/utils/api.ts && git commit -m "feat: add model list suggestions datalist to LLM runtime config page"
```

---

### Task 5: Show requested vs actual model in AIAnalysis page

**Files:**
- Modify: `frontend/src/pages/AIAnalysis.tsx`

- [ ] **Step 1: Extend llmInfo type to include requested_model**

Find the `useState` declaration at line 1841:

```typescript
const [llmInfo, setLLMInfo] = useState<{
  method?: string;
  model?: string;
  requested_model?: string;  // 新增
  cached?: boolean;
  latency_ms?: number;
} | null>(null);
```

- [ ] **Step 2: Persist requested_model when setting llmInfo**

Find where llmInfo is set from the analysis result (around line 2576-2581):

```typescript
setLLMInfo({
  method: recoveredResult.analysis_method || historySession.analysis_method || 'history',
  model: recoveredResult.model || historySession.llm_model,
  requested_model: recoveredResult.requested_model,  // 新增
  cached: recoveredResult.cached,
  latency_ms: recoveredResult.latency_ms,
});
```

Also update other `setLLMInfo` calls similarly (search for all `setLLMInfo({` occurrences — there are several for different code paths).

- [ ] **Step 3: Update the display component**

Find the LLM info display section (around line 6890-6900), update to show mismatch:

```tsx
{llmInfo && (
  <div className="text-xs text-gray-500 mt-1">
    {llmInfo.method === 'llm' ? (
      <>
        <span className="font-medium">LLM</span>
        {llmInfo.model && (
          <span className="text-gray-400">
            ({llmInfo.requested_model && llmInfo.requested_model !== llmInfo.model
              ? `${llmInfo.requested_model} → ${llmInfo.model} ⚠️`
              : llmInfo.model})
          </span>
        )}
        {llmInfo.cached && <span className="text-green-500 ml-1">缓存</span>}
        {llmInfo.latency_ms && <span className="ml-1">{llmInfo.latency_ms}ms</span>}
      </>
    ) : llmInfo.method === 'none' ? (
      <span className="text-gray-400">规则分析</span>
    ) : null}
  </div>
)}
```

- [ ] **Step 4: Verify frontend builds**

```bash
cd /root/logoscope/frontend && npm run build 2>&1 | tail -5
```

Expected: build succeeds.

- [ ] **Step 5: Commit**

```bash
cd /root/logoscope && git add frontend/src/pages/AIAnalysis.tsx && git commit -m "feat: show requested vs actual model difference in AIAnalysis page"
```
