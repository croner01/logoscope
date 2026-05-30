# AI Follow-up: LLM-in-the-Loop ReAct 闭环设计

> **状态:** 设计文档
> **日期:** 2026-05-29
> **前置设计:** [2026-05-28-ai-followup-structured-output-phase1-design.md](./2026-05-28-ai-followup-structured-output-phase1-design.md), [2026-05-28-ai-followup-structured-output-phase2-design.md](./2026-05-28-ai-followup-structured-output-phase2-design.md)

## 问题

当前 auto-exec 循环是单向的：LLM 调用一次生成 actions，循环只执行命令，不回头调 LLM。当命令执行后的观察不足以填满所有证据槽时，循环只能返回 `react_replan_needed=true`，让客户端重试。两次 LLM 调用之间的观察数据（如 `kubectl get pods -A` 发现的 namespace）无法传递给下一次 LLM 调用，导致 LLM 在黑暗中重新推断，生成不可执行的 action，进入死循环。

**症状体现为会话 blocked 在 `react_replan_needed` 状态，因为 LLM 不知道前一轮执行发现了什么。**

## 目标和约束

**目标:** 在单次请求内完成多轮 ReAct 闭环，使 LLM 能看到前一轮执行结果并生成针对性下一步动作。

**非目标:**
- 不改变前端或客户端 API 行为
- 不改变 LLM prompt 的 system prompt 结构
- 不引入新的 LLM provider 依赖

**关键约束:**
- 确定性传播（无 LLM 调用）优先于 LLM 重规划
- LLM 重规划有严格的时间预算剩余检查
- replan 失败不增加延迟，直接走现有 fallback
- 向后兼容：V2 循环返回与 V1 完全相同的数据结构

## 架构

```
┌─────────────────────────────────────────────────────────┐
│                  单次请求 120s 窗口                        │
│                                                         │
│  LLM #1 ─→ actions ─→ 执行 ● ─→ 评估 ● ─→ 收敛? ─→ 返回  │
│                              │         │                │
│                              │         └─ 还有缺口?      │
│                              │              │           │
│                              │         ┌────┘           │
│                              │         ▼                │
│                              │   确定性传播 (fast path)   │
│                              │   从 stdout 提取 namespace │
│                              │         │                │
│                              │         ▼                │
│                              │   还有缺口? ──→ LLM #2    │
│                              │         │       │        │
│                              │         │       ▼        │
│                              │         │   新 actions    │
│                              │         │       │        │
│                              │         └───────┘        │
│                              │              │           │
│                              └──── 继续执行 ● ──────────┘│
└─────────────────────────────────────────────────────────┘
```

## 组件设计

### 1. 确定性证据传播 — `_propagate_evidence_across_actions()`

**位置:** `followup_planning_helpers.py`

**作用:** 在 `_build_followup_react_loop` 的 evidence slot 循环之后、replan 判断之前插入。

**算法:**
```
for each slot in evidence_slot_map where status == "missing":
    expected_signal = slot.expected_signal
    for each obs in safe_observations:
        if obs 的 stdout/stderr 为空: continue
        signal_match, reason = _match_expected_signal(expected_signal, obs)
        if signal_match:
            slot.status = "cross_filled"
            slot.source_obs_id = obs.command_run_id
            evidence_filled_slots += 1
            evidence_missing_slots -= 1
            break
```

**namespace 提取器 — `_extract_namespace_from_observations()`:**

```python
def _extract_namespace_from_observations(
    observations: List[Dict[str, Any]]
) -> Dict[str, str]:
    """
    从 kubectl get pods -A 输出中提取 app → namespace 映射。
    
    正则匹配: ^(\S+)\s+(\S+).*app=(\S+)
    返回: {"temporal": "default", "clickhouse": "default", ...}
    """
```

输出注入 `analysis_context["_runtime_discovered_namespaces"]`，供确定性传播和 LLM replan context 使用。

### 2. LLM Replan — `_llm_replan_callback()`

**位置:** `ai/api.py`（胶水层）

**签名:**
```python
async def _llm_replan_callback(
    *,
    original_question: str,
    analysis_context: Dict[str, Any],
    all_observations: List[Dict[str, Any]],
    executed_commands: set[str],
    current_evidence_gaps: List[str],
    filled_evidence: List[str],
    remaining_iterations: int,
    remaining_timeout: float,
    event_callback: Optional[Any] = None,
    logger: Optional[Any] = None,
) -> Optional[List[Dict[str, Any]]]:
    """如果需要 replan，返回新的 actions；如果不需要或无法生成，返回 None。"""
```

**内部调用 `run_followup_langchain`**（复用现有入口），但传 `replan_context` 而非 `question`。

### 3. Replan Context Builder — `_build_llm_replan_context()`

**位置:** `followup_planning_helpers.py`

```python
def _build_llm_replan_context(
    original_question: str,
    analysis_context: Dict[str, Any],
    command_summaries: List[str],       # "kubectl get pods -A -l app=temporal → 发现 namespace=default"
    evidence_gaps: List[str],           # 当前缺口
    filled_evidence: List[str],         # 已填补证据
    remaining_timeout_label: str,       # "剩余 20s，请聚焦核心诊断"
) -> str:
```

生成的文本追加到 LLM prompt 中，格式:

```
【上轮执行摘要】
已执行命令:
  1. kubectl get pods -A -l app=temporal
     → 发现: namespace=default, pod=temporal-7b9c8f5d6-xk3m2 (Running)

当前已知:
  - temporal 服务运行在 default 命名空间
  - temporal pod 状态正常

仍需查明:
  - temporal 与数据库之间的连接是否正常
  - 是否有鉴权失败日志

【约束】
- 不需要重新发现已知信息
```

### 4. V2 循环 — `_run_followup_auto_exec_react_loop_v2()`

**位置:** `followup_orchestration_helpers.py`

**改动:** 在现有 `_run_followup_auto_exec_react_loop` 内部注入 replan 逻辑，不重构函数签名。

```python
for iteration in range(1, max_iterations + 1):
    # 选择 & 执行（同 V1）
    iteration_actions = _select_iteration_actions(...)
    observations = await _run_followup_readonly_auto_exec(...)
    all_observations.extend(observations)
    
    # 构建 react 循环（含确定性传播，内联在 _build_followup_react_loop 中）
    final_react_loop = build_react_loop_fn(actions, all_observations, ...)
    replan_needed = bool(final_react_loop.get("replan", {}).get("needed"))
    
    # ★ 新增: LLM replan（仍有缺口且时间充足）
    if replan_needed and _has_sufficient_time(remaining) and _has_new_discoveries(all_observations):
        new_actions = await _try_llm_replan(...)
        if new_actions:
            working_actions.extend(new_actions)
            continue  # 下一轮尝试执行新 actions
    
    # 收敛或返回
    if not replan_needed:
        break
```

## 时序预算

| 阶段 | 时间预算 | 说明 |
|------|----------|------|
| 准备 | ~23s | session prepare + history + memory |
| LLM #1 | 30s | 主 LLM 调用 |
| 执行 | 30s | 1-3 轮命令执行 |
| 确定性传播 | <1ms | 纯函数，不额外消耗 |
| LLM #2 (条件) | 20s | 主调用的 60%，context 更短 |
| 执行 #2 | 15s | 新 actions 执行 |
| **合计** | **~118s** | 在 120s deadline 内 |

**LLM #2 触发条件（全部满足）:**
1. 确定性传播后仍有缺口
2. 剩余时间 >= 25s（有至少 20s 给 LLM + 期望执行时间）
3. `_runtime_discovered_namespaces` 或已执行命令有增量信息

## 测试策略

### 确定性传播（纯函数，4 个测试）

| 测试 | 输入 | 预期 |
|------|------|------|
| namespace 从 kubectl get pods stdout 中提取 | stdout 含 pod 行 | `{"temporal": "default"}` |
| 跨动作证据填充 | action A 缺 namespace 证据，action B 的 observation 含 namespace | slot A 标记为 cross_filled |
| 无匹配时不误填 | 观察不含任何匹配 token | 证据槽不变 |
| 空观察列表不报错 | observations=[] | 正常运行，0 命中 |

### LLM Replan 循环（mock replan_fn，3 个测试）

| 测试 | mock 行为 | 预期 |
|------|-----------|------|
| replan 生成新动作 | 返回 1 个新 action | working_actions 增加，循环继续 |
| replan 返回空 | 返回 None | 循环正常结束，不走 fallback |
| replan 超时 | 模拟 TimeoutError | 循环正常结束，replan 不影响已执行结果 |

### 端到端（1 个测试）

- 完整请求: mock LLM 返回含 `kubectl get pods -A` 的 actions → mock 执行返回 stdout → 验证确定性传播填充 namespace 证据槽 → 二次 react loop 不应标记 replan_needed

## 向后兼容

- `_run_followup_auto_exec_react_loop` 函数签名不变
- 返回数据结构新增字段:
  - `final_react_loop.observe.propagation_hits: int`
  - `final_react_loop.observe.llm_replan_triggered: bool`
  - `react_iterations[].replan_type: str` (`deterministic | llm | template | none`)
- 旧字段不删除，只追加

## 监控指标

```
ai_followup_react_propagation_eligible_total     # 进入确定性传播的次数
ai_followup_react_propagation_hits_total         # 确定性传播命中次数
ai_followup_react_llm_replan_attempted_total     # LLM replan 尝试次数
ai_followup_react_llm_replan_success_total       # LLM replan 成功次数
ai_followup_react_llm_replan_timeout_total       # LLM replan 超时次数
ai_followup_converged_in_request_total           # 单请求收敛次数
ai_followup_converged_via_propagation_total      # 确定性传播收敛次数
ai_followup_converged_via_replan_total           # LLM replan 收敛次数
```
