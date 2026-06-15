# 全业务链分析 Skill (business_chain_analyzer) 设计

> **For agentic workers:** This is a design spec. The implementation plan is generated separately via `superpowers:writing-plans`.

**目标：** 新增 `business_chain_analyzer` Skill，通过三通道 ClickHouse 服务发现 + 调用树重建 + LLM 分析，输出完整的跨服务业务链路文字报告。

**核心思路：** 当前 `cross_component_correlation` 只查 ERROR 级别日志 + 只取 3 个错误最热服务 + 单层 trace_id 查询，导致缺少 keystone 等无 trace_id 关联的服务。新 Skill 以**先发现全量服务、再逐层匹配锚点、最后 LLM 综合分析**取代单一锚点查询。

---

## 背景

### 现有方案缺陷

| 问题 | 影响 |
|------|------|
| 只查 ERROR/WARN/FATAL/CRITICAL | INFO 级别的正常调用日志被过滤，链条片段化 |
| 只取 data_flow 中错误最热的 3 个服务 | 参与但无错误的服务完全不被拉取 |
| 单层 trace_id 等值查询 | 跨服务调用若生成新 trace_id 则下游丢失 |
| data_flow 是扁平列表而非有向图 | 无法重建 service→service 调用时序 |

### 用户确认的关键事实

- Neo4j 未使用 → 服务拓扑来自 ClickHouse 自身数据
- coverage: 所有 OpenStack 服务（nova, cinder, proton, glance, keystone, heat 等）
- 输出形式: 文字分析报告，非结构化 JSON
- proton 是社区 OpenStack 网络服务，替换 neutron

---

## 架构

### 三通道服务发现策略

不使用 Neo4j，而是从 ClickHouse 的三个数据通道获取服务列表：

```
通道1 (trace_id 关联)  ────→  精确: 共享同一 trace_id 的服务
通道2 (req-xxx 匹配)   ────→  次精确: message 中含有 req-xxx 的服务
通道3 (时间窗口)        ────→  兜底: 时间窗口内活跃的所有服务
```

优先级: `trace_id > req_xxx > time_window`

### 五步流程

```
Step 1: anchor-resolve
  输入: trace_id / os_request_id
  动作: ClickHouse logs.logs + logs.events 初始查询
  输出: 时间窗口 + 初始服务列表 + trace_id
  
Step 2: chain-service-discovery
  动作: 三通道并行 ClickHouse DISTINCT 查询
  输出: {service_name: anchor_type} 映射表（上限 15 个）
  
Step 3: supplement-fetch
  动作: 对每个服务执行独立 ClickHouse 查询
  策略: 按 anchor_type 选择 WHERE 子句
  限制: 每服务 LIMIT 300, 不限制 level
  
Step 4: trace-tree-rebuild
  条件: 仅当 trace_id 存在
  动作: 查询 logs.traces 的 parent_span_id, 客户端重建调用树
  输出: 树状文本表示
  
Step 5: chain-analyze
  动作: 将 Step 1-4 结果聚合为 context, 调用 LLM 分析
  输出: 文字分析报告
```

---

## Step 详解

### Step 1: anchor-resolve

**SQL:**
```sql
-- logs.logs 主查询
SELECT timestamp, service_name, level, message, trace_id
FROM logs.logs
WHERE (trace_id = '{trace_id}' OR message LIKE '%{req_xxx}%')
  AND timestamp BETWEEN '{start}' AND '{end}'
ORDER BY timestamp ASC LIMIT 2000 FORMAT PrettyCompact

-- logs.events 辅助查询
SELECT timestamp, entity_name, event_type, level, content, trace_id, span_id, labels
FROM logs.events
WHERE (trace_id = '{trace_id}' OR content LIKE '%{req_xxx}%')
ORDER BY timestamp ASC LIMIT 1000 FORMAT PrettyCompact
```

**输出：**
- `time_window_start` / `time_window_end`（最早日志时间 → 最晚 + 2min buffer）
- `initial_services: set[str]`
- `found_trace_id: str | None`
- `secondary_trace_ids: list[str]`（日志中发现的其他 trace_id，用于 Step 4 扩展）

### Step 2: chain-service-discovery

**SQL（三路并行）：**

```sql
-- 通道1: trace_id 关联
SELECT DISTINCT service_name FROM logs.logs
WHERE trace_id = '{trace_id}' AND timestamp BETWEEN '{start}' AND '{end}'

-- 通道2: req-xxx 匹配
SELECT DISTINCT service_name FROM logs.logs
WHERE timestamp BETWEEN '{start}' AND '{end}' AND message LIKE '%{req_xxx}%'

-- 通道3: 时间窗口全服务
SELECT DISTINCT service_name FROM logs.logs
WHERE timestamp BETWEEN '{start}' AND '{end}'
```

**归并逻辑（Python）：**

```python
all_services: Dict[str, str] = {}  # service_name → anchor_type
for svc in channel1: all_services[svc] = "trace_id"
for svc in channel2:
    if svc not in all_services: all_services[svc] = "req_xxx"
for svc in channel3:
    if svc not in all_services: all_services[svc] = "time_window"
```

**截断策略：** 若 `len(all_services) > 15`，按优先级排序保留前 15 个。优先级排序标准：`trace_id > req_xxx > time_window`，同一优先级内按 A-Z。

### Step 3: supplement-fetch

每服务执行一次查询（并发上限 15 个）：

```python
for svc, anchor in all_services.items():
    if anchor == "trace_id":
        cond = f"trace_id = '{tid}' AND service_name = '{svc}'"
    elif anchor == "req_xxx":
        cond = f"message LIKE '%{req_xxx}%' AND service_name = '{svc}'"
    else:
        cond = f"timestamp BETWEEN '{start}' AND '{end}' AND service_name = '{svc}'"

    sql = f"""
        SELECT timestamp, level, message
        FROM logs.logs
        WHERE {cond}
        ORDER BY timestamp ASC LIMIT 300 FORMAT PrettyCompact
    """
```

### Step 4: trace-tree-rebuild

**SQL：**
```sql
SELECT trace_id, span_id, parent_span_id, service_name,
       operation_name, status, duration_ms, timestamp, span_kind
FROM logs.traces
PREWHERE trace_id = '{trace_id}'
ORDER BY timestamp ASC
```

**客户端树重建（Python）：**
```python
children: Dict[str, List[Span]] = defaultdict(list)
for span in spans:
    children[span.parent_span_id].append(span)

def build_tree(parent_id: str, depth: int = 0) -> List[str]:
    lines = []
    for span in children.get(parent_id, []):
        indent = "  " * depth
        lines.append(f"{indent}{span.service_name} [{span.span_kind}] "
                      f"{span.operation_name} ({span.timestamp}, {span.duration_ms}ms)")
        lines.extend(build_tree(span.span_id, depth + 1))
    return lines
```

### Step 5: chain-analyze

**配置化 Prompt Template：**

```python
# 默认 prompt template（可通过 skill_context["chain_analysis_prompt"] 覆盖）
DEFAULT_CHAIN_PROMPT = """你是一个云平台业务链分析专家。

## 业务链日志数据

### 时间窗口
{time_window}

### 锚点信息
- req-xxx: {req_xxx}
- trace_id: {trace_id}

### 服务调用链（Trace 树）
{trace_tree}

### 各服务日志摘要
{service_logs}

### 分析要求
请基于以上日志数据，给出完整的业务链分析报告，包含：

1. **业务链全貌** — 这个请求经过了哪些服务，调用顺序如何（无论是否有错误）
2. **每个环节的行为** — 每个服务在链中承担的角色，关键操作，耗时
3. **错误/异常定位** — 准确指出哪里出了问题，错误的根因是什么
4. **瓶颈分析** — 最慢的环节在哪里，是否存在等待链
5. **缺失环节** — 预期应该出现但日志中未发现的服务，以及可能的原因

请按照以上结构输出完整的分析。"""
```

**注入方式：** 在 `plan_steps()` 方法中，从 `self.context` 读取 `chain_analysis_prompt`，若为空则使用 `DEFAULT_CHAIN_PROMPT`。该 prompt 可通过：
- `SkillContext` 携带（由上游调用者传入）
- 环境变量 `AI_CHAIN_ANALYSIS_PROMPT` 覆盖默认值
- ConfigMap 配置

---

## 集成方案

### 触发策略

在 `matcher.py` 中新增触发关键词。当用户问题匹配以下关键词组时触发 `business_chain_analyzer`：

```python
_CHAIN_ANALYSIS_KEYWORDS = {
    "业务链", "全链路", "完整流程", "调用链", "服务链",
    "链条", "flow", "full trace", "end-to-end",
}
```

不匹配时走现有 `cross_component_correlation` 流程。

### 注册方式

```python
@register_skill("business_chain_analyzer", priority=85)
class BusinessChainAnalyzerSkill(DiagnosticSkill):
    name = "business_chain_analyzer"
    priority = 85
    description = "全业务链分析 — 多通道服务发现 + 调用树重建 + LLM 报告"

    def plan_steps(self, context: SkillContext) -> List[SkillStep]:
        # Step 1-5 实现
        ...
```

### 与现有 Pipeline 的关系

```
iter=1: log_flow_analyzer (P1, priority=100) — 错误热点发现保持不变

iter=2: 分支选择
  ├── 用户问题含全链路关键词 → business_chain_analyzer (priority=85)
  └── 否则 → cross_component_correlation (priority=90)

iter=3+: 常规 skill 匹配
```

### Planning Node 改动（关键）

当前 `planning.py` 在 `iter==2` 处**硬编码**注入 `cross_component_correlation`（`_PHASE2_SKILL`）。需要将其改为判断逻辑：

```python
# planning.py iter==2 分支（简化示意）
if state.iteration == 2 and _phase1_succeeded(state):
    # 判断是否触发全链路分析
    question = state.question or ""
    is_chain_analysis = any(kw in question for kw in _CHAIN_ANALYSIS_KEYWORDS)

    target_skill = "business_chain_analyzer" if is_chain_analysis else "cross_component_correlation"

    if _inject_mandatory_skill(state, target_skill):
        injected_count += 1
    # ...原后续逻辑保持不变（failure_hints 等）
```

具体改动：
- 在 `planning.py` 中定义 `_CHAIN_ANALYSIS_KEYWORDS`（与 `matcher.py` 保持一致，或引用同一常量）
- `_PHASE2_SKILL` 常量不再使用 → 改为 `_get_phase2_skill(question: str) -> str` 函数

---

## 文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `ai/skills/builtin/business_chain_analyzer.py` | **新增** | 五步完整实现 |
| `ai/skills/builtin/__init__.py` | 修改 | import `BusinessChainAnalyzerSkill` |
| `ai/skills/matcher.py` | 修改 | 添加 `_CHAIN_ANALYSIS_KEYWORDS`（也可导出为公共常量） |
| `ai/runtime_v4/langgraph/nodes/planning.py` | 修改 | iter==2 从硬编码改为关键词判断，两路分支 |
| `ai/skills/base.py` | 修改（可选） | `SkillContext` 增加 `chain_analysis_prompt` 字段 |

---

## 边界情况处理

| 场景 | 处理方式 |
|------|---------|
| 没有任何 trace_id 和 req-xxx | Step 2 通道3 兜底 → 时间窗口所有服务 → Step 5 标注"仅时间窗口匹配" |
| 只发现 1 个服务 | Step 5 报告标注"短链"，可能是时间窗口过小或数据不足 |
| Step 4 trace 表为空 | 跳过 Step 4，Step 5 按时序分析 |
| 服务超过 15 个 | 按置信度截断前 15 个 |
| Step 3 某服务查询失败 | 静默跳过该服务，Step 5 标注"数据获取失败" |
| 所有查询都空 | Step 5 返回"时间窗口内未找到关联日志" |

---

## 后续步骤

1. 用户 review 本设计文档
2. 用户批准后 → 编写实现计划
3. 按计划实现
