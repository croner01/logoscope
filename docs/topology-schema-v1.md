# Topology Schema v1

版本: `topology-schema-v1`  
生效阶段: `M1`  
适用接口: `GET /api/v1/topology/hybrid`

## Node 主键

- 主键定义: `node_key = service.namespace + ":" + service.name + ":" + env`
- 兼容字段:
- `id`: 继续保留为服务标识（兼容旧前端/旧查询）
- `label`: 展示名称

## Edge 主键

- 主键定义: `edge_key = src_node_key + "|" + dst_node_key + "|" + protocol + "|" + endpoint_pattern`
- 兼容字段:
- `id`: 继续保留为 `source-target`
- `source` / `target`: 继续保留

## Node 字段

- `service.namespace`
- `service.name`
- `service.env`
- `node_key`
- `evidence_type`
- `coverage`
- `quality_score`
- `metrics.confidence`

## Edge 字段

- `protocol`
- `endpoint_pattern`
- `edge_key`
- `evidence_type`
- `coverage`
- `quality_score`
- `p95`
- `p99`
- `timeout_rate`
- `metrics.confidence`

## 证据类型

- `observed`: 直接观测证据（例如 traces）
- `inferred`: 推断证据（例如 logs heuristic）

## 质量分（quality_score）说明

- 取值范围: `0-100`
- 计算输入（M1）:
- `error_rate`
- `p95`
- `p99`
- `timeout_rate`
- `retries`
- `pending`
- `dlq`

说明: 分值越高表示边质量越好。该分值用于拓扑边质量排序，不替代 SLA/SLO 指标。
