# AI Service — 需求推断与代码审计报告

**日期:** 2026-06-05
**范围:** AI Service 全模块（15 spec、16 plan、30+ commit、~22,000 行代码）

---

## 一、需求全景推断

基于文档和代码演进轨迹，核心需求一句话概括：

> **构建一个能自主诊断、自动执行、多层安全、深度绑定 Logoscope 生态的 AI 运维专家，可靠性优先，渐进式演进。**

### 1.1 自主诊断 Agent（非建议型工具）

| 需求维度 | 证据 | 成熟度 |
|----------|------|--------|
| 自动执行命令 | 12 个 skill、ReAct 闭环、`execute_command_tool` | ✅ 已实现 |
| 多轮迭代排障 | LLM-in-the-Loop、`_run_followup_auto_exec_react_loop` | ✅ 已实现 |
| 从日志定位目标 | metadata-driven exec（刚实现） | ⚠️ 刚合并，待验证 |
| 跨组件关联分析 | trace_id/request_id 级联查询、Phase 1-2-3 技能链 | ✅ 已实现 |

### 1.2 运维专家级诊断能力

12 个 builtin 技能覆盖：
- K8s Pod（CrashLoopBackOff / OOMKilled / ImagePull / Pending / Evicted）
- ClickHouse 日志深查（聚合/时间窗/错误频率）
- 跨组件日志关联（trace_id / request_id / X-Request-ID）
- 日志流路径分析（上下游调用链追踪）
- 读路径延迟排查 / 日志相关性补锚
- Linux 系统（OOM / disk / CPU / dmesg / systemctl）
- 资源用量 / 网络连通性
- OpenStack / MariaDB 诊断

### 1.3 多层安全防护

```
Layer 1: 命令头白名单 (kubectl, clickhouse-client, curl, ...)
Layer 2: 读写分类 (query vs repair)
Layer 3: 审批票据机制 (confirmation_ticket / elevation_required)
Layer 4: Cost Preflight Gate (全集群范围 / 大时间窗口 / 命令数上限)
Layer 5: 会话内幂等去重 (ExecutionJournal)
Layer 6: OPA 策略引擎 (policy_opa_client)
Layer 7: Target Registry Gate
```

### 1.4 深度绑定 Logoscope 生态

- 知道 6 个核心服务 + 4 条数据链路
- K8s 标签定位 / ClickHouse SQL / Neo4j 拓扑
- metadata-driven exec：从日志 `pod_name`/`namespace`/`labels`/`node_name` 自动定位

### 1.5 可靠性优先

大量 spec 修"堵点"而非加功能：
- 上下文缺失不阻塞 → evidence gating
- 缺 trace_id 自动降级 log mode
- 结构化输出失败有 NL 提取回退
- 编译失败有 compact normalizer 修复
- Session 不卡在 approval → planning incomplete 优化

### 1.6 渐进式架构演进

```
v1 FastAPI legacy (api/ai.py)  →  v2 Thread-Run-Action (api/ai_runtime_v2.py)
                                →  v4 Temporal + LangGraph (runtime_v4/)
```

新旧并存，v1 仍在服务前端，v2 支撑 runtime agent，v4 在建设中的持久化编排层。

---

## 二、代码审计

审计覆盖 `ai/agent_runtime/`、`ai/skills/`、`ai/runtime_v4/`、`ai/followup_*.py`、`api/ai.py`。

### 2.1 致命问题（Critical）

| # | 文件 | 问题 |
|---|------|------|
| **C1** | `agent_runtime/service.py` | **ExecutionJournal 指纹不匹配**——`_bridge_command_run` 用 `command_execution_key` 字符串存储，但 `_stage_check_command_idempotency` 用 `journal.fingerprint(command_spec)` 哈希查找。两个路径使用完全不同的指纹格式，**会话内命令去重机制完全失效**。 |
| **C2** | `skills/diagnostics/*.py` | **10 个 diagnostics 技能使用 `@DiagnosticSkill.register`（abc.ABC 虚子类注册）而非 `@register_skill`**。这 10 个技能在 skill registry 中不可见，永远不会被匹配触发。**~1,200 行代码为死代码**。 |
| **C3** | `skills/configmap_loader.py:246` | `from ai.skills.registry import get_registry` — **`get_registry` 函数不存在**（实际函数名为 `get_skill_registry`）。调用时 `ImportError`，ConfigMap 热加载功能完全不可用。 |

### 2.2 高危问题（High）

| # | 文件 | 问题 |
|---|------|------|
| **H1** | `followup_command.py` | `_repair_clickhouse_query_text()` **大小写敏感 bug**——多词修复（如 `re.sub(r"ORDERBY", " ORDER BY ", repaired)`）不处理小写 `orderby`，LLM 经常输出全小写 SQL，修复失效。同时会**损坏列名和字符串字面量**（如 `ORDERBY_test` → `ORDER BY _test`）。 |
| **H2** | `followup_command.py` | `execute_action_spec()` **死代码**——无任何生产调用者。引用的 tool namespace 已过时（不含 `generic_exec`）。 |
| **H3** | `followup_command_spec.py` | `_GENERIC_EXEC_ALLOWED_HEADS` 比 `_FOLLOWUP_COMMAND_ALLOWED_HEADS` 多出 8 个允许头（openstack/psql/postgres/mysql/mariadb/timeout/ps/ss）。**两个 allowlist 不一致，存在安全策略裂口**——旧路径拒绝而新路径放行。 |
| **H4** | `followup_orchestration_helpers.py` | **LLM replan 后 `continue` 绕过迭代计数**——`_run_followup_auto_exec_react_loop` 中 replan 回调返回非空 actions 后执行 `continue`，不推进 `for iteration` 循环。如果 replan 永远返回 actions，**可能造成无限循环**。 |
| **H5** | `agent_runtime/service.py` | `_stage_cost_preflight_gate` **未调用 `self.request_approval()`**——approval timer 未设置。`execute_command_tool` 中其他 gate 路径都正确调用了 `request_approval()`，唯独 cost gate 遗漏。 |
| **H6** | `agent_runtime/service.py` | `_summarize_command_result` **不处理 None/空 dict**——直接调用 `.get()` 可能抛 `AttributeError`。 |
| **H7** | `project_knowledge_pack.py` | **所有 asset 路径解析到不存在的目录**——`_default_knowledge_root()` 的 4 个候选路径均不存在。虽然 `select_project_knowledge()` 优雅降级返回空 registry，但知识包功能实质上不可用。 |
| **H8** | `runtime_v4/temporal/workflows.py` | **Temporal workflow 从未调用 inner LangGraph loop**——规划-执行-观察-重规划循环与 Temporal outer engine 完全断开。Temporal 只做信号路由，不驱动诊断流程。 |

### 2.3 中等问题（Medium）

| # | 文件 | 问题 |
|---|------|------|
| M1 | `followup_command.py` | `_repair_followup_command_spacing()` 正则误匹配——`-o` 和 `-l` 标记检测对非目标 flag 产生误报（如 `-option=value` → `-o ption=value`） |
| M2 | `followup_command_spec.py` | `_SQL_COMPACT_MULTIWORD_REPAIRS` 与 `followup_command.py` 中对应列表不一致——两边 JOIN 变体和 EXPLAINPIPELINE 覆盖不同 |
| M3 | `command_router.py` | `_SIMPLE_SELECT_RE` 使用 `re.DOTALL` 和 `OVER(` 检测会误拒绝含 `OVER()` 字符串字面量的查询 |
| M4 | `command_router.py` | `host_node` 检查不可达——`_infer_generic_exec_target` 永远不会返回 `host_node` |
| M5 | `agent_runtime/service.py` | `execute_command_tool` flow 中 cost preflight 通过后重跑整个 gate 阶段（浪费但正确） |
| M6 | `followup_orchestration_helpers.py` | `_loop_deadline` 只在 LLM replan 前检查，不触发 replan 时永不过期 |
| M7 | `followup_planning_helpers.py` | 部分证据槽传播后 `evidence_partial_slots` 计数不会递减，与 `evidence_filled_slots` 不一致 |
| M8 | `followup_react_helpers.py` | React memory 扫描无时间戳过滤——跨 session 的旧命令可能被加载 |
| M9 | `followup_orchestration_helpers.py` | 缺少 timeout 保护的反压——命令执行时间无上限约束在编排层 |
| M10 | `agent_runtime/service.py` | run 对象在 `_bridge_command_run` 并发场景下可能过时——last-write-wins 竞态 |
| M11 | `skills/base.py` | `source_target` 字段已定义/注入/有辅助方法，但**零个 skill 的 `plan_steps()` 实际使用它** |
| M12 | `runtime_v4/backend/openhands_provider.py` | 硬编码 `/app/...` 路径在生产部署中错误；subprocess 依赖 `/opt/openharness-venv` 无优雅降级 |
| M13 | `runtime_v4/targets/service.py` | parent cluster fallback 返回 `result: "allow"` 但 `missing_capabilities` 非空，语义矛盾 |

### 2.4 低优先级问题（Low）

| # | 文件 | 问题 |
|---|------|------|
| L1 | `followup_command.py` | `_is_sed_inplace_token()` 死代码 |
| L2 | `followup_command.py` | `_has_curl_local_output()` 非 curl 命令上的误匹配 |
| L3 | `followup_command_spec.py` | `preflight_sql_syntax()` 死代码（仅测试调用） |
| L4 | `followup_command_spec.py` | `normalize_followup_command_spec()` 输出字段重复写（顶层 + args 子层） |
| L5 | `followup_command_spec.py` | `normalize_followup_command_spec()` 不验证 tool 类型 |
| L6 | `cost_preflight.py` | `Decision.WARN` 不可达；`estimated_rows`/`target_nodes` 阈值从未读取 |
| L7 | `cost_preflight.py` | `_LARGE_TIME_WINDOW_RE` 不覆盖 HOUR/MINUTE 间隔 |
| L8 | `skills/matcher.py` | `extract_high_confidence_skills()` 导出但从未被调用 |
| L9 | `skills/registry.py` | `match_skills()` 导出但从未被调用 |
| L10 | `api/ai.py` | `_normalize_diagnosis_contract` 与 `service.py` 中版本重复，`max_items` 不一致（6 vs 8） |
| L11 | `skills/builtin` | `log_flow_analyzer` 触发模式过宽（`error`/`fail`/`timeout` 匹配几乎所有内容），靠排除列表保护 |
| L12 | `skills/builtin` | `observability_log_correlation_gap` 触发模式过窄（要求英文 "anchor/trace/request" 共现，但中文场景用 "时间窗"） |
| L13 | `langgraph/nodes/planning.py:415` | 冗余的分数阈值检查（matcher 已过滤） |
| L14 | `langgraph/nodes/acting.py` | 已分发但未观察的 action 会静默消耗迭代（有界但无提示） |

---

## 三、需求满足度评估

| 需求 | 状态 | 差距 |
|------|------|------|
| 自主诊断 Agent | ⚠️ 部分满足 | C1 导致去重失效；H4 可能无限循环 |
| 12 技能专家系统 | ⚠️ 部分满足 | C2 使 10 个技能不可见；M11 source_target 未被使用；H7 知识包不可用 |
| 多层安全防护 | ⚠️ 部分满足 | H3 安全策略裂口；H5 cost gate 缺少审批计时器 |
| Logoscope 生态绑定 | ✅ 基本满足 | metadata-driven exec 刚实现；M3 路由过保守 |
| 可靠性优先 | ⚠️ 部分满足 | H1 SQL 修复 bug；H8 Temporal+LangGraph 断开；M2 不一致列表 |
| 渐进式演进 | ✅ 基本满足 | v1/v2/v4 并存；C3 ConfigMap 热加载损坏 |

### 优先级行动建议

1. **立即修复（P0）:** C1（指纹不匹配）、C2（diagnostics 技能不可见）、C3（ConfigMap ImportError）
2. **本周修复（P1）:** H1（SQL 修复 bug）、H4（无限循环风险）、H5（cost gate 审批计时器）、H3（安全策略裂口）
3. **本迭代修复（P2）:** H7（知识包路径）、H8（Temporal+LangGraph 集成）、M11（source_target 使用）
4. **Backlog（P3）:** 所有 Medium/Low 问题

---

## 四、死代码清理建议

| 代码 | 位置 | 原因 |
|------|------|------|
| `execute_action_spec()` | `followup_command.py:1585` | 无生产调用者，tool namespace 过时 |
| `_is_sed_inplace_token()` | `followup_command.py:606` | 从未被调用 |
| `preflight_sql_syntax()` | `followup_command_spec.py:876` | 仅测试调用，逻辑已内联到 `compile_followup_command_spec` |
| `extract_high_confidence_skills()` | `skills/matcher.py:110` | 导出但未被调用 |
| `match_skills()` | `skills/registry.py:79` | 导出但未被调用 |
| `Decision.WARN` | `cost_preflight.py:16` | 枚举值从未被返回 |
| `diagnostics/` 包 (~1,200 行) | `skills/diagnostics/*.py` | 注册方式错误，不可见 |
