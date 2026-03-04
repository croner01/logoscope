# Logoscope 数据流架构

> 版本: v3.21.0
> 更新: 2026-02-11

## 📊 数据流概览

```mermaid
graph TB
    subgraph "数据采集层"
        FB[Fluent Bit DS]
        OC[OTel Collector Agent]
    end

    subgraph "数据处理层"
        GW[OTel Collector Gateway]
        SE[Semantic Engine API]
        SW[Semantic Engine Worker]
    end

    subgraph "存储层"
        CH[(ClickHouse)<br/>日志/追踪/指标]
        NJ[(Neo4j)<br/>服务图/依赖]
        RD[(Redis Stream)<br/>事件总线]
    end

    subgraph "前端层"
        FE[Frontend<br/>可视化界面]

    FB --> OC
    OC --> GW
    GW --> SE
    SE --> SW
    SW --> CH
    SW --> NJ
    SE --> RD
    SW --> RD

    RD -.->|实时查询|WebSocket| FE
    NJ -.->|拓扑查询|REST API| FE
    CH -.->|日志查询|REST API| FE
```

---

## 🔄 详细数据流

### 1. 日志采集阶段

```
┌─────────────────────────────────────────────────────┐
│  Kubernetes Pod                               │
│  ┌──────────────────────────────────────────────┐ │
│  │  应用容器 (Python/Go/Nginx等)    │ │
│  │                                        │ │
│  │  Fluent Bit DaemonSet               │ │
│  │  - 读取 /var/log/containers/*/*.log │ │
│  │  - 添加 Kubernetes metadata      │ │
│  │  - 输出 OTLP 格式               │ │
│  └───────────────────────────────────────┘ │
└─────────────────────────────────────────────────────┘
                 │
                 ▼
        ┌────────────────────────────────────────────────┐
        │  OTel Collector Agent (DaemonSet)    │
        │  - 接收 Fluent Bit OTLP 数据           │
        │  - 提取 Kubernetes metadata          │
        │  - 添加 service_name 识别             │
        │  - 转发到 Gateway                  │
        └──────────────────────────────────────────┘
                 │
                 ▼
```

**关键配置**:
- `FLUENT_BIT_HOST`: fluent-bit.logoscope.svc
- `OTEL_COLLECTOR_HOST`: otel-collector.logoscope.svc
- `OTEL_GATEWAY_HOST`: otel-gateway.logoscope.svc

### 2. 数据汇聚阶段

```
┌─────────────────────────────────────────────────────────┐
│  OTel Collector Gateway (Deployment)            │
│  ┌───────────────────────────────────────────────┐ │
│  │  Pipeline: gRPC (4317)              │ │
│  │                                        │ │
│  │  Receivers:                            │ │
│  │  ├── otlp (4317)                   │ │
│  │  └── otlp-http (4318)            │ │
│  │                                        │ │
│  │  Processors:                          │ │
│  │  ├── batch (批次: 1000)             │ │
│  │  ├── memory_limiter (512MB)          │ │
│  │  └── attributes (添加元数据)       │ │
│  │                                        │ │
│  │  Exporters:                            │ │
│  │  ├── otlphttp/logs (→ Semantic Engine)│
│  │  ├── otlphttp/traces (→ Semantic Engine)│
│  │  └── otlphttp/metrics (→ Semantic Engine)│
│  │                                        │ │
│  │  Purpose:                             │ │
│  │  - 接收 Agent 数据                   │ │
│  │  - 批量处理 (减少请求)              │ │
│  │  - 负载均衡                      │ │
│  │  - 路由到后端                      │ │
│  └──────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────┘
                 │
                 ▼
        ┌────────────────────────────────────────────────┐
        │  Semantic Engine API (Deployment)     │
        │  - POST /v1/logs                   │ │
        │  - POST /v1/traces                  │ │
        │  - POST /v1/metrics                 │ │
        │  - GET /health                         │ │
        │  - 批量写入 Redis Stream             │ │
        │  - WebSocket 推送                   │ │
        └──────────────────────────────────────────┘
                 │
                 ▼
```

**关键配置**:
- `GATEWAY_BATCH_SIZE`: 1000 (避免 gRPC 消息超限)
- `GATEWAY_TIMEOUT`: 30s
- `MEMORY_LIMIT`: 512MB

### 3. 数据处理阶段

```
┌─────────────────────────────────────────────────────────┐
│  Redis Stream (事件总线)                 │
│  ┌──────────────────────────────────────────────┐ │
│  │  Stream: otel_logs                   │ │
│  │  Consumer Group: semantic-workers      │ │
│  │                                        │ │
│  │  Semantic Engine Worker (Replica: 3)   │
│  │  ┌────────────────────────────────────┐ │ │
│  │  │  1. Pop OTLP logs       │ │
│  │  │  - 解析 protobuf           │ │
│  │  │  - 提取 K8s metadata      │ │
│  │  │  - 识别 service_name       │ │
│  │  │  - 分类日志级别          │ │
│  │  └─────────────────────────────┘ │ │
│  │                                        │ │
│  │  2. 标准化日志             │ │
│  │  │  - 去重                 │ │
│  │  │  - 结构化               │ │
│  │  │  - 关联 trace_id         │ │
│  │  └─────────────────────────────┘ │ │
│  │                                        │ │
│  │  3. 写入 ClickHouse        │ │
│  │  └─────────────────────────────┐ │ │
│  │  - logs.logs 表            │ │
│  │  - traces 表              │ │
│  │  - metrics 表             │ │
│  │  - topology_snapshots 表   │ │
│  └─────────────────────────────┘ │ │
│  │                                        │ │
│  │  4. 构建服务拓扑       │ │
│  │  └─────────────────────────────┐ │ │
│  │  - 写入 Neo4j 图数据库   │ │
│  │  - 服务节点               │ │
│  │  - 调用关系               │ │
│  │  - 依赖层级               │ │
│  └─────────────────────────────┘ │ │
└─────────────────────────────────────────────────────┘
```

**关键特性**:
- **异步处理**: 非阻塞消费，提高吞吐量
- **批量写入**: 批量插入 ClickHouse，提升性能
- **自动重试**: 失败自动重试，保证数据完整性

### 4. 数据存储阶段

```
┌───────────────────────────────────────────────────────────────────────────┐
│                    ClickHouse (时序数据库)              │
│  ┌────────────────────────────────────────────────────────┐   │
│  │  Database: logs                        │   │
│  │  ├── logs.logs (1.1M+ 记录)           │   │
│  │  │   - 字段:                        │   │
│  │  │   - id, timestamp, service_name, pod_name │   │
│  │  │   - namespace, level, message, trace_id    │   │
│  │  │   - span_id, attributes_json            │   │
│  │  │   - 分区: 按日期 (toDay())    │   │
│  │  │   - 索引: service_name, timestamp     │   │
│  │  └───────────────────────────────────────────┘   │
│  │                                        │   │
│  ├── traces (38K+ spans)                   │   │
│  │   - 字段: trace_id, span_id, parent_span_id│   │
│  │   - service_name, operation_name, duration_ms  │   │
│  │   - start_time, status, tags          │   │
│  │   - 分区: 按日期                    │   │
│  │                                        │   │
│  ├── metrics (指标数据)                   │   │
│  │   - 字段: service_name, metric_name, value  │   │
│  │   - timestamp, labels                   │   │
│  │                                        │   │
│  └── topology_snapshots (快照)            │   │
│  │   - 拓扑快照，支持历史对比       │   │
│  └───────────────────────────────────────────────────┘   │
│                                                 │
└─────────────────────────────────────────────────────────────────┘
                 │
                 ▼
        ┌────────────────────────────────────────────────────────┐
        │  Neo4j (图数据库)                      │
│  ┌──────────────────────────────────────────────────┐  │
│  │  Database: neo4j.service              │   │
│  │  ├── 节点 (Service)                     │   │
│  │  │   - name, type, namespace          │   │
│  │  │   - labels, metrics, properties       │   │
│  │  ├── 关系 (CALLS)                     │   │
│  │  │   - from_service, to_service        │   │
│  │  │   - confidence, call_count            │   │
│  │  └── 元数据 (TopologyMetadata)         │   │
│  │  - snapshot_id, created_at, node_count   │   │
│  └──────────────────────────────────────────────┘ │
│                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**关键特性**:
- **时序优化**: 按时间分区，提升查询性能
- **图查询**: Neo4j Cypher 查询语言
- **快照管理**: 支持历史拓扑对比

### 5. 前端展示阶段

```
┌─────────────────────────────────────────────────────────────────┐
│  Frontend (Dashboard)                       │
│  ┌───────────────────────────────────────────────────┐ │
│  │  Data Sources:                           │ │
│  │  ├── REST API (/api/v1/*)                │ │
│  │  │   - 查询日志: /logs              │ │
│  │  │   - 查询追踪: /traces            │ │
│  │  │   - 查询拓扑: /topology/hybrid    │ │
│  │  │   - AI 分析: /ai/analyze-log     │ │
│  │  └───────────────────────────────────────────┘ │ │
│  │                                        │ │
│  ├── WebSocket 实时更新                   │ │
│  │  │  - ws://.../topology/subscribe       │ │
│  │  │  - 实时推送拓扑变化               │ │
│  │  │  - 心跳保活                       │ │
│  │  └───────────────────────────────────────────┘ │ │
│  │                                        │ │
│  └── 可视化组件                           │ │
│  │  - G6 图渲染 (服务拓扑)            │ │
│  │  - ECharts 图表 (指标趋势)         │ │
│  │  - 日志表格 (搜索+详情)          │ │
│  │  - 追踪瀑布图 (Span 时间线)       │ │
│  └──────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

---

## 🚀 性能指标

### 吞吐量

| 组件 | 吞吐量 | 说明 |
|------|--------|------|
| Fluent Bit | 10K+ events/sec | 日志采集速度 |
| OTel Gateway | 50K+ spans/sec | 数据处理速度 |
| Semantic Engine | 10K+ logs/sec | 日志处理速度 |
| ClickHouse 写入 | 100K+ rows/sec | 批量写入性能 |

### 延迟

| 路径 | 延迟 | 说明 |
|------|------|------|
| 日志采集 → API | < 100ms | 实时采集 |
| API → Redis | < 50ms | 异步处理 |
| Redis → Worker | < 100ms | 流式消费 |
| Worker → ClickHouse | < 500ms | 批量写入 |
| ClickHouse → Frontend | < 1s | API 查询 |

### 可靠性

- **采集可靠性**: 99.9%+ (DaemonSet 高可用)
- **处理可靠性**: 99.5%+ (自动重试 + 幂等性)
- **存储可靠性**: 99.99% (ClickHouse 副本)
- **整体 SLA**: 99.5%+

---

## 🔐 数据格式

### OTLP 日志格式

```protobuf
LogRecord {
  string time_unix_nano;
  string severity_number;
  string severity_text;
  string body;
  Resource {
    string attributes;
  string dropped_attributes_count;
    KeyValueList {
      repeated KeyValue values;
    }
  }
  ScopeLogs {
    repeated ScopeLog log_records = 1;
    Scope {
      string attributes;
      string dropped_attributes_count;
      KeyValueList {
        repeated KeyValue values;
      }
    }
  }
}
```

### ClickHouse 表结构

```sql
-- logs.logs 表
CREATE TABLE logs.logs (
    id String,
    timestamp DateTime64(9),
    service_name String,
    pod_name String,
    namespace String,
    level String,
    message String,
    trace_id String,
    span_id String,
    attributes_json String
) ENGINE = MergeTree()
PARTITION BY toDay(timestamp)
ORDER BY timestamp;

-- traces 表
CREATE TABLE logs.traces (
    trace_id String,
    span_id String,
    parent_span_id String,
    service_name String,
    operation_name String,
    duration_ms UInt32,
    status String,
    start_time DateTime
) ENGINE = MergeTree()
PARTITION BY toDate(start_time)
ORDER BY start_time;
```

### Redis Stream 数据

```
Key: otel_logs
Type: stream
Length: 10000
Consumer Group: semantic-workers

Message Format (JSON):
{
  "resource_logs": [...],    # OTLP ResourceLogs
  "body": "...",            # 日志内容
  "severity_number": 9,     # ERROR 级别
  "k8s_metadata": {...},    # Kubernetes 元数据
  "timestamp": "..."        # 时间戳
}
```

---

## 📈 监控指标

### 关键指标

| 指标 | 说明 | 告警阈值 |
|------|------|--------|
| **采集速率** | Fluent Bit 输出 < 5K/sec | ⚠️ 可能卡顿 |
| **处理延迟** | OTel Gateway P99 > 1s | ⚠️ 处理瓶颈 |
| **错误率** | 任何服务 error_rate > 5% | 🚨 需要关注 |
| **存储空间** | ClickHouse 磁盘 > 80% | ⚠️ 需要扩容 |
| **内存使用** | Worker Pod > 2GB | 🚨 OOM 风险 |

### 健康检查端点

- `/health` - 组件健康状态
- `/metrics` - Prometheus 指标暴露
- `/readyz` - 就绪探针

---

## 🔒 安全考虑

### 数据安全

- TLS 加密传输 (OTLP over gRPC/HTTP)
- Redis 认证 (AUTH token)
- ClickHouse 用户权限控制
- API 访问控制 (CORS + Rate Limiting)

### 隔离

- 网络隔离 (不同 Namespace)
- 资源配额 (CPU/Memory 限制)
- 租户隔离 (Multi-tenancy)

---

**文档维护**: Semantic Engine Team
**最后更新**: 2026-02-11
