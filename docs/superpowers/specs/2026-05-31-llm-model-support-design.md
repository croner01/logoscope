# LLM 运行时配置增强设计方案

## 背景

用户将 AI Service 的 LLM 模型配置为 `deepseek-v4-flash`（DeepSeek 官方模型）后，发现分析会话中显示的模型名降级为 `deepseek-chat`。同时，当前配置页面只提供自由文本输入框，用户需要知道准确的模型名才能配置。

## 目标

1. **模型选择 UI 增强**：在设置页面增加按 provider 分组的模型建议列表
2. **降级修复**：修复后续会话从历史记录继承旧模型名的问题
3. **可观测性增强**：追踪 requested_model vs response.model 差异

## 方案（方案 A）

### Section 1: 后端模型目录 API

**文件：**
- 新增：`ai-service/ai/llm_service.py:PROVIDER_MODELS` 字典
- 新增：`ai-service/api/ai.py` → `GET /api/v1/ai/llm/models`

**PROVIDER_MODELS 定义：**

```python
# ai/llm_service.py
PROVIDER_MODELS = {
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
```

**API：**

```python
@router.get("/llm/models")
async def get_llm_models(provider: str = "") -> Dict[str, Any]:
    if provider:
        models = PROVIDER_MODELS.get(provider.lower(), [])
        return {"provider": provider, "models": models}
    return {"models": PROVIDER_MODELS}
```

模型列表当前硬编码，后续可扩展为从环境变量或配置文件加载。

### Section 2: 前端模型选择 UI

**文件：**
- 修改：`frontend/src/pages/Settings.tsx`

**改动：**

1. `handleLoadLLMModels` — 选择 provider 时调用 `GET /api/v1/ai/llm/models?provider=xxx`，获取模型列表
2. 将 model 输入框从纯 `<input>` 改为带 `<datalist>` 建议的输入框，用户可选也可手动输入
3. provider 切换时重新拉取模型列表
4. 不影响现有提交逻辑，model 字段仍是自由文本

**数据流：**

```
选择 provider "deepseek"
  → 前端 GET /api/v1/ai/llm/models?provider=deepseek
  → 返回 ["deepseek-chat", "deepseek-reasoner", "deepseek-v4-flash", "deepseek-v4-pro"]
  → 渲染 datalist 建议
  → 用户选择或手动输入 model
  → 提交到 POST /api/v1/ai/llm/runtime/update
```

### Section 3: 降级修复 + 追踪

**文件：**
- 修改：`ai-service/ai/followup_session_helpers.py`
- 修改：`ai-service/ai/llm_service.py`
- 修改：`ai-service/ai/followup_session_helpers.py` / `ai-service/api/ai.py`

**3a. followup 会话模型回退修复**

在 `_build_followup_session_seed` 中，优先使用当前环境变量 `LLM_MODEL` 配置的模型，而不是直接继承 `analysis_context.llm_info.model`：

```python
# followup_session_helpers.py
inherited_model = _as_str((analysis_context.get("llm_info") or {}).get("model"))
current_model = _as_str(os.getenv("LLM_MODEL")).strip()
effective_model = current_model or inherited_model
```

当用户更新配置后发起追问时，`llm_model` 将反映当前配置而非历史会话的旧模型。

**3b. LLMResponse 增加 requested_model 字段**

```python
@dataclass
class LLMResponse:
    content: str
    model: str          # API 返回的模型名（response.model）
    requested_model: str  # 实际请求的模型名（self.config.model）
    provider: str
    ...
```

在 `OpenAIProvider.generate()` 中记录：
```python
return LLMResponse(
    content=content,
    model=response.model,
    requested_model=self.config.model,
    ...
)
```

**3c. session 保存两个模型字段**

`_persist_analysis_session` 中增加 `requested_model` 保存到 context/result。

**3d. 前端展示**

`llmInfo` 增加 `requested_model` 字段，展示时如果两者不一致给出提示标记（⚠️ 降级）。

## 改动文件清单

| 文件 | 改动类型 | 内容 |
|------|----------|------|
| `ai-service/ai/llm_service.py` | 修改 | 新增 `PROVIDER_MODELS` 字典；`LLMResponse` 增加 `requested_model` 字段；`OpenAIProvider.generate()` 增加 tracking |
| `ai-service/api/ai.py` | 修改 | 新增 `GET /llm/models` 端点 |
| `ai-service/ai/followup_session_helpers.py` | 修改 | `_build_followup_session_seed` 中优先使用当前配置的模型 |
| `ai-service/api/ai.py` 或 `ai-service/ai/followup_persistence_helpers.py` | 修改 | session 保存 `requested_model` |
| `frontend/src/pages/Settings.tsx` | 修改 | model 输入框增加 datalist 建议，provider 切换时加载模型列表 |
| `frontend/src/pages/AIAnalysis.tsx` | 修改 | `llmInfo` 展示 requested vs actual 差异 |

## 风险与注意事项

- 模型列表硬编码在 backend，DeepSeek 更新模型名时需要同步更新代码
- `requested_model` 只对 LLM 分析有效（非 rule-based），rule-based 路径没有模型信息
- multi-worker 下 `reset_llm_service()` 只影响当前 worker，但单 Pod 部署不受影响
