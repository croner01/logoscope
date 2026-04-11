# 单条日志查不到时的故障排查地图

> 版本: v3.25.0  
> 更新: 2026-04-01  
> 状态: 当前代码实现  
> 适用范围: 一条日志已经产生，但在前端日志页查不到，如何逐层定位  

## 目标

这份文档只回答一个问题：

> 当一条日志在前端查不到时，按最短路径应该逐层看哪个 topic、哪个表、哪个接口、哪个字段。

核心原则不是“看服务是否 healthy”，而是：

1. 先确认查询条件是不是把它过滤掉了。
2. 再确认 ClickHouse `logs.logs` 里到底有没有这条数据。
3. 如果没有，再往前倒查 worker、Kafka、ingest、上游采集。

## 最短排查顺序

```text
前端查询参数
  -> query-service /api/v1/logs
  -> ClickHouse logs.logs
  -> semantic-engine-worker 消费/落库
  -> Kafka logs.raw / logs.raw.dlq
  -> ingest-service /v1/logs 与队列状态
  -> Fluent Bit / OTel Collector
```

如果你一开始就从 ingest 往前翻，通常会浪费时间。  
因为最常见的问题并不是“没进系统”，而是“进了系统但被过滤条件、字段规范化或时间锚点排除了”。

## 分层排查矩阵

| 层级 | 先看哪里 | 对象类型 | 要看什么 | 关键字段 | 结论 |
|---|---|---|---|---|---|
| 1 | 浏览器请求或前端状态 | 接口请求 | `/api/v1/logs` 实际传了什么参数 | `service_name`, `service_names`, `namespace`, `trace_id`, `request_id`, `level`, `search`, `start_time`, `end_time`, `time_window`, `anchor_time`, `exclude_health_check` | 先排除“查法错了” |
| 2 | query-service | REST 接口 | 直接调用 `/api/v1/logs` | 返回 `data`, `count`, `next_cursor`, `anchor_time` | 确认是前端问题还是后端查询问题 |
| 3 | ClickHouse | 表 | `logs.logs` | `timestamp`, `id`, `service_name`, `pod_name`, `namespace`, `level`, `message`, `trace_id`, `span_id`, `labels`, `attributes_json` | 这是日志查询真相源 |
| 4 | semantic-engine-worker | 消费/落库 | worker 日志与消费状态 | `Processed logs count`, normalize/save 错误, DLQ 迹象 | 确认 Kafka 到 ClickHouse 这一段是否断了 |
| 5 | Kafka | topic | `logs.raw` 和 `logs.raw.dlq` | `signal_type`, `batched`, `record_count`, `records[]`; DLQ 中的 `source_topic`, `source_partition`, `source_offset` | 确认 ingest 是否成功入队、worker 是否失败回退 |
| 6 | ingest-service | 接口和状态 | `/v1/logs`, `/health`, `/api/v1/queue/stats` | `queue_connected`, `kafka_connected`, `kafka_written`, `memory_queue_size`, `backpressure_rejected`, `wal_*` | 确认系统是否收到请求并成功写队列 |
| 7 | OTel/Fluent Bit | 上游采集 | collector/fluent-bit 配置与日志 | 原始日志内容、resource attrs、k8s attrs | 如果 ingest 没请求，只能往这层查 |

## 第 1 层：先排除“查询条件错了”

前端日志页真正请求的是 `GET /api/v1/logs`。  
先不要猜存储有没有数据，先把请求参数抄下来。

最容易把日志查没的参数：

- `service_name` / `service_names`
- `namespace` / `namespaces`
- `trace_id`
- `request_id`
- `level` / `levels`
- `search`
- `start_time` / `end_time`
- `time_window`
- `anchor_time`
- `exclude_health_check`

### 必须知道的过滤语义

#### `service_name` 不是简单等值

query-service 会做“规范服务名”计算：

- 优先用 `service_name`
- `service_name` 为空或 `unknown` 时退回 `pod_name`
- 自动剥离 Deployment / DaemonSet / StatefulSet 的 pod 后缀

所以库里可能是：

- `service_name = unknown`
- `pod_name = query-service-6d7cfb9d54-abcde`

但 UI 仍然按 `query-service` 命中。

#### `request_id` 不是顶级列过滤

query-service 会从 `attributes_json` 这些路径找：

- `request_id`
- `request.id`
- `x_request_id`
- `http.request_id`
- `trace.request_id`

如果这些都没有，才回退到 message 文本正则提取。  
所以“按 request_id 查不到”经常不是日志丢了，而是 request_id 根本没被结构化出来。

#### `exclude_health_check=true` 可能把日志过滤掉

如果你的目标日志就是 `/health`、`readiness`、probe 类日志，这个开关会直接排除。

#### `anchor_time` 会冻结分页时间线

分页查询用的是 keyset cursor + `anchor_time`。  
如果第一页的 `anchor_time` 已经固定，之后新写入的日志不会自动进入同一条翻页时间线。

## 第 2 层：直接复现 query-service 返回

目标不是看前端，而是确认 query-service 作为读侧 API 怎么回答。

接口:

- `GET /api/v1/logs`

应该关注：

- `data` 是否为空
- `anchor_time` 是不是过早
- `next_cursor` 是否推进
- 指定 `trace_id` / `request_id` 时结果是否为空

如果接口能查到而前端看不到，问题在：

- 前端 transform
- 前端本地过滤
- 实时列表和分页列表状态合并

如果接口也查不到，再查 ClickHouse。

## 第 2.5 层：如果是“REST 能查到，但实时流看不到”

先明确，`/ws/logs` 不是 `GET /api/v1/logs` 的流式版。  
两者都读 ClickHouse，但读法不同。

### `/ws/logs` 的事实

- 它轮询 `logs.logs`
- 只看连接建立后的新增日志
- 没有订阅者时会把内部游标推进到当前时间，不回放历史
- 服务端订阅过滤只支持:
  - `service_name`
  - `namespace`
  - `level`
  - `exclude_health_check`

### 一个高风险差异

按当前代码，`GET /api/v1/logs` 的服务过滤支持规范服务名计算，可在必要时回退 `pod_name`。  
而 `/ws/logs` 的轮询 SQL 直接用顶级列 `service_name` 过滤。

这带来的直接现象是：

- REST 能查到
- WebSocket 实时流查不到

典型场景：

- 行里的 `service_name` 是 `unknown`
- 但 `pod_name = query-service-6d7cfb9d54-abcde`
- REST 查询会把它归并到 `query-service`
- `/ws/logs` 轮询 SQL 不一定会把它拉出来

所以如果你只在“实时模式”看不到日志，不要立刻怀疑 ingest 或 Kafka，先核对实时路径本身的读取语义。

## 第 3 层：ClickHouse `logs.logs` 是日志查询真相源

表:

- `logs.logs`

先查最小集字段：

```sql
SELECT
  timestamp,
  id,
  service_name,
  pod_name,
  namespace,
  level,
  message,
  trace_id,
  span_id
FROM logs.logs
WHERE timestamp >= now() - INTERVAL 1 HOUR
ORDER BY timestamp DESC
LIMIT 50;
```

### 如果已知 trace_id

```sql
SELECT
  timestamp,
  service_name,
  pod_name,
  namespace,
  level,
  message,
  trace_id,
  span_id
FROM logs.logs
WHERE trace_id = 'YOUR_TRACE_ID'
ORDER BY timestamp DESC
LIMIT 100;
```

### 如果已知服务名但不确定是 service 还是 pod

```sql
SELECT
  timestamp,
  service_name,
  pod_name,
  namespace,
  level,
  message
FROM logs.logs
WHERE service_name = 'query-service'
   OR pod_name LIKE 'query-service-%'
ORDER BY timestamp DESC
LIMIT 100;
```

### 如果按 request_id 查不到

`request_id` 多半在 `attributes_json`，先直接看结构：

```sql
SELECT
  timestamp,
  service_name,
  message,
  attributes_json
FROM logs.logs
WHERE attributes_json ILIKE '%request_id%'
ORDER BY timestamp DESC
LIMIT 50;
```

### 这一层要看哪些列

- `timestamp`
- `id`
- `service_name`
- `pod_name`
- `namespace`
- `level`
- `message`
- `trace_id`
- `span_id`
- `labels`
- `attributes_json`
- `host_ip`

### 判断标准

- 表里有，接口没查到: 读侧过滤或字段提取问题
- 表里没有: 问题发生在 worker 之前或 worker 落库阶段

## 第 4 层：semantic-engine-worker 是否成功消费并落库

worker 关注点不是“进程在不在”，而是三件事：

1. 有没有收到 `logs.raw`
2. 有没有 normalize 失败
3. 有没有写 ClickHouse 失败

关键日志线索：

- `Subscribing to stream=logs.raw`
- `Worker started and waiting for messages`
- `Processed logs count=...`
- `Failed to normalize log payload`
- `Failed to save normalized event`
- `Failed to persist batched events`
- `Failed to flush pending log writer buffer before ACK`

这一层一旦失败，典型结果有两种：

- 消息反复重试，`logs.logs` 迟迟没数据
- 消息进入 `logs.raw.dlq`

### 这里最该盯的字段

worker 在处理单条记录时最关键的映射字段：

- 输入 record: `log`, `timestamp`, `severity`, `service.name`, `trace_id`, `span_id`, `attributes`, `kubernetes`
- 规范事件: `entity.name`, `event.level`, `event.raw`, `context.trace_id`, `context.span_id`, `context.k8s`, `_raw_attributes.log_meta`
- ClickHouse 行: `service_name`, `pod_name`, `namespace`, `message`, `trace_id`, `span_id`, `labels`, `attributes_json`

### 如果这里有问题，优先怀疑什么

- ingest 转出来的 `records[]` 结构不符合 worker 预期
- 时间戳解析失败导致落库时间异常
- `service_name` 为空，后续筛选时被误判
- `request_id` 根本没进入 `_raw_attributes`

## 第 5 层：Kafka 看 `logs.raw` 和 `logs.raw.dlq`

默认 topic:

- `logs.raw`

失败死信 topic:

- `logs.raw.dlq`

### `logs.raw` 里应该看到什么

日志消息是 batched envelope，不是单条裸日志：

```json
{
  "signal_type": "logs",
  "batched": true,
  "record_count": 10,
  "records": [
    {
      "log": "...",
      "timestamp": "...",
      "severity": "INFO",
      "service.name": "...",
      "trace_id": "...",
      "span_id": "...",
      "attributes": { "...": "..." },
      "kubernetes": { "...": "..." }
    }
  ]
}
```

### 这里要核对的字段

- envelope: `signal_type`, `batched`, `record_count`
- record: `log`, `timestamp`, `severity`, `service.name`, `trace_id`, `span_id`
- k8s: `kubernetes.pod_name`, `kubernetes.namespace_name`, `kubernetes.labels`
- attrs: `attributes.log_meta`, `attributes.request_id`, `attributes.http.request_id`

### `logs.raw.dlq` 表示什么

说明 worker 收到了消息，但多次处理失败，Kafka adapter 把消息转入 DLQ。  
DLQ 里应检查：

- `source_topic`
- `source_partition`
- `source_offset`
- 原始消息体

如果日志在 `logs.raw` 有、`logs.raw.dlq` 也有，说明问题不在 ingest，而在 worker 消费或写库逻辑。

## 第 6 层：ingest-service 是否真的收到并成功写入 Kafka

关键接口:

- `POST /v1/logs`
- `GET /health`
- `GET /api/v1/queue/stats`

### `/health` 关键字段

- `queue_backend`
- `queue_connected`
- `kafka_connected`
- `stats.total_written`
- `stats.kafka_written`
- `stats.memory_queued`
- `stats.memory_queue_flushed`
- `stats.memory_queue_flush_failures`
- `stats.backpressure_rejected`
- `stats.wal_appended`
- `stats.wal_acked`

### `/api/v1/queue/stats` 关键字段

- `queue.queue_connected`
- `queue.kafka_connected`
- `queue.total_written`
- `queue.kafka_written`
- `queue.memory_queue_size`
- `queue.memory_queue_fill_ratio`
- `queue.backpressure_rejected`

### 这一层如何判断

- `POST /v1/logs` 成功且 `kafka_written` 增长: ingest 基本正常
- `POST /v1/logs` 成功但 `memory_queued` 一直涨: Kafka 不通，正在降级缓冲
- `backpressure_rejected` 增长: 请求被挡在 ingest，后面都不必看
- `total_written` 不增长: 上游根本没打到 ingest

## 第 7 层：如果 ingest 根本没收到请求，只能往上游查

仓库内主链路到 ingest 为止，ingest 之前通常是：

- Fluent Bit
- OTel Collector Agent/Gateway

如果 `ingest-service` 的 `total_written` 没变化，说明问题还没进入本仓库主链路。  
这时应检查：

- 上游 exporter 目标是不是 `/v1/logs`
- Content-Type / Content-Encoding 是否正确
- OTel resource attrs 和 k8s attrs 是否被保留下来

## 按字段倒查

### 按 `trace_id` 倒查

顺序：

1. 前端请求里确认 `trace_id`
2. `GET /api/v1/logs?trace_id=...`
3. `logs.logs.trace_id`
4. Kafka record `trace_id`
5. 原始 OTLP `traceId`

只要 Kafka record 就已经没有 `trace_id`，后面所有链路都会失效。

### 按 `request_id` 倒查

顺序：

1. 前端请求里确认 `request_id`
2. query-service 是否能从 `attributes_json` 提取
3. `logs.logs.attributes_json` 是否包含 `request_id`
4. worker `_raw_attributes`
5. Kafka record `attributes`
6. 原始输入是否真的包含 request_id 或至少在 message 里有稳定模式

这条链路最脆弱，因为 `request_id` 不是顶级列。

### 按 `service_name` 倒查

顺序：

1. 先同时看 `service_name` 和 `pod_name`
2. 判断是否被 pod 后缀规范化
3. Kafka record 里的 `service.name`
4. worker `entity.name`
5. ClickHouse `service_name`

这一条最常见的误判是：

- 原始日志没有 `service.name`
- 系统靠 pod 名推导
- 用户却用完整 pod 名或错误服务名过滤

### 按 `labels` 倒查

顺序：

1. 原始 OTLP resource / attrs 或 Fluent Bit kubernetes labels
2. Kafka record `kubernetes.labels`
3. worker `context.k8s.labels`
4. ClickHouse `labels`
5. query-service decode 后的 `labels`
6. frontend `Event.labels`

如果 ClickHouse `labels` 为空，前端无法凭空恢复。

## 最常见的根因，不是“服务挂了”

最常见的真实根因通常是这几类：

1. 查询参数不对，尤其是 `service_name`、`request_id`、`anchor_time`
2. 日志已入库，但字段不满足 query-service 的过滤提取规则
3. worker 正常消费失败，消息转入 `logs.raw.dlq`
4. ingest 降级到内存/WAL 队列，Kafka 没真正写入
5. 上游采集没把日志送到 `/v1/logs`

## 配套文档

- 当前运行时链路: [../architecture/log-ingest-query-runtime-path.zh-CN.md](../architecture/log-ingest-query-runtime-path.zh-CN.md)
- 历史数据流文档: [../architecture/data-flow.md](../architecture/data-flow.md)

历史文档如果和这份冲突，以代码和本排障地图为准。
