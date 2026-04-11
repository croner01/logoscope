# 单条日志接入到前端查询的运行时全链路

> 版本: v3.25.0  
> 更新: 2026-04-01  
> 状态: 当前代码实现  
> 适用范围: 单条日志从接入、入队、标准化、落库到前端查询/实时展示  

## 为什么要单独写这份文档

现有 [data-flow.md](./data-flow.md) 包含 Redis Stream 和旧版 Semantic Engine API 路径，已经不再代表当前运行时。  
这份文档只以仓库代码为准，回答两个问题：

1. 一条日志实际上经过哪些服务、topic、表、接口。
2. 关键字段在每一层如何变化，哪里最容易丢失或变形。

## 结论先行

当前代码里的日志主链路不是 `Semantic Engine API -> Redis -> Worker`，而是：

```text
Fluent Bit / OTel Collector
  -> ingest-service POST /v1/logs
  -> Kafka topic: logs.raw
  -> semantic-engine-worker
  -> ClickHouse table: logs.logs
  -> query-service GET /api/v1/logs
  -> frontend LogsExplorer
```

实时日志也不是 `Kafka -> WebSocket -> 前端`，而是：

```text
ClickHouse logs.logs
  -> query-service /ws/logs 轮询推送
  -> frontend useRealtimeLogs()
```

并且实时流与普通查询不是同一套读取语义：

- `/api/v1/logs` 走完整日志查询服务，支持更多过滤条件与规范服务名逻辑
- `/ws/logs` 走 ClickHouse 轮询，只覆盖实时新增数据，不回放历史积压

## 组件边界

### 采集入口

- `ingest-service` 暴露 `/v1/logs`、`/v1/metrics`、`/v1/traces`
- 代码入口: [../../ingest-service/internal/ingest/http.go](../../ingest-service/internal/ingest/http.go)
- 它负责协议/格式适配，不负责最终存储

### 队列与异步处理

- 队列后端已经统一为 Kafka
- 默认日志 topic: `logs.raw`
- 配置入口: [../../ingest-service/internal/ingest/config.go](../../ingest-service/internal/ingest/config.go), [../../semantic-engine/config.py](../../semantic-engine/config.py)
- worker 入口: [../../semantic-engine/msgqueue/worker.py](../../semantic-engine/msgqueue/worker.py)

### 最终存储

- 日志最终落到 ClickHouse `logs.logs`
- 结构化语义事件可选写入语义存储，但前端日志查询主数据源是 `logs.logs`

### 查询与展示

- REST 查询入口: `GET /api/v1/logs`
- 实时代码入口: `/ws/logs`
- query-service 代码: [../../query-service/api/query_routes.py](../../query-service/api/query_routes.py), [../../query-service/api/query_logs_service.py](../../query-service/api/query_logs_service.py), [../../query-service/api/websocket.py](../../query-service/api/websocket.py)
- frontend 消费入口: [../../frontend/src/utils/api.ts](../../frontend/src/utils/api.ts), [../../frontend/src/hooks/useApi.ts](../../frontend/src/hooks/useApi.ts), [../../frontend/src/pages/LogsExplorer.tsx](../../frontend/src/pages/LogsExplorer.tsx)

## 全链路分层

### 1. 上游把日志送入 ingest-service

入口接口:

- `POST /v1/logs`

接入层做的事:

- 限制请求体大小
- 处理 `Content-Encoding`
- 自动识别 protobuf / JSON / binary
- 生成 `metadata`
- 调用 `QueueWriter.WriteToQueue(...)`

关键 metadata:

- `content_type`
- `content_encoding`
- `parsed_format`
- `auto_gzip_magic`
- `_parsed_payload`

代码位置:

- [../../ingest-service/internal/ingest/http.go](../../ingest-service/internal/ingest/http.go)

### 2. ingest-service 把原始请求转换成 Kafka 可消费的日志记录

日志不是原样把整个 HTTP body 扔进 Kafka。  
它先被拆成标准化的 record，再被包装成 batched envelope。

核心代码:

- [../../ingest-service/internal/ingest/queue.go](../../ingest-service/internal/ingest/queue.go)
- [../../ingest-service/internal/ingest/transform.go](../../ingest-service/internal/ingest/transform.go)

Kafka 中日志 envelope 形状:

```json
{
  "signal_type": "logs",
  "batched": true,
  "record_count": 2,
  "records": [
    {
      "log": "GET /health 200",
      "timestamp": "1711946558123456789",
      "severity": "INFO",
      "service.name": "query-service",
      "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
      "span_id": "00f067aa0ba902b7",
      "flags": 1,
      "trace_id_source": "otlp",
      "attributes": {
        "log_meta": {
          "line_count": 1,
          "wrapped": false,
          "merged": false,
          "ingest_format": "protobuf"
        }
      },
      "resource": {},
      "kubernetes": {
        "pod_name": "query-service-6d7cfb9d54-abcde",
        "namespace_name": "islap",
        "node_name": "node-a",
        "container_name": "query-service",
        "labels": {
          "app": "query-service"
        }
      }
    }
  ]
}
```

这一层的本质:

- OTLP 日志会被拆平并提取 `resource` / `attributes` / `kubernetes`
- Fluent Bit JSON 会被转换成统一结构
- `service.name` 会优先从 pod 名推导
- `log_meta` 在这里第一次形成

### 3. semantic-engine-worker 消费 Kafka envelope

worker 启动时默认订阅:

- `logs.raw`
- `metrics.raw`
- `traces.raw`

日志处理入口:

- `_process_log_envelope(...)`
- `_process_log_records_batch(...)`
- `_normalize_log_payload(...)`

代码位置:

- [../../semantic-engine/msgqueue/worker.py](../../semantic-engine/msgqueue/worker.py)
- [../../semantic-engine/normalize/normalizer.py](../../semantic-engine/normalize/normalizer.py)

这一层做的事:

- 识别 `signal_type=logs` 且 `records` 为数组的 batched envelope
- 对每条 record 调 `normalize_log(...)`
- 修复 `service_name`
- 修复 `context.k8s`
- 统一时间戳与时区
- 把 `log_meta` 合并回 `_raw_attributes`

worker 产出的规范事件模型大致长这样:

```json
{
  "id": "9d4c9f92-cc27-4e26-ae8d-7a3c6d8f1ab2",
  "timestamp": "2026-04-01T04:42:38.437000+00:00",
  "entity": {
    "type": "service",
    "name": "query-service",
    "instance": "query-service-6d7cfb9d54-abcde"
  },
  "event": {
    "type": "log",
    "level": "info",
    "name": "log",
    "raw": "GET /health 200"
  },
  "context": {
    "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
    "trace_id_source": "otlp",
    "span_id": "00f067aa0ba902b7",
    "k8s": {
      "pod": "query-service-6d7cfb9d54-abcde",
      "namespace": "islap",
      "node": "node-a",
      "container_name": "query-service",
      "labels": {
        "app": "query-service"
      }
    }
  },
  "_raw_attributes": {
    "log_meta": {
      "line_count": 1,
      "wrapped": false,
      "merged": false,
      "timestamp_utc": "2026-04-01T04:42:38.437000+00:00"
    }
  }
}
```

### 4. worker 把规范事件映射成 ClickHouse `logs.logs` 行

核心代码:

- `_prepare_event_row(...)`
- `_save_events_batch(...)`

代码位置:

- [../../semantic-engine/msgqueue/worker.py](../../semantic-engine/msgqueue/worker.py)

最终写入 `logs.logs` 的关键列:

- `id`
- `timestamp`
- `service_name`
- `pod_name`
- `namespace`
- `node_name`
- `pod_id`
- `container_name`
- `container_id`
- `container_image`
- `level`
- `severity_number`
- `message`
- `trace_id`
- `span_id`
- `flags`
- `labels`
- `attributes_json`
- `host_ip`

这一层最关键的事实:

- `labels` 和 `attributes_json` 都是序列化后的 JSON 字符串列
- `log_meta` 最终存放在 `attributes_json.log_meta`
- `request_id` 不是顶级列，查询时主要从 `attributes_json` 或 message 文本提取

### 5. query-service 从 `logs.logs` 查询并返回前端友好的投影

对外接口:

- `GET /api/v1/logs`

主查询代码:

- [../../query-service/api/query_routes.py](../../query-service/api/query_routes.py)
- [../../query-service/api/query_logs_service.py](../../query-service/api/query_logs_service.py)

查询 SQL 并不把整行原样透出，而是返回轻量投影:

```sql
SELECT
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
    host_ip
FROM logs.logs
```

返回结构的关键字段:

- `data`
- `count`
- `has_more`
- `next_cursor`
- `anchor_time`

这一层的关键行为:

- `service_name` 过滤不是简单等值，而是带 pod 后缀清洗的“规范服务名”过滤
- 分页使用 `timestamp + id` keyset cursor，不是 offset
- `labels` 和 `log_meta` 会先 decode
- 前端看到的是 query-service 选择后的投影，不是 ClickHouse 原始整行

### 6. frontend 把后端投影重建为 UI 里的 `Event`

代码入口:

- `api.getEvents()`
- `transformEvent()`
- `useEvents(...)`
- `LogsExplorer`

代码位置:

- [../../frontend/src/utils/api.ts](../../frontend/src/utils/api.ts)
- [../../frontend/src/hooks/useApi.ts](../../frontend/src/hooks/useApi.ts)
- [../../frontend/src/pages/LogsExplorer.tsx](../../frontend/src/pages/LogsExplorer.tsx)

前端做的事:

- 调 `/api/v1/logs`
- 把返回项交给 `transformEvent()`
- 重新构建 `attributes.k8s`
- 重新挂接 `labels`
- 把 `log_meta` 和 message 解析结果合并
- 生成前端展示模型 `Event`

这意味着:

- 前端展示对象不是 ClickHouse row 原样
- 前端看到的 `service_name` 可能已经再次走过 canonical 规范化
- 如果 UI 查不到，原因可能在前端筛选、query-service 过滤、存储字段缺失三个层面之一

## 实时流和普通查询的关键差异

### 普通查询

- 接口: `GET /api/v1/logs`
- 数据源: `logs.logs`
- 支持 keyset 分页
- 支持较完整的筛选条件
- `service_name` 过滤会使用规范服务名表达式，必要时回退 `pod_name`

### 实时流

- 接口: `/ws/logs`
- 数据源: `logs.logs`
- 通过 query-service 周期轮询 ClickHouse 推送
- 只推送连接建立后出现的新行
- 没有订阅者时，poller 会把游标推进到当前时间，避免新订阅时回放历史日志
- 服务端过滤只覆盖 `service_name`、`namespace`、`level`、`exclude_health_check`

### 一个重要差异

按代码实现，`/api/v1/logs` 的服务过滤使用“规范服务名”表达式，而 `/ws/logs` 的轮询 SQL 过滤使用顶级列 `service_name`。  
这意味着某些依赖 `pod_name -> service_name` 推导的日志，可能 REST 能查到，但实时流看不到。

## 字段演化图

| 关注字段 | ingest 接口原始输入 | Kafka record | worker 规范事件 | ClickHouse `logs.logs` | query-service 返回 | frontend `Event` |
|---|---|---|---|---|---|---|
| 时间 | `timeUnixNano` / `timestamp` / 包装时间 | `timestamp` | `timestamp` 标准化到 UTC | `timestamp` | `timestamp` | `timestamp` |
| 服务名 | `service.name` 或 pod 名 | `service.name` | `entity.name` | `service_name` | `service_name` | `service_name` |
| Pod | `k8s.pod.name` / `kubernetes.pod_name` | `kubernetes.pod_name` | `context.k8s.pod` | `pod_name` | `pod_name` | `pod_name` |
| 命名空间 | `k8s.namespace.name` | `kubernetes.namespace_name` | `context.k8s.namespace` | `namespace` | `namespace` | `namespace` |
| Trace | `traceId` / `trace_id` | `trace_id` | `context.trace_id` | `trace_id` | `trace_id` | `trace_id` |
| Span | `spanId` / `span_id` | `span_id` | `context.span_id` | `span_id` | `span_id` | `span_id` |
| Labels | OTLP attrs / k8s labels | `kubernetes.labels` | `context.k8s.labels` | `labels` | `labels` | `labels` |
| log_meta | 接入层创建 | `attributes.log_meta` | `_raw_attributes.log_meta` | `attributes_json.log_meta` | `log_meta` | `log_meta` |
| request_id | attributes 或 message 文本 | 可能在 `attributes` 内 | 仍在 `_raw_attributes` | `attributes_json` | 查询时动态提取/匹配 | `attributes.correlation_request_id` 或 message 展示上下文 |

## 最容易误判的几个点

### 1. `service_name` 不等于 Pod 名

worker 和 query-service 都会做服务名规范化。  
如果 Pod 是 `query-service-6d7cfb9d54-abcde`，UI 里常看到的是 `query-service`。

### 2. `request_id` 不是 ClickHouse 顶级列

`request_id` 主要在 `attributes_json` 内，query-service 查询时会额外从这些 key 提取：

- `request_id`
- `request.id`
- `x_request_id`
- `http.request_id`
- `trace.request_id`

如果结构化字段没有，才会退回从 message 文本里正则抽取。

### 3. 实时日志不是从 Kafka 直接推到前端

`/ws/logs` 只会轮询 ClickHouse `logs.logs`。  
所以 Kafka 有消息但 ClickHouse 没落库时，前端实时流也不会看到日志。

### 4. 前端查的是“投影”

query-service 只返回轻量字段集合，前端再重建展示模型。  
因此“库里有字段但前端没有展示”并不一定是落库丢数据，也可能只是查询投影没有选出来。

## 代码锚点

- ingest 接口入口: [../../ingest-service/internal/ingest/http.go](../../ingest-service/internal/ingest/http.go)
- ingest 队列写入: [../../ingest-service/internal/ingest/queue.go](../../ingest-service/internal/ingest/queue.go)
- ingest 日志转换: [../../ingest-service/internal/ingest/transform.go](../../ingest-service/internal/ingest/transform.go)
- worker 消费与写库: [../../semantic-engine/msgqueue/worker.py](../../semantic-engine/msgqueue/worker.py)
- 日志规范化: [../../semantic-engine/normalize/normalizer.py](../../semantic-engine/normalize/normalizer.py)
- 日志查询: [../../query-service/api/query_logs_service.py](../../query-service/api/query_logs_service.py)
- 日志查询路由: [../../query-service/api/query_routes.py](../../query-service/api/query_routes.py)
- 实时 WebSocket: [../../query-service/api/websocket.py](../../query-service/api/websocket.py)
- 前端日志 API: [../../frontend/src/utils/api.ts](../../frontend/src/utils/api.ts)
- 前端实时 hook: [../../frontend/src/hooks/useApi.ts](../../frontend/src/hooks/useApi.ts)
- 前端页面: [../../frontend/src/pages/LogsExplorer.tsx](../../frontend/src/pages/LogsExplorer.tsx)

## 非目标

这份文档不展开：

- metrics / traces 全链路
- topology-service 图构建细节
- ai-service / exec-service 控制平面

这些属于别的链路，不应该混进“单条日志查询路径”里。
