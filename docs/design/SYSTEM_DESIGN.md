# Logoscope 系统设计文档

> 版本: v3.21.0  
> 更新: 2026-02-11  
> 状态: 发布

---

## 📋 目录

- [1. 系统概述](#1-系统概述)
- [2. 架构设计](#2-架构设计)
- [3. 模块设计](#3-模块设计)
- [4. 数据模型](#4-数据模型)
- [5. 接口设计](#5-接口设计)
- [6. 技术选型](#6-技术选型)
- [7. 性能设计](#7-性能设计)
- [8. 安全设计](#8-安全设计)

---

## 1. 系统概述

### 1.1 项目简介

**Logoscope** 是一个云原生可观测性平台，专为 Kubernetes 环境设计的智能日志分析和分布式追踪系统。

#### 核心能力

- 🪄 **统一日志采集**: Fluent Bit + OpenTelemetry Collector
- 🔍 **智能检索**: ClickHouse 驱动的高性能查询引擎
- 🕸️ **分布式追踪**: OpenTelemetry 标准追踪
- 🕸️ **服务拓扑**: 自动发现 + AI 增强的依赖关系
- 🤖 **AI 分析**: 语义理解、异常检测、根因分析
- 📊 **可观测性**: 指标、事件、标签多维分析

### 1.2 设计目标

| 目标 | 说明 | 优先级 |
|------|------|--------|
| **高性能** | 支持每秒 10K+ 日志写入，查询响应 < 100ms | P0 |
| **高可用** | 99.9%+ 可用性，无单点故障 | P0 |
| **云原生** | Kubernetes 原生部署，自动扩缩容 | P0 |
| **智能分析** | AI 驱动的异常检测和根因分析 | P1 |
| **标准兼容** | OpenTelemetry 标准兼容 | P1 |

### 1.3 系统边界

```
┌─────────────────────────────────────────────────────────────────┐
│                    Logoscope 系统边界                     │
├─────────────────────────────────────────────────────────────────┤
│                                                               │
│  ┌──────────────┐       ┌─────────────┐    ┌──────────┐ │
│  │   K8s 集群   │──────▶│ OTel Gateway│───▶│ Semantic │ │
│  │              │       │             │    │ Engine   │ │
│  │ ┌────────┐   │       └─────────────┘    │          │ │
│  │ │Fluent │   │              ▲            │          │ │
│  │ │  Bit  │───┼──────────────┘            │          │ │
│  │ └────────┘   │                           │          │ │
│  │              │                           ▼          │ │
│  │ ┌────────┐   │              ┌──────────┐ │          │ │
│  │ │ Apps   │───┼─────────────▶│ ClickHouse│◀─────────┘ │
│  │ └────────┘   │              └──────────┘               │
│  │              │                           ▲              │
│  │              │              ┌───────────┤              │
│  └──────────────┘              │   Neo4j   │              │
│                                └───────────┘              │
└─────────────────────────────────────────────────────────────────┘
```

---

## 2. 架构设计

### 2.1 总体架构

#### 分层架构

```
┌─────────────────────────────────────────────────────────────┐
│                   表现层 (Presentation)                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────────┐  │
│  │ 前端 UI  │  │ Grafana  │  │ API Gateway │  │
│  └──────────┘  └──────────┘  └──────────────┘  │
└─────────────────────────────────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────┐
│                    应用层 (Application)                 │
│  ┌──────────────────────────────────────────────────┐   │
│  │       Semantic Engine (FastAPI)              │   │
│  │  ┌─────────┐ ┌─────────┐ ┌───────────┐ │   │
│  │  │ Ingest   │ │  Query  │ │  AI/ML    │ │   │
│  │  └─────────┘ └─────────┘ └───────────┘ │   │
│  └──────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────┐
│                    数据层 (Data)                        │
│  ┌─────────────┐        ┌─────────────┐           │
│  │ ClickHouse   │        │   Neo4j    │           │
│  │ (时序数据)  │        │  (图数据库)  │           │
│  └─────────────┘        └─────────────┘           │
│         ▲                       ▲                   │
│         └───────────┬───────────┘                   │
│                    ▼                                │
│         ┌─────────────┐                            │
│         │   Redis    │                            │
│         │  (缓存)    │                            │
│         └─────────────┘                            │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 数据流架构

```
应用日志 
   │
   ▼
┌──────────────┐
│ Fluent Bit   │ (DaemonSet on each node)
│  日志采集    │
└──────────────┘
   │ OTLP (HTTP/gRPC)
   ▼
┌──────────────┐
│ OTel        │ (Deployment)
│  Collector  │ (批处理、内存限制)
└──────────────┘
   │ OTLP (HTTP)
   ▼
┌──────────────┐
│ OTel        │ (StatefulSet)
│  Gateway    │ (路由、转发)
└──────────────┘
   │                       │
   │ (Logs/Traces)        │ (Metrics)
   ▼                       ▼
┌──────────────┐      ┌──────────────┐
│ Semantic    │      │   ClickHouse│
│  Engine     │─────▶│   Writer    │
│  Ingest API │      └──────────────┘
└──────────────┘
   │
   ▼
┌──────────────────────────┐
│   数据标准化 (Normalizer)  │
│  - 日志级别提取            │
│  - 服务名识别 (8层回退)    │
│  - Trace ID 生成          │
└──────────────────────────┘
   │                │
   ▼                ▼
┌──────────────┐ ┌──────────────┐
│ ClickHouse  │ │   Neo4j    │
│  (logs)     │ │  (拓扑)      │
└──────────────┘ └──────────────┘
```

### 2.3 部署架构

#### Kubernetes 部署拓扑

```yaml
命名空间:
  - islap:        基础设施 (OTel, Redis)
  - logoscope:    应用 (Semantic Engine, 前端)

核心组件:
  - fluent-bit:          DaemonSet (每个节点)
  - otel-collector:      DaemonSet (每个节点)
  - otel-gateway:       StatefulSet (3副本)
  - semantic-engine:    Deployment (2副本)
  - clickhouse:         StatefulSet (3副本)
  - neo4j:             StatefulSet (3副本)
  - redis:             StatefulSet (1副本)
```

---

## 3. 模块设计

### 3.1 日志采集模块

#### Fluent Bit 配置
- 实时日志流采集
- 多行日志合并
- K8s 元数据注入
- OTLP 格式转换

#### OpenTelemetry Collector Pipeline
- 日志/追踪/指标接收
- 批处理优化
- 内存限制管理
- OTLP 转发

### 3.2 日志处理模块

#### Normalizer 核心功能
- 服务名提取 (8层回退策略)
- 日志级别推断
- Trace ID 生成
- K8s 上下文提取

### 3.3 查询模块

#### Query API
- 高级日志查询
- 全文检索
- 分页支持
- ClickHouse 查询优化

#### Traces API
- 按 trace_id 查询
- 分布式追踪可视化
- 性能分析

### 3.4 拓扑模块

#### Realtime Topology API
- 混合拓扑生成 (ClickHouse + Neo4j)
- 拓扑变化检测
- 拓扑快照
- WebSocket 实时更新

#### Topology Adjustment API
- AI 置信度计算
- 拓扑异常检测
- 自动修复

### 3.5 AI 分析模块

#### 智能分析
- 日志异常检测
- 模式识别
- 根因分析
- 语义理解

### 3.6 告警模块

#### 告警管理
- 规则 CRUD
- 实时评估
- 事件通知
- 告警聚合

---

## 4. 数据模型

### 4.1 ClickHouse Schema

#### logs 表
```sql
CREATE TABLE logs.logs (
    id String,
    timestamp DateTime64(3),
    entity Tuple(type String, name String, instance String),
    event Tuple(type String, level String, name String, raw String),
    context Tuple(
        trace_id String,
        span_id String,
        host String,
        k8s Tuple(...)
    ),
    severity_number UInt8,
    flags UInt8
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (timestamp, service_name, level)
```

#### traces 表
```sql
CREATE TABLE logs.traces (
    trace_id String,
    span_id String,
    parent_span_id String,
    timestamp DateTime64(3),
    duration_ns UInt64,
    service_name String,
    operation String,
    status_code UInt16
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (trace_id, timestamp)
```

### 4.2 Neo4j Schema

#### 节点类型
```cypher
(:Service {id, name, type, log_count, health_status})
(:K8sNamespace {id, name})
(:K8sNode {id, name})
(:K8sPod {id, name, service_name})
```

#### 关系类型
```cypher
(:Service)-[:CALLS {count, avg_latency_ms, confidence_score}]->(:Service)
(:Service)-[:HOSTED_ON]->(:K8sPod)
(:K8sPod)-[:RUNS_ON]->(:K8sNode)
```

---

## 5. 接口设计

### 5.1 API 设计原则

#### RESTful 规范
```
GET    /api/v1/resources          # 列表
GET    /api/v1/resources/{id}     # 详情
POST   /api/v1/resources          # 创建
PUT    /api/v1/resources/{id}     # 更新
DELETE /api/v1/resources/{id}     # 删除
```

#### 响应格式标准
```json
{
  "status": "success",
  "data": { ... },
  "meta": {
    "timestamp": "2026-02-11T10:00:00Z",
    "request_id": "req-abc123"
  }
}
```

### 5.2 WebSocket API

#### 拓扑订阅
```javascript
const ws = new WebSocket('ws://localhost:8080/api/v1/topology/subscribe');
ws.send(JSON.stringify({
  action: 'subscribe',
  channel: 'topology_updates',
  filters: { time_window: '1 HOUR' }
}));
```

---

## 6. 技术选型

### 6.1 技术栈

| 层级 | 技术 | 版本 | 说明 |
|------|------|------|
| **日志采集** | Fluent Bit | v2.2 | 轻量级，K8s 原生 |
| **遥测** | OpenTelemetry | v0.91.0 | 标准兼容 |
| **API 框架** | FastAPI | v0.104+ | 高性能异步 |
| **数据库** | ClickHouse | v23.3 | 列式存储，OLAP |
| **图数据库** | Neo4j | v5.x | 图查询，拓扑 |
| **缓存** | Redis | v7.x | KV 存储，队列 |
| **容器** | Docker | v24+ | 容器化 |
| **编排** | Kubernetes | v1.28+ | 集群管理 |
| **前端** | React | v18 | SPA |
| **语言** | Python | v3.12+ | 主要语言 |

### 6.2 选型理由

#### ClickHouse vs TimescaleDB
- ✅ 写入性能: 10x 更快
- ✅ 查询性能: 列式优化
- ✅ 压缩率: 高

#### Neo4j vs ArangoDB
- ✅ 图查询语言: Cypher 专用
- ✅ 遍历性能: 深度优先优化

---

## 7. 性能设计

### 7.1 性能目标

| 指标 | 目标 | 监控方式 |
|------|------|----------|
| **日志写入吞吐** | 10K+ logs/s | ClickHouse metrics |
| **查询响应时间** | P95 < 200ms | API latency |
| **拓扑生成延迟** | < 5s | Topology timing |
| **API 可用性** | 99.9% | Uptime monitoring |

### 7.2 性能优化策略

#### 写入优化
- 批量写入 (batch=1000)
- 异步处理 (asyncio)
- 连接池管理

#### 查询优化
- 分区裁剪
- 索引优化
- 物化视图
- 缓存策略 (5分钟 TTL)

---

## 8. 安全设计

### 8.1 认证授权

#### API 认证
- JWT Token 验证
- RBAC 权限控制

#### RBAC
```python
class Permission(str, Enum):
    READ_LOGS = "read:logs"
    WRITE_LOGS = "write:logs"
    ADMIN = "admin"
```

### 8.2 数据安全

#### 传输加密
- ✅ HTTPS/TLS (生产环境)
- ✅ gRPC over TLS
- ✅ WebSocket over WSS

#### 存储加密
- ClickHouse 磁盘加密 (建议)
- Neo4j 字段级加密 (建议)

### 8.3 网络安全

#### Kubernetes NetworkPolicy
```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: logoscope-policy
spec:
  podSelector:
    matchLabels:
      app: semantic-engine
  policyTypes:
  - Ingress
  - Egress
```

---

## 附录

### A. 术语表

| 术语 | 定义 |
|------|------|
| **OTel** | OpenTelemetry，云原生可观测性标准 |
| **OTLP** | OpenTelemetry Protocol |
| **Span** | 追踪中的单个操作 |
| **Trace** | 分布式追踪中的完整调用链 |
| **Topology** | 服务依赖关系图 |
| **Confidence Score** | AI 计算的拓扑边置信度 (0-1) |

### B. 参考文档

- [OpenTelemetry 规范](https://opentelemetry.io/docs/reference/specification/)
- [ClickHouse 文档](https://clickhouse.com/docs)
- [Neo4j Cypher 手册](https://neo4j.com/docs/cypher-manual/)
- [FastAPI 文档](https://fastapi.tiangolo.com/)
- [Kubernetes 最佳实践](https://kubernetes.io/docs/concepts/)

---

**文档版本**: v3.21.0  
**最后更新**: 2026-02-11  
**维护者**: Semantic Engine Team
