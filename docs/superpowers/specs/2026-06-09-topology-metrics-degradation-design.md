# 拓扑页面无效指标优化 — 设计文档

> 基于 `docs/topology-metrics-validity-analysis.md` 的分析结论，解决无 Traces 数据时指标严重误导的问题。

---

## 1. 问题陈述

当 ClickHouse `logs.traces` 表无数据或 `duration_ms` 列未填充时，拓扑页面以下指标产生严重误导：

| 指标 | 无 Traces 表现 | 误导后果 |
|------|:-------------:|---------|
| P99 / P95 | 全部 **0ms** | 误以为延迟极低 |
| 边 error_rate | 全部 **0%** | 无法发现错误调用 |
| 边 quality_score | 全部 **100 分** | 假满分，掩盖质量问题 |
| issueScore / riskLevel | 全部 **3分 / 低风险** | 真实问题被标记为安全 |
| timeout_rate / retries / pending / dlq | 全部 **0** | 无法发现超时/重试 |

## 2. 方案选择

中等改造（方案 B）：后端新增数据质量元数据 + 降级评分体系 + 前端多态展示。

---

## 3. 后端改造

### 3.1 `metadata.data_quality` 结构

在 `HybridTopologyBuilder.build_topology()` 的 metadata 中新增 `data_quality` 区块：

```python
metadata = {
    # ... 原有字段 ...
    "data_quality": {
        "traces_available": bool,       # traces 表有节点或边
        "logs_available": bool,         # logs 表有节点
        "metrics_available": bool,      # metrics 表有节点
        "dimension_status": {
            "latency": "available" | "missing",
            "error_rate_edge": "available" | "missing",
            "call_volume": "available" | "degraded" | "missing",
            "quality_score": "full" | "logs_only",
        },
        "score_logs_only": float | None,  # 降级替代质量分（0-100）
    },
}
```

**判定逻辑**（新增 `HybridTopologyBuilder._compute_data_quality()`）：
- `traces_available` = `len(traces_data["nodes"]) > 0 or len(traces_data["edges"]) > 0`
- `logs_available` = `len(logs_data["nodes"]) > 0`
- `metrics_available` = `len(metrics_data["nodes"]) > 0`
- `latency` = `"available"` 如果任一 traces 边的 `durations` 列表有有效值
- `error_rate_edge` = `"available"` 如果 `traces_data["edges"]` 长度 > 0
- `call_volume` = `"available"` 如果 traces 边 > 0；`"degraded"` 如果仅有推断边
- `quality_score` = `"full"` 如果 latency 可用；否则 `"logs_only"`

### 3.2 降级 quality_score 算法

新增 `confidence_calculator.py:calculate_logs_only_quality_score()`：

```python
def calculate_logs_only_quality_score(
    log_count: float,           # 源节点的日志量
    error_rate_node: float,     # 源节点的日志错误率
    is_inferred: bool,          # 是否为推断边
    call_count: Optional[int],  # 推断边为 None
    confidence: float,          # 边的置信度
) -> float:
    score = 100.0
    # 1. 错误率惩罚（从源节点日志错误率）
    score -= min(error_rate_node * 100 * 0.50, 35)
    # 2. 样本不足惩罚
    effective_samples = max(call_count or 0, log_count)
    if effective_samples < 100: score -= 10
    elif effective_samples < 500: score -= 5
    # 3. 推断边惩罚
    if is_inferred: score -= 8
    # 4. 低置信度惩罚
    if confidence < 0.4: score -= 5
    return max(0.0, min(100.0, score))
```

调用位置：`confidence_calculator.py:recalculate_topology_confidence()` 在计算边 quality_score 后，判断 data_sources 中是否包含 `"traces"`，若不包含则覆盖为降级分。

### 3.3 P99/P95/error_rate null 化

**执行位置**：`_apply_contract_schema()` 中、contract 转换之后、最终赋值之前。此时 `data_quality.dimension_status` 已经计算完毕，可根据 `latency` / `error_rate_edge` 状态做精细判断。

不使用 `"traces" in data_sources` 做单一判断，因为存在 "traces 有数据但无 duration_ms" 的中间态。

**判定表**：

| 字段 | 变 null 条件 | 原因 |
|------|-------------|------|
| `p95`, `p99` | `latency != "available"` | 即使有 traces 数据，无 duration_ms 也无法算百分位 |
| `timeout_rate` | `latency != "available"` | 超时判断基于 duration_ms ≥ timeout_ms |
| `error_rate`（边级） | `error_rate_edge != "available"` | 推断边的 error_rate = 0 是硬编码值，不真实 |
| `retries`, `pending`, `dlq` | `error_rate_edge != "available"` | 同 error_rate，推断边硬编码 0 |

**代码示例**：

```python
# _apply_contract_schema 中，contract 转换为 edges 后：
dim_status = data_quality["dimension_status"]
for edge in edges:
    em = edge.setdefault("metrics", {})
    if dim_status.get("latency") != "available":
        em["p95"] = None
        em["p99"] = None
        em["timeout_rate"] = None
    if dim_status.get("error_rate_edge") != "available":
        em["error_rate"] = None
        em["retries"] = None
        em["pending"] = None
        em["dlq"] = None
```

> Edge 层的 `coverage` / `call_count` 不受此影响：coverage 有自己的公式，call_count 在 traces 有数据时有效、推断边为 None 是已有逻辑。

---

## 4. 前端改造

### 4.1 类型定义

在 `frontend/src/utils/api.ts` 新增：

```typescript
interface DataQualityDimensionStatus {
  latency: 'available' | 'missing';
  error_rate_edge: 'available' | 'missing';
  call_volume: 'available' | 'degraded' | 'missing';
  quality_score: 'full' | 'logs_only';
}

interface DataQuality {
  traces_available: boolean;
  logs_available: boolean;
  metrics_available: boolean;
  dimension_status: DataQualityDimensionStatus;
  score_logs_only: number | null;
}
```

在 `TopologyGraph` 接口的 `metadata` 中增加 `data_quality?: DataQuality`。

### 4.2 `DataQualityIndicator` 组件

新增 `frontend/src/components/topology/DataQualityIndicator.tsx`：

```tsx
interface Props {
  dataQuality: DataQuality;
  onDismiss: () => void;  // 关闭后 localStorage 记 24h
}
```

- 位置：拓扑顶栏 refresh 按钮旁边
- 仅在任一 `*_available === false` 时渲染
- 头部：`⚠ 数据完整性: ● Traces 缺失 ● Logs 正常 ● Metrics 正常`
- 详情行：`部分指标已降级 — 当前无 Trace 数据，P99/错误率等指标不可用`
- 可折叠折叠按钮
- 关闭按钮，关闭后 `localStorage.setItem('topology:quality-dismissed', Date.now())`，24h 后恢复

### 4.3 节点卡片降级

文件：`frontend/src/pages/TopologyPage.tsx`

节点卡片的 quality_score 展示（约第 4446 行）：

```tsx
// 当前
质量分 {toNum(hoverCard.node?.quality_score ?? ... , 1)}

// 改为
{qualityScore !== null && qualityScore !== undefined ? (
  质量分 {toNum(qualityScore, 1)}
) : (
  质量分 {toNum(scoreLogsOnly, 1)} <span className="text-[9px] text-amber-400">(降级)</span>
)}
```

+ 悬停 `ⓘ` 图标，显示"Traces 数据不可用，基于日志评估"

### 4.4 边卡片降级

P99/P95 展示（约第 4516 行）：

```tsx
{latencyAvailable ? (
  <div>P95/P99 {p95}/{p99}ms</div>
) : (
  <div className="text-slate-600">
    P95/P99 --/-- 
    <Tooltip content="无 Trace 数据">ⓘ</Tooltip>
  </div>
)}
```

质量分展示（约第 4519 行）：

```tsx
{qualityScore !== null ? (
  质量分 {toNum(qualityScore, 1)}
) : logsOnlyScore !== null ? (
  <div className="rounded border border-amber-700/40 bg-amber-900/20 px-1.5 py-1">
    质量分 {toNum(logsOnlyScore, 1)}
    <span className="ml-1 text-[9px] text-amber-400">(降级)</span>
  </div>
) : (
  <div className="text-slate-600">质量分 --</div>
)}
```

边颜色在 `getEdgeColor()`（第 845 行）中，当 `latency=missing` 时降级规则：

```
inferred 边       → 紫色 (不变)
observed 边       → 蓝色 (不变)
logs_only 模式
  + 降级 issueScore >= 50 → 灰色虚线 "数据存疑"
  + 降级 issueScore < 50  → 浅紫色 (与 inferred 区分)
```

### 4.5 issueScore / 风险排序降级

新增 `topologyProblemSummary.ts`：

```typescript
export function computeDegradedEdgeIssueScore(
  edge: TopologyEdgeEntity,
  scoreLogsOnly: number | null,
): number {
  if (scoreLogsOnly === null || scoreLogsOnly === undefined) {
    return resolveEdgeIssueScore(edge); // 回退原逻辑
  }
  const edgeMetrics = asRecord((edge as any)?.metrics) || {};
  const isInferred = resolveEdgeEvidence(edge) === 'inferred';
  const nodeErrorRate = toNum(edgeMetrics.node_error_rate, 0);
  const confidence = toNum(edgeMetrics.confidence, 0);

  let score = 0;
  score += Math.min(nodeErrorRate * 100, 1) * 35;       // 日志错误率
  score += Math.max(0, (60 - scoreLogsOnly) / 60) * 25;  // 降级质量分惩罚
  if (isInferred) score += 10;                           // 推断惩罚
  if (confidence < 0.35) score += 5;                     // 低置信度惩罚

  return Math.round(score * 100) / 100;
}
```

降级风险阈值：
- 低风险: < 25
- 中风险: 25-49
- 高风险: ≥ 50

### 4.6 排序面板降级

文件 `TopologyPage.tsx` 约第 2384 行：

- `edgeSortMode` 下拉选项中，`error_rate` / `p99` / `timeout_rate` 在对应 `dimension_status` 字段为 `missing` 时**禁用**，hover 显示"需要 Trace 数据"
- 默认排序模式在降级时自动切换到 `anomaly`（已用降级算法）
- 面板标题从 `问题边` → `问题边 (降级模式)`
- 面板顶部加一条提示文字

### 4.7 工具提示（Tooltip）策略

三层：
1. **持久横幅（Banner）** — 页面顶部的 DataQualityIndicator，说明整体缺失情况
2. **字段级 ⓘ 图标** — 每个被降级/隐藏的字段旁的悬浮工具提示
3. **禁用排序模式的 Tooltip** — 下拉选项禁用时的悬浮提示

---

## 5. 涉及修改的文件清单

### 后端

| 文件 | 修改内容 |
|------|---------|
| `topology-service/graph/hybrid_topology.py` | 新增 `_compute_data_quality()`，build_topology 中写入 `data_quality` 到 metadata |
| `topology-service/graph/confidence_calculator.py` | 新增 `calculate_logs_only_quality_score()`，recalculate_topology_confidence 中判断降级模式 |
| `topology-service/graph/topology_contract.py` | `apply_edge_contract()` 中 p95/p99/timeout_rate/error_rate 等字段在无 traces 时返回 `None` |

### 前端

| 文件 | 修改内容 |
|------|---------|
| `frontend/src/utils/api.ts` | 新增 `DataQuality` / `DataQualityDimensionStatus` 类型 |
| `frontend/src/components/topology/DataQualityIndicator.tsx` | **新文件**：数据完整性指示器组件 |
| `frontend/src/utils/topologyProblemSummary.ts` | 新增 `computeDegradedEdgeIssueScore()`，`isEdgeQualityDegraded()` |
| `frontend/src/utils/topologyGraph.ts` | `computeEdgeIssueScore()` 加入降级分支判断 |
| `frontend/src/pages/TopologyPage.tsx` | 卡片降级展示、排序面板降级、顶栏集成 DataQualityIndicator |
| `frontend/src/hooks/useApi.ts` | 如有必要调整 topology 返回类型 |

---

## 6. 测试策略

### 后端

| 测试场景 | 验证点 |
|---------|--------|
| traces 有完整数据 | `data_quality.dimension_status.latency == "available"` |
| traces 无数据 | `latency == "missing"`, `error_rate_edge == "missing"`, `quality_score == "logs_only"` |
| 无 traces 边 quality_score | 验证使用 `calculate_logs_only_quality_score()` 而非原扣分制 |
| 无 traces 边 p99 | 验证返回 `None` 而非 `0.0` |
| 混合场景（traces 有节点无边） | `latency == "missing"` 但 `traces_available == True` |

### 前端

| 测试场景 | 验证点 |
|---------|--------|
| 无 traces 响应 | DataQualityIndicator 显示，P99 显示 `--` |
| 降级质量分显示 | 显示 `(降级)` 标记 |
| 排序模式禁用 | error_rate/p99 下拉项灰色 |
| 问题边面板降级标题 | 标题含 `(降级模式)` |
| Banner 关闭/24h 恢复 | localStorage 行为 |

---

## 7. 向后兼容

- 旧版本前端不识别 `data_quality` → 忽略该字段，行为不变
- 旧版本后端不提供 `data_quality` → 前端 `data_quality` 为 `undefined`，走原逻辑
- `p95` / `p99` / `error_rate` 从 `0.0` 变为 `None` → 旧前端读到 `null` 会显示空白或 0（JavaScript `Number(null) === 0`），**行为基本不变**，不会更差
