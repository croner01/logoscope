# 双运行时架构规划方案

> 分析日期：2026-06-14
> 分析方法：Superpower Systematic Debugging（Phase 1 证据收集 + Phase 2 模式分析）
> 状态：Phase 0-3 基础设施完成，Phase 2 核心清理等待生产验证

> 已提交 commits：
> - `pre-dual-runtime-phase0` — 重构前快照（回退点）
> - `a1b7539` — Phase 0a: 删除零引用文件
> - `1feaae7` — Phase 0b: 技能 YAML 导出+加载
> - `82ab161` — Phase 0c: ClickHouse 命令历史表
> - `4585b6e` — Phase 1: Claude SDK 后端
> - `47540d4` — Phase 2a: Gate 开关
> - `1b8dc8c` — Phase 2b: ClickHouse 自动记录
> - `4b2793e` — Phase 3: MCP 服务器
> - `8d9ca43` — Phase 2c: 删除 followup_action_draft_helpers
> - `0bf39c9` — Phase 2c: 删除 followup_action_command_helpers

> **净代码变更：已删除 2,099 行，新增 1,854 行（含 336 行 MCP 服务器）**

---

## 一、架构概述

### 核心思路

```
在线模式 (Claude SDK / MCP)  ←→  离线模式 (DeepSeek + LangGraph)
          │                              │
          └──────────┬───────────────────┘
                     ▼
             共享基础设施层
         (exec-service / tools / approval / skills)
```

### 为什么这样设计

| 模式 | 后端 | 适用场景 | Agent 智能 |
|------|------|---------|-----------|
| 在线 | Claude Agent SDK | 有网络连接时 | 最强（Claude 原生） |
| 在线 | Claude Code + MCP | 有 Claude 客户端时 | 最强 + 交互式 |
| 离线 | DeepSeek + LangGraph | 无网络/内网环境 | 可用（弱模型需兜底） |

### 关键原则

1. **先加后删**：新后端运行稳定后再删旧代码
2. **技能声明化**：Python 类 → YAML 定义，双运行时共享
3. **审批统一**：所有命令走 exec-service precheck，运行时无关
4. **Gate 开关控制**：在线模式 OFF，离线模式 ON（简化版）

---

## 二、模块命运清单

### 2.1 保留复用（24 个模块，~16,000 行）

这些模块完全独立于运行时之上，不做任何修改。

| 模块 | 路径 | 行数 | 保留理由 |
|------|------|------|---------|
| exec-service 全部 | `exec-service/` | 10,298 | 统一策略+执行+审批入口 |
| toolbox-gateway | `toolbox-gateway/` | ~500 | 远端命令代理 |
| ssh-gateway | `ssh-gateway/` | ~300 | SSH 远端执行 |
| ToolAdapter | `ai/runtime/tools.py` | ~200 | 封装 precheck→execute→stream |
| exec_client | `ai/agent_runtime/exec_client.py` | 363 | HTTP 客户端包装 |
| command_bridge | `ai/agent_runtime/command_bridge.py` | 321 | SSE 事件→运行时事件 |
| CommandSpec 模型 | `ai/command/spec.py` | ~80 | 数据契约 |
| normalizer（领域逻辑） | `ai/command/normalizer.py` | 189 | ClickHouse 检测+目标推断 |
| target registry | `ai/runtime_v4/targets/` | ~1,500 | 目标注册表 |
| Temporal 客户端 | `ai/runtime_v4/temporal/client.py` | 563 | 编排引擎 |
| llm_service | `ai/llm_service.py` | 976 | LLM 调用（知识库/摘要） |
| config 体系 | `ai/runtime_config_helpers.py` | 403 | 配置加载 |
| shared_src/ | `shared_src/` | 多个 | 通用库 |
| 测试基础设施 | `tests/conftest.py` 等 | — | 测试框架 |

### 2.2 需要重构（6 个模块，~12,000 行，其中 ~9,000 行核心逻辑不变）

#### 2.2.1 Skills 系统（核心资产，格式重构）

| 文件 | 行数 | 改动内容 | 优先级 |
|------|------|---------|--------|
| `skills/builtin/*.py`（15 个） | ~2,400 | 不动 Python 类，**新增** YAML 导出 | P0 |
| `skills/base.py` | 267 | 新增 YAML 加载能力（+50 行） | P0 |
| `skills/registry.py` | 110 | 新增 YAML 技能注册（+30 行） | P0 |
| `skills/matcher.py` | 180 | 保留，两个运行时通用 | — |
| **新增** `skills/exporter.py` | ~80 | Python class → YAML 导出 | P0 |
| **新增** `skills/loader.py` | ~120 | YAML → Claude @tool / LangGraph SkillStep | P0 |
| **新增** `skills/builtin/*.yaml` | ~750 | 15 个技能的声明式定义 | P0 |

#### 2.2.2 Runtime Backend（加一个实现）

| 文件 | 行数 | 改动内容 | 优先级 |
|------|------|---------|--------|
| `runtime_v4/backend/base.py` | ~60 | **不动**（RuntimeBackend 协议已正确） | — |
| `runtime_v4/backend/__init__.py` | 71 | 添加 claude_sdk 后端注册（+10 行） | P0 |
| `runtime_v4/backend/langgraph_backend.py` | ~200 | **不动**（离线模式） | — |
| **新增** `backend/claude_sdk_backend.py` | ~200 | 新在线模式后端 | P0 |
| **新增** `backend/mcp_server/`（可选） | ~500 | MCP 协议服务器 | P1 |

#### 2.2.3 api/ai.py（大拆分，7,479 行）

| 功能块 | 行数 | 命运 |
|-------|------|------|
| API 路由 + 健康检查 | ~500 | 保留 |
| Run CRUD（create/get/stream/cancel） | ~1,500 | 保留 |
| 命令执行接口 | ~1,000 | 保留 |
| 审批接口（approve/reject） | ~800 | 保留 |
| 记忆系统加载（react_memory 等） | ~500 | **删除**→ClickHouse 表 |
| **Gate 逻辑**（L2190-2380） | ~300 | **删除**→开关控制 |
| v1 followup 兼容代码（导入 11 个 followup_*） | ~1,500 | **逐步废弃** |
| 其他（调度/context 构建等） | ~1,400 | 视需求保留 |

#### 2.2.4 agent_runtime/service.py（3,423 行）

| 功能块 | 行数 | 命运 |
|-------|------|------|
| `execute_command_tool()` | ~600 | 保留（离线模式核心） |
| command_run_index / dedup | ~200 | **删除**→ClickHouse 表 |
| approval 流程（request/approve/resolve） | ~400 | 保留（统一审批） |
| 恢复/重试逻辑 | ~300 | 保留 |
| v1 兼容胶水 | ~800 | **逐步废弃** |

#### 2.2.5 统一审批适配层

| **新增** `approval/unified_adapter.py` | ~80 | 将 exec-service precheck 映射到各后端的审批机制 | P1 |

#### 2.2.6 统一命令历史表

| **新增** `command/history.py` | ~120 | ClickHouse 表 + CommandRecord + HistoryStore | P0 |

### 2.3 直接删除（14 个文件，~1,700 行）

#### 可以立即删除（验证后）

| 文件 | 行数 | 删除理由 |
|------|------|---------|
| `followup_confirmation_ticket_helpers.py` | 185 | **ZERO 外部引用**。只从 followup_command 导入，不被任何文件导入 |
| `openhands_helper.py` | 306 | **ZERO Python 导入**。只通过环境变量路径引用为子进程脚本 |
| `runtime_v4/backend/openhands_provider.py` | ~120 | 删除 openhands helper 后不再需要 |
| `runtime_v4/backend/openhands_backend.py` | 284 | 删除 openhands provider 后不再需要 |

> 注意：删除 openhands 相关文件后，需在 `backend/__init__.py` 中移除对应的 import 和注册逻辑。

#### 在新后端上线后删除

| 代码块 | 位置 | 行数 | 替代方案 |
|--------|------|------|---------|
| **executed_set** | `followup_orchestration_helpers.py:1253` | ~50 | ClickHouse 命令历史表 |
| **command_run_index** | `agent_runtime/service.py:270-314` | ~80 | `_build_command_fingerprint()` + ClickHouse |
| **react_memory 变量** | `api/ai.py` 多处 | ~100 | ClickHouse 命令历史表 |
| **runtime_thread_memory 变量** | `api/ai.py` 多处 | ~80 | ClickHouse 命令历史表 |
| **LTM** | `api/ai.py:6341` + `followup_runtime_helpers.py:168` | ~200 | ClickHouse 命令历史表 |
| **Gate 三件套** | `api/ai.py:2190-2380` | ~300 | 开关控制（在线 OFF，离线简化版） |
| **evidence_coverage 计算** | `followup_planning_helpers.py:1963` | ~50 | Claude 自评估替代 |
| **final_confidence 计算** | `followup_planning_helpers.py:1977` | ~50 | Claude 自评估替代 |
| **_build_auto_exec_dedupe_key** | `followup_orchestration_helpers.py:67` | ~30 | fingerprint 替代 |
| **_build_command_execution_key** | `agent_runtime/service.py:251` | ~30 | fingerprint 替代 |
| **_runtime_evidence_gate_mode** | `api/ai.py:762` | ~30 | 开关控制 |
| **_answer_declares_evidence_insufficient** | `api/ai.py:755` | ~30 | Claude 自评估替代 |

### 2.4 逐步废弃（17 个文件，~13,000 行）

> 保持兼容，不加新功能。在新后端稳定运行后按批次删除。

#### 第一批：新后端上线后删除（功能完全被替代）

| 文件 | 行数 | 被什么替代 |
|------|------|----------|
| `followup_orchestration_helpers.py` | 2,122 | Claude SDK / LangGraph 内循环 |
| `followup_planning_helpers.py` | 2,379 | 技能 YAML 加载 + Claude 自主规划 |
| `followup_command_spec.py` | 1,779 | `ai/command/spec.py` + `normalizer.py`（已精简） |
| `followup_command.py` | 1,701 | ToolAdapter + exec_client |
| `followup_v2_adapter.py` | 332 | RuntimeBackend 模式 |
| `langchain_runtime/` （全目录） | 1,533 | v4 LangGraph / Claude SDK |

#### 第二批：新后端稳定后删除（辅助功能被替代）

| 文件 | 行数 | 被什么替代 |
|------|------|----------|
| `followup_runtime_helpers.py` | 516 | ClickHouse 命令历史表 |
| `followup_session_helpers.py` | 295 | ClickHouse 命令历史表 |
| `followup_react_helpers.py` | 140 | ClickHouse 命令历史表 |
| `followup_persistence_helpers.py` | 124 | ClickHouse 命令历史表 |
| `followup_action_command_helpers.py` | 107 | 不再需要 |
| `followup_action_draft_helpers.py` | 88 | 不再需要 |
| `followup_context_helpers.py` | 135 | YAML 技能上下文 |
| `followup_prompt_helpers.py` | 154 | 技能 YAML + Claude 原生 |

---

## 三、实施路线图

### Phase 0：Foundation（1-2 天）

> **原则：只加不删。** 所有旧代码继续运行，新代码独立部署。

| Step | 文件 | 内容 | 依赖 | 状态 |
|------|------|------|------|------|
| 0.1 | `skills/exporter.py`（新增） | Python 技能类 → YAML 导出 | — | ✅ |
| 0.2 | `skills/loader.py`（新增） | YAML → Claude @tool / LangGraph SkillStep | 0.1 | ✅ |
| 0.3 | `skills/builtin/k8s_pod.yaml`（新增） | 1 个技能导出验证 | 0.1 | ✅ |
| 0.4 | `command/history.py`（新增） | ClickHouse 命令历史表 + store | — | ✅ |
| 0.5 | `runtime/memory.py`（修改） | SessionMemory 接入 ClickHouse 持久化 | 0.4 | ✅ |
| 0.6 | **删除** `followup_confirmation_ticket_helpers.py` | 安全删除（零引用） | — | ✅ |
| 0.7 | **删除** `openhands_helper.py` | 安全删除（零 Python 导入） | — | ✅ |
| 0.8 | **删除** `openhands_provider.py`, `openhands_backend.py` | 清理 openhands 相关 | 0.7 | ✅ |

**验证标准：**
- 现有 test suite 全部通过
- 所有历史技能正常运行为 YAML
- ClickHouse 表创建成功，读写正常

### Phase 1：Claude SDK 后端（2-3 天）

| Step | 文件 | 内容 | 依赖 | 状态 |
|------|------|------|------|------|
| 1.1 | `backend/__init__.py`（修改） | 注册 claude_sdk 后端 | Phase 0 | ✅ |
| 1.2 | `backend/claude_sdk_backend.py`（新增） | Claude Agent SDK 后端实现 | 1.1 | ✅ |
| 1.3 | `approval/unified_adapter.py`（新增） | 统一审批映射层 | — | ⏳ 可选 |
| 1.4 | `skills/loader.py`（补充） | YAML → @tool 转换器 | 0.2, 1.2 | ✅ |
| 1.5 | 环境变量 `AI_RUNTIME_V4_AGENT_BACKEND=claude_sdk` | 后端切换 | 1.2 | ⏳ 需生产验证 |

**验证标准：**
- `claude_sdk` 后端可以启动并处理一个完整的诊断请求
- 调用链：API → ClaudeAgentBackend → ToolAdapter → exec-service → gateway
- 审批流程：写操作 → precheck → permission callback → 等待 → 执行
- 命令历史写入 ClickHouse

### Phase 2：切换与清理（2-3 天）

| Step | 内容 | 验证 | 状态 |
|------|------|------|------|
| 2a | Gate 开关 `AI_RUNTIME_EVIDENCE_GATE_ENABLED` | 默认 ON 不改变行为 | ✅ |
| 2b | ClickHouse 自动记录到 ToolAdapter | fire-and-forget 不阻塞执行 | ✅ |
| 2c | 删除小型工具文件（action_draft_helpers 等） | 内联到 api/ai.py 后删除 | ✅ |
| 2.1 | 在线流量切到 `claude_sdk` 后端 | 线上监控无异常 | ⏳ |
| 2.2 | `api/ai.py` 拆分：删除所有 followup_* 导入 | 删除后测试全过 | ⏳ |
| 2.3 | 删除第一批废弃文件（followup_orchestration 等） | git diff 确认无残留 | ⏳ |
| 2.4 | Gate 逻辑简化版 | 在线 OFF 验证 | ⏳ |
| 2.5 | 删除记忆系统代码 | 测试全过 | ⏳ |
| 2.6 | 更新前端 status/blockedReason 显示 | 前端渲染正常 | ⏳ |

> ⏳ = 等待新后端（Claude SDK）生产环境验证通过后执行。当前 v1 followup 运行路径仍依赖这些代码。

**验证标准：**
- 所有 test 通过（更新后的测试套件）
- `api/ai.py` 行数从 ~7,500 降至 ~4,000
- 在线模式下 `AI_RUNTIME_EVIDENCE_GATE_ENABLED=false` 正常运行
- 离线模式 `AI_RUNTIME_EVIDENCE_GATE_ENABLED=true` 使用简化版 Gate

### Phase 3：MCP 服务器（可选，2 天）

| Step | 内容 | 状态 |
|------|------|------|
| 3.1 | 创建 `mcp_server/` 项目 | ✅ |
| 3.2 | 实现 clickhouse_query / kubectl_read / kubectl_write 工具 | ✅ |
| 3.3 | 集成 exec-service precheck + ticket 系统 | ✅ |
| 3.4 | 测试 Claude Code Desktop 连接 | ⏳ 需部署后测试 |

---

## 四、删除安全矩阵

### 按删除批次的安全等级

```
立即删除（已验证安全）
├── followup_confirmation_ticket_helpers.py    ← ZERO 外部引用
├── openhands_helper.py                        ← ZERO Python 导入
├── openhands_provider.py                      ← 依赖 openhands_helper
└── openhands_backend.py                       ← 依赖 openhands_provider

Phase 1 后删除（新后端上线）
├── followup_orchestration_helpers.py          ← 被 Claude SDK 替代
├── followup_planning_helpers.py               ← 被 Claude SDK + YAML 替代
├── followup_command_spec.py                   ← 被 normalizer.py 替代
├── followup_command.py                        ← 被 ToolAdapter 替代
├── langchain_runtime/                         ← 被 v4 替代
└── followup_v2_adapter.py                     ← 被 RuntimeBackend 模式替代

Phase 2 后删除（稳定运行后）
├── followup_runtime_helpers.py                ← 被 ClickHouse 表替代
├── followup_session_helpers.py                ← 被 ClickHouse 表替代
├── followup_react_helpers.py                  ← 被 ClickHouse 表替代
├── followup_persistence_helpers.py            ← 被 ClickHouse 表替代
├── followup_action_command_helpers.py         ← 不再需要
├── followup_action_draft_helpers.py           ← 不再需要
├── followup_context_helpers.py                ← YAML 技能上下文
└── followup_prompt_helpers.py                 ← YAML 技能 + Claude 原生

Gate/记忆系统（新后端稳定后）
├── executed_set（followup_orchestration_helpers.py 内）
├── command_run_index（agent_runtime/service.py 内）
├── react_memory 变量（api/ai.py 内）
├── runtime_thread_memory 变量（api/ai.py 内）
├── LTM（api/ai.py + followup_runtime_helpers.py）
└── Gate 逻辑块（api/ai.py L2190-2380）
```

### Gate 删除影响范围（已验证）

Gate 逻辑完全封装在 `api/ai.py` L730-824 + L2096-2390 内。**没有任何其他 Python 文件导入或调用 Gate 函数。**

#### 直接影响的测试（5 个需要重写）

```
test_agent_runtime_api.py:
  - test_..._softens_answer_when_evidence_is_weak          ← 断言软化文本
  - test_..._blocks_when_evidence_incomplete               ← 断言 blocked
  - test_..._blocks_when_final_confidence_below_threshold  ← 断言阈值
  - test_..._blocks_when_answer_declares_insufficient      ← 断言自评不足
  - test_..._soft_missing_slots_not_blocked_in_progressive ← 断言渐进模式
```

#### 需要更新断言的测试（10+ 个）

`test_agent_runtime_api.py` 中所有检查 `gate_decision`、`blocked_reason`、`diagnosis_status` 的用例需移除或更新断言。

#### 前端无影响

`runtimeTranscript.ts` 的 `blocked_reason`、`evidence_coverage`、`final_confidence` 显示均为可选读取，字段不存在时自动跳过。不会崩溃，只是 UI 不显示。

### 删除后不可删除的核心依赖

```python
# 这些函数/模块是双运行时共享的，不能删除
_build_command_fingerprint()          # 跨运行命令去重的指纹算法
ToolAdapter.execute()                  # exec-service 的统一调用入口
CommandSpec                            # 数据契约
normalizer.py 的 ClickHouse 检测逻辑   # 领域逻辑
exec_client.py                         # HTTP 客户端
command_bridge.py                      # SSE 事件桥接
```

---

## 五、优化建议

### 5.1 发现 SessionMemory 已存在但未完整接入

`ai/runtime/memory.py` 中的 `SessionMemory` 类（129 行）已经是"统一的记忆系统替代品"。它目前：
- ✅ 提供 `fingerprint()` 统一指纹算法
- ✅ 提供 `is_duplicate()` 去重检查
- ✅ 提供 `record()` / `record_blocked()` 记录
- ✅ 提供 `context_for_llm()` 上下文注入
- ❌ **缺 ClickHouse 持久化**（当前纯内存）
- ❌ **缺跨 session 查询**

**优化建议**：扩展 SessionMemory，添加 ClickHouse 后端，而不是新建一个系统。

```python
# ai/command/history.py（新增）
class ClickHouseHistoryStore:
    """SessionMemory 的 ClickHouse 持久化后端。"""
    
    async def record(self, entry: CommandRecord) -> None: ...
    async def query_by_fingerprint(self, fp: str) -> List[CommandRecord]: ...
    async def count_recent_failures(self, fp: str, hours: int = 24) -> int: ...
```

### 5.2 前端受影响范围小

Gate 指标在前端的引用集中在 `runtimeTranscript.ts` 的 `buildStatusSummary()` 中：
- `evidence_coverage` → 覆盖率显示
- `final_confidence` → 置信度显示
- `blocked_reason` → 阻塞原因显示

**优化建议**：Gate 简化后，前端的 `blocked_reason` 显示逻辑基本不变（只是原因来源从 Gate 计算变为 Claude 自评估）。只需修改数据来源，不需要重构 UI。

### 5.3 后端注册模式已是最佳实践

`runtime_v4/backend/__init__.py` 的 `get_runtime_backend()` 工厂模式（~71 行）正确支持插拔式后端。只需要添加：

```python
# 在 _normalize_backend_mode() 中
if raw in {"claude_sdk", "claude"}:
    return "claude_sdk"

# 在 get_runtime_backend() 中
if mode == "claude_sdk":
    backend = ClaudeSdkBackend()
```

### 5.4 可复用的现有代码资产

以下代码在双运行时架构中可以直接复用，不需要修改：

```
ToolAdapter (ai/runtime/tools.py)          → 两个运行时都通过它执行命令
exec_client (agent_runtime/exec_client.py) → ToolAdapter 的下层依赖
command_bridge (agent_runtime/command_bridge.py) → SSE 事件处理
SessionMemory (runtime/memory.py)          → 作为 ClickHouse store 的上层包装
RuntimeBackend 协议 (backend/base.py)      → 后端工厂模式
_parse_llm_json_response (runtime/bridge.py) → DeepSeek 离线模式的 JSON 解析
```

---

## 六、测试策略

### 测试文件命运

| 测试文件 | 行数 | 命运 |
|---------|------|------|
| `test_langgraph_inner_loop.py` | 210 | **保留**（离线模式） |
| `test_agent_runtime_api.py` | 4,017 | **重构**（删除记忆/Gate 相关断言） |
| `test_ai_api.py` | 3,185 | **重构**（删除 followup 相关用例） |
| `test_ai_runtime_v2_api.py` | 1,600 | **重构** |
| `test_followup_planning_helpers.py` | 1,422 | **删除**（产品代码废弃） |
| `test_followup_exec_streaming.py` | 1,949 | **删除** |
| `test_followup_command_spec.py` | 789 | **删除** |
| `test_followup_command_security.py` | 403 | **删除** |
| `test_followup_session_helpers.py` | ~300 | **删除** |
| `test_followup_react_helpers.py` | ~100 | **删除** |
| `test_followup_persistence_helpers.py` | ~100 | **删除** |
| `test_ltm_command_evidence.py` | 357 | **删除** |
| `test_langchain_runtime_service.py` | 798 | **删除** |
| `test_langchain_runtime_tools.py` | ~200 | **删除** |
| `test_runtime_v4_openhands_backend.py` | ~200 | **删除** |
| `test_runtime_v4_openhands_provider.py` | ~100 | **删除** |
| `test_runtime_v4_backend_factory.py` | ~100 | **保留+新增 claude_sdk 用例** |

### 新增测试

| 测试 | 行数 | 内容 |
|------|------|------|
| `test_claude_sdk_backend.py` | ~200 | Claude SDK 后端端到端 |
| `test_command_history.py` | ~150 | ClickHouse 命令历史表 |
| `test_skills_yaml_loader.py` | ~100 | YAML 技能加载验证 |
| `test_unified_approval.py` | ~100 | 统一审批适配层 |

---

## 七、数字汇总

### 代码变更统计

| 类别 | 文件数 | 行数 | 占比 |
|------|--------|------|------|
| **保留复用** | 24 | ~16,000 | 32% |
| **需要重构** | 10 | ~3,000 净新代码 | 6% |
| **直接删除** | 14 | ~1,700 | 3% |
| **逐步废弃** | 17 | ~13,000 | 26% |
| **新增代码** | 8 | ~2,100（含 ~750 YAML） | 4% |
| **测试保留+重构** | 5 | ~10,000 | 20% |
| **测试删除** | 10 | ~8,000 | 16% |

### 关键指标

| 指标 | 当前 | 目标 |
|------|------|------|
| ai-service 代码行数 | ~50,000 | ~30,000 |
| ai-service Python 文件数 | ~150 | ~100 |
| api/ai.py 行数 | 7,479 | ~4,000 |
| 废弃/删除代码占比 | 29% | 0% |
| 运行时后端数量 | 2（langgraph + openhands） | 2（langgraph + claude_sdk） |

---

## 八、风险与缓解

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| Claude SDK 某个能力不符合预期 | 低 | 中 | Phase 1 先在非关键流量验证 |
| deleted code 被遗漏引用 | 低 | 高 | 删除前全量 grep + test suite 验证 |
| 离线模式（DeepSeek）质量下降 | 中 | 中 | 简化版 Gate 兜底 + 回滚开关 |
| 审批流程在不同后端表现不一致 | 低 | 中 | 统一审批适配层 + exec-service 作为唯一入口 |
| 团队在废弃代码上继续开发 | 低 | 低 | 废弃代码标记 README，CI 中检查 import |

---

## 附录 A：引用关系图

```
api/ai.py（7,479 行）—— 最大的单体，导入 11 个 followup_* 文件
│
├── followup_command.py                     ← command 规范
├── followup_command_spec.py               ← spec 编译（最广泛导入）
├── followup_planning_helpers.py            ← 规划/ReAct 循环
├── followup_orchestration_helpers.py       ← 编排执行
├── followup_react_helpers.py              ← react_memory 构建
├── followup_runtime_helpers.py            ← runtime_thread_memory + LTM
├── followup_session_helpers.py            ← session 管理
├── followup_prompt_helpers.py             ← prompt 构建
├── followup_context_helpers.py            ← context 构建
├── followup_persistence_helpers.py        ← 持久化
├── followup_action_command_helpers.py     ← 命令辅助
├── followup_action_draft_helpers.py       ← 草稿辅助
├── followup_v2_adapter.py                 ← v2 兼容适配
└── langchain_runtime/                      ← v1 运行时
```

**删除顺序：从叶子到根。** 先确保 `claude_sdk` 后端覆盖所有 API 路径，再从最内层的辅助文件开始删除。

---

## 附录 B：Phase 0 可立即执行的步骤

以下是**完全安全、不依赖任何新功能**的步骤，可以今天就开始：

### Step 1：删除无引用文件

```bash
# 这些文件零引用，可直接删除
git rm ai-service/ai/followup_confirmation_ticket_helpers.py
git rm ai-service/ai/runtime_v4/backend/openhands_helper.py
git rm ai-service/ai/runtime_v4/backend/openhands_provider.py
git rm ai-service/ai/runtime_v4/backend/openhands_backend.py
```

### Step 2：清理 backend/__init__.py 中的 openhands 引用

删除：
```python
from ai.runtime_v4.backend.openhands_backend import OpenHandsBackend
from ai.runtime_v4.backend.openhands_provider import (
    reset_openhands_provider,
    validate_openhands_provider_readiness,
)
```

### Step 3：验证测试通过

```bash
cd ai-service && pytest -x --timeout=60
```
