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
