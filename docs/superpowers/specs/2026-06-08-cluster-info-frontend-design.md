# 集群信息显示方案设计

> **Goal:** 在日志详情和拓扑页展示/筛选集群信息（source_cluster），全链路打通 ClickHouse → 后端 API → 前端 UI

**Architecture:** 五层增量打通，每层独立可验证。后端 SQL 加字段 → API 透传 → 前端类型补充 → 日志详情展示 → 拓扑页集群筛选

**Tech Stack:** ClickHouse, query-service (Python), topology-service (Python), React + TypeScript

---

## 数据流

```
ClickHouse logs.logs                query-service                        前端
┌────────────────────┐     ┌──────────────────────────┐     ┌──────────────────────┐
│ source_cluster     │     │ _LOGS_LIGHT_FIELDS       │     │ Event.source_cluster  │
│ (已有数据)          │────→│   + source_cluster       │────→│                       │
│                    │     │                          │     │ 日志详情              │
│                    │     │ query_logs_facets        │     │  · 所属集群标签       │
│                    │     │   + clusters facet        │     │                       │
│                    │     └──────────┬───────────────┘     │ 拓扑页                │
│                    │                │                      │  · 集群筛选下拉框      │
│                    │     ┌──────────▼───────────────┐     │  · API 参数联动        │
│                    │     │ topology-service          │     └──────────────────────┘
│                    │────→│  build_topology()         │
│                    │     │  + source_cluster 参数     │
│                    │     │  → _get_logs_topology()   │
│                    │     │  → _get_traces_topology() │
│                    │     │  → _get_metrics_topology()│
│                    │     └──────────────────────────┘
```

---

## 改动清单

### Layer 1: query-service — SQL 查询加字段

#### 1.1 `_LOGS_LIGHT_FIELDS` 加 `source_cluster`

**文件:** `query-service/api/query_logs_service.py:585`

```python
_LOGS_LIGHT_FIELDS = """
    id,
    timestamp,
    toUnixTimestamp64Nano(timestamp) AS _cursor_ts_ns,
    service_name,
    level,
    message,
    pod_name,
    namespace,
    node_name,
    container_name,
    container_id,
    container_image,
    pod_id,
    trace_id,
    span_id,
    labels,
    JSONExtractRaw(attributes_json, 'log_meta') AS log_meta,
    attributes_json,
    host_ip,
    source_cluster       -- ← 新增
"""
```

#### 1.2 Facets 查询加 `clusters` 字段

在 `query_logs_facets()` 的 SQL 查询中添加一个 `source_cluster` 的 GROUP BY 查询（与 services/namespaces/levels 同级），返回可选集群列表：

```python
# Facets 返回新增:
"clusters": [
    {"value": "openstack-cluster-01", "count": 12345},
    {"value": "islap-cluster-01", "count": 67890},
]
```

**文件:** `query-service/api/query_logs_service.py:1072`

#### 1.3 日志详情查询加 field

`query_log_detail()` 中使用 `_LOGS_LIGHT_FIELDS` 或独立 SELECT，确保包含 `source_cluster`。

**文件:** `query-service/api/query_logs_service.py:2543`

---

### Layer 2: topology-service — 按集群过滤拓扑

#### 2.1 `HybridTopologyBuilder.build_topology()` 加参数

```python
def build_topology(
    self,
    time_window: str = "1 HOUR",
    namespace: str = None,
    source_cluster: str = None,    # ← 新增
    confidence_threshold: float = 0.3,
    inference_mode: Optional[str] = None,
    ...
) -> Dict[str, Any]:
```

**文件:** `topology-service/graph/hybrid_topology.py:473`

#### 2.2 三个数据源方法透传

`_get_logs_topology()`, `_get_traces_topology()`, `_get_metrics_topology()` 均新增 `source_cluster` 参数。

在 SQL 查询的 `PREWHERE/WHERE` 条件中追加:

```python
if source_cluster:
    prewhere_conditions.append("source_cluster = {source_cluster:String}")
    params["source_cluster"] = source_cluster
```

**文件:** `topology-service/graph/hybrid_topology.py`

具体位置:
- `_get_logs_topology()` — 约 line 1224, 对 logs 和 traces 表的查询加 WHERE
- `_get_traces_topology()` — 约 line 1485, 对 traces 表的查询加 WHERE
- `_get_metrics_topology()` — 约 line 1570, 对 metrics 表的查询加 WHERE

注意: traces 和 metrics 表不一定有 `source_cluster` 字段，需要用 `EXISTS (SELECT name FROM system.columns ...)` 做兼容判断，有则加过滤、无则跳过。

#### 2.3 WebSocket 订阅参数透传

**文件:** `topology-service/api/websocket.py:441`

订阅消息解析加 `source_cluster` 字段，透传给 `build_topology()`。

---

### Layer 3: 前端 Event 类型加字段

**文件:** `frontend/src/utils/api.ts:352`

```typescript
export interface Event {
  id: string;
  timestamp: string;
  // ... 现有字段
  source_cluster?: string;   // ← 新增
  // ...
}
```

---

### Layer 4: 前端日志详情展示

**文件:** `frontend/src/pages/LogsExplorer.tsx`

在 sidebar 的 `'detail'` tab 中，容器/主机信息区域下方新增一行：

```
所属集群:  openstack-cluster-01    [筛选按钮 🔍]
```

使用已有的 `pickText()` 工具函数提取 `source_cluster`：

```typescript
// 在 renderSidebar 中提取
const sourceCluster = pickText(
  log.attributes?.source_cluster,
  log.source_cluster,
);
```

渲染样式参考 namespace 的显示模式——蓝色标签加点击筛选功能。

---

### Layer 5: 前端拓扑页集群筛选

**文件:** `frontend/src/pages/TopologyPage.tsx`

#### 5.1 新增状态

```typescript
const [selectedCluster, setSelectedCluster] = useState<string>('');
const [availableClusters, setAvailableClusters] = useState<string[]>([]);
```

#### 5.2 集群列表获取

通过 `useLogFacets` 获取（需等待 facets API 返回 `clusters` 字段），或在拓扑页 onMount 时调用:

```
GET /api/v1/logs/facets?limit_clusters=50
```

返回的 `data.clusters` 填充 `availableClusters` 状态。

#### 5.3 Hook 类型加参数

**文件:** `frontend/src/hooks/useApi.ts:354`

```typescript
export const useHybridTopology = createApiHook(
  (params) => api.getHybridTopology(params),
  {} as {
    time_window?: string;
    namespace?: string;
    source_cluster?: string;           // ← 新增
    confidence_threshold?: number;
    inference_mode?: 'rule' | 'hybrid_score';
    force_refresh?: boolean;
    message_target_enabled?: boolean;
    message_target_patterns?: string;
    message_target_min_support?: number;
    message_target_max_per_log?: number;
  }
);
```

#### 5.4 联动拓扑查询

将 `selectedCluster` 传入 `useHybridTopology` (HTTP initial fetch) 和 `useRealtimeTopology` (WebSocket 订阅):

```typescript
const { data, loading, error, refetch } = useHybridTopology({
  time_window: timeWindow,
  namespace: queryNamespace,
  source_cluster: selectedCluster || undefined,  // ← 新增
  confidence_threshold: confidenceThreshold,
  inference_mode: inferenceMode,
  // ...
});

// WebSocket 实时订阅
const { topology: realtimeTopology } = useRealtimeTopology({
  enabled: realtimeTopologyEnabled,
  subscription: {
    time_window: timeWindow,
    namespace: queryNamespace,
    source_cluster: selectedCluster || undefined,  // ← 新增
    confidence_threshold: confidenceThreshold,
    inference_mode: inferenceMode,
    // ...
  },
});
```

#### 5.5 拓扑页筛选器 UI

在现有筛选栏区域新增集群下拉框（与 namespace 筛选器同级）:

---

## 兼容性与边界处理

| 场景 | 处理方式 |
|------|----------|
| 老数据无 source_cluster | 空字符串，前端显示 `-` |
| traces/metrics 表无 source_cluster 列 | 用 `EXISTS (SELECT ...)` 检测，没有则跳过 WHERE |
| 集群列表为空（无跨集群部署） | 筛选器隐藏/禁用 |
| 选择集群后拓扑无数据 | 正常 empty state 显示 |
| facet degrade | clusters 也参与 degrade 机制，超时则返回空列表 |

---

## 验证方式

1. **query-service:** `pytest query-service/tests/` — 确认 `source_cluster` 出现在日志响应中
2. **topology-service:** `pytest topology-service/tests/` — 确认 `source_cluster` 过滤生效
3. **前端日志:** 展开日志详情 → Detail tab → 可见「所属集群」行
4. **前端拓扑:** 筛选栏 → 集群下拉 → 选择 → 拓扑图过滤
5. **手动:** 分别创建本地集群和 openstack 集群的日志，确认拓扑正确按集群拆分

---

## 未涵盖 / 后续

- **跨集群拓扑聚合:** 当前设计只做按集群筛选，不做跨集群的调用关系聚合。后续可按需做 `source_cluster = ''`（本地） + 指定集群的对比视图
- **拓扑节点标注:** 当前仅在筛选层加集群，不修改拓扑节点本身的显示。后续可以考虑在节点标签上显示 `service_name (cluster)` 的格式
- **自动发现集群:** 当前靠 facets 驱动。后续可加定时任务探测新集群并自动加入下拉选项
