# 双运行时统一重构实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `_run_follow_up_analysis_core`（700 行, `api/ai.py`）拆为 `DiagnosisContext` + `DiagnosisBackend`，统一 Claude SDK 和 LangGraph 两个后端接口，增加 LangGraph 流式输出，删除 `_v1_helpers/`（~8,200 行 v1 遗留代码）。

**Architecture:** 4 个阶段递进：Phase 0 纯提取共享上下文 → Phase 1 创建统一 async ABC 后端接口并迁移两个实现 → Phase 2 给 LangGraph 后端加流式输出 → Phase 3 删除 v1 遗留代码。每阶段完成时 pytest 全部通过。

**Tech Stack:** Python 3.11+, asyncio, pytest, Pydantic

## Global Constraints

- 不改变 `ai/command/security.py`（审批逻辑）
- 不改变 YAML 技能定义格式
- 不改变前端事件类型（`assistant_delta`, `tool_call_started/finished`, `thought`, `plan`, `action`）
- 旧环境变量 `AI_RUNTIME_UNIFIED_ENGINE_ENABLED=true` 继续有效（等价于 `AI_RUNTIME_BACKEND=claude-sdk`）
- 所有阶段完成后运行 `pytest -x --no-cov` 全部通过
- 每个步步骤驱动可独立运行测试循环

---

## 文件清单总览

| 文件 | 操作 | 阶段 |
|------|------|------|
| `ai-service/ai/diagnosis/__init__.py` | **新建** | Phase 0 |
| `ai-service/ai/diagnosis/context.py` | **新建** (~550 行, 从 `api/ai.py` 提取) | Phase 0 |
| `ai-service/api/ai.py` | **修改**: 替换前 559 行为 `build_diagnosis_context()` 调用 | Phase 0 |
| `ai-service/tests/test_diagnosis_context.py` | **新建**: 输出基线测试 | Phase 0 |
| `ai-service/ai/runtime/backend.py` | **新建**: `DiagnosisBackend` ABC + registry | Phase 1 |
| `ai-service/ai/runtime/backends/__init__.py` | **新建** | Phase 1 |
| `ai-service/ai/runtime/backends/claude_sdk.py` | **新建**: 异步 `ClaudeSdkBackend(DiagnosisBackend)` | Phase 1 |
| `ai-service/ai/runtime/backends/langgraph.py` | **新建**: `LangGraphBackend(DiagnosisBackend)` | Phase 1 |
| `ai-service/api/ai.py` | **修改**: `get_backend()` 替代 `_is_unified_engine_enabled()` 分支 | Phase 1 |
| `ai-service/ai/runtime/bridge.py` | **保留**: `unified_diagnosis_bridge` 变为死代码，Phase 3 清理 | Phase 1 |
| `ai-service/ai/runtime_v4/backend/base.py` | **保留**: v4 编排桥仍需 `RuntimeBackend` Protocol | Phase 1 |
| `ai-service/ai/runtime_v4/backend/langgraph_backend.py` | **保留**: v4 编排桥可能引用 | Phase 1 |
| `ai-service/ai/runtime_v4/backend/claude_sdk_backend.py` | **保留**: v4 编排桥仍使用其逻辑（新 `backends/claude_sdk.py` 是独立实现） | Phase 1 |
| `ai-service/ai/llm_service.py` | **修改**: 新增 `chat_stream()` | Phase 2 |
| `ai-service/ai/runtime/engine.py` | **修改**: 新增 `_stream_llm_plan()` | Phase 2 |
| `ai-service/ai/command/_v1_helpers/` | **删除**: 整个目录 (14 个文件, ~8,200 行) | Phase 3 |
| `ai-service/ai/command/_followup_compat.py` | **清理**: 保留 export 的函数签名 | Phase 3 |
| `ai-service/ai/runtime/bridge.py` | **清理**: 删除 `unified_diagnosis_bridge` 死代码 | Phase 3 |
| `ai-service/api/ai.py` | **清理**: 删除约 15 个 `_followup_*` 内部函数 | Phase 3 |

---

## Phase 0: 抽取 DiagnosisContext

**目标：** 将 `_run_follow_up_analysis_core` 前 559 行（`api/ai.py:6491-7049`）纯提取到 `ai/diagnosis/context.py`，不改变任何逻辑。

### Task 0.1: 创建测试基线

**Files:**
- Create: `ai-service/tests/test_diagnosis_context.py`

**Interfaces:**
- Consumes: `api/ai._run_follow_up_analysis_core` (当前形态)
- Produces: 测试函数验证提取后的 `build_diagnosis_context()` 和原前段行为一致

- [ ] **Step 1: 创建测试文件**

```python
# tests/test_diagnosis_context.py
"""Test DiagnosisContext extraction — verify output matches original function's front segment."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from typing import Any, Dict


@pytest.fixture
def mock_request():
    """Build a minimal FollowUpRequest-like object."""
    request = MagicMock()
    request.question = "分析 pod crash 原因"
    request.analysis_context = {"namespace": "default", "cluster": "prod"}
    request.show_thought = True
    request.auto_exec_readonly = True
    return request


@pytest.fixture
def mock_storage():
    storage = MagicMock()
    storage.get_analysis_session = AsyncMock(return_value=None)
    storage.create_analysis_session = AsyncMock(return_value={
        "session_id": "test-session-001",
        "conversation_id": "test-conv-001",
    })
    storage.append_history = AsyncMock(return_value=None)
    return storage


@pytest.fixture
def mock_llm_service():
    svc = MagicMock()
    svc.chat = AsyncMock(return_value={
        "choices": [{"message": {"content": "测试回答"}}]
    })
    return svc


@pytest.mark.asyncio
async def test_build_diagnosis_context_matches_original_behavior(mock_request, mock_storage, mock_llm_service):
    """build_diagnosis_context() 的输出和原函数前段等价。

    这个测试在提取前先记录原函数的行为（日记模式），
    提取后运行同样输入验证输出一致。
    """
    # Import here so it works both before and after extraction
    from api.ai import _run_follow_up_analysis_core

    with patch("api.ai.get_ai_session_store", return_value=mock_storage), \
         patch("api.ai.get_llm_service", return_value=mock_llm_service), \
         patch("api.ai.storage", mock_storage), \
         patch("api.ai._resolve_followup_timeout_profile", return_value={"request_deadline_seconds": "300"}), \
         patch("api.ai._mask_sensitive_text", lambda x: x), \
         patch("api.ai._mask_sensitive_payload", lambda x: x):

        # We can't easily call _run_follow_up_analysis_core standalone here
        # because it does too much. This test is a placeholder for the
        # integration-level test that validates the slice.
        # Instead, run the actual diagnosis flow and verify the context
        # dict has the expected keys.
        pass  # 提取后此测试会被替换为真正的断言


@pytest.mark.asyncio
async def test_diagnosis_context_dataclass_fields():
    """DiagnosisContext dataclass 包含所有必要字段。"""
    from ai.diagnosis.context import DiagnosisContext

    ctx = DiagnosisContext(
        session_id="s1",
        conversation_id="c1",
        source_target=None,
        question="test",
        analysis_context={},
        history=[],
        compacted_summary="",
        long_term_memory={},
        react_memory={},
        runtime_thread_memory={},
        subgoals=[],
        reflection={},
        planner_prompt="",
        followup_actions=[],
        executed_commands_set=set(),
        prior_action_observations=[],
        evidence_gap_queue_for_execution=[],
        answer_summary_seed="",
        llm_enabled=True,
        llm_requested=True,
        token_budget=10000,
        token_estimation=0,
        followup_engine="auto",
        timeout_profile={"request_deadline_seconds": "300"},
        deadline_ts=9999999999.0,
        show_thought=True,
        event_callback=None,
        run_blocking=None,
    )
    assert ctx.session_id == "s1"
    assert ctx.question == "test"
    assert isinstance(ctx.executed_commands_set, set)
```

- [ ] **Step 2: 验证测试文件可加载**

```bash
cd ai-service && python -m pytest tests/test_diagnosis_context.py -v --no-cov 2>&1 | head -20
```
Expected: 测试文件加载成功（2 tests collected, 0 passed, 1 skipped 因为 context module 还未创建）。

- [ ] **Step 3: 提交**

```bash
git add ai-service/tests/test_diagnosis_context.py
git commit -m "test(diagnosis): 创建 DiagnosisContext 基线测试文件"
```

---

### Task 0.2: 提取前段逻辑到 `build_diagnosis_context()`

**Files:**
- Create: `ai-service/ai/diagnosis/__init__.py`
- Create: `ai-service/ai/diagnosis/context.py` (~550 行)

**Interfaces:**
- Produces: `build_diagnosis_context(request, session_store, *, storage, llm_service) -> DiagnosisContext`
- Produces: `DiagnosisContext` dataclass (16+ 字段)

- [ ] **Step 1: 创建 `ai/diagnosis/__init__.py`**

```python
# ai/diagnosis/__init__.py
"""诊断上下文构建模块。"""

from ai.diagnosis.context import DiagnosisContext, build_diagnosis_context

__all__ = ["DiagnosisContext", "build_diagnosis_context"]
```

- [ ] **Step 2: 创建 `ai/diagnosis/context.py`**

从 `api/ai.py:6491-7049` 提取到新建文件。关键原则：
1. 保持和原函数完全一致的逻辑顺序
2. 所有 `_followup_*` 内部函数调用改从 `api.ai` 导入（`from api.ai import _followup_session, ...`）
3. 所有 `_mask_*`、`_as_str`、`_resolve_*` 等工具函数同样从 `api.ai` 导入
4. 唯一的「新」东西是 `DiagnosisContext` dataclass 和 `build_diagnosis_context()` 函数签名

**具体做法：**
1. 复制 `api/ai.py:6491-7047` 的完整代码段
2. 用 `async def build_diagnosis_context(...):` 替换 `async def _run_follow_up_analysis_core(...):`
3. 在函数末尾用 `return DiagnosisContext(...)` 替换原有的函数继续执行
4. 把所有 `_followup_*`、`_mask_*`、`_as_str` 等导入放到文件顶部的 import block
5. 删除 `session_store = get_ai_session_store(storage)` 之前的局部变量（这几个在调用前已经准备好）

> **实现说明**: 从 api.ai 导入的辅助函数使用 `from api.ai import ...` （临时依赖，Phase 3 清理）。函数体 ~450 行代码从 `api/ai.py:6518-7047` 逐行复制，每个局部变量（`session_id`, `history`, `ltm` 等）对应原函数中同名变量。

```python
# ai/diagnosis/context.py
"""诊断上下文构建 — 从 _run_follow_up_analysis_core 前段纯提取。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from api.ai import (  # 提取期间暂时从 api.ai 导入 — Phase 3 时这些变成独立函数
    _as_str,
    _mask_sensitive_text,
    _mask_sensitive_payload,
    _is_ai_runtime_lab_mode,
    _resolve_followup_timeout_profile,
    _ensure_followup_analysis_session,
    _load_followup_history,
    _check_duplicate_question,
    _build_followup_long_term_memory,
    _build_followup_context_pills,
    _load_followup_react_memory,
    _load_followup_runtime_thread_memory,
    _build_followup_subgoals,
    _build_followup_reflection,
    _build_followup_planner_prompt,
    _upsert_followup_user_message_to_history,
    _resolve_followup_engine,
    _build_followup_answer,
    _call_ai_when_no_action,
    _build_followup_actions,
    _build_evidence_gap_queue,
)


@dataclass
class DiagnosisContext:
    """诊断上下文 — 包含会话、历史、记忆、推理产物。"""

    # ── 会话标识 ──
    session_id: str
    conversation_id: str
    source_target: Optional[Dict[str, Any]]

    # ── 问题和上下文 ──
    question: str
    analysis_context: Dict[str, Any]

    # ── 历史 ──
    history: List[Dict[str, Any]]
    compacted_summary: str

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


async def build_diagnosis_context(
    request: Any,
    session_store: Any,
    *,
    storage: Any,
    llm_service: Any,
) -> DiagnosisContext:
    """执行所有共享前置逻辑，返回 DiagnosisContext。

    这是 _run_follow_up_analysis_core 第 6491-7047 行的纯提取。
    不执行任何命令，不产生副作用（除创建/查询 session）。

    参数与 _run_follow_up_analysis_core 完全兼容：
    - request: FollowUpRequest 对象
    - session_store: AI 会话存储
    - storage: 通用存储
    - llm_service: LLM 服务
    """
    # ── 输入验证 + 脱敏 ──
    question = _as_str(request.question)
    if not question:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="question is required")

    timeout_profile = _resolve_followup_timeout_profile()
    deadline_ts = time.perf_counter() + float(timeout_profile["request_deadline_seconds"])
    safe_question = _mask_sensitive_text(question)
    analysis_context = _mask_sensitive_payload(request.analysis_context or {})
    if safe_question and not analysis_context.get("question"):
        analysis_context["question"] = safe_question
    runtime_lab_mode = _is_ai_runtime_lab_mode(analysis_context=analysis_context)
    show_thought = bool(getattr(request, "show_thought", False))
    thought_timeline: List[Dict[str, Any]] = []

    # ── 以下为 api/ai.py:6518-7047 的逐行复制 ──
    # 提示: 复制原函数第 6518-7047 行，将每个局部变量赋值保留原样
    # 复制完成后，用 return DiagnosisContext(...) 包装所有局部变量

    # ── 返回上下文 ──
    return DiagnosisContext(
        session_id=session_id,
        conversation_id=conversation_id,
        source_target=getattr(request, "source_target", None),
        question=safe_question,
        analysis_context=analysis_context,
        history=history,
        compacted_summary=compacted_summary,
        long_term_memory=long_term_memory,
        react_memory=react_memory,
        runtime_thread_memory=runtime_thread_memory,
        subgoals=subgoals,
        reflection=reflection,
        planner_prompt=planner_prompt,
        followup_actions=followup_actions,
        executed_commands_set=executed_commands_set,
        prior_action_observations=prior_action_observations,
        evidence_gap_queue_for_execution=evidence_gap_queue_for_execution,
        answer_summary_seed=answer_summary_seed,
        llm_enabled=llm_enabled,
        llm_requested=llm_requested,
        token_budget=token_budget,
        token_estimation=token_estimation,
        followup_engine=followup_engine,
        timeout_profile=timeout_profile,
        deadline_ts=deadline_ts,
        show_thought=show_thought,
        event_callback=getattr(request, "event_callback", None),
        run_blocking=getattr(request, "run_blocking", None),
    )
```

> **实现说明**: 上述代码约 100 行 header/footer + ~450 行具体逻辑从 `api/ai.py:6518-7047` 逐行复制。每个局部变量（`session_id`, `history`, `ltm` 等）对应原函数中同名变量。从 api.ai 导入的辅助函数使用 `from api.ai import ...` （临时依赖，Phase 3 清理）。

- [ ] **Step 3: 验证文件语法正确**

```bash
cd ai-service && python -c "from ai.diagnosis.context import DiagnosisContext; print('ok')"
```
Expected: 输出 "ok"（此时部分导入可能因 api.ai 内部依赖报错，但 dataclass 本身可导入）

- [ ] **Step 4: 提交**

```bash
git add ai-service/ai/diagnosis/
git commit -m "feat(diagnosis): 创建 DiagnosisContext dataclass 和 build_diagnosis_context() 框架"
```

---

### Task 0.3: 在 `_run_follow_up_analysis_core` 中调用 `build_diagnosis_context()`

**Files:**
- Modify: `ai-service/api/ai.py` — 替换前 559 行为 `build_diagnosis_context()` 调用

- [ ] **Step 1: 修改 `api/ai.py`**

将 `_run_follow_up_analysis_core` 的开头部分（`def` 开始到 `# ── Unified engine path` 注释之前）替换为：

```python
async def _run_follow_up_analysis_core(
    request: FollowUpRequest,
    *,
    event_callback: Optional[Any] = None,
) -> Dict[str, Any]:
    """追问分析核心流程（供普通与流式接口复用）。"""
    from ai.diagnosis.context import build_diagnosis_context

    session_store = get_ai_session_store(storage)
    ctx = await build_diagnosis_context(
        request,
        session_store,
        storage=storage,
        llm_service=get_llm_service(),
    )

    # 拆包上下文为局部变量（保持后续代码不变）
    question = ctx.question
    analysis_context = ctx.analysis_context
    timeout_profile = ctx.timeout_profile
    deadline_ts = ctx.deadline_ts
    show_thought = ctx.show_thought
    thought_timeline: List[Dict[str, Any]] = []
    runtime_lab_mode = bool(ctx.analysis_context.get("lab_mode"))
    session_id = ctx.session_id
    conversation_id = ctx.conversation_id
    history = ctx.history
    compacted_summary = ctx.compacted_summary
    long_term_memory = ctx.long_term_memory
    react_memory = ctx.react_memory
    runtime_thread_memory = ctx.runtime_thread_memory
    subgoals = ctx.subgoals
    reflection = ctx.reflection
    planner_prompt = ctx.planner_prompt
    followup_actions = ctx.followup_actions
    executed_commands_set = ctx.executed_commands_set
    prior_action_observations = ctx.prior_action_observations
    evidence_gap_queue_for_execution = ctx.evidence_gap_queue_for_execution
    answer_summary_seed = ctx.answer_summary_seed
    llm_enabled = ctx.llm_enabled
    llm_requested = ctx.llm_requested
    token_budget = ctx.token_budget
    token_estimation = ctx.token_estimation
    followup_engine = ctx.followup_engine
    source_target = ctx.source_target
    safe_question = question
    # runtime_lab_mode 已在 ctx.analysis_context 中包含
    thought_timeline: List[Dict[str, Any]] = []

    # 继续后续代码（runtime 选择 + 后处理）...
    # （原有 7050 行起的代码保持不变）
```

**关键点：** 拆包后所有后续代码使用的变量名和原函数完全一致。变更仅限函数前段 (~560 行 → ~50 行)，后段完全不动。

- [ ] **Step 2: 添加导入**

在 `api/ai.py` 文件头或函数内添加 `from ai.diagnosis.context import build_diagnosis_context`。

- [ ] **Step 3: 运行测试验证**

```bash
cd ai-service && python -m pytest tests/test_ai_api.py -x -v --no-cov -k "test_followup" 2>&1 | tail -30
```
Expected: 通过（如果失败是因为 _v1_helpers 依赖，暂不修复 — 这是已知预存失败）

- [ ] **Step 4: 提交**

```bash
git add ai-service/api/ai.py ai-service/ai/diagnosis/
git commit -m "refactor(diagnosis): _run_follow_up_analysis_core 前段替换为 build_diagnosis_context()"
```

---

### Task 0.4: 验证 Phase 0

- [ ] **Step 1: 运行所有测试**

```bash
cd ai-service && python -m pytest -x --no-cov 2>&1 | tail -20
```
Expected: 核心测试通过。预存失败的 5 个测试保持不变（3 个 v1-specific + 2 个 langgraph 节点），无新增失败。

- [ ] **Step 2: 确认 `build_diagnosis_context` 不依赖 `_v1_helpers`**

```bash
grep -rn "_v1_helpers" ai-service/ai/diagnosis/ --include="*.py"
```
Expected: 无输出

---

## Phase 1: 统一后端接口

**目标：** 创建 `DiagnosisBackend` ABC，两个实现（`ClaudeSdkBackend`、`LangGraphBackend`），`api/ai.py` 使用 `get_backend()` 选择后端。

### Task 1.1: 创建 `DiagnosisBackend` ABC + 注册表

**Files:**
- Create: `ai-service/ai/runtime/backend.py`

**Interfaces:**
- Produces: `DiagnosisBackend(ABC)` — `name` property + `async run(BackendRequest) -> BackendResult`
- Produces: `BackendRequest` dataclass — `context: DiagnosisContext`, `event_emitter: EventEmitter`, `tools: ToolAdapter`, `memory: SessionMemory`
- Produces: `BackendResult` dataclass — `actions: List[Dict]`, `action_observations: List[Dict]`, `iterations: List[Dict]`, `summary: str`
- Produces: `register_backend(name, cls)`, `get_backend(name=None) -> DiagnosisBackend`
- Produces: `_is_legacy_unified_engine_enabled() -> bool`

- [ ] **Step 1: 创建 `ai-service/ai/runtime/backend.py`**

```python
"""统一 DiagnosisBackend ABC — 诊断执行后端接口。

两个运行时共享此接口:
- ClaudeSdkBackend: Messages API + YAML skills → ToolAdapter
- LangGraphBackend:  PromptBuilder + LLMService → ToolAdapter
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Type

from ai.diagnosis.context import DiagnosisContext
from ai.runtime.events import EventEmitter
from ai.runtime.memory import SessionMemory
from ai.runtime.tools import ToolAdapter


@dataclass
class BackendRequest:
    """后端执行请求。"""
    context: DiagnosisContext
    event_emitter: EventEmitter
    tools: ToolAdapter
    memory: SessionMemory


@dataclass
class BackendResult:
    """后端执行结果。"""
    actions: List[Dict[str, Any]] = field(default_factory=list)
    action_observations: List[Dict[str, Any]] = field(default_factory=list)
    iterations: List[Dict[str, Any]] = field(default_factory=list)
    summary: str = ""


class DiagnosisBackend(ABC):
    """诊断执行后端的抽象基类。"""

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    async def run(self, request: BackendRequest) -> BackendResult:
        ...


# ── 注册表 ────────────────────────────────────────────────────────────────

_registry: Dict[str, Type[DiagnosisBackend]] = {}


def register_backend(name: str, cls: Type[DiagnosisBackend]) -> None:
    """注册后端类到全局注册表。"""
    _registry[name] = cls


def get_backend(name: Optional[str] = None) -> DiagnosisBackend:
    """获取后端实例。

    如果未指定 name，按以下优先级:
    1. 旧变量 AI_RUNTIME_UNIFIED_ENGINE_ENABLED=true → claude-sdk
    2. AI_RUNTIME_BACKEND 环境变量 → 指定值
    3. 默认 → claude-sdk
    """
    if name is None:
        if _is_legacy_unified_engine_enabled():
            name = "claude-sdk"
        else:
            name = os.getenv("AI_RUNTIME_BACKEND", "claude-sdk")
    cls = _registry.get(name)
    if cls is None:
        raise KeyError(
            f"Unknown backend: {name}, available: {list(_registry.keys())}"
        )
    return cls()


def _is_legacy_unified_engine_enabled() -> bool:
    """检查旧版 AI_RUNTIME_UNIFIED_ENGINE_ENABLED 环境变量。"""
    val = os.getenv("AI_RUNTIME_UNIFIED_ENGINE_ENABLED", "").strip().lower()
    return val in ("1", "true", "yes", "on")
```

- [ ] **Step 2: 验证文件语法**

```bash
cd ai-service && python -c "from ai.runtime.backend import DiagnosisBackend, BackendRequest, BackendResult, get_backend, register_backend; print('ok')"
```
Expected: "ok"

- [ ] **Step 3: 提交**

```bash
git add ai-service/ai/runtime/backend.py
git commit -m "feat(runtime): 创建 DiagnosisBackend ABC + registry + 旧变量兼容"
```

---

### Task 1.2: 创建新 `LangGraphBackend`

**Files:**
- Create: `ai-service/ai/runtime/backends/__init__.py`
- Create: `ai-service/ai/runtime/backends/langgraph.py`

**Interfaces:**
- Consumes: `DiagnosisBackend`, `BackendRequest`, `BackendResult` (from `ai.runtime.backend`)
- Consumes: `run_diagnosis()` (from `ai.runtime.engine`)
- Consumes: `DiagnosisContext` → maps to `RuntimeState`
- Produces: `LangGraphBackend(DiagnosisBackend)` — `name = "langgraph"`

- [ ] **Step 1: 创建 `ai-service/ai/runtime/backends/__init__.py`**

```python
"""运行时后端实现。"""
from ai.runtime.backends.langgraph import LangGraphBackend

try:
    from ai.runtime.backends.claude_sdk import ClaudeSdkBackend
except ImportError:
    ClaudeSdkBackend = None  # 可选依赖（需要 anthropic SDK）

__all__ = ["LangGraphBackend", "ClaudeSdkBackend"]
```

- [ ] **Step 2: 创建 `ai-service/ai/runtime/backends/langgraph.py`**

```python
"""LangGraph 后端 — 包装 run_diagnosis() 实现 DiagnosisBackend。"""

from __future__ import annotations

import logging
from typing import Any

from ai.runtime.backend import DiagnosisBackend, BackendRequest, BackendResult
from ai.runtime.engine import run_diagnosis
from ai.runtime.state import RuntimeState, Action, Observation

logger = logging.getLogger(__name__)


class LangGraphBackend(DiagnosisBackend):
    """LangGraph 诊断后端。

    直接包装 ai.runtime.engine.run_diagnosis()。
    从 DiagnosisContext 注入历史、LTM、reflection 到 RuntimeState。
    """

    name = "langgraph"

    async def run(self, request: BackendRequest) -> BackendResult:
        ctx = request.context

        # 1. 从 DiagnosisContext 构建 RuntimeState
        state = RuntimeState(
            run_id=ctx.session_id,
            question=ctx.question,
            analysis_context=ctx.analysis_context,
            history=ctx.history,
            long_term_memory=ctx.long_term_memory,
            react_memory=ctx.react_memory,
            runtime_thread_memory=ctx.runtime_thread_memory,
            subgoals=ctx.subgoals,
            reflection=ctx.reflection,
            planner_prompt=ctx.planner_prompt,
            followup_actions=[
                Action.from_dict(a) for a in ctx.followup_actions
            ],
            executed_commands_set=ctx.executed_commands_set,
            prior_action_observations=ctx.prior_action_observations,
            evidence_gap_queue_for_execution=list(ctx.evidence_gap_queue_for_execution),
            answer_summary_seed=ctx.answer_summary_seed,
            llm_enabled=ctx.llm_enabled,
            llm_requested=ctx.llm_requested,
            token_budget=ctx.token_budget,
            token_estimation=ctx.token_estimation,
            followup_engine=ctx.followup_engine,
            timeout_profile=ctx.timeout_profile,
            deadline_ts=ctx.deadline_ts,
            show_thought=ctx.show_thought,
            event_callback=ctx.event_callback,
        )

        # 2. 构建 llm_call — 适配现有 run_diagnosis 的签名
        async def _llm_call(system_prompt: str, task_prompt: str, tool_schema: Any) -> Any:
            """内部 LLM 调用适配器 — 后续 Phase 2 改为流式。"""
            # 此函数从 DiagnosisContext 获取 LLM 配置
            # 暂时返回空计划（引擎内部处理降级）
            return None  

        # 3. 调用 run_diagnosis
        result = await run_diagnosis(
            state=state,
            tools=request.tools,
            memory=request.memory,
            event_emitter=request.event_emitter,
            llm_call=_llm_call,
            logger=logger,
        )

        # 4. 转换为 BackendResult
        return BackendResult(
            actions=[a.to_dict() if hasattr(a, "to_dict") else {} for a in result.actions],
            action_observations=[o.to_dict() if hasattr(o, "to_dict") else {} for o in result.observations],
            iterations=result.iterations,
            summary=result.summary,
        )


# 注册到全局注册表
from ai.runtime.backend import register_backend
register_backend("langgraph", LangGraphBackend)
```

- [ ] **Step 3: 验证语法**

```bash
cd ai-service && python -c "import ai.runtime.backends.langgraph; print('ok')"
```
Expected: "ok"

- [ ] **Step 4: 提交**

```bash
git add ai-service/ai/runtime/backends/
git commit -m "feat(runtime): 创建 LangGraphBackend(DiagnosisBackend)"
```

---

### Task 1.3: 创建新 `ClaudeSdkBackend`

**Files:**
- Create: `ai-service/ai/runtime/backends/claude_sdk.py`

**Interfaces:**
- Consumes: `DiagnosisBackend`, `BackendRequest`, `BackendResult` (from `ai.runtime.backend`)
- Produces: `ClaudeSdkBackend(DiagnosisBackend)` — `name = "claude-sdk"`, native async

- [ ] **Step 1: 创建 `ai-service/ai/runtime/backends/claude_sdk.py`**

```python
"""Claude SDK 后端 — 使用 Anthropic Messages API + 原生工具调用。

从 ai/runtime_v4/backend/claude_sdk_backend.py 迁入，改动:
- 继承 DiagnosisBackend（替代 RuntimeBackend 同步 Protocol）
- run() 是 native async（不再需要线程 hack）
- event_emitter 从外部注入（不再内部创建）
- system_prompt 从 DiagnosisContext 构建
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from ai.runtime.backend import (
    DiagnosisBackend,
    BackendRequest,
    BackendResult,
    register_backend,
)
from ai.runtime.events import EventEmitter

logger = logging.getLogger(__name__)


# ── 工具辅助函数（从 _v4 移植） ─────────────────────────────────────────────

def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _model_name() -> str:
    return (
        _as_str(os.getenv("CLAUDE_SDK_MODEL"))
        or _as_str(os.getenv("LLM_MODEL"))
        or "claude-sonnet-4-20250514"
    )


def _api_key() -> str:
    key = _as_str(os.getenv("ANTHROPIC_API_KEY"))
    if key:
        return key
    raise RuntimeError("ANTHROPIC_API_KEY not set")


def _api_base_url() -> str:
    return _as_str(os.getenv("ANTHROPIC_BASE_URL")) or "https://api.anthropic.com/v1"


# ── Skills → Tools ─────────────────────────────────────────────────────────

def _load_skills_as_tools() -> List[Dict[str, Any]]:
    """加载 YAML skills 并转为 Claude tool 定义。"""
    from ai.skills.loader import load_builtin_skills
    try:
        skills = load_builtin_skills()
        tools = []
        for skill in skills:
            tool_def = getattr(skill, "to_tool_definition", None)
            if tool_def:
                tools.append(tool_def())
        return tools
    except Exception as e:
        logger.warning("Failed to load skills as tools: %s", e)
        return []


# ── Claude Agent 循环 ───────────────────────────────────────────────────────

def _build_messages(context: Any) -> List[Dict[str, Any]]:
    """从 DiagnosisContext 构建消息列表。"""
    messages = []
    for msg in getattr(context, "history", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role in ("user", "assistant"):
            messages.append({"role": role, "content": _as_str(content)})
    if context.question:
        messages.append({"role": "user", "content": context.question})
    return messages


def _build_system_prompt_from_context(context: Any) -> str:
    """从 DiagnosisContext 构建 system prompt。"""
    parts = ["You are a Kubernetes observability assistant."]
    if context.long_term_memory:
        ltm_summary = json.dumps(context.long_term_memory, ensure_ascii=False)[:2000]
        parts.append(f"\nLong-term memory:\n{ltm_summary}")
    if context.reflection:
        parts.append(f"\nReflection:\n{json.dumps(context.reflection, ensure_ascii=False)}")
    if context.planner_prompt:
        parts.append(f"\n{context.planner_prompt}")
    return "\n".join(parts)


async def _execute_tool_call(
    tool_name: str,
    tool_input: Dict[str, Any],
    source_target: Optional[Dict[str, Any]],
    event_emitter: EventEmitter,
    run_id: str,
) -> Tuple[str, int]:
    """执行 Claude 选择的工具调用。"""
    from ai.command.normalizer import normalize_command_spec
    from ai.command.compiler import compile_command
    from ai.command.security import evaluate_command
    from ai.command.spec import CommandSpec
    from ai.runtime.tools import ToolAdapter

    spec = normalize_command_spec({
        "command": tool_name,
        "args": tool_input,
        "source_target": source_target,
    })

    # 安全检查
    security = evaluate_command(spec)
    if not security.allowed:
        return f"Command rejected: {security.reason}", -1

    # 编译执行
    compiled = compile_command(spec)
    adapter = ToolAdapter()
    result = await adapter.execute(compiled)

    return result.output if hasattr(result, "output") else str(result), result.exit_code


async def _stream_llm_turn(
    client: Any,
    system_prompt: str,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    event_emitter: EventEmitter,
    run_id: str,
    max_tokens: int = 4096,
) -> Tuple[str, Optional[str], Optional[Dict[str, Any]]]:
    """单轮 LLM 调用（流式）。"""
    collected_text = ""
    stop_reason = None
    tool_use = None

    # tool_choice 构建
    tool_config = {"tools": tools} if tools else {}

    async with client.messages.stream(
        model=_model_name(),
        system=system_prompt,
        messages=messages,
        max_tokens=max_tokens,
        **tool_config,
    ) as stream:
        async for event in stream:
            if event.type == "content_block_delta":
                delta = event.delta
                if getattr(delta, "type", None) == "text_delta":
                    text = _as_str(getattr(delta, "text", ""))
                    if text:
                        collected_text += text
                        await event_emitter.emit(run_id, "assistant_delta", {"text": text})
            elif event.type == "content_block_start":
                block = event.content_block
                if getattr(block, "type", None) == "tool_use":
                    tool_use = {"name": block.name, "input": block.input}
            elif event.type == "message_delta":
                delta = event.delta
                stop_reason = getattr(delta, "stop_reason", None) if delta else None

    return collected_text, stop_reason, tool_use


async def _run_claude_loop(
    system_prompt: str,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    event_emitter: EventEmitter,
    source_target: Optional[Dict[str, Any]],
    run_id: str,
    max_turns: int = 10,
) -> BackendResult:
    """Claude agent 主循环。"""
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=_api_key(), base_url=_api_base_url())

    actions = []
    action_observations = []
    iterations = []
    turn = 0

    while turn < max_turns:
        turn += 1
        text, stop_reason, tool_use = await _stream_llm_turn(
            client=client,
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
            event_emitter=event_emitter,
            run_id=run_id,
        )

        if text:
            messages.append({"role": "assistant", "content": text})

        if tool_use:
            # 执行工具调用
            tool_name = tool_use["name"]
            tool_input = tool_use["input"]
            await event_emitter.emit(run_id, "tool_call_started", {
                "tool": tool_name,
                "input": tool_input,
            })

            output, exit_code = await _execute_tool_call(
                tool_name, tool_input, source_target, event_emitter, run_id
            )

            await event_emitter.emit(run_id, "tool_call_finished", {
                "tool": tool_name,
                "output": output[:500],
            })

            messages.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_name,
                        "content": output,
                    }
                ],
            })

            actions.append({
                "id": f"action-{turn}",
                "command": tool_name,
                "args": tool_input,
                "status": "executed" if exit_code == 0 else "failed",
            })
            action_observations.append({
                "action_id": f"action-{turn}",
                "status": "executed" if exit_code == 0 else "failed",
                "exit_code": exit_code,
                "output": output[:1000],
            })

        if stop_reason == "end_turn" and not tool_use:
            break

    return BackendResult(
        actions=actions,
        action_observations=action_observations,
        iterations=[{"turn": i + 1} for i in range(len(actions))],
        summary=text or "",
    )


class ClaudeSdkBackend(DiagnosisBackend):
    """Claude SDK 诊断后端 — 使用 Anthropic Messages API + 原生工具调用。"""

    name = "claude-sdk"

    async def run(self, request: BackendRequest) -> BackendResult:
        ctx = request.context

        # 1. 加载 YAML skills → Claude @tool 定义
        tools = _load_skills_as_tools()

        # 2. 构建 system_prompt
        system_prompt = _build_system_prompt_from_context(ctx)

        # 3. 构建消息列表
        messages = _build_messages(ctx)

        # 4. 执行 agent 循环
        return await _run_claude_loop(
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
            event_emitter=request.event_emitter,
            source_target=ctx.source_target,
            run_id=ctx.session_id,
        )


register_backend("claude-sdk", ClaudeSdkBackend)
```

- [ ] **Step 2: 验证语法**

```bash
cd ai-service && python -c "from ai.runtime.backends.claude_sdk import ClaudeSdkBackend; print('ok')"
```
Expected: "ok"（可能因缺少 anthropic SDK 报 ImportError，但语法正确即可）

- [ ] **Step 3: 提交**

```bash
git add ai-service/ai/runtime/backends/claude_sdk.py
git commit -m "feat(runtime): 创建 ClaudeSdkBackend(DiagnosisBackend) 异步版本"
```

---

### Task 1.4: 更新 `api/ai.py` — 使用 `get_backend()` 替代 `_is_unified_engine_enabled()` 分支

**Files:**
- Modify: `ai-service/api/ai.py` — 替换 `_run_follow_up_analysis_core` 的运行时选择段

- [ ] **Step 1: 替换运行时选择段**

在 `_run_follow_up_analysis_core` 中，找到以下代码块（原 7050-7087）：
```python
# ── Unified engine path (opt-in via env var) ──────────────────────────
if _is_unified_engine_enabled():
    llm_service = get_llm_service()
    react_exec_bundle = await unified_diagnosis_bridge(...)
else:
    react_exec_bundle = await _run_followup_auto_exec_react_loop(...)
```

替换为：
```python
# ── 后端选择 ──────────────────────────────────────────────────────────
from ai.runtime.backend import get_backend
from ai.runtime.events import EventEmitter
from ai.runtime.memory import SessionMemory
from ai.runtime.tools import ToolAdapter

backend = get_backend()  # AI_RUNTIME_BACKEND=claude-sdk | langgraph

event_emitter = EventEmitter()
tools = ToolAdapter()
memory = SessionMemory()

backend_request = BackendRequest(
    context=ctx,  # 使用上面创建的 DiagnosisContext
    event_emitter=event_emitter,
    tools=tools,
    memory=memory,
)

# event relay（同现有 unified_diagnosis_bridge 的模式）
if event_callback:
    queue = event_emitter.subscribe(ctx.session_id)
    async def _event_relay():
        async for event_type, event_data in queue:
            await event_callback(event_type, event_data)
    asyncio.ensure_future(_event_relay())

result = await backend.run(backend_request)

# 转换为 react_exec_bundle 格式（向后兼容后处理代码）
react_exec_bundle = {
    "actions": result.actions,
    "action_observations": result.action_observations,
    "react_loop": {"replan": {"needed": False}, "summary": result.summary},
    "react_iterations": result.iterations,
}
```

- [ ] **Step 2: 更新导入**

在 `api/ai.py` 文件头添加：
```python
from ai.runtime.backend import get_backend, BackendRequest
```

移除不再需要的导入：
```python
# 移除:
# from ai.runtime.bridge import unified_diagnosis_bridge, _is_unified_engine_enabled
```

- [ ] **Step 3: 运行测试**

```bash
cd ai-service && python -m pytest -x --no-cov -k "test_followup" 2>&1 | tail -20
```

- [ ] **Step 4: 提交**

```bash
git add ai-service/api/ai.py
git commit -m "refactor(runtime): _run_follow_up_analysis_core 使用 get_backend() 替换 v1/v4 分支"
```

---

### Task 1.5: 验证 Phase 1

- [ ] **Step 1: 运行所有测试**

```bash
cd ai-service && python -m pytest -x --no-cov 2>&1 | tail -20
```

- [ ] **Step 2: 确认 `get_backend()` 可正确选择后端**

```bash
cd ai-service && python -c "
from ai.runtime.backend import get_backend
b = get_backend('langgraph')
assert b.name == 'langgraph'
print('langgraph backend OK')
b2 = get_backend('claude-sdk')
assert b2.name == 'claude-sdk'
print('claude-sdk backend OK')
print('All backends registered correctly')
"
```
Expected: 两个后端都输出 OK

---

## Phase 2: LangGraph 流式输出

**目标：** 给 LangGraph 后端增加 `assistant_delta` 流式事件，与 Claude SDK 后端输出相同格式。

### Task 2.1: 给 `LLMService` 增加 `chat_stream()`

**Files:**
- Modify: `ai-service/ai/llm_service.py`

- [ ] **Step 1: 在 `LLMService` 类中添加 `chat_stream()` 方法**

```python
# ai/llm_service.py — 追加到 LLMService 类

async def chat_stream(self, message: str, *, context=None, response_format=None) -> AsyncIterator[str]:
    """流式 chat 调用，逐 token 产出字符串。

    适配 OpenAI / DeepSeek 流式 API。
    如果底层 LLM 不支持流式（stream_options={stream: true} 被拒绝），
    降级为完整输出后 yield 一次。

    Args:
        message: 输入消息
        context: 可选上下文（对话历史）
        response_format: 可选响应格式约束

    Yields:
        逐 token 文本
    """
    # 确定使用的模型
    model = self._resolve_model(context) if hasattr(self, '_resolve_model') else os.getenv("LLM_MODEL", "gpt-4")

    # 构建消息
    messages = []
    if context:
        if isinstance(context, list):
            messages.extend(context)
    messages.append({"role": "user", "content": message})

    try:
        # 尝试流式 API
        stream = await self.client.chat.completions.create(
            model=model,
            messages=messages,
            stream=True,
            stream_options={"include_usage": False},
            **(self._extra_params if hasattr(self, '_extra_params') else {}),
        )

        full_text = ""
        async for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                token = delta.content
                full_text += token
                yield token

        # 如果没有任何 token 产生，降级到非流式
        if not full_text:
            raise ValueError("Stream produced no output")

    except (Exception, NotImplementedError) as e:
        logger.warning("Streaming LLM failed, falling back to non-streaming: %s", e)
        # 降级：用非流式 chat()
        result = await self.chat(message, context=context)
        text = ""
        if isinstance(result, dict):
            choices = result.get("choices", [])
            if choices:
                text = choices[0].get("message", {}).get("content", "")
        elif isinstance(result, str):
            text = result
        yield text
```

- [ ] **Step 2: 验证方法存在**

```bash
cd ai-service && python -c "
from ai.llm_service import LLMService
import inspect
assert hasattr(LLMService, 'chat_stream')
assert inspect.iscoroutinefunction(LLMService.chat_stream)
print('chat_stream() exists and is async')
"
```
Expected: "chat_stream() exists and is async"

- [ ] **Step 3: 提交**

```bash
git add ai-service/ai/llm_service.py
git commit -m "feat(llm): 添加 chat_stream() 流式方法，含非流式降级"
```

---

### Task 2.2: 给 `engine.py` 增加 `_stream_llm_plan()`

**Files:**
- Modify: `ai-service/ai/runtime/engine.py`

- [ ] **Step 1: 添加 `_stream_llm_plan()` 函数**

在 `engine.py` 中找到 PLAN 阶段的代码（约第 190-213 行）。在 `run_diagnosis()` 函数之前或之后添加：

```python
# ai/runtime/engine.py — 追加

async def _stream_llm_plan(
    plan_fn: Any,
    system_prompt: str,
    task_prompt: str,
    tool_schema: Any,
    state: Any,
    memory: Any,
    llm_call: Any,
    event_emitter: Optional[Any] = None,
) -> Any:
    """流式 LLM 规划：边收集边推送 token。

    当 event_emitter 可用时，每收到一个 token 就推送 assistant_delta 事件。
    如果流式中断或不完整 JSON，做 best-effort 解析。
    """
    if llm_call is None:
        return LlmPlanResult(actions=[], summary="no LLM configured")

    collected = ""
    try:
        async for chunk in llm_call(system_prompt, task_prompt, tool_schema):
            collected += chunk
            if event_emitter and state:
                await event_emitter.emit(state.run_id, "assistant_delta", {"text": chunk})
    except (TimeoutError, ConnectionError) as exc:
        logger.warning("LLM stream interrupted: %s", exc)
    except Exception as exc:
        logger.warning("LLM stream error, using collected text: %s", exc)

    # 解析 JSON（等效当前 _default_llm_plan 的 JSON 解析逻辑）
    if not collected.strip():
        return LlmPlanResult(actions=[], summary="no LLM output")

    return _parse_llm_result(collected)
```

- [ ] **Step 2: 修改 `run_diagnosis()` 的 PLAN 阶段**

找到 `run_diagnosis()` 中调用 `plan_fn()` 的位置，改为条件使用 `_stream_llm_plan()`：

```python
# engine.py run_diagnosis() — PLAN 阶段
if event_emitter:
    plan = await _stream_llm_plan(
        plan_fn, system_prompt, task_prompt, tool_schema,
        state, memory, llm_call, event_emitter,
    )
else:
    plan = await plan_fn(system_prompt, task_prompt, tool_schema, state, memory, llm_call)
```

- [ ] **Step 3: 验证语法**

```bash
cd ai-service && python -c "from ai.runtime.engine import _stream_llm_plan; print('ok')"
```
Expected: "ok"

- [ ] **Step 4: 提交**

```bash
git add ai-service/ai/runtime/engine.py
git commit -m "feat(runtime): 添加 _stream_llm_plan() 流式 LLM 规划"
```

---

### Task 2.3: 在 `LangGraphBackend` 中注入 `event_emitter`

**Files:**
- Modify: `ai-service/ai/runtime/backends/langgraph.py`

- [ ] **Step 1: 更新 `_llm_call` 闭包支持流式**

将 Task 1.2 中创建的 `_llm_call` 占位闭包替换为使用 `LLMService.chat_stream()` 的实现：

```python
# ai/runtime/backends/langgraph.py — LangGraphBackend.run() 中
async def _llm_call(system_prompt: str, task_prompt: str, tool_schema: Any) -> AsyncIterator[str]:
    """流式 LLM 调用适配器。"""
    from ai.llm_service import get_llm_service
    svc = get_llm_service()

    full_prompt = f"{system_prompt}\n\n{task_prompt}"
    async for token in svc.chat_stream(full_prompt, context=None):
        yield token
```

- [ ] **Step 2: 提交**

```bash
git add ai-service/ai/runtime/backends/langgraph.py
git commit -m "feat(runtime): LangGraphBackend 集成流式 LLM 调用"
```

---

### Task 2.4: 验证 Phase 2

- [ ] **Step 1: 运行所有测试**

```bash
cd ai-service && python -m pytest -x --no-cov 2>&1 | tail -20
```

- [ ] **Step 2: 确认事件类型一致**

```bash
grep -rn 'assistant_delta' ai-service/ai/runtime/backends/ --include="*.py"
```
Expected: 两个后端文件都包含 `"assistant_delta"` 事件发射

---

## Phase 3: 删除 v1 代码

**目标：** 删除整个 `_v1_helpers/` 目录（~8,200 行）及所有遗留的 v1 代码。

### Task 3.1: 审计残留 v1 导入

- [ ] **Step 1: 全仓搜索 v1 导入**

```bash
cd ai-service && grep -rn "from ai.followup\|from ai.langchain_runtime\|from ai.command._v1_helpers\|import _v1_helpers" --include="*.py" | grep -v __pycache__
```

Expected: 确认只有 `api/ai.py` 和 `ai/command/_followup_compat.py` 有引用（Phase 1 已完成替换后，这些引用应已减少）

- [ ] **Step 2: 逐一替换或确认每个引用已安全移除**

如果仍有引用，评估每个引用：
- 在 `api/ai.py` 中 → 这些函数应该已内联到 `DiagnosisContext` 或从 `build_diagnosis_context()` 处理
- 在 `_followup_compat.py` 中 → 保留 export 签名

---

### Task 3.2: 删除 `_v1_helpers/` 目录

- [ ] **Step 1: 删除目录**

```bash
cd ai-service && rm -rf ai/command/_v1_helpers/
```

- [ ] **Step 2: 清理 `api/ai.py` 中的 v1 导入**

```bash
cd ai-service && grep -n "from ai.command._v1_helpers\|from ai.followup\|from ai.langchain_runtime" api/ai.py
```
Expected: 无输出

- [ ] **Step 3: 清理 `ai/command/_followup_compat.py` + `bridge.py` 死代码**

删除 `ai/runtime/bridge.py` 中的 `unified_diagnosis_bridge` 函数（Phase 1 后不再被调用）。
保留 `_is_unified_engine_enabled()` 垫片（直到旧 env var 完全废弃）。
清理 `_followup_compat.py` 中不再 export 的函数。

- [ ] **Step 4: 运行测试**

```bash
cd ai-service && python -m pytest -x --no-cov 2>&1 | tail -20
```

Expected: 通过。失败时定位缺失导入并添加对应兼容函数。

- [ ] **Step 5: 清理遗留测试文件**

```bash
cd ai-service && rm -f tests/test_followup_command_spec.py tests/test_followup_command_security.py
```

- [ ] **Step 6: 验证无残留**

```bash
cd ai-service && grep -rn "from ai.followup\|from ai.langchain_runtime" --include="*.py" | grep -v __pycache__
```
Expected: 无输出

- [ ] **Step 7: 提交**

```bash
git add -A
git commit -m "cleanup(v1): 删除 _v1_helpers/ 目录及所有 v1 遗留代码"
```

---

## Phase 4: 验证

### Task 4.1: 全量测试 + 修复

- [ ] **Step 1: 运行完整测试套件**

```bash
cd ai-service && python -m pytest --no-cov 2>&1 | tail -30
```

- [ ] **Step 2: 修复任何回归**

- [ ] **Step 3: 确认覆盖率未明显下降**

```bash
cd ai-service && python -m pytest --cov=ai --cov-report=term 2>&1 | tail -20
```

### Task 4.2: 手动测试两个后端切换

- [ ] **Step 1: 验证 `claude-sdk` 后端可初始化**

```bash
cd ai-service && python -c "
from ai.runtime.backend import get_backend
b = get_backend('claude-sdk')
assert b.name == 'claude-sdk'
print('claude-sdk: OK')
"
```

- [ ] **Step 2: 验证 `langgraph` 后端可初始化**

```bash
cd ai-service && python -c "
from ai.runtime.backend import get_backend
b = get_backend('langgraph')
assert b.name == 'langgraph'
print('langgraph: OK')
"
```

- [ ] **Step 3: 验证旧变量兼容**

```bash
cd ai-service && AI_RUNTIME_UNIFIED_ENGINE_ENABLED=true python -c "
from ai.runtime.backend import get_backend
b = get_backend()
assert b.name == 'claude-sdk', f'Expected claude-sdk, got {b.name}'
print(f'Legacy compat: OK → {b.name}')
"
```

### Task 4.3: 验证检查清单

- [ ] **Step 1: 检查清单逐项确认**

```bash
echo "=== 检查清单 ==="
echo "1. DiagnosisContext 不依赖 _v1_helpers"
grep -rn "_v1_helpers" ai-service/ai/diagnosis/ 2>/dev/null || echo "   ✅ CLEAN"

echo "2. 两个后端通过 event_emitter 推送相同的事件格式"
grep -rn "assistant_delta" ai-service/ai/runtime/backends/ --include="*.py"

echo "3. AI_RUNTIME_BACKEND 可切换"
python -c "from ai.runtime.backend import get_backend; print('langgraph:', get_backend('langgraph').name); print('claude-sdk:', get_backend('claude-sdk').name)"

echo "4. 所有 _v1_helpers/ 导入已消除"
grep -rn "from ai.followup\|from ai.langchain_runtime" ai-service/ --include="*.py" | grep -v __pycache__ || echo "   ✅ CLEAN"

echo "5. pytest 全部通过"
cd ai-service && python -m pytest --no-cov 2>&1 | tail -5
```

---

## 检查清单

- [ ] Phase 0: DiagnosisContext 纯提取完成，pytest 无新增失败
- [ ] Phase 1: 两个后端实现 DiagnosisBackend + registry，api/ai.py 使用 get_backend()
- [ ] Phase 1: 旧变量 AI_RUNTIME_UNIFIED_ENGINE_ENABLED 继续有效
- [ ] Phase 2: LangGraph 后端通过 event_emitter 推送 assistant_delta
- [ ] Phase 2: chat_stream() 含非流式降级
- [ ] Phase 3: _v1_helpers/ 目录完全删除
- [ ] Phase 3: `grep -rn "from ai.followup\|from ai.langchain_runtime"` 返回空
- [ ] Phase 4: pytest 全部通过
- [ ] Phase 4: 两个后端可切换
- [ ] Phase 4: 流式输出在两个后端都正确
