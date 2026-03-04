# Logoscope API 参考手册

> 版本: v4.1.0
> 更新: 2026-02-27

## 📚 API 概览

Logoscope 提供完整的 RESTful API 和 WebSocket 接口，包括：

- **Query Service**: 日志、指标、追踪查询
- **Topology Service**: 拓扑查询和管理
- **Semantic Engine**: AI 分析、缓存与去重统计
- **Ingest Service**: OTLP 数据摄入

---

## 🔗 服务端点

| 服务 | 地址 | 说明 |
|------|------|------|
| Query Service | http://query-service.islap.svc:8080 | 日志、事件、追踪查询 |
| Topology Service | http://topology-service.islap.svc:8080 | 拓扑查询和管理 |
| Semantic Engine | http://semantic-engine.islap.svc:8080 | AI 分析、缓存、告警、标签发现 |
| Ingest Service | http://ingest-service.islap.svc:8080 | OTLP 数据摄入 |

---

## 🧠 Semantic Engine API

### 健康检查

```bash
GET /health
```

**响应示例**:
```json
{
  "status": "healthy",
  "service": "semantic-engine",
  "version": "1.0.0"
}
```

### AI 日志分析（统一入口）

```bash
POST /api/v1/ai/analyze-log-llm
```

**请求示例**:
```json
{
  "log_content": "Database connection timeout",
  "service_name": "query-service",
  "context": {
    "trace_id": "trace-123"
  },
  "use_llm": true
}
```

### AI Trace 分析（规则）

```bash
POST /api/v1/ai/analyze-trace
```

**请求示例**:
```json
{
  "trace_id": "trace-123",
  "service_name": "query-service"
}
```

### AI Trace 分析（LLM）

```bash
POST /api/v1/ai/analyze-trace-llm
```

**请求示例**:
```json
{
  "trace_id": "trace-123",
  "service_name": "query-service"
}
```

**返回结构说明**:

AI 分析类接口统一返回以下字段：

- `overview`
- `rootCauses`
- `solutions`
- `similarCases`

说明：

- Trace 接口仅接受 `trace_id`，不再接受 `trace_data` 请求字段。

### 获取缓存统计

```bash
GET /api/v1/cache/stats
```

**响应示例**:
```json
{
  "total_entries": 128,
  "expired_entries": 4,
  "active_entries": 124
}
```

### 清除缓存（推荐）

```bash
DELETE /api/v1/cache?pattern=topology
```

**响应示例**:
```json
{
  "status": "ok",
  "cleared": 12,
  "pattern": "topology"
}
```

### 清除缓存（兼容路径）

```bash
POST /api/v1/cache/clear?pattern=topology
```

### 获取去重统计

```bash
GET /api/v1/deduplication/stats
```

**响应示例**:
```json
{
  "total_processed": 50000,
  "duplicates_found": 8500,
  "duplicates_by_id": 3000,
  "duplicates_by_semantic": 5500,
  "duplicate_rate": 0.17,
  "id_cache_size": 256,
  "semantic_cache_size": 1024,
  "cache_age_seconds": 42.5
}
```

### 清除去重缓存

```bash
POST /api/v1/deduplication/clear-cache
```

---

## 📋 Query Service API

### 根路径

```bash
GET /
```

**响应示例**:
```json
{
  "service": "query-service",
  "status": "ok",
  "version": "1.0.0"
}
```

### 健康检查

```bash
GET /health
```

**响应示例**:
```json
{
  "status": "healthy",
  "service": "query-service"
}
```

### 获取日志列表

```bash
GET /api/v1/logs?service_name=semantic-engine&level=error&limit=50
```

**查询参数**:

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `limit` | int | 100 | 返回数量限制 (1-10000) |
| `service_name` | string | - | 服务名过滤 |
| `level` | string | - | 日志级别过滤 |
| `start_time` | string | - | 起始时间 (ISO 8601) |
| `end_time` | string | - | 结束时间 (ISO 8601) |
| `exclude_health_check` | boolean | false | 过滤健康检查日志 |
| `search` | string | - | 搜索关键词 |

**响应示例**:
```json
{
  "data": [
    {
      "id": "b7c8f9e2-...",
      "timestamp": "2026-02-26 10:30:45.123",
      "service_name": "semantic-engine",
      "pod_name": "semantic-engine-7d6f8c9d-abc12",
      "namespace": "islap",
      "level": "error",
      "message": "Failed to connect to ClickHouse",
      "trace_id": "4bf8c9e2...",
      "span_id": "a1b2d3f..."
    }
  ],
  "count": 100,
  "limit": 100
}
```

### 日志聚合查询

```bash
GET /api/v1/logs/aggregated?service_name=frontend&limit=500
```

**查询参数**:

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `limit` | int | 500 | 查询日志数量 |
| `min_pattern_count` | int | 2 | 最小聚合数量 |
| `max_patterns` | int | 50 | 返回最大 pattern 数 |
| `max_samples` | int | 3 | 每个 pattern 保留示例数 |
| `service_name` | string | - | 服务名过滤 |
| `level` | string | - | 日志级别过滤 |
| `start_time` | string | - | 开始时间 |
| `end_time` | string | - | 结束时间 |
| `exclude_health_check` | boolean | true | 过滤健康检查日志 |
| `search` | string | - | 搜索关键词 |

### 获取日志统计

```bash
GET /api/v1/logs/stats
```

**响应示例**:
```json
{
  "total": 15234,
  "byService": {
    "semantic-engine": 5234,
    "frontend": 3210,
    "backend": 4523
  },
  "byLevel": {
    "INFO": 10234,
    "WARN": 3210,
    "ERROR": 1790
  }
}
```

### 获取日志详情

```bash
GET /api/v1/logs/{log_id}
```

### 获取日志上下文

```bash
# 通过 trace_id 查询
GET /api/v1/logs/context?trace_id=4bf8c9e2...

# 通过 pod_name + timestamp 查询
GET /api/v1/logs/context?pod_name=semantic-engine-abc12&timestamp=2026-02-26T10:30:45Z&before_count=10&after_count=10
```

**查询参数**:

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `trace_id` | string | - | 追踪ID（模式1） |
| `pod_name` | string | - | Pod名称（模式2） |
| `timestamp` | string | - | 时间戳（模式2） |
| `before_count` | int | 5 | 当前日志之前的条数 |
| `after_count` | int | 5 | 当前日志之后的条数 |
| `limit` | int | 100 | 返回数量限制 |

### 查询指标数据

```bash
GET /api/v1/metrics?service_name=frontend&limit=100
```

**查询参数**:

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `limit` | int | 100 | 返回数量限制 |
| `service_name` | string | - | 服务名过滤 |
| `metric_name` | string | - | 指标名过滤 |
| `start_time` | string | - | 开始时间 |
| `end_time` | string | - | 结束时间 |

### 获取指标统计

```bash
GET /api/v1/metrics/stats
```

### 查询追踪数据

```bash
GET /api/v1/traces?service_name=frontend&limit=100
```

**查询参数**:

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `limit` | int | 100 | 返回数量限制 |
| `service_name` | string | - | 服务名过滤 |
| `trace_id` | string | - | Trace ID 过滤 |
| `start_time` | string | - | 开始时间 |
| `end_time` | string | - | 结束时间 |

### 获取追踪统计

```bash
GET /api/v1/traces/stats
```

### WebSocket 实时日志流

```javascript
const ws = new WebSocket('ws://query-service.islap.svc:8080/ws/logs');

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log('New log:', data);
};
```

### WebSocket 实时拓扑更新

```javascript
// 拓扑 WS 请连接 topology-service，而不是 query-service
const ws = new WebSocket('ws://topology-service.islap.svc:8080/ws/topology');
```

### WebSocket 状态查询

```bash
GET /ws/status
```

---

## 🔗 Topology Service API

> 路径标准清单请优先参考: [topology.md](./topology.md)

### 根路径

```bash
GET /
```

**响应示例**:
```json
{
  "service": "topology-service",
  "status": "ok",
  "version": "1.0.0"
}
```

### 健康检查

```bash
GET /health
```

**响应示例**:
```json
{
  "status": "healthy",
  "service": "topology-service"
}
```

### 获取混合拓扑（推荐）

```bash
GET /api/v1/topology/hybrid?time_window=1%20HOUR&confidence_threshold=0.4
```

**查询参数**:

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `time_window` | string | "1 HOUR" | 时间窗口 |
| `namespace` | string | - | 命名空间过滤 |
| `confidence_threshold` | float | 0.3 | 置信度阈值 (0.0-1.0) |

**响应示例**:
```json
{
  "nodes": [
    {
      "id": "frontend",
      "label": "Frontend Service",
      "type": "service",
      "name": "frontend",
      "metrics": {
        "log_count": 15234,
        "trace_count": 234,
        "data_source": "traces",
        "confidence": 1.0
      }
    }
  ],
  "edges": [
    {
      "id": "frontend-backend",
      "source": "frontend",
      "target": "backend",
      "label": "calls",
      "type": "calls",
      "metrics": {
        "call_count": 1523,
        "data_sources": ["traces"],
        "confidence": 1.0
      }
    }
  ],
  "metadata": {
    "data_sources": ["traces", "logs", "metrics"],
    "time_window": "1 HOUR",
    "node_count": 8,
    "edge_count": 12,
    "generated_at": "2026-02-26T12:00:00Z"
  }
}
```

### 获取增强拓扑

```bash
GET /api/v1/topology/enhanced?time_window=1%20HOUR
```

### 获取拓扑统计

```bash
GET /api/v1/topology/stats?time_window=1%20HOUR
```

### 获取监控拓扑

```bash
GET /api/v1/monitor/topology?time_window=1%20HOUR
```

### WebSocket 实时拓扑更新

```javascript
const ws = new WebSocket('ws://topology-service.islap.svc:8080/ws/topology');

ws.onopen = () => {
  console.log('WebSocket connected');
};

ws.onmessage = (event) => {
  const message = JSON.parse(event.data);
  
  switch (message.type) {
    case 'topology_update':
      console.log('Topology updated:', message.data);
      break;
    case 'heartbeat':
      break;
    case 'pong':
      break;
  }
};

// 发送消息
ws.send(JSON.stringify({ action: 'ping' }));
ws.send(JSON.stringify({ action: 'get' }));
ws.send(JSON.stringify({ 
  action: 'subscribe', 
  params: { time_window: '30 MINUTE' } 
}));
```

### WebSocket 状态查询

```bash
GET /ws/status
```

### 拓扑订阅式 WebSocket（兼容）

```javascript
const ws = new WebSocket('ws://topology-service.islap.svc:8080/api/v1/topology/subscribe');
```

---

## 📥 Ingest Service API

### 根路径

```bash
GET /
```

**响应示例**:
```json
{
  "service": "Ingest Service",
  "version": "1.0.0",
  "description": "Logoscope OTLP 数据摄入服务",
  "mode": "normal",
  "features": {
    "lazy_redis_connection": true,
    "memory_queue_fallback": true,
    "auto_reconnect": true
  }
}
```

### 健康检查

```bash
GET /health
```

**响应示例**:
```json
{
  "status": "healthy",
  "service": "ingest-service",
  "version": "1.0.0",
  "mode": "normal",
  "redis_connected": true,
  "memory_queue": {
    "size": 0,
    "max_size": 1000,
    "dropped": 0
  },
  "stats": {
    "total_written": 15234,
    "redis_written": 15234,
    "memory_queued": 0,
    "reconnect_attempts": 0
  },
  "timestamp": "2026-02-26T12:00:00Z"
}
```

### 就绪检查

```bash
GET /ready
```

### 接收 OTLP 日志数据

```bash
POST /v1/logs
Content-Type: application/json

{
  "resourceLogs": [
    {
      "resource": {
        "attributes": [
          { "key": "service.name", "value": { "stringValue": "my-service" } }
        ]
      },
      "scopeLogs": [
        {
          "logRecords": [
            {
              "timeUnixNano": "1234567890000000000",
              "body": { "stringValue": "Application started" },
              "severityNumber": 9,
              "severityText": "INFO"
            }
          ]
        }
      ]
    }
  ]
}
```

**支持格式**: JSON 和 Protobuf

### 接收 OTLP 指标数据

```bash
POST /v1/metrics
Content-Type: application/json
```

### 接收 OTLP 追踪数据

```bash
POST /v1/traces
Content-Type: application/json
```

---

## 📈 错误码参考

| 错误码 | HTTP 状态 | 说明 |
|--------|----------|------|
| `INVALID_PARAMETER` | 400 | 参数验证失败 |
| `UNAUTHORIZED` | 401 | 未授权 |
| `FORBIDDEN` | 403 | 禁止访问 |
| `NOT_FOUND` | 404 | 资源不存在 |
| `INTERNAL_ERROR` | 500 | 内部服务器错误 |
| `SERVICE_UNAVAILABLE` | 503 | 服务不可用 |

---

**文档维护**: Logoscope Team
**最后更新**: 2026-02-26
