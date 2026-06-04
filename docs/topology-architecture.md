# 拓扑功能架构分析

> 生成日期: 2026-06-04 | 分析范围: TopologyPage + topology-service + query-service

---

## 一、数据管道架构（Data Pipeline）

**核心文件**: `topology-service/graph/hybrid_topology.py` (HybridTopologyBuilder)

```
                  ClickHouse
                      │
         ┌────────────┼────────────┐
         ▼            ▼            ▼
    traces 表     logs 表     metrics 表
         │            │            │
  _get_traces   _get_logs   _get_metrics
  _topology()   _topology() _topology()
         │            │            │
         ▼            ▼            ▼
    {nodes,edges} {nodes,edges} {nodes,edges}
         │            │            │
         └────────────┼────────────┘
                      ▼
               _merge_nodes()
               _merge_edges()
                      │
                      ▼
          ConfidenceCalculator
          .recalculate_topology_confidence()
                      │
                      ▼
          _apply_edge_red_aggregation()  ← M1-04: RED 指标覆盖
          _apply_contract_schema()       ← M1-01/02: 契约规范化
                      │
                      ▼
          confidence_threshold 过滤
                      │
                      ▼
          { nodes, edges, metadata }
```

### 关键设计决策

1. **三源 Bulkhead 模式**: 每个 `_get_*_topology()` 用 try/except 包裹，单个源失败不影响其他源。
2. **自适应时间窗口**: 首次查询三源汇总节点数为 0 时，自动扩大到 24 HOUR 重试。metadata 会正确反映实际使用的窗口（`safe_time_window = "24 HOUR"` 在重试前赋值）。
3. **扫描限制分层**:
   - `≤1h 窗口`: traces 扫描上限 80k, 推理采样 5k
   - `≤24h 窗口`: traces 扫描上限 160k, 推理采样 8k
   - `>24h 窗口`: traces 扫描上限 200k, 推理采样 12k
4. **`_get_logs_topology` 双重职责**: 既负责节点聚合（service aggregation），又负责边推断（M2 inference）。架构上边推断逻辑与节点聚合可以解耦。

---

## 二、M2 推理引擎架构（Inference Engine）

**核心文件**: `hybrid_topology.py::_infer_edges_from_logs()` + `hybrid_topology_utils.py`

无埋点场景下，系统从原始日志中推断服务间调用关系。

### 2.1 四级推断管道

```
日志采样 (MAX_INFER_SAMPLE 条, 按 timestamp DESC, 再 reverse 为时间序)
  │
  ├── Stage 1: request_id 关联 (weight=1.2, base_confidence=0.80, min_support=1)
  │     └── 同一 request_id 的多条日志按 timestamp 排序 → service 序列 → 相邻对即为边
  │
  ├── Stage 2: trace_id 关联 (weight=1.05, base_confidence=0.66, min_support=2)
  │     └── 同一 trace_id 内服务序列 → 边（request_id 缺失时的补充）
  │
  ├── Stage 3: message_target 提取 (weight=1.1, base_confidence=0.74, min_support=2)
  │     └── 从 message 文本中提取 URL 主机名/KV 键值/代理地址/RPC 端点
  │         → 匹配已知服务名 → 构建 (当前服务 → 目标服务) 边
  │
  └── Stage 4: time_window 回退 (weight=dynamic, base_confidence=0.35, min_support=4)
        └── 对无 request_id/trace_id 的记录，按时间邻近度配对
           (MAX_TIME_WINDOW_DELTA_SEC=0.8s 内的相邻日志)
```

### 2.2 推断统计模型

结果累积到 `edge_acc: Dict[(source, target, src_ns, tgt_ns), Accumulator]`：

```
{
  count: int,                  # 出现次数
  method_counts: Counter,       # 各方法贡献次数
  evidence_chain: List[dict],  # 证据链（最多 8 条）
  weighted_score: float,        # 加权总分
  namespace_match_total/hits,   # 命名空间一致性
  temporal_gaps: List[float],   # 时间间隔序列（最多 24 个）
}
```

### 2.3 推断边评估 (`evaluate_inference_edge`)

1. **确定主导方法** (最高 method_count)
2. **动态支持数阈值** (`_estimate_dynamic_support`): `hybrid_score` 模式下高流量服务的阈值按 `log10(volume + 1)` 增长
3. **支持数检查**: `count >= dynamic_min_support`
4. **hybrid_score 模式**: `inference_scorer.score_hybrid_edge()` — 多维评分（证据多样性、命名空间一致性、时间稳定性）
5. **rule 模式**: 直接使用 method_base_confidence

### 2.4 降噪策略

- **双向噪声抑制** (`compute_dropped_bidirectional_edges`): 同时存在 (A→B) 和 (B→A) 的边对，若都低于 min_support_time_window，全部丢弃
- **基础设施服务过滤**: time_window 回退阶段自动跳过 infrastructure 服务
- **registry 启发式保守化**: `image_pull_pattern` 边无强证据时跳过，有强证据时最多 2 条
- **强证据抑制弱启发式**: request_id/trace_id/message_target 的边对阻止同名服务对的启发式边生成

### 2.5 架构评价

**优势**: 四级管道从强到弱逐级降级设计合理；累加器模式统一了不同推断方法的数据结构；降噪策略较为完善。

**可改进点**:
- `_infer_edges_from_logs` 函数体约 400 行，Stage 3/4 可抽取为独立策略类
- `edge_acc` key 是四元组 `(source, target, src_ns, tgt_ns)`，跨 namespace 边合并逻辑可优化
- message_target 的 `known_services` 仅从当前批次构建，不包含历史服务名

---

## 三、置信度模型架构（Confidence Model）

**核心文件**: `topology-service/graph/confidence_calculator.py`

```
原始置信度 (来自 source_base_weight)
     │
     ▼
时间衰减因子
  ≤1h:  ×1.0  /  1-6h: ×0.8  /  6-24h: ×0.5  /  >24h: ×0.2
     │
     ▼
错误率惩罚
  0-1%: -0%  /  1-5%: -5%  /  5-10%: -15%  /  >10%: -30%
     │
     ▼
多数据源加成
  1 源: +0%  /  2 源: +20%  /  3 源: +35%
     │
     ▼
最终置信度 (0.0 ~ 1.0)
```

**数据源基础权重**:
- `traces` / `observed`: 1.0（最高可靠度）
- `logs`: 0.4（中等）
- `logs_heuristic` / `inferred` / `metrics`: 0.3（辅助）

### 架构评价

时间衰减确保旧关系权重降低；错误率惩罚确保问题边被标记；多源融合确保被多方验证的边置信度更高。

**注意**: `reference_time` 使用 `datetime.now(timezone.utc)`，意味着每次调用产生不同的衰减结果——缓存场景下同一拓扑在不同时刻可能有不同的 confidence 值。

---

## 四、拓扑契约架构（Topology Contract）

**核心文件**: `topology-service/graph/topology_contract.py`

**M1-01/02 目标**: 统一节点和边的数据结构。

### Node 主键

```
node_key = namespace:service_name:env
```

### Edge 主键

```
edge_key = src_node_key + dst_node_key + protocol + endpoint_pattern
```

### `apply_node_contract()`

- 提取 `service.namespace`, `service.name`, `service.env`
- 生成 `node_key`（稳定的全局唯一标识）
- 补全 `display_name`, `evidence_type`, `coverage`, `quality_score`
- `evidence_type`: `"observed"` (traces/metrics) 或 `"inferred"` (日志推断)

### `apply_edge_contract()`

- 生成 `edge_key`（稳定标识，用于去重和变更检测）
- 从 operation_name 推断 `protocol` (http/grpc/mq)
- 从 operation_name 归一化 `endpoint_pattern` (`/api/orders/123` → `/api/orders/:id`)
- 计算 `coverage`: `min(总调用 / 时间窗口秒数, 1.0)`
- 计算 `quality_score`: `confidence × (1 - error_rate) × evidence_type_weight`

### 架构评价

契约层是拓扑系统的"类型系统"，确保前端接收到的数据结构一致，无论数据来自 traces、logs 还是 metrics。

---

## 五、前端渲染架构（Rendering Engine）

**核心文件**: `frontend/src/pages/TopologyPage.tsx` (~4859 行)

### 5.1 渲染管线

```
visibleEdges
  │
  ▼
分组 (by edge pair key = "source|||target")
  │
  ▼
排序 (by priority: selected > path > focus > normal, then by issue score)
  │
  ▼
edge → EdgeRenderDatum
  │
  ├─ 图层 1: SVG path (可见边, 贝塞尔曲线)
  │    path = M x1 y1 C c1x c1y c2x c2y x2 y2
  │    其中:
  │      normalX = -dy/distance  (法向量)
  │      normalY =  dx/distance
  │      curveStrength = min(150, max(30, distance*0.2)) + bundleOffset + idShift
  │      c1 = start + 0.28*delta + normal * curveStrength
  │      c2 = start + 0.72*delta + normal * curveStrength
  │    strokeWidth = min(6.8, max(2.2, log10(rps+10)*1.75))
  │    opacity = 选中0.98 / 聚焦0.86 / 有高亮0.18 / 正常0.72
  │
  ├─ 图层 2: SVG path (透明宽点击区域, strokeWidth=22)
  │
  ├─ 图层 3: 动画流动点 (<circle> + <animateMotion>)
  │
  └─ 图层 4: Edge 标签 (防重叠放置算法)
       candidates = [0, 28, -28, 54, -54, 82, -82]
       → 选第一个不与已有 label 重叠的偏移
```

### 5.2 Edge 捆束 (Bundling)

同一 `(source, target)` 对的多条边共享相同的 pair key：

- **未展开** (collapsed): 所有边用相同路径，中间那条承载标签 `"calls ×3"`
- **展开** (expanded): zoom ≥ 1.15 或 edge 被选中或属于 selectedPath
  - 每条边偏移: `(index - (size-1)/2) * spacing`
  - spacing: 展开时 12-18px, 折叠时 4-6px

### 5.3 性能分级

| 模式 | 条件 | 行为 |
|------|------|------|
| Normal | edges < 180 | 完整渲染 |
| Dense | edges ≥ 180 | 简化 label, 非聚焦边降低 opacity |
| Heavy/UltraDense | edges ≥ 320 | 最小 flow dots, 无 label (除选中) |
| DragDegrade | 拖拽节点时 | 仅渲染选中路径边 |

### 5.4 架构评价

**优势**: 手写 SVG 避免引入 D3/cytoscape 等重型库；捆束机制减少视觉混乱；性能分级策略确保大规模拓扑下不卡顿。

**可改进点**: 4859 行单体组件维护成本高；渲染逻辑（2900+ 行）与状态管理（1000+ 行）混杂；edge/node/hover-card 渲染各自应拆分为独立组件。

---

## 六、布局系统架构（Layout System）

### 6.1 三种布局模式

| 模式 | 算法 | 新增节点 | 持久化 |
|------|------|---------|--------|
| **Swimlane** | 按 namespace 分 lane → lane 内按已有 Y 坐标稳定排序 + 新节点按名称字母排序 | 追加到 lane 末尾 | 无 |
| **Grid** | 固定列数网格 | 追加到网格末尾 | 无 |
| **Free** | 初始网格 + 用户拖拽 | 放到网格位置 | localStorage, key=`namespace:timeWindow:inferenceMode` |

### 6.2 Swimlane 稳定排序算法

```
1. 按 namespace 分组 visibleNodes
2. 每组内分离 existing (同一 lane 的旧节点) 和 newcomers
3. existing 按 prev Y 坐标稳定排序 (保持相对位置)
4. newcomers 按 service_name 字母排序
5. 统一布局: existing 在前, newcomers 在后
6. 计算 lane 高度 → 下一个 lane 的起始 Y
```

**为什么保持相对位置**: 避免数据刷新时节点在屏幕上跳动。

### 6.3 Free 布局持久化

```
key = `topology:free-layout:${namespace}:${timeWindow}:${inferenceMode}`
value = { [nodeId]: { x, y }, ... }
```

**key 包含 `inferenceMode` 的原因**: 不同推理模式下节点集合不同，free layout 快照需要隔离。

### 6.4 架构评价

Swimlane 的稳定排序避免了数据刷新时的节点跳动。但 hashing key 策略在参数频繁变化时会产生多次 localStorage 写入。三种模式的布局计算代码有明显的重复。

---

## 七、边生命周期架构（Edge Lifecycle）

```
旧边集 (prevKeys)          新边集 (newKeys)
     │                          │
     └──────────┬───────────────┘
                ▼
     entering = newKeys - prevKeys   → 状态 'entering' (2s 后 → 'active')
     active   = newKeys ∩ prevKeys   → 状态 'active'
     departing = prevKeys - newKeys  → 状态 'departing' (5s 后删除)
```

**渲染效果**:
- `entering`: 青色 (`#22d3ee`) + 虚线
- `active`: 正常颜色
- `departing`: 红色 (`#fb7185`) + 虚线 → 5 秒后移除

**注意**: `departingEdgesRef` 仅在 `visibleEdges` 变化时写入，WS 推送相同边集合（仅 metrics 变化）不会触发 departing——这是正确的行为。

---

## 八、实时更新架构（Realtime Update）

```
Frontend: useRealtimeTopology (WebSocket)
     │
     ▼
Backend: TopologyConnectionManager (api/websocket.py)
     │
     ├─ connect() → 注册连接 + 初始 subscription
     ├─ on message "subscribe" → 更新 params → 立即缓存并推送
     ├─ on message "get" → 构建 + 推送
     ├─ on message "ping" → "pong"
     │
     └─ Poller (每 5s):
          1. 按 subscription group 分组连接
          2. build_hybrid_topology_coalesced()
          3. should_push_topology():
             ├─ 距上次推送 ≥ 15s ?
             ├─ 新节点 (需连续出现 2 次) ≥ 1 ?
             └─ → 推送 / 缓存
```

### 前后端数据合并策略

```typescript
// TopologyPage.tsx: 实时数据优先，参数匹配时使用；否则降级到 REST 快照
if (realtimeTopology &&
    realtimeWindow === expectedWindow &&
    realtimeNamespace === expectedNamespace &&
    realtimeInferenceMode === inferenceMode &&
    realtimeMessageTargetEnabled === messageTargetEnabled &&
    realtimePatterns === expectedPatterns) {
  return realtimeTopology;
}
return data; // 降级到快照
```

### 架构评价

双重通道（REST snapshot + WS realtime）确保可用性——WS 断开时快照仍然可用。Poller 按 subscription group 聚合减少了 ClickHouse 负载。但 15s 最小推送间隔意味着实时性有最大 15s 的延迟。

---

## 九、边日志预览架构（Edge Log Preview）

**核心文件**: `query-service/api/query_logs_service.py::query_topology_edge_logs_preview()`

### 9.1 查询构建

```sql
-- 有 anchor_time 时:
PREWHERE timestamp <= {anchor_time}
  AND timestamp > {anchor_time} - INTERVAL {time_window}
  AND (service_name = {source} OR service_name = {target})

-- 无 anchor_time 时:
PREWHERE timestamp > now() - INTERVAL {time_window}
  AND (service_name = {source} OR service_name = {target})
```

**`anchor_time` 来自拓扑快照的 `metadata.generated_at`**，确保历史拓扑的时间窗口正确。

### 9.2 三级降级策略

```
初始查询: namespace 限定 (来自 edge 的 source/target namespace)
  │
  ├─ 有结果? → 返回
  │
  └─ 无结果? → degrade 1: 放宽 namespace (仅 service_name 匹配)
       │
       ├─ 有结果? → 返回 + { context: { degraded: true } }
       │
       └─ 无结果? → degrade 2: 关闭 exclude_health_check + 放宽 namespace
            │
            └─ 返回 (可能为空)
```

### 9.3 关联扩展

预览返回 `context.trace_ids` 和 `context.request_ids`，前端用于导航跳转时的关联过滤。

### 架构评价

降级策略确保即使 namespace 信息不准确也能找到关联日志。`anchor_time` 的正确使用（来自拓扑快照时间而非 now()）是确保时间窗口准确性的关键。

---

## 十、导航跳转架构（Topology → LogsExplorer）

### 10.1 完整数据流

```
TopologyPage                          LogsExplorer
───────────                           ────────────

buildEdgePreviewLogJump(log)
  → anchorTime: log.timestamp         URL:
  → sourceService, targetService      /logs?source_service=X&target_service=Y
  → correlationMode: 'or'                  &time_window=15+MINUTE
  → traceIds, requestIds                   &anchor_time=2026-...Z
                                      │
goToEffectiveLogs(options)            ▼
  → timeWindow: topology window    URLSearchParams 解析
  → anchorTime: options.anchorTime │
    || topologyAnchorTime          ▼
  → namespace: topology namespace  setStartTime / setEndTime
                                      (via resolveTimeWindowRange)
navigation.goToLogs(...)              │
  → URL params 构造                  ▼
  → navigate(`/logs?...`)           apiParams 构造
                                      → start_time, end_time,
                                        source_service, target_service, ...
                                      │
                                      ▼
                                    ClickHouse SQL
```

### 10.2 时间精度问题（已修复: commit c39fb6e）

```
log.timestamp          → "2026-06-04T03:33:36.492501Z" (微秒精度)
new Date(anchorTime)   → Date 对象 (毫秒精度, 0.492501 → 0.492)
end.toISOString()      → "2026-06-04T03:33:36.492Z" (截断)
_convert_timestamp     → "2026-06-04 03:33:36.492000"
ClickHouse timestamp   → "2026-06-04 03:33:36.492501"
492501 > 492000 → timestamp <= end_time = false → 日志被排除

修复方案: endWithBuffer = new Date(end.getTime() + 1000)
         → "2026-06-04T03:33:37.492Z" → 37.492000 ≥ 36.492501 ✓
```

**修复位置**: `frontend/src/pages/LogsExplorer.tsx::resolveTimeWindowRange()` (line 526)

**根因**: JavaScript `Date.prototype.toISOString()` 只输出 3 位小数（毫秒），而 ClickHouse 存储 6 位（微秒）。截断导致 `end_time` 早于实际日志时间戳。

### 10.3 导航链路关键设计点

- **`anchor_time` 使用日志时间戳而非拓扑快照时间** — 确保查询窗口以目标日志为中心
- **`time_window` 使用拓扑的当前窗口** — 确保日志在窗口范围内
- **`correlation_mode: 'or'`** — 使用 OR 模式放宽 trace_id/request_id 匹配
- **`+1s` 缓冲补偿 JS 精度损失** — JavaScript Date 毫秒精度 vs ClickHouse 微秒精度的桥接

---

## 十一、整体架构评价

| 层面 | 设计质量 | 关键优势 | 可改进点 |
|------|---------|---------|---------|
| 数据管道 | ★★★★ | 三源 Bulkhead + 自适应窗口 | 无缓存层，重复查询 |
| 推理引擎 | ★★★★★ | 四级管道 + 累加器模式 + 降噪 | 函数体过长，可策略化 |
| 置信度模型 | ★★★★ | 时间衰减 + 错误率 + 多源融合 | 与当前时间耦合 |
| 契约层 | ★★★★ | node_key/edge_key 稳定标识 | 版本演进策略不明确 |
| 渲染引擎 | ★★★★★ | 手写 SVG 完全控制 + 捆束 + 性能分级 | 单体组件 4859 行 |
| 布局系统 | ★★★★ | 稳定排序避免跳动 + 持久化 | 三种模式代码重复 |
| 生命周期 | ★★★★ | entering/departing 可视化演化 | 5s 硬编码 |
| 实时更新 | ★★★★ | REST + WS 双通道 + 按组聚合 | 15s 推送延迟 |
| 日志预览 | ★★★★★ | 降级策略 + anchor_time 正确使用 | 查询可能较重 |
| 导航跳转 | ★★★★ | 时间戳 + 缓冲区补偿精度 | 链路跨多层需仔细追踪 |

---

## 附录: 关键文件索引

| 文件 | 职责 |
|------|------|
| `topology-service/graph/hybrid_topology.py` | HybridTopologyBuilder — 三源拓扑构建 + M2 推理 |
| `topology-service/graph/hybrid_topology_utils.py` | 推断工具函数 (merge/accumulate/evaluate) |
| `topology-service/graph/topology_contract.py` | Node/Edge 契约规范化 (schema-v1) |
| `topology-service/graph/confidence_calculator.py` | 时间衰减 + 错误率 + 多源融合置信度 |
| `topology-service/graph/inference_scorer.py` | hybrid_score 模式多维评分 |
| `topology-service/api/websocket.py` | WS 连接管理 + Poller + 推送稳定性控制 |
| `topology-service/api/realtime_topology.py` | 实时拓扑缓存 + 快照管理 + 变更检测 |
| `query-service/api/query_logs_service.py` | 边日志预览查询 + 降级策略 |
| `frontend/src/pages/TopologyPage.tsx` | TopologyPage 主组件 (4859 行) |
| `frontend/src/pages/LogsExplorer.tsx` | resolveTimeWindowRange + TopologyJumpContext |
| `frontend/src/hooks/useApi.ts` | useHybridTopology / useRealtimeTopology / useTopologyEdgeLogPreview |
| `frontend/src/hooks/useNavigation.ts` | goToLogs — URL 构造与导航 |

---

## 附录 B: 页面数据字典

> 按页面区域逐一说明每个数据的含义、来源（哪个 API / 哪个字段 / 前端如何计算）。

---

### B.1 顶部工具栏状态栏

| 显示内容 | 含义 | 来源 |
|---------|------|------|
| **来源: 实时推送/查询快照** | 当前渲染的拓扑数据是从 WebSocket 实时推送还是 REST 快照 | 前端 `topologyRenderSource` memo：优先使用 `realtimeTopology`（参数匹配时），否则 fallback 到 `useHybridTopology` 的 `data` |
| **WS: 已连接/未连接** | 与 `/ws/topology` 的 WebSocket 连接状态 | `useRealtimeTopology` 返回的 `isConnected` |
| **新鲜度: ...·...** | 拓扑数据的"新鲜程度" | `topologyFreshness` memo：用 `effectiveTopologyAnchorTime`（即 `metadata.generated_at`）与当前时间对比计算。`statusLabel` = 实时(≤2min) / 新鲜(≤10min) / 稍旧(≤30min) / 陈旧(>30min)；`ageLabel` = 具体分钟数 |
| **时间窗: 15M/30M/...** | 当前查询的时间窗口 | `effectiveTopologyTimeWindow`：优先取 `topologyData.metadata.time_window`，fallback 到 UI 下拉框选择的 `timeWindow` |
| **锚点: ...** | 拓扑快照的生成时间 | `effectiveTopologyAnchorTime` = `topologyData.metadata.generated_at`（后端 `datetime.now(timezone.utc).isoformat()`） |
| **推断: rule/hybrid_score** | 推理模式 | `topologyInferenceModeLabel` memo：优先取 `metadata.inference_quality.inference_mode`，fallback 到 `inferenceMode` 状态 |
| **命名空间: ...** | 当前命名空间过滤 | `effectiveTopologyNamespace`：优先 URL `?namespace=`，fallback 到 topology metadata |

### B.2 拓扑态势面板——统计数据

| 显示内容 | 含义 | 来源 |
|---------|------|------|
| **节点 / N** | 当前过滤后可见的节点数 | `filteredTopology.nodes.length`（经过 evidence/weak/focus depth 等过滤器处理后） |
| **边 / N** | 当前过滤后可见的边数 | `filteredTopology.edges.length` |
| **泳道 / N** | 泳道数量 | `laneBands.length`（仅在 swimlane 模式下 > 0；按 namespace 分 lane） |
| **状态: 在线/离线** | WebSocket 实时连接状态 | `realtimeConnected` |
| **过滤耗时: Nms** | 前端过滤器链的总耗时 | `filteredTopology.costMs`（filterByEvidenceMode + filterGraphByFocusDepth + filterWeakEvidenceEdges 的耗时合计） |
| **问题节点/链路: N/N** | 有问题的节点和边数量 | `issueSummary.unhealthyNodes / unhealthyEdges`（来自 `resolveIssueSummary` → 后端 `metadata.issue_summary` 或前端客户端计算） |
| **全量视图: N节点/N边** | 过滤前的原始拓扑规模 | `filteredTopology.baseNodeCount / baseEdgeCount` |
| **当前视图: N节点/N边** | 过滤后实际渲染的规模 | `visibleNodes.length / visibleEdges.length` |

### B.3 拓扑态势面板——连线图例

| 颜色 | 含义 | 判定逻辑 |
|------|------|---------|
| **青色线** | 正常观测链路 | `evidence_type = "observed"` 且 riskLevel = "低风险" |
| **紫色线** | 推断链路 | `evidence_type = "inferred"`（来自 M2 推断，无 traces 验证） |
| **琥珀色线** | 预警链路 | riskLevel = "中风险"：错误率 > 3% 或超时率 > 2% 或 P99 > 650ms 或质量分 < 80 |
| **红色线** | 高风险链路 | riskLevel = "高风险"：错误率 > 8% 或超时率 > 5% 或 P99 > 1200ms 或质量分 < 60 |

### B.4 画布节点卡片

每个节点是一个绝对定位的 `<div>`，展示 3 个指标：

| 显示内容 | 含义 | 数据来源 |
|---------|------|---------|
| **服务名称** | 服务名 | `resolveServiceName(node)`：优先级 `node.service.name` → `node.metrics.service_name` → `node.label` → `node.id` |
| **泳道标签** | 所属 namespace | `resolveLane(node).label`：优先 `node.namespace`，fallback 到 `node.metrics.namespace`，最终 "默认" |
| **状态圆点** | 健康状态 | `getNodeStatus(node)`：`"error"`（错误率>5%且错误数>2 或大量日志且高错误率） / `"warning"`（错误率>0 或错误数>0） / `"normal"` |
| **节点颜色** | 按时间窗着色 | `getNodePalette(node, timeWindow)`：根据 `TIME_WINDOW_NODE_THEMES` 按时间窗给 service/database/cache 不同类型不同渐变色 |
| **log N** | 该服务日志总数 | `node.metrics.log_count` ← ClickHouse `COUNT(*)` GROUP BY service_name |
| **err N** | 该服务错误数 | `node.metrics.error_count` ← ClickHouse `SUM(CASE WHEN level IN ('error','fatal') THEN 1 ELSE 0 END)` |
| **cov N%** | 覆盖率 | `node.coverage ?? node.metrics.coverage` ← `apply_node_contract()` 中计算：`min(data_source_count / 3, 1.0)` |

### B.5 画布边渲染

| 显示内容 | 含义 | 数据来源 |
|---------|------|---------|
| **边颜色** | 风险等级语义 | `getEdgeColor(edge)`：根据 `riskLevel` + `error_rate` + `timeout_rate` + `evidence_type` 决定：红色(高风险) / 琥珀(预警) / 紫色(推断) / 青色(观测) |
| **边粗细** | 流量大小 | `min(6.8, max(2.2, log10(rps+10)*1.75))`：流量越高线越粗 |
| **透明度** | 聚焦状态 | 选中0.98 / 聚焦0.86 / 有高亮存在0.18 / 正常0.72 |
| **虚线** | 生命周期状态 | `entering`(青色虚线) / `departing`(红色虚线) / `active`(实线) |
| **流动小圆点** | 数据流向 | `<circle>` + `<animateMotion>` 沿贝塞尔曲线运动，duration 按路径长度 + index 交错 |
| **边标签** | 关系类型+指标 | 显示: `{relation.label}` (如 "HTTP调用") / `err {error_rate%}` / `p99 {p99ms}` / `qos {quality_score}` / `score {issueScore}` |
| **捆束标签** | 多条同向边聚合 | 未展开时中间那条标签显示 `"calls ×N"` |
| **箭头标记** | 调用方向+风险 | 5 种 SVG marker: `arrow-observed`(青) / `arrow-inferred`(紫) / `arrow-warning`(琥珀) / `arrow-danger`(红) / `arrow-entering`(青虚线) |

### B.6 节点 Hover 卡片

| 显示内容 | 含义 | 数据来源 |
|---------|------|---------|
| **服务名称** | 服务名 | `resolveServiceName(hoverCard.node)` |
| **Namespace \| Lane** | 命名空间和泳道 | `resolveNamespaceLabel(node)` + `resolveLane(node).label` |
| **异常/预警/正常** | 健康状态徽章 | `getNodeStatus(node)` |
| **日志 N** | 日志总数 | `node.metrics.log_count` |
| **错误 N** | 错误数 | `node.metrics.error_count` |
| **覆盖率 N%** | 数据覆盖率 | `node.coverage ?? node.metrics.coverage` → `Math.round(* 100)%` |
| **质量分 N** | 综合质量评分 | `node.quality_score ?? node.metrics.quality_score` ← `apply_node_contract()` 中计算 |

### B.7 边 Hover 卡片

| 显示内容 | 含义 | 数据来源 |
|---------|------|---------|
| **source → target** | 边端点 | `edge.source` / `edge.target`（node_id 格式） |
| **高风险/中风险/低风险** | 风险等级徽章 | `getRiskLevel(errorRate, timeoutRate, p99, qualityScore)` |
| **关系类型 · evidence** | 调用类型+证据级别 | `getRelationshipLabel(reason).label` + `evidence_type`（observed/inferred） |
| **错误率 N%** | 调用错误率 | `edge.metrics.error_rate` |
| **超时率 N%** | 调用超时率 | `edge.metrics.timeout_rate` |
| **P95/P99 N/Nms** | 延迟分位值 | `edge.metrics.p95` / `edge.metrics.p99` |
| **质量分 N** | 综合质量评分 | `edge.metrics.quality_score` ← `apply_edge_contract()` 中计算 |

### B.8 链路情报看板（Issues Panel）

展示 Top 10（最多显示前 7 条）问题边，按排序模式排序：

| 显示内容 | 含义 | 数据来源 |
|---------|------|---------|
| **source → target** | 边端点 | `edge.source` / `edge.target` |
| **err N%** | 错误率 | `edge.metrics.error_rate` |
| **p99 Nms** | P99 延迟 | `edge.metrics.p99 ?? edge.p99` |
| **to N%** | 超时率 | `edge.metrics.timeout_rate` |
| **score N** | 问题评分 | `resolveEdgeIssueScore(edge)` ← `resolveEdgeProblemSummary(edge).issueScore` |

排序模式 (`edgeSortMode`):
- **综合** (`anomaly`): `sortEdgesByIssueScore()` — 按 issueScore 降序
- **错误率**: 按 `error_rate` 降序
- **超时率**: 按 `timeout_rate` 降序
- **P99**: 按 `p99` 降序

**Issue Score 计算** (`resolveEdgeProblemSummary`，当后端未返回 `problem_summary` 时前端计算):

```
latencyScore  = min((p95 + p99) / 2500, 1) * 30
qualityPenalty = max(0, (70 - qualityScore) / 70) * 30
timeoutScore  = min(timeoutRate * 100, 1) * 25
errorScore    = min(errorRate * 100, 1) * 50
evidencePenalty = evidence === 'inferred' ? 3 : 0
issueScore    = round(errorScore + timeoutScore + latencyScore + qualityPenalty + evidencePenalty)
riskLevel     = issueScore >= 70 ? '高风险' : issueScore >= 35 ? '中风险' : '低风险'
```

### B.9 节点详情面板

| 区域 | 显示内容 | 含义 | 数据来源 |
|------|---------|------|---------|
| 头部 | 服务名称 | 服务名 | `resolveServiceName(selectedNode)` |
| 头部 | Namespace + Lane | 命名空间和泳道 | `resolveNamespaceLabel(selectedNode)` + `resolveLane(selectedNode).label` |
| 问题摘要 | **TS-02 节点问题摘要** | 风险等级+评分+标题+建议 | `resolveNodeProblemSummary(selectedNode)`：优先取后端 `node.problem_summary`，否则前端计算 |
| 问题摘要 | score N · headline | 问题评分+简要描述 | 后端注入的 `problem_summary.headline`，或前端留空 |
| 问题摘要 | 建议: ... | 修复建议 | 后端注入的 `problem_summary.suggestion` |
| 指标网格 | 日志数 | 日志总数 | `selectedNode.metrics.log_count` |
| 指标网格 | 错误数 | 错误总数 | `selectedNode.metrics.error_count` |
| 指标网格 | 覆盖率 | 数据源覆盖率 | `selectedNode.coverage ?? selectedNode.metrics.coverage` |
| 指标网格 | 质量分 | 综合质量评分 | `selectedNode.quality_score ?? selectedNode.metrics.quality_score` |
| 路径分析 | 上游/下游路径 | BFS 展开的上下游路径 | `focusPathSummaries` → `enumerateDirectionalPaths()` 在 `visibleEdges` 上 BFS 深度遍历 |
| 路径分析 | req/err/to/P99/qos/risk/hop | 路径聚合指标 | 路径上所有边的 `rps`(求和)、`error_rate`(max)、`timeout_rate`(max)、`P95/P99`(max)、`quality_score`(min)、`riskLevel`(综合) |
| 当前路径 | 路径解释文字 | 自然语言路径描述 | `selectedPath.explanation` |
| 导航按钮 | 查看服务日志 | 跳转到 LogsExplorer | `goToEffectiveLogs({ serviceName, namespace })` |
| 导航按钮 | 查看服务告警 | 跳转到 Alerts | `goToEffectiveAlerts({ scope: 'service', ... })` |
| 导航按钮 | AI 分析 | 跳转到 AI 对话 | `navigation.goToAIAnalysis({ logData: buildNodeAiPayload(node) })` |

**Node Issue Score 计算** (`resolveNodeProblemSummary`，当后端未返回时):

```
issueScore = min(errorCount, 8) * 4
           + min(errorRate * 100, 1) * 40
           + min(timeoutRate * 100, 1) * 20
           + max(0, (85 - qualityScore) / 85) * 25
riskLevel  = issueScore >= 70 ? '高风险' : issueScore >= 35 ? '中风险' : '低风险'
```

### B.10 边详情面板

| 区域 | 显示内容 | 含义 | 数据来源 |
|------|---------|------|---------|
| 头部 | source → target | 边端点 | `resolveEdgeEndpointService(edge, 'source')` → `resolveEdgeEndpointService(edge, 'target')` |
| 头部 | 关系类型 / evidence | 调用分类+证据级别 | `getRelationshipLabel(reason).label` / `evidence_type` |
| 头部 | 风险等级徽章 | high/medium/low | `edgeProblemSummary.riskLevel` |
| 问题摘要 | score N · headline | 问题评分+一句话描述 | `resolveEdgeProblemSummary(selectedEdge)` |
| 问题摘要 | 建议: ... | 修复建议 | `edgeProblemSummary.suggestion` |
| 链路描述 | **标准化链路描述** | 可读模板 | `formatEdgeDescription(edge)` = `"SRC → DST \| protocol \| N rpm \| 错误率 N% \| 超时率 N% \| P95 Nms / P99 Nms \| 质量分 N \| 证据 observed/inferred \| riskLevel（描述）"` |
| 指标网格 | **RPS(近似)** | 每秒请求数近似 | `edge.metrics.rps ?? edge.metrics.call_count` ← 后端 `call_count / window_seconds` |
| 指标网格 | **错误率** | 调用错误率 | `edge.metrics.error_rate` |
| 指标网格 | **P95 / P99** | 延迟分位值 | `edge.metrics.p95` / `edge.metrics.p99` |
| 指标网格 | **超时率** | 调用超时率 | `edge.metrics.timeout_rate` |
| 指标网格 | **覆盖率** | 边覆盖率 | `edge.coverage ?? edge.metrics.coverage` ← `apply_edge_contract()` 计算 |
| 指标网格 | **质量分** | 综合质量评分 | `edge.quality_score ?? edge.metrics.quality_score` ← `apply_edge_contract()` 计算 |
| Direction | **Direction 一致性贡献** | 有向性一致性指标 | `resolveDirectionalContribution(edge)`：从 `edge.metrics.directional_consistency` 读取，拆分为 confidence 贡献(+0.24×value) 和 evidence 贡献(+2.0×value) |
| 日志预览 | **链路问题日志预览(QS-01)** | 最多 6 条关联日志 | `edgeLogPreviewData` ← `useTopologyEdgeLogPreview(edgePreviewParams)` → `GET /api/v1/logs/preview/topology-edge` |
| 日志预览 | seed_count / expanded_count | 种子日志数 / 扩展日志数 | `edgeLogPreviewData.context` |
| 日志预览 | trace_id/request_id count | 关联 ID 数量 | `edgeLogPreviewData.context.trace_id_count / request_id_count` |
| 日志预览 | 每条日志条目 | 时间戳+级别+消息片段+边方向+匹配类型+关联精度 | 后端返回的 `data[]` 条目 |
| RED 指标 | **M1-04: Edge RED** | 边级 RED( Rate/Error/Duration) 聚合 | 后端 `_apply_edge_red_aggregation()` 注入 `edge.metrics.red_*` 字段 |
| 导航按钮 | 查看 Trace-Lite 片段 | 跳转 Traces | 使用 `edgePreviewCorrelationFilters.traceIds` |
| 导航按钮 | 查看边关联日志 | 跳转 LogsExplorer | 使用 `source_service + target_service + time_window` |
| 导航按钮 | 查看边告警 | 跳转 Alerts | `goToEffectiveAlerts({ scope: 'edge', ... })` |
| 导航按钮 | AI 分析 | 跳转 AI 对话 | `navigation.goToAIAnalysis({ logData: buildEdgeAiPayload(edge) })` |

### B.11 日志预览条目详情

每条边日志预览中的日志条目，点击可跳转到 LogsExplorer：

| 显示内容 | 含义 | 数据来源 |
|---------|------|---------|
| **服务名 + 边方向徽章** | source/target/correlated | 后端日志匹配结果中的 `edge_side` 字段 |
| **匹配类型徽章** | request_id/trace_id/message_target/time_window | 后端日志匹配结果中的 `match_kind` 字段 |
| **关联精度徽章** | trace_id/request_id 匹配精度 | 与 edge context 的 trace_ids/request_ids 交叉比对 |
| **级别徽章** | 日志级别 | 红色(ERROR/FATAL)、琥珀(WARN)、灰色(其他) |
| **时间戳** | 日志时间 | `log.timestamp`（ClickHouse 原生，微秒精度） |
| **message（高亮）** | 带高亮的日志内容 | source_service 和 target_service 名称用 cyan 高亮 |
| **关联类型** | seed/expanded/candidate | 后端日志扩展时的分类 |
| **匹配描述** | 匹配原因的简短文字 | 后端返回 |

### B.12 完整数据溯源：从 ClickHouse 到页面

```
ClickHouse 原始字段                    → 后端处理                              → 前端展示
─────────────────────────────────────────────────────────────────────────────────────

logs.logs.service_name                → node.metrics.service_name               → resolveServiceName(node)
logs.logs.namespace                   → node.namespace                          → resolveNamespace(node)
logs.logs.timestamp                   → node.metrics.last_seen                  → topologyFreshness
COUNT(*)                             → node.metrics.log_count                   → 节点卡片 "log N"
SUM(error)                           → node.metrics.error_count                 → 节点卡片 "err N"
error_count / log_count              → node.metrics.error_rate                  → 节点状态判定
log_count / window_seconds           → node.metrics.rps                         → (未直接在节点展示)

logs.traces.service_name             → node (from traces)                      → 合并到同节点
logs.traces.parent_span_id           → edge (from traces)                      → observed edge
logs.traces.duration_ms              → edge.metrics.p95/p99                    → 边指标

M2 推理 (request_id/trace_id/mt)     → edge (from logs inference)              → inferred edge
  accumulated count                  → edge.metrics.call_count                  → "RPS(近似)"
  weighted_score                     → edge.metrics.confidence                  → confidence_threshold 过滤
  evidence_chain                     → edge.metrics.evidence_chain              → (debug only)

confidence_calculator                → edge.metrics.confidence (recalculated)  → 置信度
  time_decay × error_penalty + boost

topology_contract                    → node.node_key / edge.edge_key           → 稳定标识
  coverage                           → node.coverage                           → "cov N%"
  quality_score                      → node/edge.quality_score                 → "质量分"
  evidence_type                      → node/edge.evidence_type                 → "observed"/"inferred"
  protocol                           → edge.metrics.protocol                   → 链路描述
  endpoint_pattern                   → edge.metrics.endpoint_pattern           → 链路描述

_apply_edge_red_aggregation          → edge.metrics.red_{rate,error,duration}  → RED 指标面板

build_topology() metadata
  generated_at (now())               → metadata.generated_at                   → anchor_time / 锚点 / 新鲜度
  time_window                        → metadata.time_window                    → 时间窗参数
  inference_quality.*                → metadata.inference_quality               → 推断统计 / 状态栏
  issue_summary                      → metadata.issue_summary                   → 问题节点/链路统计
  source_breakdown                   → metadata.source_breakdown               → (debug only)
  avg_confidence                     → metadata.avg_confidence                 → (debug only)
```

### B.13 状态汇总

| 字段 | 后端来源 | 前端类型 | 说明 |
|------|---------|---------|------|
| `id` | `_build_node_id()` / `_build_edge_id()` | `string` | 拓扑范围内唯一标识 |
| `node_key` | `topology_contract.build_node_key()` | `string` | 全局唯一节点主键 `ns:name:env` |
| `edge_key` | `topology_contract.build_edge_key()` | `string` | 全局唯一边主键 |
| `label` | `service_name` / `operation_name` | `string` | 显示名称 |
| `type` | 硬编码 `"service"` / `"database"` 等 | `string` | 节点/边类型 |
| `metrics.log_count` | `COUNT(*)` | `number` | 窗口内总日志数 |
| `metrics.error_count` | `SUM(CASE error)` | `number` | 窗口内错误日志数 |
| `metrics.error_rate` | `error_count / log_count` | `number` | 错误率 (0-1) |
| `metrics.rps` | `log_count / window_seconds` | `number` | 近似每秒请求数 |
| `metrics.p95` / `metrics.p99` | `quantile(duration)` | `number` | 延迟分位值 (ms) |
| `metrics.timeout_rate` | 超时占比 | `number` | 超时率 (0-1) |
| `metrics.confidence` | `ConfidenceCalculator` 计算 | `number` | 置信度 (0-1) |
| `metrics.data_source` | `"traces"/"logs"/"metrics"/"inferred"` | `string` | 主要数据来源 |
| `metrics.data_sources` | `["traces", "logs", ...]` | `string[]` | 所有数据来源 |
| `metrics.evidence_type` | `apply_*_contract()` | `string` | `"observed"` / `"inferred"` |
| `metrics.evidence_chain` | 推理累积 | `object[]` | 推理证据链 (最多 8 条) |
| `metrics.inference_method` | 主导推理方法 | `string` | `"request_id"/"trace_id"/"message_target"/"time_window"` |
| `metrics.reason` | 推理原因标签 | `string` | 如 `"trace_id_correlation"` |
| `coverage` | `apply_*_contract()` | `number` | 数据源覆盖度 (0-1) |
| `quality_score` | `apply_*_contract()` | `number` | 综合质量分 (0-100) |
| `evidence_type` | `apply_*_contract()` | `string` | `"observed"` / `"inferred"` |
| `problem_summary` | 后端注入（可选） | `object` | 预计算的问题摘要 |
| `metadata.generated_at` | `datetime.now(utc).isoformat()` | `string` | 快照生成时间 |
| `metadata.time_window` | 实际使用的窗口 | `string` | 如 `"15 MINUTE"` |
| `metadata.inference_quality` | 推理统计 | `object` | 各方法边数/推断率/假阳性率等 |
| `metadata.issue_summary` | 问题汇总 | `object` | unhealthy_nodes, unhealthy_edges, risk 分布 |
