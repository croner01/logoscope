# 集群信息显示 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在日志详情展示所属集群（source_cluster），在拓扑页增加集群筛选器，全链路打通

**Architecture:** query-service SQL 加字段 → 前端 Event 类型补全 → 日志详情展示 → topology-service 加集群过滤参数 → 拓扑页集群筛选 UI

**Tech Stack:** ClickHouse, Python (query-service + topology-service), React + TypeScript

---

## 文件清单

| 文件 | 改动 | 职责 |
|------|------|------|
| `query-service/api/query_logs_service.py` | `_LOGS_LIGHT_FIELDS` + facets + detail | 日志查询返回 source_cluster |
| `frontend/src/utils/api.ts` | `Event` 接口加 `source_cluster` | 前端类型定义 |
| `frontend/src/pages/LogsExplorer.tsx` | sidebar detail tab 加集群行 | 日志详情展示 |
| `topology-service/graph/hybrid_topology.py` | `build_topology()` 加 `source_cluster` 参数 | 拓扑按集群过滤 |
| `topology-service/api/websocket.py` | 订阅消息解析加 `source_cluster` | WebSocket 透传 |
| `frontend/src/hooks/useApi.ts` | `useHybridTopology` 类型加 `source_cluster` | Hook 参数类型 |
| `frontend/src/pages/TopologyPage.tsx` | 集群状态 + 筛选器 + API 联动 | 拓扑页集群筛选 |

---

### Task 1: query-service — source_cluster 加入日志查询

**Files:**
- Modify: `query-service/api/query_logs_service.py:585-605` (_LOGS_LIGHT_FIELDS)
- Modify: `query-service/api/query_logs_service.py:~1072-1500` (facets clusters)
- Modify: `query-service/api/query_logs_service.py:~2543` (query_log_detail)

- [ ] **Step 1: `_LOGS_LIGHT_FIELDS` 加 source_cluster**

在 `query-service/api/query_logs_service.py` line 604 的 `host_ip` 后追加一行 `source_cluster`:

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
    source_cluster
"""
```

- [ ] **Step 2: Facets 查询加 clusters**

在 `query_logs_facets()` 中，在 namespace 查询块之后（约 line 1420-1430），增加 clusters 查询：

```python
# ===== clusters facet =====
cluster_query = f"""
    SELECT
        source_cluster AS value,
        count() AS count
    FROM logs.logs
    {namespace_prewhere}
    {namespace_where}
    WHERE source_cluster != ''
    GROUP BY value
    ORDER BY count DESC, value ASC
    LIMIT {{limit_clusters:Int32}}
    SETTINGS optimize_use_projections = 1, max_threads = {{max_threads:Int32}}
"""
cluster_params = {
    "limit_clusters": 50,
    "max_threads": max_threads,
}
cluster_params.update(shared_params)
cluster_rows = storage_adapter.execute_query(cluster_query, cluster_params)

cluster_buckets = [
    {
        "value": str(row.get("value") or "").strip(),
        "count": int(row.get("count") or 0),
    }
    for row in cluster_rows
    if str(row.get("value") or "").strip()
]
```

在 return dict 中新增 `"clusters": cluster_buckets`:

```python
return {
    "services": service_buckets,
    "namespaces": namespace_buckets,
    "levels": level_buckets,
    "clusters": cluster_buckets,  # ← 新增
    "context": { ... },
}
```

- [ ] **Step 3: `query_log_detail()` 加 source_cluster**

在 `query-service/api/query_logs_service.py` 的 `query_log_detail()` 函数（约 line 2543）中，确认其 SQL SELECT 也包含 `source_cluster`。如果该函数使用 `_LOGS_LIGHT_FIELDS` 则已自动包含；若使用独立 SELECT 则手动追加 `source_cluster`。

- [ ] **Step 4: 运行测试**

```bash
cd query-service && python -m pytest tests/ -x -q 2>&1 | tail -20
```

Expected: tests pass (no regressions, since source_cluster is a new optional field)

- [ ] **Step 5: 提交**

```bash
git add query-service/api/query_logs_service.py
git commit -m "feat(query): add source_cluster to log queries and facets

- _LOGS_LIGHT_FIELDS: add source_cluster column
- query_logs_facets: add clusters facet for cluster filter dropdown
- query_log_detail: include source_cluster in result

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Frontend — Event 类型 + 日志详情集群显示

**Files:**
- Modify: `frontend/src/utils/api.ts:352` (Event interface)
- Modify: `frontend/src/pages/LogsExplorer.tsx` (sidebar detail tab)

- [ ] **Step 1: Event 接口加 source_cluster**

在 `frontend/src/utils/api.ts` 的 `Event` 接口中，在 `correlation_kind` 定义附近新增：

```typescript
export interface Event {
  id: string;
  timestamp: string;
  // ... 现有字段
  source_cluster?: string;   // ← 新增
  // ...
  correlation_kind?: 'seed' | 'expanded' | 'candidate';
}
```

- [ ] **Step 2: 日志详情 sidebar 显示集群**

在 `frontend/src/pages/LogsExplorer.tsx` 的 `renderSidebar` 函数中，在「详情」tab 渲染区域（约 line 2400-2600），在容器/主机信息之后新增「所属集群」行。

先在 renderSidebar 函数顶部提取 `sourceCluster` 变量（与 `host`、`container` 等提取放在一起，约 line 1982-1995）：

```typescript
const sourceCluster = pickText(
  log.source_cluster,
  (log.attributes as Record<string, unknown>)?.source_cluster,
);
```

然后在详情信息区域（在 host/container 信息之后）新增：

```tsx
{/* 所属集群 */}
{sourceCluster && sourceCluster !== '-' && (
  <div className="flex items-center justify-between py-2" style={{ borderBottom: '1px solid var(--app-border)' }}>
    <span className="text-xs font-medium" style={{ color: 'var(--app-text-subtle)' }}>所属集群</span>
    <span
      className="text-xs font-medium flex items-center gap-1 px-2 py-0.5 rounded"
      style={{
        background: 'var(--color-info-soft)',
        color: 'var(--color-info-dark)',
        border: '1px solid var(--color-info-border)',
      }}
    >
      <Server className="w-3 h-3" />
      {sourceCluster}
    </span>
  </div>
)}
```

注意：不需要加快速筛选按钮，集群筛选只在拓扑页提供。

- [ ] **Step 3: 编译检查**

```bash
cd frontend && npm run typecheck 2>&1 | tail -20
```

Expected: no type errors

- [ ] **Step 4: 提交**

```bash
git add frontend/src/utils/api.ts frontend/src/pages/LogsExplorer.tsx
git commit -m "feat(ui): show source_cluster in log detail sidebar

- Event interface: add optional source_cluster field
- Log detail sidebar: display '所属集群' with quick-filter button

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: topology-service — 按集群过滤

**Files:**
- Modify: `topology-service/graph/hybrid_topology.py` (build_topology + data source methods)
- Modify: `topology-service/api/websocket.py` (subscription parsing)

- [ ] **Step 1: `build_topology()` 加 `source_cluster` 参数**

```python
def build_topology(
    self,
    time_window: str = "1 HOUR",
    namespace: str = None,
    source_cluster: str = None,       # ← 新增
    confidence_threshold: float = 0.3,
    inference_mode: Optional[str] = None,
    message_target_enabled: Optional[bool] = None,
    message_target_patterns: Optional[Any] = None,
    message_target_min_support: Optional[int] = None,
    message_target_max_per_log: Optional[int] = None,
) -> Dict[str, Any]:
```

- [ ] **Step 2: 数据源方法透传 `source_cluster`**

在 `build_topology()` 中，将 `source_cluster` 透传给三个数据源方法：

```python
traces_data = self._get_traces_topology(
    safe_time_window,
    namespace,
    source_cluster=source_cluster,    # ← 新增
)
logs_data = self._get_logs_topology(
    time_window=safe_time_window,
    namespace=namespace,
    source_cluster=source_cluster,    # ← 新增
    inference_mode=inference_mode,
    ...
)
metrics_data = self._get_metrics_topology(
    safe_time_window,
    namespace,
    source_cluster=source_cluster,    # ← 新增
)
```

并在自适应时间窗口的重试调用中也加上同样的参数。

- [ ] **Step 3: `_get_logs_topology()` 加 source_cluster 过滤**

在 `_get_logs_topology()` 方法签名中加参数（约 line 1224）：

```python
def _get_logs_topology(
    self,
    time_window: str,
    namespace: str = None,
    source_cluster: str = None,     # ← 新增
    inference_mode: Optional[str] = None,
    ...
):
```

在 SQL 查询的 WHERE/PREWHERE 条件列表追加：

```python
if source_cluster:
    prewhere_conditions.append("source_cluster = {source_cluster:String}")
    params["source_cluster"] = source_cluster
```

需要加到所有涉及 `logs.logs` 和 `logs.traces` 表的查询中（约 line 1258、line 1639、line 1651 附近）。

- [ ] **Step 4: `_get_traces_topology()` 加 source_cluster 过滤**

方法签名（约 line 1485）：

```python
def _get_traces_topology(
    self,
    time_window: str,
    namespace: str = None,
    source_cluster: str = None,     # ← 新增
):
```

在 traces 表的查询中追加 `source_cluster` WHERE 条件。注意：traces 表可能没有 source_cluster 列，需要先检测列存在性：

```python
# 在类中增加列存在性检查缓存
def _has_traces_source_cluster_column(self) -> bool:
    if self._traces_source_cluster_exists_cache is not None:
        return self._traces_source_cluster_exists_cache
    rows = self.storage.execute_query("""
        SELECT name FROM system.columns
        WHERE database = 'logs' AND table = 'traces' AND name = 'source_cluster'
    """)
    self._traces_source_cluster_exists_cache = len(rows) > 0
    return self._traces_source_cluster_exists_cache

# 使用
if source_cluster and self._has_traces_source_cluster_column():
    prewhere_conditions.append("source_cluster = {source_cluster:String}")
    params["source_cluster"] = source_cluster
```

- [ ] **Step 5: `_get_metrics_topology()` 加 source_cluster 过滤**

方法与 Step 4 类似，增加 `_has_metrics_source_cluster_column()` 缓存检查和 WHERE 条件。

- [ ] **Step 6: WebSocket 订阅解析加 source_cluster**

在 `topology-service/api/websocket.py` 的 `topology_websocket_endpoint()` 函数中，解析 subscription 消息时增加字段：

```python
source_cluster = str(subscription_data.get("source_cluster", "") or "").strip() or None
```

在调用 `builder.build_topology()` 时传入：

```python
result = builder.build_topology(
    time_window=time_window,
    namespace=namespace,
    source_cluster=source_cluster,     # ← 新增
    confidence_threshold=confidence_threshold,
    inference_mode=inference_mode,
    ...
)
```

- [ ] **Step 7: 运行测试**

```bash
cd topology-service && python -m pytest tests/ -x -q 2>&1 | tail -20
```

Expected: tests pass

- [ ] **Step 8: 提交**

```bash
git add topology-service/
git commit -m "feat(topology): add source_cluster filtering for multi-cluster support

- HybridTopologyBuilder.build_topology: add source_cluster parameter
- _get_logs_topology/_get_traces_topology/_get_metrics_topology: pass
  source_cluster to SQL WHERE clauses
- WebSocket subscription: parse and forward source_cluster
- Column existence checks for traces/metrics tables (backward compat)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Frontend — 拓扑页集群筛选器

**Files:**
- Modify: `frontend/src/hooks/useApi.ts:354` (useHybridTopology type)
- Modify: `frontend/src/pages/TopologyPage.tsx` (cluster state + filter + API linkage)

- [ ] **Step 1: `useHybridTopology` hook 类型加 source_cluster**

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

- [ ] **Step 2: TopologyPage 新增集群状态**

在 `TopologyPage.tsx` 的 state 定义区域（约 line 1040）新增：

```typescript
const [selectedCluster, setSelectedCluster] = useState<string>('');
const [availableClusters, setAvailableClusters] = useState<string[]>([]);
```

- [ ] **Step 3: 集群列表获取**

在 `TopologyPage` 组件的 useEffect 中，页面加载时通过 facets API 获取集群列表：

```typescript
// 获取可用集群列表
useEffect(() => {
  const fetchClusters = async () => {
    try {
      const result = await api.getLogFacets({ limit_clusters: 50 });
      const clusters = (result?.clusters || [])
        .map((c: { value: string }) => c.value)
        .filter(Boolean);
      setAvailableClusters(clusters);
    } catch {
      // 静默失败，不阻塞拓扑加载
    }
  };
  void fetchClusters();
}, []);
```

- [ ] **Step 4: 将 selectedCluster 传入拓扑查询**

找到 `useHybridTopology` 调用处（约 line 1129），在 params 中加 `source_cluster`：

```typescript
const { data, loading, error, refetch } = useHybridTopology({
  time_window: timeWindow,
  namespace: queryNamespace,
  source_cluster: selectedCluster || undefined,  // ← 新增
  confidence_threshold: confidenceThreshold,
  inference_mode: inferenceMode,
  // ...
});
```

在 `useRealtimeTopology` 的 subscription 中也加入：

```typescript
const { topology: realtimeTopology, isConnected: realtimeConnected } = useRealtimeTopology({
  enabled: realtimeTopologyEnabled,
  subscription: {
    time_window: timeWindow,
    namespace: queryNamespace,
    source_cluster: selectedCluster || undefined,  // ← 新增
    // ...
  },
});
```

- [ ] **Step 5: 筛选器 UI**

在拓扑页的筛选栏区域（约 line 1055-1060 附近的 filter controls 区域），新增集群下拉框。参考现有的 namespace/time_window 筛选器样式：

```tsx
{/* 集群筛选 */}
<div className="flex items-center gap-2">
  <Server className="w-4 h-4" style={{ color: 'var(--app-text-subtle)' }} />
  <select
    value={selectedCluster}
    onChange={(e) => setSelectedCluster(e.target.value)}
    className="input input-sm"
    style={{ minWidth: 140 }}
  >
    <option value="">全部集群</option>
    {availableClusters.map((cluster) => (
      <option key={cluster} value={cluster}>{cluster}</option>
    ))}
  </select>
</div>
```

注意需要导入 `Server` icon（如果尚未导入的话）。

- [ ] **Step 6: 编译检查**

```bash
cd frontend && npm run typecheck 2>&1 | tail -20
```

Expected: no type errors

- [ ] **Step 7: 提交**

```bash
git add frontend/src/hooks/useApi.ts frontend/src/pages/TopologyPage.tsx
git commit -m "feat(topology): add cluster filter dropdown to topology page

- useHybridTopology hook: add source_cluster parameter type
- TopologyPage: cluster state, facets-based cluster list fetch
- Filter dropdown + API linkage for HTTP and WebSocket subscriptions

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## 验证清单

1. `cd query-service && python -m pytest tests/ -x -q` — query-service 无回归
2. `cd topology-service && python -m pytest tests/ -x -q` — topology-service 无回归
3. `cd frontend && npm run typecheck` — TypeScript 无类型错误
4. 日志详情展开 → Detail tab → 可见「所属集群」行（含集群名称和快速筛选按钮）
5. 拓扑页筛选栏 → 集群下拉 → 可选已知集群 → 选择后拓扑图只显示该集群数据
