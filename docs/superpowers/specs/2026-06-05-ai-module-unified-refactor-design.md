# AI Module Unified Refactor — Design Spec

**Date:** 2026-06-05
**Status:** Design approved
**Branch:** `feat/ai-module-refactor`
**Scope:** Full refactoring of AI service command layer, runtime engine, and security policy

---

## Problem Statement

经过 15 份 spec、16 份 plan、30+ commit 的迭代演进，AI 模块在三个维度上积累了显著的架构债务：

### 维度 1：命令处理层分裂

`followup_command.py` (~1,700行) 和 `followup_command_spec.py` (~1,600行) 在同一领域各做各的：
- 两套 allowlist 不一致（`_FOLLOWUP_COMMAND_ALLOWED_HEADS` 缺 8 个工具）
- SQL 关键字修复规则分裂在两处，覆盖不同
- 自由文本提取 + structured spec 双路径，始终存在回退和边界

### 维度 2：运行时入口分裂

三个入口各自实现了 plan→act→observe→replan 循环：
- v1 followup (`api/ai.py` 中的 `_run_followup_auto_exec_react_loop`)
- v2 agent runtime (`agent_runtime/service.py` 中的 `execute_command_tool`)
- v4 LangGraph (`runtime_v4/langgraph/nodes/`)

每条路径有自己的状态管理、prompt 构建、证据传播逻辑。

### 维度 3：安全策略散落

同一个命令执行前的安全检查分散在 5 处：
- `followup_command.py._classify_followup_command`
- `followup_command_spec.py._GENERIC_EXEC_ALLOWED_HEADS`
- exec-service `policy.py`
- `agent_runtime/service.py._stage_apply_command_gates`
- `agent_runtime/cost_preflight.py`

### 根因

代码通过层层补丁（compact normalizer、NL 提取回退、双路径编译、重试循环）去修正 LLM 输出的格式问题，而非从架构层面建立统一的处理管线。

---

## Design Principles

1. **每个概念只在一个地方表达** — allowlist、SQL 修复规则、plan→act→observe→replan 都在唯一的位置
2. **LLM 直接输出 structured spec，不保留自由文本回退路径** — 格式错误 → 重试 tool call，不做 NL 提取
3. **安全策略集中在 `command/security.py`，外部沙箱由 exec-service 负责** — 两层正交的安全

---

## Architecture

### 目标分层

```
┌────────────────────────────────────────────────────────────┐
│                      API Layer (入口)                       │
│  api/ai.py (v1)  │  api/ai_runtime_v2.py  │  未来入口...    │
│  全部调用 runtime/engine.py 的 run_diagnosis()              │
└──────────────────────────┬─────────────────────────────────┘
                           │
┌──────────────────────────▼─────────────────────────────────┐
│                   Runtime Layer (运行时)                     │
│  runtime/engine.py    — 唯一的 plan→act→observe→replan 循环  │
│  runtime/state.py     — 统一状态模型                         │
│  runtime/tools.py     — 工具执行适配器（区分本地/远程通道）    │
│  runtime/prompt.py    — 集中式 prompt 构建                   │
│  runtime/memory.py    — 会话记忆 + 去重                      │
│  runtime/events.py    — SSE 事件分发                        │
└──────────────────────────┬─────────────────────────────────┘
                           │
┌──────────────────────────▼─────────────────────────────────┐
│                  Command Layer (命令)                        │
│  command/spec.py      — CommandSpec 数据模型（Pydantic）     │
│  command/normalizer.py — LLM 输出 → 规范 CommandSpec         │
│  command/compiler.py   — CommandSpec → 可执行 shell 命令     │
│  command/security.py   — 统一的 allowlist + 分类 + cost gate │
└──────────────────────────┬─────────────────────────────────┘
                           │
┌──────────────────────────▼─────────────────────────────────┐
│             External Sandbox (外部沙箱，不变)                 │
│  exec-service → toolbox-gateway / ssh-gateway               │
│  query-service → ClickHouse                                 │
└────────────────────────────────────────────────────────────┘
```

### 数据流（一条直线）

```
LLM tool call → dict
  │
  ▼
command/normalizer.py
  normalize_command_spec(raw_dict) → CommandSpec
  │
  ▼
command/security.py
  evaluate_command(spec, session_state) → SecurityDecision
  │
  ├── blocked → 返回拒绝原因给 LLM
  ├── requires_approval → 发射 approval_required 事件
  │
  ▼ (auto / approved)
command/compiler.py
  compile_command(spec) → CompiledCommand
  │
  ▼
runtime/tools.py
  execute(compiled) → ToolResult
  │
  ├── local channel → query-service
  └── remote channel → exec-service → toolbox-gateway
```

---

## Detailed Design

### 1. Command Layer (`ai/command/`)

#### 1.1 `spec.py` — 唯一的数据模型

```python
from pydantic import BaseModel, Field
from enum import Enum
from typing import Optional


class ToolType(str, Enum):
    GENERIC_EXEC = "generic_exec"
    CLICKHOUSE_QUERY = "clickhouse_query"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CommandType(str, Enum):
    QUERY = "query"       # 只读
    REPAIR = "repair"     # 写入（需提权）


class CommandSpec(BaseModel):
    """统一的命令规范——整个命令层的唯一数据契约。

    LLM tool call 输出、前端展示、安全策略、执行引擎
    全部围绕这一个 model。
    """
    tool: ToolType
    command: str = ""                              # shell 命令（先 normalizer 填充，再 compiler 编译）
    target_kind: str = ""                          # k8s_cluster / clickhouse_cluster / host_node
    target_identity: str = ""                      # pod:xxx/namespace:yyy 或 database:logs
    purpose: str = ""                              # 一句话描述
    risk_level: RiskLevel = RiskLevel.LOW
    command_type: CommandType = CommandType.QUERY
    timeout_seconds: int = Field(default=20, ge=1, le=120)


class CompiledCommand(BaseModel):
    """编译产物：经过安全校验、准备执行的命令。"""
    spec: CommandSpec
    shell_command: str                             # 实际执行的 shell 命令
    route: str                                     # "local" | "remote"
    executor_profile: str = ""                     # toolbox-k8s-readonly 等
    sql_preflight_passed: bool = False
```

#### 1.2 `normalizer.py` — LLM 输出 → 规范 CommandSpec

```
normalize_command_spec(raw: dict) → CommandSpec

处理：
1. Pydantic 字段校验
2. 命令头提取（从 command 字段解析）
3. target 推断：
   - 有 source_target.pod_name → 自动填充 target_kind=k8s_cluster, target_identity=pod:xxx
   - 有 source_target.namespace → 补充命名空间
   - ClickHouse query → target_kind=clickhouse_cluster
4. 命令类型推断（SELECT → query, INSERT/UPDATE/DELETE → repair）
5. 返回规范化的 CommandSpec 或 raise ValidationError

不再做的事（LLM tool calling 已保证格式）：
- compact normalizer（6 阶段正则修补）
- CJK-ASCII 边界空格插入
- NL 命令提取回退
- 双路径（自由文本 / structured spec）
```

#### 1.3 `compiler.py` — CommandSpec → shell 命令

```python
def compile_command(spec: CommandSpec, *, run_sql_preflight: bool = False) -> CompiledCommand:
    """将规范 spec 编译为可执行命令。

    generic_exec:
      - 直接使用 spec.command
      - 做 token 合法性检查（不允许重定向、管道等危险操作符）
      - route = "remote"

    clickhouse_query:
      - 包装为 kubectl exec -i <pod> -- clickhouse-client --query "..."
      - 自动解析 ClickHouse pod selector
      - 可选 EXPLAIN SYNTAX preflight
      - route = "local" (简单 SELECT) 或 "remote" (复杂聚合)
    """
```

路由规则（从 `command_router.py` 迁移）：
- 简单 SELECT（无 JOIN/GROUP BY/子查询/窗口函数）+ target=logs.events → local (query-service)
- 复杂 ClickHouse SQL → remote (kubectl exec)
- 其他所有命令 → remote (exec-service)

#### 1.4 `security.py` — 唯一的 allowlist + 分类 + cost gate

```python
# 唯一的 allowlist —— 整个代码库只在这里维护
ALLOWED_HEADS: set[str] = {
    "kubectl", "curl",
    "clickhouse-client", "clickhouse",
    "grep", "rg", "cat", "tail", "head", "awk", "jq",
    "ls", "echo", "pwd", "sed", "helm",
    "systemctl", "service",
    "openstack", "psql", "postgres", "mysql", "mariadb",
    "timeout", "ps", "ss",
}

BLOCKED_OPERATORS: set[str] = {";", "&", ">", ">>", "<", "<<", "|", "||", "&&", "$(", "`"}


@dataclass
class SecurityDecision:
    allowed: bool
    reason: str = ""
    requires_approval: bool = False
    requires_elevation: bool = False
    command_type: CommandType = CommandType.QUERY
    risk_level: RiskLevel = RiskLevel.LOW
    cost_estimate: dict | None = None


@dataclass
class SessionCostState:
    commands_executed: int = 0
    estimated_rows_scanned: int = 0
    targets_touched: set[str] = field(default_factory=set)
    session_command_limit: int = 10


def evaluate_command(
    spec: CommandSpec,
    *,
    session_cost: SessionCostState,
    write_enabled: bool = False,
) -> SecurityDecision:
    """唯一的命令安全评估入口。

    检查顺序（短路）：
    1. command head 不在 ALLOWED_HEADS → blocked
    2. 含 BLOCKED_OPERATORS → blocked
    3. 写入命令 && !write_enabled → blocked
    4. 写入命令 → requires_elevation
    5. cost 超阈值：
       - commands_executed >= limit → requires_approval
       - 含 -A/--all-namespaces → requires_approval
       - 大时间窗口或全表扫描 → requires_approval
    6. 默认 → auto
    """
```

### 2. Runtime Layer (`ai/runtime/`)

#### 2.1 `engine.py` — 唯一的运行时循环

```python
async def run_diagnosis(
    state: RuntimeState,
    *,
    tools: ToolAdapter,
    prompt_builder: PromptBuilder,
    memory: SessionMemory,
    event_emitter: EventEmitter,
) -> RuntimeResult:
    """唯一的诊断运行循环。

    v1 followup、v2 agent run、v4 LangGraph/Temporal
    三个入口最终都调用这个函数。

    区别只在外层的 session 管理和事件分发方式。
    """

    deadline = time.monotonic() + state.timeout_seconds

    for iteration in range(1, state.max_iterations + 1):
        # 超时检查（每轮都检查，不只 replan 时）
        if time.monotonic() > deadline:
            break

        state.iteration = iteration

        # 1. PLAN: LLM 输出 action 列表
        plan = await _plan(state, prompt_builder, memory)
        if not plan.actions:
            break

        # 2. ACT: 顺序执行（每步超时保护）
        for action in plan.actions:
            # memory 去重检查
            if memory.is_duplicate(action):
                continue

            # security evaluate
            decision = evaluate_command(action.command_spec, session_cost=state.cost)
            if not decision.allowed:
                if decision.requires_approval:
                    approved = await event_emitter.request_approval(decision)
                    if not approved:
                        continue
                else:
                    memory.record_blocked(action, decision.reason)
                    continue

            # 执行
            compiled = compile_command(action.command_spec)
            result = await tools.execute(compiled)

            # 记录
            state.add_observation(action, result)
            memory.record(action, result)
            state.cost.commands_executed += 1

            await event_emitter.emit_action_result(action, result)

        # 3. OBSERVE: 证据是否充分？
        if state.evidence_sufficient():
            state.phase = "done"
            break

        # 4. REPLAN: 证据不足 → 下一轮带着观察结果继续
        # iteration 正常推进（修复审计 H4: continue 绕过计数的 bug）

    return RuntimeResult(
        summary=state.build_summary(),
        observations=state.observations,
        memory_snapshot=memory.snapshot(),
    )
```

**关键修复：**
- 修复 H4：不再有 `continue` 绕过迭代计数
- 修复审计发现 #15：超时每轮检查，不只 replan 时
- 修复审计发现 #17：`_summarize_command_result` None 安全

#### 2.2 `state.py` — 统一状态模型

```python
@dataclass
class RuntimeState:
    run_id: str
    question: str
    analysis_context: dict
    source_target: dict | None = None

    iteration: int = 0
    max_iterations: int = 4
    phase: str = "planning"
    timeout_seconds: int = 300

    actions: list[Action] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    evidence_slots: dict[str, EvidenceSlot] = field(default_factory=dict)

    cost: SessionCostState = field(default_factory=SessionCostState)

    evidence_sufficient: bool = False
    diagnosis_summary: str = ""
```

#### 2.3 `tools.py` — 统一的工具执行

```python
class ToolAdapter:
    """统一的工具执行层。

    内部集成：
    - 路由逻辑（原 command_router.py）→ 本地/远程通道
    - query-service 客户端（原 query_client.py）
    - exec-service 客户端（原 exec_client.py）
    """

    def __init__(
        self,
        query_service_url: str | None = None,
        exec_service_url: str | None = None,
    ):
        ...

    async def execute(self, compiled: CompiledCommand) -> ToolResult:
        if compiled.route == "local":
            return await self._execute_local(compiled)
        else:
            return await self._execute_remote(compiled)
```

#### 2.4 `prompt.py` — 集中式 prompt 管理

```python
class PromptBuilder:
    """所有 prompt 在一个地方构建。

    整合当前分散在 ~5 个文件中的 prompt 逻辑：
    - followup_prompt_helpers.py
    - followup_planning_helpers.py
    - langgraph/nodes/planning.py
    - project_knowledge_pack.py
    - skills/matcher.py (catalog 构建)
    """

    def build_system(self, state: RuntimeState, memory: SessionMemory) -> str:
        """role instruction + 知识包 + skill catalog + 执行日志"""

    def build_task(self, state: RuntimeState) -> str:
        """问题 + 日志内容 + 上下文 + 历史观察"""

    def build_tool_schema(self) -> dict:
        """LLM tool calling 的 function schema"""
```

#### 2.5 `memory.py` — 会话记忆

```python
class SessionMemory:
    """会话级记忆。

    整合当前分散在 4 处的记忆逻辑：
    - ExecutionJournal (command dedup + result summary)
    - react_memory (followup_react_helpers.py)
    - long_term_memory (followup_runtime_helpers.py)
    - conversation_history

    修复审计 C1: 指纹计算和查找使用同一算法。
    """

    def is_duplicate(self, action: Action) -> bool:
        """SHA1(command + tool + target_identity) 去重"""

    def record(self, action: Action, result: ToolResult) -> None:
        """记录执行结果"""

    def record_blocked(self, action: Action, reason: str) -> None:
        """记录被安全策略拦截的 action"""

    def context_for_llm(self) -> str:
        """生成 LLM 上下文注入文本"""
```

#### 2.6 `events.py` — SSE 事件分发

```python
class EventEmitter:
    """SSE 事件分发。复用现有 event_protocol.py 的事件类型常量。"""

    async def emit_action_result(self, action: Action, result: ToolResult):
        """发射 tool_call_started / tool_call_finished / tool_call_output_delta"""

    async def request_approval(self, decision: SecurityDecision) -> bool:
        """发射 approval_required，等待用户确认/拒绝"""

    async def emit_reasoning(self, text: str):
        """发射 reasoning_step / reasoning_summary_delta"""
```

---

## Files Changed

### New Files (12)

| File | Purpose |
|------|---------|
| `ai/command/__init__.py` | Command layer public API |
| `ai/command/spec.py` | CommandSpec, CompiledCommand, enums |
| `ai/command/normalizer.py` | LLM output → CommandSpec |
| `ai/command/compiler.py` | CommandSpec → shell command |
| `ai/command/security.py` | Unified allowlist + classification + cost gate |
| `ai/runtime/__init__.py` | Runtime layer public API |
| `ai/runtime/engine.py` | Single plan→act→observe→replan loop |
| `ai/runtime/state.py` | RuntimeState, Action, Observation |
| `ai/runtime/tools.py` | ToolAdapter — dual-channel execution |
| `ai/runtime/prompt.py` | PromptBuilder — centralized prompt assembly |
| `ai/runtime/memory.py` | SessionMemory — unified memory + dedup |
| `ai/runtime/events.py` | EventEmitter — SSE event fan-out |

### Deprecated (to be deleted after migration)

| File | Merged into |
|------|-------------|
| `ai/followup_command.py` | `command/security.py` + `command/normalizer.py` |
| `ai/followup_command_spec.py` | `command/spec.py` + `command/compiler.py` |
| `ai/agent_runtime/command_router.py` | `runtime/tools.py` |
| `ai/agent_runtime/cost_preflight.py` | `command/security.py` |
| `ai/agent_runtime/execution_journal.py` | `runtime/memory.py` |
| `ai/agent_runtime/query_client.py` | `runtime/tools.py` |
| `ai/skills/diagnostics/*.py` (10 files) | Dead code, deleted |

### Modified (adapter layer)

| File | Change |
|------|--------|
| `api/ai.py` | Call `runtime/engine.py` instead of `_run_followup_auto_exec_react_loop` |
| `api/ai_runtime_v2.py` | Call `runtime/engine.py` |
| `ai/agent_runtime/service.py` | Adapt to call engine + tools |
| `ai/skills/builtin/_helpers.py` | Use `command/spec.py` types |
| `ai/skills/builtin/*.py` | Adapt `plan_steps()` to return `CommandSpec` |

### Unchanged

| Service | Reason |
|---------|--------|
| exec-service | External sandbox, stable API |
| query-service | External log query, stable API |
| toolbox-gateway | Command execution proxy, stable API |
| frontend | Consumes same SSE events, no change |
| semantic-engine, ingest-service | Not in scope |

### Fixed Bugs (from audit)

| Audit ID | Fix |
|----------|-----|
| C1 | `memory.py` uses same fingerprint algorithm for store and lookup |
| C2 | Dead `diagnostics/` skills deleted |
| C3 | `configmap_loader.py` import fixed or file deleted |
| H1 | SQL repair rules removed (not needed with tool calling) |
| H3 | Single allowlist in `security.py` |
| H4 | `engine.py` loop uses `for iteration in range()` — no `continue` bypass |
| H5 | Approval timer always set via `EventEmitter.request_approval()` |
| H6 | `_summarize_command_result` has None guard |
| H7 | Knowledge pack paths fixed in `prompt.py` |
| M2 | Single SQL normalizer, no split lists |
| M11 | `source_target` consumed by `normalizer.py` to auto-fill target fields |

---

## Implementation Phases

### Phase 1: Command Layer (独立，无外部依赖)

```
1.1 command/spec.py      — 数据模型
1.2 command/security.py  — allowlist + 分类 + cost gate
1.3 command/normalizer.py — LLM 输出规范化
1.4 command/compiler.py   — spec 编译为 shell
```

每个模块单独测试，Phase 完成时 command 层可独立 import 使用。

### Phase 2: Runtime Layer (依赖 command 层)

```
2.1 runtime/state.py    — 状态模型
2.2 runtime/memory.py   — 记忆 + 去重
2.3 runtime/events.py   — 事件分发
2.4 runtime/prompt.py   — prompt 构建
2.5 runtime/tools.py    — 工具执行适配器
2.6 runtime/engine.py   — 主循环（串联以上全部）
```

### Phase 3: 适配现有入口

```
3.1 api/ai.py            — v1 followup → engine
3.2 api/ai_runtime_v2.py — v2 runs → engine
3.3 ai/skills/builtin/   — 适配新 CommandSpec 类型
3.4 ai/agent_runtime/    — 适配 engine + tools
```

### Phase 4: 清理

```
4.1 删除 deprecated 文件
4.2 删除 diagnostics/ 死代码
4.3 修复 configmap_loader.py 或删除
4.4 创建迁移测试确保行为不变
```
