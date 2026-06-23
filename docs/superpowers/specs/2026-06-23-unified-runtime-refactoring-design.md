# 双运行时统一重构设计

**日期**: 2026-06-23
**状态**: 设计评审中

## 1. 概述

### 1.1 问题

Logoscope AI Service 当前有 3 个互不兼容的诊断执行引擎：

| 引擎 | 位置 | 流式输出 | 状态 |
|------|------|---------|------|
| `run_diagnosis()` | `ai/runtime/engine.py` | ❌ 无 token 流 | 通过 `unified_diagnosis_bridge` 调用 |
| `ClaudeSdkBackend` | `ai/runtime_v4/backend/claude_sdk_backend.py` | ✅ `assistant_delta` | 同步接口 + 线程 hack |
| `LangGraphBackend` | `ai/runtime_v4/backend/langgraph_backend.py` | ❌ 仅返回 summary | 同步，不参与诊断循环 |
| `_run_followup_auto_exec_react_loop` | `api/ai.py` (inline) | ❌ v1 遗留 | 等待删除 |

此外，`_run_follow_up_analysis_core`（~700 行）混在一起做三件事：共享设置构建、运行时选择、结果后处理。它的前 550 行共享设置大量调用 `_v1_helpers/`，是删除所有 v1 遗留代码的阻塞点。

### 1.2 目标

1. **提取共享上下文**：将 `_run_follow_up_analysis_core` 的前段拆为 `DiagnosisContext`，消除 `_v1_helpers/` 的外部依赖
2. **统一后端接口**：两个运行时实现同一个 `DiagnosisBackend`（async ABC），可无缝切换
3. **双端流式输出**：LangGraph 后端增加 token 流，前端不感知后端类型
4. **删除 v1 代码**：全部 `_v1_helpers/`、`_followup_compat.py` 中未导出的部分、遗留测试文件

### 1.3 非目标

- ❌ 不合并两个运行时为一个——Claude SDK 保留原生工具调用，LangGraph 保留规则编排
- ❌ 不改动安全/审批逻辑（`ai/command/security.py`）
- ❌ 不改动 YAML 技能定义格式
- ❌ 不改动执行层（ToolAdapter、exec-service、toolbox-gateway）
- ❌ 不改动前端事件消费逻辑

## 2. 架构

### 2.1 新依赖图

```
api/ai.py endpoint
  │
  ├─ build_diagnosis_context(request) → DiagnosisContext
  │    └─ 纯函数，不执行命令，不依赖 _v1_helpers
  │
  ├─ get_backend(context) → DiagnosisBackend
  │    └─ 根据配置选择 claude-sdk / langgraph
  │
  ├─ backend.run(BackendRequest) → BackendResult
  │    ├─ [claude-sdk] Messages API + YAML skills → ToolAdapter
  │    └─ [langgraph]  PromptBuilder + LLMService → ToolAdapter
  │
  └─ post_process_result(result) → dict
       └─ metrics、answer 重写、observations 合并
```

### 2.2 共享基础设施（不变）

```
ai/command/
  spec.py            — CommandSpec / CommandType / RiskLevel / ToolType
  security.py        — evaluate_command() + SessionCostState
  normalizer.py      — normalize_command_spec()
  compiler.py        — compile_command()

ai/skills/
  *.yaml             — 声明式技能定义
  loader.py          — YAML → SkillStep / @tool 定义
  base.py            — DiagnosticSkill / SkillContext / SkillStep

ai/runtime/
  state.py           — RuntimeState / Action / Observation / EvidenceSlot
  memory.py          — SessionMemory（去重、结果记录）
  events.py          — EventEmitter（事件发布-订阅）
  tools.py           — ToolAdapter（命令执行适配器）
  prompt.py          — PromptBuilder（提示词构建）
```

## 3. 模块设计

### 3.1 `ai/diagnosis/context.py` — 诊断上下文构建

**数据模型：**

```python
@dataclass
class DiagnosisContext:
    # ── 会话标识 ──
    session_id: str
    conversation_id: str
    source_target: Optional[Dict[str, Any]]
    
    # ── 问题和上下文 ──
    question: str                              # 脱敏后的问题
    analysis_context: Dict[str, Any]           # 脱敏后的分析上下文
    
    # ── 历史 ──
    history: List[Dict[str, Any]]              # 对话历史
    compacted_summary: str                     # 压缩摘要
    
    # ── 记忆 ──
    long_term_memory: Dict[str, Any]
    react_memory: Dict[str, Any]
    runtime_thread_memory: Dict[str, Any]
    
    # ── 推理产物 ──
    subgoals: List[Dict[str, Any]]
    reflection: Dict[str, Any]
    planner_prompt: str
    
    # ── 动作 ──
    followup_actions: List[Dict[str, Any]]
    executed_commands_set: Set[str]
    prior_action_observations: List[Dict[str, Any]]
    evidence_gap_queue_for_execution: List[str]
    answer_summary_seed: str
    
    # ── LLM ──
    llm_enabled: bool
    llm_requested: bool
    token_budget: int
    token_estimation: int
    followup_engine: str
    
    # ── 运行时 ──
    timeout_profile: Dict[str, Any]
    deadline_ts: float
    show_thought: bool
    
    # ── 回调 ──
    event_callback: Optional[Callable]
    run_blocking: Callable
```

**构建函数：**

```python
async def build_diagnosis_context(
    request: FollowUpRequest,
    session_store: Any,
    *,
    storage: Any,
    llm_service: Any,
) -> DiagnosisContext:
    """执行所有共享前置逻辑，返回 DiagnosisContext。
    
    这是 _run_follow_up_analysis_core 第 6491-7047 行的纯提取。
    不执行任何命令，不产生副作用（除创建/查询 session）。
    """
```

**函数内部时序：**

1. 输入验证 + 脱敏（question, analysis_context）
2. 会话创建（`_ensure_followup_analysis_session`）
3. 历史加载（含超时/降级）
4. 重复问题检测（lab mode）
5. 长期记忆构建（含超时/降级）
6. 引用和 context pills 构建
7. React memory 加载（含超时/降级）
8. Runtime thread memory 加载
9. 子目标分解 + reflection 构建 + planner_prompt
10. 用户消息 upsert
11. LLM 回答生成（调用 `_resolve_followup_engine` / 规则模式）
12. Followup actions 构建 + 优先级排序
13. 初始证据缺口提取
14. 返回 `DiagnosisContext`

**副作用：** 唯一副作用是 session 创建/查询和 memory 加载（都需要异步 IO）。这些都是纯读取，不修改全局状态。

### 3.2 `ai/runtime/backend.py` — 统一后端接口

```python
@dataclass
class BackendRequest:
    context: DiagnosisContext
    event_emitter: EventEmitter
    tools: ToolAdapter
    memory: SessionMemory

@dataclass
class BackendResult:
    actions: List[Dict[str, Any]]
    action_observations: List[Dict[str, Any]]
    iterations: List[Dict[str, Any]]
    summary: str

class DiagnosisBackend(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        ...
    
    @abstractmethod
    async def run(self, request: BackendRequest) -> BackendResult:
        ...
```

注册机制：

```python
_registry: Dict[str, Type[DiagnosisBackend]] = {}

def register_backend(name: str, cls: Type[DiagnosisBackend]):
    _registry[name] = cls

def get_backend(name: Optional[str] = None) -> DiagnosisBackend:
    """获取后端实例。name 为 None 时从环境变量读取默认后端。"""
    if name is None:
        name = os.getenv("AI_RUNTIME_BACKEND", "claude-sdk")
    cls = _registry.get(name)
    if cls is None:
        raise KeyError(f"Unknown backend: {name}, available: {list(_registry.keys())}")
    return cls()
```

### 3.3 `ai/runtime/backends/` — 两个后端的实现

#### 3.3.1 `ai/runtime/backends/claude_sdk.py`

**现状：** `ai/runtime_v4/backend/claude_sdk_backend.py`（580 行）

**改造：**

```python
class ClaudeSdkBackend(DiagnosisBackend):
    name = "claude-sdk"
    
    async def run(self, request: BackendRequest) -> BackendResult:
        # 1. 加载 YAML skills → Claude @tool 定义
        tools = _load_skills_as_tools()
        
        # 2. 构建 system_prompt（注入 context 中的历史、LTM、reflection）
        system_prompt = build_system_prompt_from_context(request.context)
        
        # 3. 初始化 Messages API 消息列表
        messages = _build_messages(request.context)
        
        # 4. 执行 agent 循环（最大 turns 由配置决定）
        return await _run_claude_loop(
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
            event_emitter=request.event_emitter,
            source_target=request.context.source_target,
        )
```

主要改动：
- 移除 `RuntimeBackend`（同步 Protocol）继承，改为 `DiagnosisBackend`
- 不再需要线程 hack（run 是 native async）
- `event_emitter` 从外部注入（不再内部创建）
- system_prompt 从 `DiagnosisContext` 构建（包含历史、LTM、reflection）

**`_run_claude_loop` 保持 Messages API 原生调用不变**（~250 行代码无需改动）

#### 3.3.2 `ai/runtime/backends/langgraph.py`

**现状：** `run_diagnosis()` 在 `ai/runtime/engine.py` + `LangGraphBackend` 在 `ai/runtime_v4/backend/langgraph_backend.py`

**改造：**

```python
class LangGraphBackend(DiagnosisBackend):
    name = "langgraph"
    
    async def run(self, request: BackendRequest) -> BackendResult:
        # 1. 构建 RuntimeState（从 DiagnosisContext 注入数据）
        state = _build_runtime_state(request.context)
        
        # 2. 调用 run_diagnosis（即现有 engine）
        result = await run_diagnosis(
            state=state,
            tools=request.tools,
            memory=request.memory,
            event_emitter=request.event_emitter,
            llm_call=_build_llm_call(request.context),
            logger=logger,
        )
        
        # 3. 转换结果格式
        return _to_backend_result(state, result)
```

主要改动：
- 移除 `LangGraphBackend`（同步）的旧实现
- 直接包装 `run_diagnosis()`，不再经过 LangGraph 状态机
- 从 `DiagnosisContext` 注入历史、LTM、reflection 到 `RuntimeState`
- 现有的 `ai/runtime_v4/langgraph/` 目录保留但不再作为后端主要入口

#### 3.3.3 后端选择逻辑

```python
# api/ai.py（post-refactor）

context = await build_diagnosis_context(request, ...)

backend = get_backend()  # AI_RUNTIME_BACKEND=claude-sdk | langgraph

event_emitter = EventEmitter()
tools = ToolAdapter()
memory = SessionMemory()

request = BackendRequest(
    context=context,
    event_emitter=event_emitter,
    tools=tools,
    memory=memory,
)

# 两个后端都通过统一的 event_emitter 推送流式事件
# event_callback 从 DiagnosisContext 中传入
if context.event_callback:
    queue = event_emitter.subscribe(context.session_id)
    # 启动 event relay（同现有 unified_diagnosis_bridge 的模式）

result = await backend.run(request)

# 后续处理（不变）
promoted_actions, action_observations = _merge_result(result, context)
# ... metrics、answer 重写 ...
```

### 3.4 流式输出

#### 3.4.1 Claude SDK（已有，保持）

```python
# claude_sdk_backend.py 中的 _stream_llm_turn
async with client.messages.stream(...) as stream:
    async for event in stream:
        if event.type == "content_block_delta":
            delta = event.delta
            if getattr(delta, "type", None) == "text_delta":
                text = _as_str(getattr(delta, "text", ""))
                if text:
                    collected_text += text
                    # 通过注入的 event_emitter 推送
                    await event_emitter.emit(run_id, "assistant_delta", {"text": text})
```

#### 3.4.2 LangGraph / `run_diagnosis()`（新增）

改动点：`run_diagnosis()` 的 LLM 规划阶段

```python
# engine.py — PLAN 阶段（当前）
plan = await plan_fn(system_prompt, task_prompt, tool_schema, state, memory, llm_call)

# 改为流式
plan = await _stream_llm_plan(
    plan_fn, system_prompt, task_prompt, tool_schema, 
    state, memory, llm_call,
    event_emitter,  # 新增
)
```

`_stream_llm_plan` 的实现：

```python
async def _stream_llm_plan(plan_fn, sp, tp, schema, state, mem, lc, ee):
    """流式 LLM 规划：边收集边推送 token。"""
    if lc is None:
        return LlmPlanResult(actions=[], summary="no LLM configured")
    
    # 要求 llm_call 支持流式返回
    collected = ""
    async for chunk in lc(sp, tp, schema):
        collected += chunk
        if ee:
            await ee.emit(state.run_id, "assistant_delta", {"text": chunk})
    
    # 解析 JSON（等效当前 _default_llm_plan 的 JSON 解析逻辑）
    return _parse_llm_result(collected)
```

要求 `LLMService` 增加流式方法：

```python
# ai/llm_service.py
async def chat_stream(self, message: str, *, context=None, response_format=None) -> AsyncIterator[str]:
    """流式 chat 调用，逐 token 产出字符串。"""
    # 适配 OpenAI / DeepSeek 流式 API
    # 返回 AsyncIterator[str]
```

### 3.5 删除 `_v1_helpers/`

条件：以上三步完成后验证通过。

**删除清单：**

```
删除文件：
  ai/command/_v1_helpers/__init__.py
  ai/command/_v1_helpers/context_helpers.py
  ai/command/_v1_helpers/persistence_helpers.py
  ai/command/_v1_helpers/prompt_helpers.py
  ai/command/_v1_helpers/react_helpers.py
  ai/command/_v1_helpers/runtime_helpers.py
  ai/command/_v1_helpers/session_helpers.py
  ai/command/_v1_helpers/orchestration_helpers.py
  ai/command/_v1_helpers/planning_helpers.py
  ai/command/_v1_helpers/v2_adapter.py
  ai/command/_v1_helpers/langchain_runtime/__init__.py
  ai/command/_v1_helpers/langchain_runtime/memory.py
  ai/command/_v1_helpers/langchain_runtime/prompts.py
  ai/command/_v1_helpers/langchain_runtime/schemas.py
  ai/command/_v1_helpers/langchain_runtime/service.py
  ai/command/_v1_helpers/langchain_runtime/tools.py

保留但清理：
  ai/command/_followup_compat.py  — 仅保留 _followup_compat.py 中 export 的函数签名
  ai/command/line_normalizer.py   — 仅保留 ai/skills/base.py 等引用的函数
  
保留不变：
  ai/command/security.py
  ai/command/normalizer.py
  ai/command/compiler.py
  ai/command/spec.py
```

**api/ai.py 中需要清理的遗留函数**（约 15 个）：

```python
_followup_* 前缀的内部函数 — 待确认是否继续使用后逐一评估：
  _followup_event              → replaced by EventEmitter
  _followup_analysis_session   → in DiagnosisContext
  _followup_history            → in DiagnosisContext
  _followup_long_term_memory   → in DiagnosisContext
  _followup_context_pills      → in DiagnosisContext
  _followup_react_memory       → in DiagnosisContext
  _followup_subgoals           → in DiagnosisContext
  _followup_reflection         → in DiagnosisContext
  _followup_actions            → in DiagnosisContext
  _followup_user_message       → in DiagnosisContext
  _followup_answer             → in DiagnosisContext
  _followup_planner_prompt     → in DiagnosisContext
  _followup_auto_exec_react_loop → deleted (replaced by DiagnosisBackend)
```

## 4. 实施计划

### Phase 0（1-2 天）：抽取 DiagnosisContext

```
文件: ai/diagnosis/context.py (~550 行)
动作: 从 api/ai.py:6491-7047 中纯提取，不改变逻辑
测试: pytest 全部通过
```

### Phase 1（1-2 天）：创建统一接口 + 后端路由

```
文件:
  ai/runtime/backend.py           — DiagnosisBackend ABC + registry
  ai/runtime/backends/__init__.py
  ai/runtime/backends/claude_sdk.py   — 从 _v4 搬过来，改接口
  ai/runtime/backends/langgraph.py    — 新的 wrapper
  
动作:
  - 两个后端都实现 DiagnosisBackend
  - api/ai.py 中替换 unified_diagnosis_bridge 调用
  - api/ai.py 中 enable/disable 逻辑被 backend = get_backend() 替代
```

### Phase 2（1 天）：LangGraph 流式输出

```
文件:
  ai/runtime/engine.py             — _stream_llm_plan 新增
  ai/llm_service.py                — chat_stream() 新增
  
动作:
  - LangGraph 后端通过 event_emitter 推送 assistant_delta
  - 两个后端的事件格式完全一致
```

### Phase 3（1 天）：删除 v1 代码

```
动作:
  - 删除 _v1_helpers/ 目录
  - 删除 langchain_runtime/ 目录
  - 删除遗留测试文件
  - 清理 ai/command/_followup_compat.py 中未导出的函数
  - 精简 api/ai.py 中不再需要的函数
  - grep 验证无残留导入
```

### Phase 4（0.5 天）：验证+修复

```
动作:
  - pytest 全部通过
  - 手动测试两个后端切换
  - 验证流式输出（两个后端）
  - 验证审批流程
```

## 5. 向后兼容

- `build_diagnosis_context()` 的输入和 `_run_follow_up_analysis_core` 的前段完全兼容
- `BackendResult` 的输出 schema 和现有 `unified_diagnosis_bridge` 的输出完全一致（`{actions, action_observations, react_loop, react_iterations}`）
- 事件类型（`assistant_delta`、`tool_call_started/finished`、`thought`、`plan`、`action`）完全不变
- 前端代码无需改动

## 6. 检查清单

- [ ] DiagnosisContext 不依赖 `_v1_helpers` 中的任何函数
- [ ] 两个后端通过 `event_emitter` 推送相同的事件格式
- [ ] `AI_RUNTIME_BACKEND=claude-sdk` 和 `AI_RUNTIME_BACKEND=langgraph` 可切换
- [ ] 所有 `_v1_helpers/` 导入已消除
- [ ] `grep -rn "from ai.followup\|from ai.langchain_runtime"` 返回空
- [ ] pytest 全部通过
- [ ] 流式输出在两个后端下都正确
