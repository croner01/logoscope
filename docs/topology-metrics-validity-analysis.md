# 拓扑页面数据指标有效性分析

> 基于代码全链路追踪，分析拓扑页面每个数据指标的来源、计算方式和有效条件。
>
> 分析基准：`topology-service/graph/hybrid_topology.py` + `topology_contract.py` + `confidence_calculator.py` + 前端 `topologyGraph.ts` / `topologyProblemSummary.ts`
>
> 最后更新: 2026-06-09

---

## 1. 数据源总览

拓扑图的数据从三个数据源采集，按优先级合并：

```
┌─────────────────────────────────────────────────────────────────────┐
│                        HybridTopologyBuilder                        │
│  ┌─────────────┐   ┌────────────┐   ┌──────────────┐               │
│  │ Traces      │   │ Logs       │   │ Metrics      │               │
│  │ logs.traces │   │ logs.logs  │   │ logs.metrics │               │
│  │             │   │            │   │              │               │
│  │ 边: 精确    │   │ 节点: 日志 │   │ 节点: metric │               │
│  │ 调用关系    │   │ 统计       │   │ 计数         │               │
│  │ (父子span)  │   │            │   │              │               │
│  │             │   │ 边: 推断   │   │ 边: 无       │               │
│  │ p99/err率   │   │ (requestID │   │              │               │
│  │ 等延迟指标  │   │  /时间窗)  │   │              │               │
│  └──────┬──────┘   └─────┬──────┘   └──────┬───────┘               │
│         │                │                  │                        │
│         └────────────────┼──────────────────┘                        │
│                          ▼                                           │
│  ┌─────────────────────────────────────────────────────┐            │
│  │                 merge_nodes + merge_edges            │            │
│  │  (traces 优先，logs 补充，metrics 辅助 + 置信度加成) │            │
│  └──────────────────────────┬──────────────────────────┘            │
│                             ▼                                       │
│  ┌─────────────────────────────────────────────────────┐            │
│  │            ConfidenceCalculator 重算置信度            │            │
│  └──────────────────────────┬──────────────────────────┘            │
│                             ▼                                       │
│  ┌─────────────────────────────────────────────────────┐            │
│  │    RED Metrics 聚合覆盖 (get_edge_red_metrics)       │            │
│  │    (仅当有 traces 边时生效)                           │            │
│  └──────────────────────────┬──────────────────────────┘            │
│                             ▼                                       │
│  ┌─────────────────────────────────────────────────────┐            │
│  │    Contract Schema 统一输出                            │            │
│  │    (coverage / quality_score / evidence_type)         │            │
│  └──────────────────────────┬──────────────────────────┘            │
│                             ▼                                       │
│                     拓扑 API 响应                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### 三个数据源各自产出的字段

| 数据源 | 查询的表 | 节点产出字段 | 边产出字段 | 依赖条件 |
|--------|---------|-------------|-----------|---------|
| **Traces** | `logs.traces` | trace_count, span_count, avg_duration, error_count | **call_count**, **error_rate**, **p95**, **p99**, **timeout_rate**, retries, pending, dlq | 必须有 span 的 `parent_span_id` + `duration_ms` |
| **Traces RED** | `logs.trace_edges_1m` 或 traces 自连接 | — | 与 traces 相同，预聚合后覆盖 | 至少有一条 traces 边才会触发查询 |
| **Logs** | `logs.logs` | **log_count**, pod_count, **error_count**, **error_rate**, **rps**, last_seen | 推断边：p95/p99/timeout_rate = **硬编码 0.0**；call_count = None | 仅需有日志 |
| **Metrics** | `logs.metrics` | metric_count, unique_metrics | 无（仅辅助验证） | 需有 metric 数据 |

> **关键结论**：`p95`, `p99`, `timeout_rate`, `retries`, `pending`, `dlq` 这 6 个指标**完全依赖 traces 数据源**。如果环境没有 traces（或 traces 没有 `duration_ms`），这些指标全部是 0 或默认值。

---

## 2. 节点指标

### 2.1 `log_count` — 日志量

| 项目 | 内容 |
|------|------|
| **定义** | 时间窗口内该服务的日志条目总数 |
| **计算公式** | `SELECT COUNT(*) FROM logs.logs WHERE ... GROUP BY service_name` |
| **数据来源** | Logs 源 `_get_logs_topology()` 第 1295-1308 行 |
| **UI 位置** | 节点卡片左上角：`log {log_count}` |
| **代码行** | `frontend/.../TopologyPage.tsx:4310` |
| **有效判定** | ✅ **有效**。只要有日志数据就总有值。|
| **失效条件** | 仅当 ClickHouse `logs.logs` 表为空或时间窗口内无日志。 |

### 2.2 `error_count` — 错误数

| 项目 | 内容 |
|------|------|
| **定义** | 时间窗口内 level=error/fatal 的日志条目数 (logs) 或 status=error 的 span 数 (traces) |
| **计算公式** | **Logs**: `SUM(CASE WHEN lower(level) IN ('error', 'fatal') THEN 1 ELSE 0 END)` |
| **数据来源** | Logs 源（第 1301 行）合并时优先保留非零值。Traces 源也有 `error_count` 但按 merge 规则 logs 的优先保留。 |
| **UI 位置** | 节点卡片右上：`err {error_count}` |
| **代码行** | `TopologyPage.tsx:4311` |
| **有效判定** | ✅ **有效**。日志中有 error/fatal 级别就总有值。 |
| **失效条件** | 日志中没有 error/fatal 级别数据。 |

### 2.3 `error_rate`（节点级）— 节点错误率

| 项目 | 内容 |
|------|------|
| **定义** | 错误日志占总日志的比例 |
| **计算公式** | `round(error_count / log_count, 4)` |
| **数据来源** | Logs 源 `_get_logs_topology()` 第 1358 行 |
| **UI 位置** | 悬停卡片"错误率"字段、边颜色判定依据之一 |
| **有效判定** | ✅ **有效**。直接来自日志统计。 |
| **失效条件** | `log_count = 0` 时结果为 0（除以 0 保护）。 |

### 2.4 `coverage`（覆盖率）— 节点

| 项目 | 内容 |
|------|------|
| **定义** | 0~1 的数值，表示对该服务节点的**数据观测充分程度**，不是"多少服务被覆盖了" |
| **计算公式** | `coverage_score(log_count, trace_count, data_sources)` 详见下文 |
| **数据来源** | `topology_contract.py:coverage_score()` → `apply_node_contract()` 第 166-170 行 |
| **UI 位置** | 节点卡片右下：`cov XX%` |
| **代码行** | `TopologyPage.tsx:4312` |

#### 计算公式拆解

```python
def coverage_score(call_count, log_count, trace_count, data_sources):
    score = 0
    score += min(0.60, (call_count / 200) * 0.60)   # 调用量维度，最多60分
    score += min(0.25, (trace_count / 80) * 0.25)    # Trace维度，最多25分   ← 节点不传call_count
    score += min(0.15, (log_count / 500) * 0.15)      # 日志维度，最多15分
    if len(set(sources)) >= 2: score += 0.05          # 多源加成
    if len(set(sources)) >= 3: score += 0.05          # 三源加成
    return min(1.0, score)
```

> **注意**：节点 `apply_node_contract()` 不传 `call_count`，因此**节点覆盖率只有日志和 trace 两个数据维度**。

#### 仅有 Logs 时的实际表现

```
coverage = 0 + 0 + min(0.15, log_count/500 * 0.15) + 0 + 0
         = min(0.15, log_count × 0.0003)
```

| log_count | coverage | 页面显示 |
|-----------|----------|---------|
| 0 | 0.0 | cov **0%** |
| 100 | 0.03 | cov **3%** |
| 500 | 0.15 | cov **15%** ← 上限 |
| 5000 | 0.15 | cov **15%** ← 有日志但不是无限增加 |

→ **不恒定**。在 0~500 条日志间线性增长到 15%，超过 500 条后恒定为 15%。

| 项目 | 内容 |
|------|------|
| **有效判定** | ⚠️ **部分有效**。仅反映日志数据是否充足，不能反映调用关系覆盖度。仅有 logs 时最大 15%，容易误读为"覆盖率很低"。 |
| **失效条件** | 无 logs 且无 traces 时 = 0。但只要有日志，始终有一定的 coverage 值。 |

### 2.5 `quality_score`（质量分）— 节点

| 项目 | 内容 |
|------|------|
| **定义** | 0~100 的综合质量评分，越高越好 |
| **计算公式** | 如果已有 `metrics.quality_score > 0` 则直接使用，否则**兜底公式**: `max(0, min(100, confidence × 100 - error_rate × 20))` |
| **数据来源** | `topology_contract.py:apply_node_contract()` 第 172-176 行 |
| **UI 位置** | 悬停卡片"质量分"字段 |
| **代码行** | `TopologyPage.tsx:4446` |

> `confidence` 在 logs 单源时为 ~0.5，减去 `error_rate × 20` 后一般在 30~50 分范围。**这不反映延迟或调用成功率**，仅反映日志错误率 + 置信度。

| 项目 | 内容 |
|------|------|
| **有效判定** | ⚠️ **部分有效**。只反映"日志错误率 + 数据陈旧度"，不反映 P99 延迟、超时等。有 traces 时会结合更多维度，更准确。 |
| **失效条件** | 本身总有值，但在无 traces 时信息不完整。 |

### 2.6 `confidence`（置信度）— 节点

| 项目 | 内容 |
|------|------|
| **定义** | 0~1，表示系统对该节点数据可靠程度的判断 |
| **计算公式** | `confidence_calculator.py:calculate_node_confidence()` |
| **公式拆解** | `base=0.5 + log10(log_count+1)×0.15 + log10(trace_count+1)×0.25`（上限 0.8） → × time_decay → × (1-error_penalty) → + multi_source_boost |
| **数据来源** | `recalculate_topology_confidence()` 第 354-364 行 |
| **有效判定** | ⚠️ **部分有效**。反映数据是否新鲜、多源交叉验证程度。logs 单源 ≈ 0.5~0.6。不能作为"数据是否准确"的判断依据。 |
| **失效条件** | 总有值，但在 logs 单源时偏乐观（~0.5 被视为"中等置信"，但实际上只观测到了日志，未验证调用关系）。 |

---

## 3. 边指标

### 3.1 `call_count` — 调用次数

| 项目 | 内容 |
|------|------|
| **定义** | 时间窗口内从 source 到 target 的调用次数 |
| **计算公式** | **Traces**: span 父子关系聚合 `edges[edge_key]["call_count"] += 1`（第 1154 行） |
| **数据来源** | Traces 源；推断边此值为 `call_count: None` |
| **UI 位置** | 边描述、路径摘要的 `rpm` 字段 |
| **有效判定** | ✅ Traces 源 **有效** / ❌ 推断边 **无效（None）** |
| **失效条件** | 无 traces 数据时，所有边的 call_count 为 None。 |

### 3.2 `error_rate`（边级）— 边错误率

| 项目 | 内容 |
|------|------|
| **定义** | 该调用边上的错误比例 |
| **计算公式** | **Traces**: `data["error_count"] / call_count`（第 1197 行） |
| **数据来源** | Traces 源；推断边 **硬编码 0.0**（第 1463 行） |
| **UI 位置** | 悬停卡片"错误率"、边颜色判定 |
| **有效判定** | ✅ Traces 源 **有效** / ❌ **推断边永远为 0** |
| **失效条件** | 无 traces 数据 → 全部 0，真实错误无法发现。 |

### 3.3 `p95` / `p99` — 延迟百分位（核心指标）

| 项目 | 内容 |
|------|------|
| **定义** | P95 = 该调用的第 95 百分位耗时（ms）；P99 = 第 99 百分位耗时 |
| **计算公式** | `self._percentile(data["durations"], 0.95/0.99)` 线性插值百分位数 |
| **数据来源** | **仅 Traces** 源从 `duration_ms` 聚合（第 1198-1199 行） |
| **RED 覆盖** | `trace_edges_1m` 的 `max(p99_ms)` 或 traces 自连接的 `max(p99_per_minute)` |
| **UI 位置** | 悬停卡片 `P95/P99 XXXms`、路径摘要、边排序模式 |

#### 数据流图

```
                      ┌─────────────────────────────┐
                      │   Fluent Bit / OTel Collector │
                      │   采集 span (含 duration_ms)  │
                      └─────────────┬───────────────┘
                                    ▼
                      ┌─────────────────────────────┐
                      │   logs.traces 表              │
                      │   (duration_ms 列)           │
                      └─────────────┬───────────────┘
                                    │
          ┌─────────────────────────┼─────────────────────────┐
          ▼                         ▼                         ▼
┌──────────────────┐   ┌──────────────────────┐   ┌──────────────────┐
│ _get_traces       │   │ trace_edges_1m        │   │ 推断边           │
│ topology()        │   │ (预聚合)              │   │ (_get_logs       │
│                   │   │                       │   │  _topology)      │
│ 实时计算 p99      │   │ SELECT max(p99_ms)    │   │                  │
│ percentile(       │   │ FROM trace_edges_1m   │   │ hardcoded:       │
│   durations,0.99) │   │                       │   │ p99: 0.0         │
└────────┬─────────┘   └──────────┬────────────┘   └────────┬─────────┘
         │                        │                          │
         └───────────┬────────────┘                          │
                     ▼                                       │
         ┌──────────────────────┐                            │
         │    RED Metrics 聚合   │ ← 仅当有 traces 边时触发   │
         │    (apply_edge_red)  │                            │
         └──────────┬───────────┘                            │
                    ▼                                        ▼
                    ┌─────────────────────────────────────────┐
                    │    最终 edge.p99 = 非零值 / 0.0          │
                    └─────────────────────────────────────────┘
```

| 项目 | 内容 |
|------|------|
| **有效判定** | ✅ Traces 源 **完全有效** / ❌ **无 traces 时全部 0ms，且无法从其他数据源补全** |
| **失效条件** | (1) traces 表无数据 → 0ms；(2) traces 表有数据但 `duration_ms` 列未填充 → 0ms；(3) traces 表有数据但无 `parent_span_id`（无父子关系）→ 没有边 |

> ⚠️ **这是拓扑页面中最重要的无效指标之一**。P99 在很多环境中是核心服务质量指标，如果 traces 没有数据，P99 显示为 0ms 会给运维人员"延迟极低"的错觉。

### 3.4 `timeout_rate` — 超时率

| 项目 | 内容 |
|------|------|
| **定义** | 该边上超时调用占总调用的比例 |
| **计算公式** | **Traces**: `(timeout_count / call_count)`（第 1200 行），其中 `duration_ms ≥ timeout_ms(1000)` 记为超时 |
| **数据来源** | **仅 Traces** 源；推断边硬编码 0.0 |
| **有效判定** | ✅ Traces 源 **有效** / ❌ **无 traces 时全部 0** |
| **失效条件** | 同 P99。完全依赖 traces 数据。 |

### 3.5 `retries` / `pending` / `dlq`

| 项目 | 内容 |
|------|------|
| **定义** | 重试次数、排队中数量、死信队列数量（平均每次调用） |
| **计算公式** | Traces: `data[key] / call_count`（第 1201-1203 行） |
| **数据来源** | **仅 Traces** 源（从 span attributes 提取）；推断边硬编码 0.0 |
| **有效判定** | ❌ **多数环境无效**。即使有 traces，这些字段也需要 span attributes 中显式传递，大部分埋点不会产出。 |
| **失效条件** | 无 traces 或无对应 attributes。 |

### 3.6 `quality_score`（质量分）— 边

| 项目 | 内容 |
|------|------|
| **定义** | 0~100，该边的通信质量 |
| **计算公式** | **路径 A**（`confidence_calculator.py:calculate_edge_quality_score`）: 从 100 分扣减 |
| **扣分项** | |

| 扣分项 | 公式 | 最多扣分 | 依赖 traces? |
|--------|------|---------|:----------:|
| error_rate | `min(error_rate × 100 × 0.60, 40)` | 40 | ✅ |
| p95 > 300ms | `min((p95 - 300) / 25, 12)` | 12 | ✅ |
| p99 > 800ms | `min((p99 - 800) / 30, 15)` | 15 | ✅ |
| timeout_rate | `min(timeout_rate × 100 × 0.80, 18)` | 18 | ✅ |
| retries | `min(retries × 2, 8)` | 8 | ✅ |
| pending | `min(pending × 0.5, 4)` | 4 | ✅ |
| dlq | `min(dlq × 1.5, 8)` | 8 | ✅ |

**路径 B**（`topology_contract.py:apply_edge_contract` 兜底）: `max(0, min(100, confidence × 100 - error_rate × 50))`

| 项目 | 内容 |
|------|------|
| **UI 位置** | 悬停卡片"质量分"、边描述 `qos {score}` |
| **代码行** | `TopologyPage.tsx:3649, 4150-4151, 4519` |
| **有效判定** | ❌ **无 traces 时完全无效**。所有扣分项均为 0 → 质量分恒为 **100/100**，产生"所有边质量极好"的严重误导。 |
| **失效条件** | 无 traces 数据或 RED 聚合查询失败。 |

### 3.7 `confidence`（置信度）— 边

| 项目 | 内容 |
|------|------|
| **定义** | 0~1，系统对该边存在性的确信程度 |
| **计算公式** | `confidence_calculator.py:calculate_edge_confidence()` |
| **拆解** | `base = SOURCE_BASE_WEIGHTS[data_source]`（traces=1.0, inferred=0.3, logs_heuristic=0.3）→ × time_decay → × (1-error_penalty) → + multi_source_boost + call_count_boost |
| **UI 位置** | 间接用于过滤（低于 0.3 的边被隐藏）和边排序 |
| **有效判定** | ⚠️ **部分有效**。反映该边的"证据强度"而非实际通信质量。traces 边高、推断边低，这个区分有意义。 |
| **失效条件** | 总有值。在 logs 推断场景下典型值 ~0.3~0.5。 |

### 3.8 `evidence_type` — 证据类型

| 项目 | 内容 |
|------|------|
| **定义** | `observed` = 来自 traces/真实观测；`inferred` = 来自日志推断/启发式规则 |
| **判定规则** | `topology_contract.py:evidence_type_from_source()`: traces/observed/metrics/logs → `observed`；logs_heuristic/inferred → `inferred` |
| **UI 位置** | 边颜色：蓝色=observed，紫色=inferred；证据类型筛选器 |
| **有效判定** | ✅ **有效**。基于数据源名称判定，不存在丢失场景。 |

### 3.9 `coverage`（覆盖率）— 边

| 项目 | 内容 |
|------|------|
| **定义** | 与节点 coverage 类似，但增加了 `call_count` 维度 |
| **计算公式** | `coverage_score(call_count, log_count, trace_count, data_sources)` |
| **参数来源** | `call_count` = 边的 metrics.call_count；`log_count`/`trace_count` = 源节点对应的日志/trace 计数 |
| **有效判定** | ⚠️ **部分有效**。traces 边可以有较高的 coverage（由 call_count 贡献最高 60 分）。推断边无 call_count → 仅从源节点的 log_count 贡献最多 15%。 |
| **失效条件** | 同节点 coverage。 |

### 3.10 `issueScore` / `riskLevel` — 问题分 / 风险等级

| 项目 | 内容 |
|------|------|
| **定义** | 综合风险评分，0~100+，越高越危险。≥70=高风险，≥35=中风险，<35=低风险 |
| **计算公式** | `topologyProblemSummary.ts:resolveEdgeProblemSummary()` |

```typescript
// 边问题分
errorScore = min(error_rate × 100, 1) × 50         // 最多50分
timeoutScore = min(timeout_rate × 100, 1) × 25      // 最多25分
latencyScore = min((p95 + p99) / 2500, 1) × 30      // 最多30分
qualityPenalty = max(0, (70 - qualityScore) / 70) × 30  // 最多30分
evidencePenalty = inferred ? 3 : 0

issueScore = errorScore + timeoutScore + latencyScore + qualityPenalty + evidencePenalty
```

```typescript
// 节点问题分
issueScore = min(errorCount, 8) × 4 +
             min(errorRate × 100, 1) × 40 +
             min(timeoutRate × 100, 1) × 20 +
             max(0, (85 - qualityScore) / 85) × 25
```

| 项目 | 内容 |
|------|------|
| **UI 位置** | 右边"问题边"面板、边颜色（红/黄/蓝）、节点状态（红/黄/绿） |
| **代码行** | `TopologyPage.tsx:2384-2398（问题边排序）`, `845-868（边颜色）`, `774-792（节点状态）` |
| **有效判定** | ❌ **无 traces 时完全无效**。|

#### 失效原因（无 traces 场景）

```
errorRate(边)     = 0     → errorScore = 0
timeoutRate(边)   = 0     → timeoutScore = 0
p95 = p99 = 0       → latencyScore = 0
qualityScore = 100   → qualityPenalty = max(0, (70-100)/70) × 30 = 0
evidencePenalty      = 3 (推断边)

issueScore = 0 + 0 + 0 + 0 + 3 = 3 → "低风险"
```

**所有边都标记为"低风险"（绿色），即使某条边背后存在严重的超时或高延迟问题。** 这是整个拓扑页面最严重的误导性指标。

---

## 4. 聚合元数据

### 4.1 `avg_confidence`

| 项目 | 内容 |
|------|------|
| **定义** | 所有过滤后边的置信度平均值 |
| **计算公式** | `sum(edge.confidence) / len(edges)` |
| **有效判定** | ⚠️ 同 edge confidence，部分有效。 |

### 4.2 `inference_quality`

| 字段 | 含义 | 有效判定 |
|------|------|---------|
| `coverage` | 所有边的平均覆盖率 | ⚠️ 同边 coverage |
| `inferred_ratio` | 推断边占总边的比例 | ✅ 准确 |
| `false_positive_rate` | 推断边中与观测不一致的比例 | ✅ 但需要 observed baseline |
| `request_id_edges` | 通过 request_id 关联发现的边数 | ✅ |
| `trace_id_edges` | 通过 trace_id 关联发现的边数 | ✅ |
| `message_target_edges` | 通过消息目标提取发现的边数 | ✅ |
| `time_window_edges` | 通过时间窗推断的边数 | ✅ |
| `evidence_sparse` | 是否存在强证据 | ✅ |

### 4.3 `source_breakdown`

| 字段 | 含义 | 有效判定 |
|------|------|---------|
| `traces.nodes/edges` | traces 数据源产出的节点/边数 | ✅ 准确反映各数据源贡献 |
| `logs.nodes/edges` | logs 数据源产出的节点/边数 | ✅ |
| `metrics.nodes/edges` | metrics 数据源产出的节点数 | ✅ |

---

## 5. 特殊情况场景分析

### 5.1 仅有 Logs，无 Traces

这是最常见的缺失场景。各指标表现：

| 指标 | 表现 | 是否误导 |
|------|------|:-------:|
| log_count | ✅ 正常 | 否 |
| error_count | ✅ 正常 | 否 |
| 节点 error_rate | ✅ 正常 | 否 |
| 节点 coverage | ⚠️ max **15%** | 可能偏低 |
| 节点 quality_score | ⚠️ 基于 confidence(0.5) - error_rate×20 | 未反映延迟 |
| 边 error_rate | ❌ **全部 0** | **是** |
| 边 p95/p99 | ❌ **全部 0ms** | **是** |
| 边 quality_score | ❌ **全部 100 分** | **严重误导** |
| issueScore/riskLevel | ❌ **全部低风险** | **严重误导** |
| 边 confidence | ⚠️ 0.3~0.5 | 合理但偏低 |
| coverage（边） | ⚠️ max 15% | 偏低 |

### 5.2 仅有 Traces，无 Logs

| 指标 | 表现 |
|------|------|
| 节点 coverage | ⚠️ max 25%（trace_count 维度贡献） |
| 节点 error_rate | ❌ **0**（logs 源不提供，traces 不写 nodes.error_rate） |
| 节点 quality_score | ⚠️ confidence(1.0)×100 - 0 = 100 |

### 5.3 Traces 表有数据但无 `duration_ms`

| 指标 | 表现 |
|------|------|
| p95/p99 | ❌ `percentile([0,0,0,...]) = 0` |
| error_rate | ✅ 有效（有 error_count/call_count） |
| call_count | ✅ 有效 |
| quality_score | ❌ 所有扣分项中 p95/p99 部分为 0，其他项可能有效 |

### 5.4 空窗口自适应

```python
# hybrid_topology.py 第 575-610 行
if total_nodes == 0 and safe_time_window != "24 HOUR":
    safe_time_window = "24 HOUR"  # 自动扩大到 24 小时重试
```

如果 1 小时间窗口内没有任何数据，系统自动扩大到 24 小时重试。此时指标反映的是 24 小时聚合值，时间粒度变大。

---

## 6. 汇总表

### 有效指标（有可靠数据源）

| 指标 | 适用对象 | 可靠性 | 备注 |
|------|---------|:------:|------|
| `log_count` | 节点 | ★★★ | 日志直接统计 |
| `error_count` | 节点 | ★★★ | 日志级别统计 |
| `node.error_rate` | 节点 | ★★★ | `error_count / log_count` |
| `call_count` | 边（traces） | ★★★ | 仅 traces 源 |
| `evidence_type` | 边 | ★★★ | 数据源名称判定 |
| `rps` | 节点（logs） | ★★★ | `log_count / window_seconds` |
| `source_breakdown` | 元数据 | ★★★ | 统计各源贡献 |

### 部分有效指标

| 指标 | 适用对象 | 可靠性 | 备注 |
|------|---------|:------:|------|
| `coverage` | 节点/边 | ★★ | 仅反映输入数据量，不反映实际覆盖范围 |
| `node.quality_score` | 节点 | ★★ | 反映错误率 + 置信度，不反映延迟 |
| `node.confidence` | 节点 | ★★ | 反映数据新鲜度/多源性 |
| `edge.confidence` | 边 | ★★ | 按数据源给分，traces=高/inferred=低，区分有意义 |

### 无效指标（完全依赖 traces，或无 traces 时产生误导）

| 指标 | 适用对象 | 无 traces 时表现 | 危害 |
|------|---------|:---------------:|:----:|
| `p95` / `p99` | 边 | 全部 **0ms** | 误以为延迟极低 |
| `edge.error_rate` | 边 | 全部 **0%** | 无法发现错误调用 |
| `timeout_rate` | 边 | 全部 **0%** | 无法发现超时问题 |
| `edge.quality_score` | 边 | 全部 **100 分** | ⭐ **最严重误导** |
| `issueScore` | 边/节点 | 全部 **≈3 分** | 问题边不被标记 |
| `riskLevel` | 边/节点 | 全部 **低风险（绿色）** | 假安全 |
| `retries/pending/dlq` | 边 | 全部 **0** | 数据完全缺失 |

---

## 7. 改进建议

| 问题 | 建议 |
|------|------|
| 无 traces 时 quality_score 满分 100 的误导 | 检测到无 traces 时，降级 quality_score 显示为 `N/A` 或上限设 50 |
| 无 traces 时 issueScore 永远低风险 | 降级 issueScore 计算：当 p99/error_rate/timeout_rate 全部为 0 时改为未知态 |
| 无 traces 时 p99 显示 0ms | 检测到 traces 无数据时显示 `--` 或 `N/A` |
| coverage max 15%（logs-only）不够区分 | 增加日志相对量维度（如同 namespace 下服务间对比） |
| `source_breakdown` 信息在 UI 不可见 | 在元数据面板中展示各数据源的节点/边贡献数 |

---

> **参考文献**
>
> - `topology-service/graph/hybrid_topology.py` — 混合拓扑构建主逻辑
> - `topology-service/graph/topology_contract.py` — 统一契约 + coverage/quality_score 计算
> - `topology-service/graph/confidence_calculator.py` — 置信度和质量评分算法
> - `topology-service/graph/hybrid_topology_utils.py` — 合并、推断、RED 聚合工具
> - `shared_src/logoscope_storage/adapter.py` — RED 指标聚合查询
> - `frontend/src/utils/topologyProblemSummary.ts` — 前端问题分/风险等级计算
> - `frontend/src/utils/topologyGraph.ts` — 前端边排序/证据过滤
> - `frontend/src/pages/TopologyPage.tsx` — 拓扑页面渲染
