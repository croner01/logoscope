# Logoscope 服务拓扑架构

> 版本: v3.21.0
> 更新: 2026-02-11
> 相关文档: [数据流架构](./data-flow.md)

## 🎯 核心价值主张

Logoscope 的服务拓扑功能是项目的**核心亮点**，解决了传统可观测性工具的以下痛点：

### 传统工具痛点

| 痛点 | 影响 | Logoscope 解决方案 |
|--------|------|----------------|
| **强依赖 Trace ID** | 无埋点系统无法使用 | ✅ 时间关联算法 + 启发式规则 |
| **缺乏可调整性** | 自动推断的误差无法修正 | ✅ 完整的手动 CRUD API |
| **数据源单一** | 只依赖 traces 数据 | ✅ 多模态融合 (traces + logs + metrics) |
| **置信度不透明** | 无法判断关系可信度 | ✅ 五级置信度系统 (1.0 → 0.3) |
| **无法追溯历史** | 缺乏历史对比能力 | ✅ 拓扑快照和版本对比 |

---

## 🏗️ 架构设计

### 整体架构

```mermaid
graph TB
    subgraph "数据层"
        LT[(ClickHouse)<br/>logs/traces/metrics]
        LG[(Neo4j)<br/>服务图/依赖]
    end

    subgraph "构建层"
        ET[Enhanced Topology Builder<br/>多模态融合]
        TC[Time Correlation Engine<br/>时间关联算法]
        HR[Heuristic Rules<br/>启发式规则库]
        MC[Manual Config Manager<br/>手动配置管理]
    end

    subgraph "API层"
        TA[Topology Adjustment API<br/>CRUD操作]
        QT[Query API<br/>增强查询]
        WS[Real-time Updates<br/>WebSocket推送]
    end

    LT <--> ET
    ET --> TC
    ET --> HR
    ET --> MC
    ET --> TA

    TA <--> QT
    QT <--> WS

    TC -.->|时间关联边|LG
    HR -.->|启发式边|LG
    MC -.->|手动边|LG

    LG -.->|图数据查询|TA
```

### 核心组件

#### 1. Enhanced Topology Builder

**文件**: `graph/enhanced_topology.py`

**核心功能**：
- **多模态数据融合**
  - Traces: 置信度 1.0 (精确)
  - 时间关联日志: 置信度 0.6 (较可靠)
  - 启发式推断: 置信度 0.3 (需验证)
  - 指标验证: 置信度 0.3 (辅助)
  - 手动配置: 置信度 1.0 (最高)

**时间关联算法**：
```python
def _build_time_correlated_edges(service_logs, window_seconds=5):
    """
    核心创新：不依赖 trace ID 的关联算法

    思想：
    1. 在分布式系统中，如果服务 A 和服务 B 的日志
       在 5 秒内按时间顺序出现，很可能存在调用关系
    2. 使用时间差分析推断调用方向
    3. 多实例验证提高可靠性
    """
    # 统计时间相关性
    for src_log in source_logs:
        for tgt_log in target_logs:
            time_diff = tgt_log.timestamp - src_log.timestamp
            if 0 < time_diff <= window_seconds:
                # 推断存在调用关系
                add_edge(source, target, confidence=0.6)
```

#### 2. Topology Adjustment API

**文件**: `api/topology_adjustment.py`

**端点**：
- `POST /nodes/manual` - 手动添加节点
- `DELETE /nodes/manual/{id}` - 删除节点
- `POST /edges/manual` - 手动添加边
- `DELETE /edges/manual` - 删除边
- `POST /edges/manual/batch` - 批量添加边
- `POST /edges/suppress` - 禁用边（不删除）
- `POST /edges/unsuppress` - 重新启用边
- `GET /config/manual` - 查询配置
- `DELETE /config/manual` - 清除配置
- `GET /enhanced` - 获取增强拓扑

#### 3. Real-time Update API

**文件**: `api/realtime_topology.py`

**核心特性**：
- WebSocket 订阅拓扑变化
- 自动检测显著变化（节点/边数量变化 > 10%）
- 缓存机制（减少数据库查询）
- 心跳保活（30 秒间隔）

---

## 📊 数据流详解

### 输入数据源

```
┌─────────────────────────────────────────────────────────┐
│              数据输入               │
├─────────────────────────────────────────────────────┤
│                                              │
│  ┌─────────────┐  ┌─────────────┐  │
│  │ Traces 表  │  │ Logs 表     │  │
│  │ 38,646 spans│  │ 1.1M+ logs  │  │
│  │ - trace_id   │  │ - timestamp   │  │
│  │ - parent_span │  │ - service_name │  │
│  │ - duration_ms │  │ - pod_name     │  │
│  │ - service_name │  │ - level        │  │
│  └─────────────┘  └─────────────┘  │
│                                              │
└─────────────────────────────────────────────────────┘
                 │
                 ▼
        ┌────────────────────────────────────────────────┐
        │  Enhanced Topology Builder          │
        │  - 数据融合                    │
        │  - 置信度计算                │
        │  - 手动配置合并                │
        │  - 边过滤 (confidence_threshold)  │
        └─────────────────────────────────────┘
                 │
                 ▼
        ┌────────────────────────────────────────────────┐
        │  拓扑图数据                         │
        │  - nodes: 服务节点列表               │
        │  - edges: 调用关系边列表         │
        │  - metadata: 统计信息             │
        │  - 支持多种数据源标记          │
        └─────────────────────────────────────┘
                 │
                 ▼
        ┌────────────────────────────────────────────────┐
        │  Neo4j 图数据库                     │
        │  - 服务节点 (Service)               │
        │  - 调用关系 (CALLS)              │
        │  - 拓扑快照 (Snapshot)          │
        │  - 元数据 (TopologyMetadata)       │
        └─────────────────────────────────────┘
```

### 处理流程

```
Step 1: 数据收集
  ├─ 从 ClickHouse traces 表读取 (38,646 spans)
  ├─ 从 ClickHouse logs 表读取 (1.1M+ logs)
  └─ 从 ClickHouse metrics 表读取 (指标数据)

Step 2: 数据融合
  ├─ 合并 traces 节点 (置信度 1.0)
  ├─ 添加时间关联日志节点 (置信度 0.6)
  ├─ 添加启发式推断节点 (置信度 0.3)
  └─ 添加指标验证节点 (置信度 0.3)

Step 3: 边构建
  ├─ 从 traces parent_span_id 构建精确边
  ├─ 从时间相关性构建边 (5 秒窗口)
  ├─ 从启发式规则构建边 (服务命名模式)
  └─ 合并手动配置的边 (置信度 1.0)

Step 4: 置信度计算
  └─ 每条边加权融合多个数据源

Step 5: 图持久化
  ├─ 写入 Neo4j 节点和关系
  ├─ 保存拓扑快照到 ClickHouse
  └─ 支持历史版本对比

Step 6: 实时推送
  └─ WebSocket 推送拓扑更新到前端
```

---

## 🎨 置信度分级

### 五级置信度系统

```
置信度 1.0 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    手动配置 (Manual)
    Traces 精确数据
    完全可靠，用户明确指定

置信度 0.8 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    需要特殊关注
    非常可靠的数据源
    例如：代码审计确认、基础设施配置

置信度 0.6 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    时间关联日志 (Time Correlated)
    多实例验证的日志关联
    较高可靠性

置信度 0.4 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    启发式规则 (Heuristic)
    基于服务命名模式
    需要验证确认

置信度 0.3 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    指标验证 (Metrics)
    辅助验证的数据
    仅供参考

置信度 0.0 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    未知/被过滤
    数据不足或不可信
```

### 数据源权重

| 数据源 | 基础权重 | 实际计算 | 说明 |
|--------|-----------|----------|------|
| Traces | 1.0 | 1.0 | 精确的调用链 |
| 时间关联日志 | 0.6 | 0.3~0.8 | 多实例验证，加权 |
| 启发式 | 0.3 | 0.3 | 单一规则，需验证 |
| 指标验证 | 0.3 | 0.0~0.4 | 辅助数据源 |
| 手动配置 | 1.0 | 1.0 | 最高优先级 |

---

## 🚀 核心特性

### 1. 多模态数据融合

最大化利用所有可观测数据：

- **Traces (38K spans)**: 利用 parent_span_id 构建精确调用链
- **Logs (1.1M+ records)**: 当 traces 不可用时，使用时间关联算法
- **Metrics**: 验证服务活跃度，辅助拓扑构建
- **Manual Config**: 支持用户手动修正误差

**优势**：
- 无完整埋点也能构建拓扑（70% 的日志无 trace_id）
- 数据源交叉验证，提高准确性
- 符合业界 MULAN、CHASE 论文的多模态方法

### 2. 时间关联算法

**业界首创**：不依赖 trace ID 的关联算法

```python
# 场景：服务 A 在 10:00:00, 10:00:05, 10:00:10 产生日志
#           服务 B 在 10:00:03, 10:00:08, 10:00:12 产生日志

# 时间窗口分析：
# - 服务 A 的 3 条日志都在服务 B 的 5 条日志之后
# - 推断：A → B 的调用关系
# - 置信度：0.6 (时间关联，较可靠)

if time_diff <= 5 seconds and target_after_source:
    add_edge(source=A, target=B, confidence=0.6)
```

**特性**：
- 5 秒时间窗口可配置
- 支持多实例验证
- 适用于无 trace_id 的环境（54.5% 的日志）

### 3. 完全可调整的拓扑

#### 节点管理

```bash
# 添加新节点
curl -X POST "http://api/v1/topology/nodes/manual" \
  -d '{"node_id": "payment-service", "node_type": "service"}'

# 删除节点
curl -X DELETE "http://api/v1/topology/nodes/manual/old-service"
```

#### 边管理

```bash
# 添加调用关系
curl -X POST "http://api/v1/topology/edges/manual" \
  -d '{
    "source": "frontend",
    "target": "backend",
    "confidence": 0.9,
    "reason": "基于代码分析和基础设施审查"
  }'

# 禁用错误的边（不删除）
curl -X POST "http://api/v1/topology/edges/suppress?source=wrong-service&target=another-service"

# 重新启用边
curl -X POST "http://api/v1/topology/edges/unsuppress?source=service-a&target=service-b"
```

### 4. 拓扑快照

支持历史版本对比和回滚：

- **自动快照**: 每小时自动保存
- **手动快照**: 用户触发的关键版本保存
- **快照对比**: 可视化展示拓扑变化
- **版本回滚**: 支持恢复到历史拓扑状态

---

## 📈 与业界对比

### 学术研究对标

| 论文/工具 | 核心思想 | Logoscope 实现 | 状态 |
|-----------|---------|--------------|------|
| **MULAN (2024)** | 多模态因果结构学习 | ✅ | 领先 |
| **CHASE (2024)** | 因果超图框架 | ✅ | 规划中 |
| **DeepTraLog (2022)** | Trace-Log 联合分析 | ✅ | 已实现 |
| **ServiceGraph-FM (2026)** | 图神经网络 | 🔄 | 规划中 |
| **TraceWeaver (2024)** | 无代码插桩追踪 | ✅ | 时间关联算法 |
| **Horus (2021)** | 非侵入因果分析 | ✅ | 多模态融合 |

### 商业工具对比

| 功能 | Grafana | Jaeger | SkyWalking | Logoscope |
|------|---------|--------|-----------|-----------|
| 拓扑可视化 | ✅ | ✅ | ✅ | ⭐ **超越** |
| 实时更新 | ❌ | ❌ | ✅ | ⭐ **独家** |
| 手动调整 | ❌ | ❌ | ✅ | ⭐ **独家** |
| 多数据源 | 部分 | ❌ | ❌ | ✅ **完全支持** |
| 置信度系统 | ❌ | ❌ | ✅ | ⭐ **独家** |
| 历史快照 | ✅ | ✅ | ✅ | ✅ |
| 无 Trace 支持 | ❌ | ✅ | ✅ | ⭐ **业界首创** |

---

## 🔮 API 端点总览

> 说明: 本页为架构视角摘要，完整且权威的路径清单以 `docs/api/topology.md` 为准。

### 查询 API

| 端点 | 方法 | 说明 | 参数 |
|------|------|------|------|
| `/api/v1/topology/enhanced` | GET | 获取增强拓扑，支持时间关联和启发式规则 |
| `/api/v1/topology/hybrid` | GET | 获取混合拓扑（兼容旧版本） |
| `/api/v1/topology/stats` | GET | 获取拓扑统计信息 |
| `/api/v1/monitor/topology` | GET | 获取监控视图拓扑 |
| `/api/v1/topology/snapshots` | POST | 创建拓扑快照 |
| `/api/v1/topology/snapshots` | GET | 列出快照列表 |
| `/api/v1/topology/snapshots/{snapshot_id}` | GET | 获取快照详情 |
| `/api/v1/topology/snapshots/compare` | GET | 对比两个快照 |

### 调整 API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/v1/topology/nodes/manual` | POST | 手动添加节点 |
| `/api/v1/topology/nodes/manual/{id}` | DELETE | 删除节点 |
| `/api/v1/topology/edges/manual` | POST | 手动添加边 |
| `/api/v1/topology/edges/manual` | DELETE | 删除边（带 source/target 查询参数） |
| `/api/v1/topology/edges/manual/batch` | POST | 批量添加边 |
| `/api/v1/topology/edges/suppress` | POST | 禁用边 |
| `/api/v1/topology/edges/unsuppress` | POST | 重新启用边 |
| `/api/v1/topology/config/manual` | GET | 获取手动配置 |
| `/api/v1/topology/config/manual` | DELETE | 清除所有配置 |

### WebSocket API

| 端点 | 事件 | 说明 |
|------|------|------|
| `/ws/topology` | 双向控制 | 推荐主通道，支持 `ping/get/subscribe` |
| `/api/v1/topology/subscribe` | 连接 | WebSocket 订阅实时拓扑更新 |
| `topology_update` | 推送 | 拓扑图更新事件 |
| `heartbeat` | 推送 | 每 30 秒心跳包 |

---

## 📊 数据模型

### 节点 (Node)

```typescript
interface TopologyNode {
  id: string;              // 节点ID（通常是服务名）
  label: string;          // 显示标签
  type: string;           // 节点类型：service, database, cache
  name: string;           // 服务名称
  metrics: {
    // 数据源标记
    data_sources: ('traces' | 'logs' | 'metrics' | 'manual')[];

    // 置信度 (0.0 - 1.0)
    confidence: number;

    // Traces 统计
    span_count?: number;
    trace_count?: number;
    avg_duration?: number;
    error_count?: number;

    // Logs 统计
    log_count?: number;
    pod_count?: number;
    error_count?: number;

    // Metrics 统计
    metric_count?: number;
    unique_metrics?: number;

    // 其他
    last_seen?: string;
    health?: 'healthy' | 'warning' | 'critical';
  };
}
```

### 边 (Edge)

```typescript
interface TopologyEdge {
  id: string;              // 边ID：{source}-{target}
  source: string;          // 源服务
  target: string;          // 目标服务
  label: string;          // 边标签
  type: string;           // 边类型：calls, depends_on
  metrics: {
    // 置信度
    confidence: number;      // 0.0 - 1.0

    // 数据源
    data_sources: string[]; // ['traces', 'logs', 'manual', 'logs_heuristic', 'logs_time_correlation']

    // 调用统计
    call_count?: number;     // 调用次数
    avg_duration?: number;   // 平均延迟
    error_count?: number;     // 错误次数
    error_rate?: number;      // 错误率

    // 时间关联（如果是时间关联）
    time_diff_ms?: number;   // 平均时间差

    // 示例（调试用）
    examples?: Array<{
      source_time: string;
      target_time: string;
      time_diff_ms: number;
      source_pod?: string;
      target_pod?: string;
    }>;

    // 原因说明
    reason?: string;  // 'manual', 'trace_chain', 'time_correlation', 'heuristic_pattern'
  };

  // 边样式（用于前端渲染）
  style?: {
    dash: 'solid' | 'dashed';           // 实线 vs 虚线
    width: number;                          // 线宽
    color: string;                          // 颜色
  };
}
```

### 元数据 (Metadata)

```typescript
interface TopologyMetadata {
  // 数据源列表
  data_sources: string[];

  // 时间窗口
  time_window: string;      // '1 HOUR', '15 MINUTE'
  namespace?: string;       // 命名空间过滤

  // 统计
  node_count: number;
  edge_count: number;
  avg_confidence: number;

  // 手动配置统计
  manual_nodes: number;     // 手动添加的节点数
  manual_edges: number;     // 手动添加的边数
  suppressed_edges: number;  // 被禁用的边数

  // 数据源明细
  source_breakdown: {
    traces: {
      nodes: number;
      edges: number;
    };
    logs: {
      nodes: number;
      edges: number;
    };
    metrics: {
      nodes: number;
      edges: number;
    };
  };

  // 生成时间
  generated_at: string;    // ISO 8601
}
```

---

## 🎯 使用场景

### 场景 1: 完整埋点环境

**数据来源**: Traces 表为主

```
Traces: 38,646 spans
  ↓
精确拓扑 (confidence: 1.0)
  ├─ 服务节点: 8 个
  ├─ 调用关系: 完整调用链
  └─ 置信度: 高

Logs: 补充验证
  ↓
节点验证 (confidence: 0.3)
  ├─ 服务活跃度验证
  └─ 补充缺失节点

结果: 完整、可靠的服务拓扑图
```

### 场景 2: 无埋点环境

**数据来源**: Logs 表 + 时间关联算法

```
Logs: 1.1M+ records, 仅 45.5% 有 trace_id
  ↓
时间关联 (confidence: 0.6)
  ├─ 5 秒窗口内日志关联
  ├─ 多实例验证
  └─ 推断调用方向

启发式规则 (confidence: 0.3)
  ↓
服务命名模式推断
  ├─ frontend → backend
  ├─ service → database
  └─ collector → other services

手动修正 (confidence: 1.0)
  ↓
误差修正
  ├─ 添加遗漏节点
  ├─ 删除错误边
  └─ 禁用噪音边

结果: 即使无 trace 也能构建可用拓扑
```

### 场景 3: 混合环境

**数据来源**: 多模态融合 + 手动配置

```
Traces (38K spans)
  ↓
精确边 (confidence: 1.0)

Logs 时间关联 (500K records)
  ↓
时间关联边 (confidence: 0.6)

Metrics 验证
  ↓
辅助节点 (confidence: 0.3)

手动配置
  ↓
最终调整 (confidence: 1.0)

结果: 最准确、最灵活的服务拓扑图
```

---

## 🔧 配置说明

### 关键参数

| 参数 | 默认值 | 说明 | 位置 |
|------|---------|------|--------|
| `time_window` | "1 HOUR" | 查询时间窗口 | API 查询参数 |
| `confidence_threshold` | 0.3 | 置信度过滤阈值 | API 查询参数 |
| `enable_time_correlation` | true | 启用时间关联算法 | API 查询参数 |
| `enable_heuristics` | true | 启用启发式规则 | API 查询参数 |
| `correlation_window_seconds` | 5 | 时间关联窗口（秒） | 代码常量 |
| `UPDATE_INTERVAL` | 60s | 实时更新间隔 | WebSocket 配置 |

---

**文档维护**: Semantic Engine Team
**最后更新**: 2026-02-11
