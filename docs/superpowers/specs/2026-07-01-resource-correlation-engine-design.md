# Logoscope Data Architecture v1 — Event-driven AI Observability Platform

> **Event Sourcing + CQRS + Unified Context — 只有 Raw Event 是 Source of Truth。**
> 所有数据是 Projection；所有查询通过 Context API；所有变换通过 Schema Registry 管理版本。
>
> 定义从 Raw Log → Event Pipeline → Semantic Engine → Schema-enforced Multi-topic Event Bus → Projection Layer → Unified Context API → Inference → Planner → Workflow → Capability → Action 的完整闭环。

**Status:** Draft v8
**Date:** 2026-07-01
**Authors:** croner01, Claude

---

## 1. Problem

### 1.1 当前架构的系统性瓶颈

v7.1 引入了 Raw Event 作为 Source of Truth 和 Projection 框架，但仍有根本性问题：

**① 没有 Schema Registry。**
所有 Event 以裸对象传递。一年后 `NormalizedEvent` 新增 `tenant_id` 字段，旧 Raw Event replay 时要么缺失字段、要么反序列化崩溃。没有 schema version 就没有安全的 schema evolution。

**② 没有 Event Envelope。**
Producer、schema_version、event_type 等元信息丢失。无法追溯"谁产生了这个 Event"；无法判断 payload 格式。

**③ Semantic Engine 职责过重。**
同时承担 normalize + aggregate（多行日志合并）+ dedup（10000 条重复错误）+ enrich（host→AZ→Rack）+ route（Kubernetes vs OpenStack），是 monolith 架构。

**④ Projection Checkpoint 不能用 event_id。**
Kafka replay 依赖 partition + offset。用 `last_event_id` 做 checkpoint 无法精确计算 lag、无法从指定 offset 恢复。

**⑤ 没有统一查询入口。**
Topology 直接调 Graph Projection、Workflow 直接调 Context Builder、Rule Engine 直接调 State Projection。外部系统知道 Neo4j、Redis、ClickHouse 的存在——这将导致架构侵蚀。

**⑥ Workflow 与执行方式耦合。**
Workflow 直接写 `SSH compute-01; systemctl restart`，抽象层缺失。替换执行方式（SSH → k8s exec → VMware API）需要改 Workflow 定义。

**⑦ Planner 不存在。**
Inference 直接输出 Workflow。正确的模式是 Inference → Finding → Planner → Workflow。AI 负责"诊断"，Planner 负责"开药"。

### 1.2 v8 核心理念

```
所有 Event 通过 EventEnvelope 携带 schema_version。
所有 Schema 通过 Schema Registry 管理版本和迁移。
所有预处理通过 Event Pipeline（Processor 链）。
所有查询通过 Unified Context API——外部不知道 Neo4j/Redis/ClickHouse。
所有执行通过 Capability 抽象——Workflow 不知道 SSH/kubectl。
所有决策由 Inference → Planner → Workflow 三级完成。
```

### 1.3 v7.1 → v8 变更

| 维度 | v7.1 | v8 |
|------|------|----|
| **Source of Truth** | Raw Event（裸对象） | Raw Event + EventEnvelope + Schema |
| **Schema 管理** | 无 | **Schema Registry + Schema Evolution** |
| **Event 传输** | 裸对象 | **EventEnvelope**（schema_version, producer, event_type, payload） |
| **Semantic Engine** | normalize + aggregate + dedup + enrich + route（monolith） | **只有 normalize**；预处理拆为 Event Pipeline |
| **Event Pipeline** | 无 | **Processor 链**：aggregate → dedup → sample → enrich → route |
| **Topic 命名** | `normalized-events`, `raw-events` | **Domain-first**：`platform.normalized`, `platform.raw` |
| **Projection Checkpoint** | last_event_id（不可靠） | **Partition + Offset**（可靠，支持 lag 监测） |
| **查询入口** | 各自直接调 Projection | **Unified Context API**（隐藏所有存储） |
| **Workflow 执行** | 直接 SSH/kubectl | **Capability 抽象层** |
| **Inference → Workflow** | 直接映射 | **Planner**（架构预留） |
| **Event Bus Topics** | 10 topics | **10 topics（domain 命名）** |

---

## 2. Architecture

### 2.1 整体架构

```
                    Raw Logs / Metrics / Traces / Events
                               |
                               v
                     ┌─────────────────────┐
                     │   Raw Event Store    │  ← 真正的 Source of Truth
                     │ (immutable, WAL +    │
                     │  Kafka platform.raw) │
                     └──────────┬───────────┘
                                |
                                v
                   Event Bus (platform.raw)
                     EventEnvelope{raw.event, v1}
                                |
                                v
┌───────────────────────────────────────────────────┐
│                Event Pipeline                     │
│  ┌────────┬────────┬────────┬────────┬────────┐   │
│  │Aggr.   │Dedup   │Sample  │Enrich  │Route   │   │
│  │(Trace- │(exp    │(INFO→  │(host→  │(OS→OS  │   │
│  │ back)  │ backoff)│1%)    │ AZ)    │ K8s→K8s)│  │
│  └────────┴────────┴────────┴────────┴────────┘   │
└──────────────────────────┬────────────────────────┘
                           |
                           v
                    Semantic Engine
                     Schema Registry
                     (consume EventEnvelope,
                      produce EventEnvelope)
                           |
                   NormalizedEvent (EventEnvelope)
                           |
                   Event Bus (platform.normalized)
                           |
       ┌──────────┬───────┴───────┬──────────┐
       v          v               v          v
 Entity      State         Interaction    Timeline
 Projector   Projector      Projector     MV (CH)
       |          |               |
       v          v               v
 platform.entity  platform.state  platform.interaction
       |          |               |
       └────┬─────┴───────┬───────┘
            │             │
            v             v
      Inventory      DynamicRel
      Projection     Projection
      (Neo4j)        (ClickHouse)
            │             │
            └──────┬──────┘
                   │
            Graph Projection
              (只存拓扑)
                   │
              ┌────┴────┐
              │ Context │ ← Unified Entry Point
              │   API   │
              └────┬────┘
                   │
     ┌─────┬───────┼───────┬─────┬──────┐
     │     │       │       │     │      │
     v     v       v       v     v      v
 Topology  Rule  Inference Planner Workflow
           Engine         (rsv)   Engine
                                    │
                              Capability
                              Registry
                                    │
                     ┌──────+──────┬──+──────┐
                     v      v      v      v
                   SSH   kubectl  API   VMware
                                    │
                             Generated Events
                                    │
                            Event Bus (loop)
```

### 2.2 Event Sourcing 原则

```
原则 1: 只有 Raw Event（EventEnvelope 包裹）是真正的不可变 Source of Truth
原则 2: 所有 Event 携带 schema_version，Schema Registry 管理迁移
原则 3: 所有 Projection 可以从上游 Event Stream 重建，重建基于 Offset
原则 4: Projection 可以随时删除重建，不影响 Raw Event
原则 5: 查询永远走 Context API——不直接查 Projection
原则 6: 写入永远通过 Event Bus——不直接修改 Projection
原则 7: 所有 Projection 带 Epoch 标记，支持零停机切换
原则 8: Projection 依赖图自动决定重建顺序（拓扑排序）
原则 9: Schema 变更必须向前兼容（只加字段，不改/删字段）
原则 10: 所有 Workflow 执行通过 Capability，不直接依赖执行方式
```

### 2.3 EventEnvelope + Schema Registry

#### 2.3.1 EventEnvelope

```python
@dataclass
class EventEnvelope:
    """
    所有 Event 的通用信封。
    Payload 是序列化的具体 Event 对象。

    为什么需要 Envelope：
      - 记录 schema_version → 支持 schema evolution
      - 记录 producer → 追溯"谁产生了这个 Event"
      - 记录 event_type → consumer 只订阅需要的 type
      - metadata 携带路由/追踪信息
    """
    envelope_version: str = "v1"           # Envelope 自身版本（几乎不变）
    schema_version: int = 1                # Payload schema 版本
    event_type: str = ""                   # "raw.log", "normalized.event", "entity.seen"
    producer: str = ""                     # "semantic-engine", "entity-projector"
    event_id: str = ""                     # UUID7
    timestamp: datetime = field(default_factory=datetime.utcnow)
    payload: bytes = b""                   # 序列化的具体 Event（protobuf / JSON / msgpack）
    metadata: Dict[str, str] = field(default_factory=dict)
    # 常用 metadata key:
    #   trace_id, span_id, cluster, namespace, raw_id
```

**为什么不用裸对象：**

| 场景 | 裸对象 | EventEnvelope |
|------|--------|---------------|
| Schema 升级 | 反序列化崩溃 | SchemaRegistry.migrate(v1→v2) |
| 追溯 producer | 丢失 | envelope.producer = "semantic-engine" |
| Filter by type | 需要反序列化才知道 | envelope.event_type 直接可读 |
| Routing | 不适用 | metadata.cluster → 路由到指定集群 |

#### 2.3.2 Schema Registry

```python
@dataclass
class Schema:
    """Schema 定义。"""
    event_type: str
    version: int
    fields: Dict[str, type]
    created_at: datetime


@dataclass
class SchemaMigration:
    """从一个版本到下一个版本的迁移函数。"""
    from_version: int
    to_version: int
    migrate: Callable[[Dict], Dict]  # payload dict → payload dict


class SchemaRegistry:
    """
    Schema 注册表。
    管理所有 Event Type 的 Schema 版本和迁移链。

    职责：
      1. 注册 Schema 版本
      2. 注册迁移函数（v1→v2, v2→v3）
      3. 反序列化时自动迁移到最新版本
      4. 验证 Event 是否符合当前 Schema

    迁移原则：
      - 只新增字段（默认值=空字符串/0/None）
      - 不改字段名
      - 不删字段
      - 不改字段类型
    """

    def __init__(self):
        self._schemas: Dict[str, Dict[int, Schema]] = {}
        self._migrations: Dict[str, Dict[int, SchemaMigration]] = {}

    def register(self, event_type: str, version: int, schema: Schema):
        """注册一个 Schema 版本。"""
        if event_type not in self._schemas:
            self._schemas[event_type] = {}
        self._schemas[event_type][version] = schema

    def register_migration(self, event_type: str,
                           from_version: int, to_version: int,
                           migrate_fn: Callable[[Dict], Dict]):
        """注册从 from_version 到 to_version 的迁移函数。"""
        key = f"{event_type}"
        if key not in self._migrations:
            self._migrations[key] = {}
        self._migrations[key][from_version] = SchemaMigration(
            from_version=from_version,
            to_version=to_version,
            migrate=migrate_fn,
        )

    def latest_version(self, event_type: str) -> int:
        """获取 Event Type 的最新 Schema 版本。"""
        return max(self._schemas.get(event_type, {}).keys(), default=1)

    def deserialize(self, envelope: EventEnvelope) -> Any:
        """
        反序列化 EventEnvelope，自动迁移到最新版本。

        1. 从 payload 反序列化为 dict
        2. 如果 schema_version < latest_version：
           遍历 migration chain，逐步升级
        3. 返回完整对象
        """
        payload = self._deserialize_payload(envelope.payload)
        current_version = envelope.schema_version
        latest = self.latest_version(envelope.event_type)

        while current_version < latest:
            migration = self._migrations.get(envelope.event_type, {}).get(current_version)
            if not migration:
                raise SchemaMigrationError(
                    f"No migration from v{current_version} to v{current_version + 1} "
                    f"for {envelope.event_type}"
                )
            payload = migration.migrate(payload)
            current_version = migration.to_version

        return payload

    def serialize(self, event_type: str, payload: Dict,
                  producer: str, **metadata) -> EventEnvelope:
        """
        序列化为 EventEnvelope。
        自动使用最新的 schema_version。
        """
        version = self.latest_version(event_type)
        return EventEnvelope(
            schema_version=version,
            event_type=event_type,
            producer=producer,
            event_id=generate_uuid7(),
            payload=self._serialize_payload(payload),
            metadata=metadata,
        )

    def validate(self, envelope: EventEnvelope, payload: Dict) -> bool:
        """验证 payload 是否符合注册的 Schema。"""
        schema = self._schemas.get(envelope.event_type, {}).get(envelope.schema_version)
        if not schema:
            return False
        for field, field_type in schema.fields.items():
            if field not in payload:
                return False
            if not isinstance(payload[field], field_type):
                return False
        return True


# 全局实例
schema_registry = SchemaRegistry()
```

**Schema Evolution 示例：**

```python
# v1 NormalizedEvent（2026-07-01）
schema_registry.register("normalized.event", 1, Schema(
    event_type="normalized.event",
    version=1,
    fields={
        "event_id": str, "service_name": str, "message": str,
        "instance_uuid": str, "severity": str, "timestamp": str,
    },
))

# v2 NormalizedEvent（2027-01-01 — 新增 tenant_id）
schema_registry.register("normalized.event", 2, Schema(
    event_type="normalized.event",
    version=2,
    fields={
        "event_id": str, "service_name": str, "message": str,
        "instance_uuid": str, "severity": str, "timestamp": str,
        "tenant_id": str,  # NEW
    },
))

schema_registry.register_migration("normalized.event", 1, 2,
    migrate_fn=lambda p: {**p, "tenant_id": ""}  # v1→v2: 空字符串默认值
)

# Replay 时自动迁移：
#   envelope.schema_version=1 → SchemaRegistry.migrate(v1→v2) → 完整 v2 对象
```

### 2.4 Topic 结构（Domain-first 命名）

```
Topic 命名规范：{domain}.{event_type}

Domain                  | Topics
------------------------|------------------------------------------------
platform                | platform.raw, platform.normalized
platform.entity        | platform.entity, platform.state
platform.interaction   | platform.interaction
platform.graph         | platform.graph
platform.alert         | platform.alert
platform.workflow      | platform.workflow.command, platform.workflow.event
platform.system        | platform.system (platform-events)
```

```
Topic                      | 分区 key         | Retention | Schema
---------------------------|------------------|-----------|-------
platform.raw               | source           | 90d       | raw.event.v1
platform.normalized        | service_name     | 7d+90d    | normalized.event.v1
platform.entity            | entity_type:id   | 90d       | entity.seen.v1
platform.state             | entity_type:id   | 24h       | state.event.v1
platform.interaction       | source:target    | 90d       | interaction.recorded.v1
platform.graph             | entity_id        | 90d       | graph.update.v1
platform.alert             | severity         | 30d       | alert.triggered.v1
platform.workflow.command  | workflow_id      | 7d        | workflow.command.v1
platform.workflow.event    | workflow_id      | 90d       | workflow.event.v1
platform.system            | category         | 90d       | platform.event.v1
```

**好处：**
- 未来新增 `network.*`, `storage.*`, `security.*` 不需要改现 consumer
- 多集群时 `clusterA.platform.raw` vs `clusterB.platform.raw` 易区分
- Consumer group 允许通配符：`platform.*`

### 2.5 Projection 框架

```python
class Projection(ABC):
    """所有 Projection 的统一基类。"""

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    @abstractmethod
    def epoch(self) -> str:
        """
        projection_epoch — 表示此 Projection 从事件流的哪个位置重建。
        格式：YYYYMMDD。
        不同算法的 Projection 使用不同的 class 名（不同 epoch 可并行）。
        """
        ...

    @property
    def upstream_topics(self) -> List[str]:
        """此 Projection 消费的上游 topic 列表。用于重建顺序编排。"""
        return []

    @abstractmethod
    def apply(self, envelope: EventEnvelope):
        """增量更新：收到 EventEnvelope 时更新此 Projection。"""
        ...

    @abstractmethod
    def rebuild(self, event_source: EventSource, checkpoint: 'ProjectionCheckpoint'):
        """全量重建：从 Event Stream 重建整个 Projection。"""
        ...

    @abstractmethod
    def checkpoint(self) -> 'ProjectionCheckpoint':
        """返回当前 checkpoint。"""
        ...

    @abstractmethod
    def status(self) -> ProjectionStatus:
        ...


@dataclass
class ProjectionStatus:
    projection_epoch: str
    event_count: int
    checkpoint: 'ProjectionCheckpoint'
    is_rebuilding: bool = False
    rebuild_progress: float = 0.0
    lag: int = 0  # 总 lag（所有 partition 合计）
```

### 2.6 Projection Checkpoint（Offset-based）

**v8 核心变更。** 用 `topic + partition + offset` 取代 `last_event_id`。

```python
@dataclass
class PartitionOffset:
    """单个 Partition 的消费位置。"""
    topic: str
    partition: int
    offset: int


@dataclass
class ProjectionCheckpoint:
    """
    Projection 的消费进度。

    为什么用 Offset 不用 event_id：
      - Kafka replay 依赖 partition + offset
      - UUID7 按时间排序，Kafka partition 不一定连续（key-hash 可能乱序）
      - 无法精确计算 lag（consumer offset vs latest offset）

    为什么需要 records：
      - 一个 Projection 可能消费多个 topic
      - 每个 topic 有多个 partition
      - 写入存储时是事务性的——batch 写入后同时 checkpoint
    """
    projection: str
    epoch: str
    records: Dict[str, Dict[int, int]]  # topic → {partition: offset}
    updated_at: datetime

    def update(self, topic: str, partition: int, offset: int):
        if topic not in self.records:
            self.records[topic] = {}
        # 只更新 offset（Kafka offset 严格递增）
        if partition not in self.records[topic] or offset > self.records[topic][partition]:
            self.records[topic][partition] = offset
        self.updated_at = datetime.utcnow()

    def get_lag(self, topic: str, partition: int,
                latest_offset: int) -> int:
        """计算指定 partition 的 lag。"""
        current = self.records.get(topic, {}).get(partition, 0)
        return latest_offset - current

    def total_lag(self, topic_latest: Dict[str, Dict[int, int]]) -> int:
        """计算总 lag（所有 topic × partition 合计）。"""
        total = 0
        for topic, partitions in topic_latest.items():
            for partition, latest in partitions.items():
                total += self.get_lag(topic, partition, latest)
        return total
```

**Checkpoint 更新流程：**

```python
class CheckpointedProjection(Projection):
    """
    带 Checkpoint 的 Projection 基类。
    写入存储和 checkpoint 在同一事务中完成。
    """

    def process_batch(self, envelopes: List[EventEnvelope]):
        # 1. 消费一批 Event
        for env in envelopes:
            self.apply(env)

        # 2. 事务性写入存储
        with self.store.transaction():
            for env in envelopes:
                self._write_to_store(env)
            # 3. 在同一事务中更新 checkpoint
            for env in envelopes:
                for (topic, partition, offset) in self._parse_kafka_meta(env):
                    self._checkpoint.update(topic, partition, offset)

    @property
    def lag(self) -> int:
        """当前 lag——用于监控和告警。"""
        return self.checkpoint().total_lag(
            self._kafka.get_latest_offsets(self.upstream_topics)
        )
```

### 2.7 Projection Dependency Graph

（同 v7.1，仅更新 topic 名为 domain 格式）

```python
@dataclass
class ProjectionDependency:
    projection: str
    depends_on: List[str]
    produces: Optional[str]


PROJECTION_DEPENDENCIES = {
    "raw":         ProjectionDependency("raw",          [],                              None),
    "normalized":  ProjectionDependency("normalized",   ["platform.raw"],                "platform.normalized"),
    "inventory":   ProjectionDependency("inventory",    ["platform.normalized"],         "platform.entity"),
    "state":       ProjectionDependency("state",        ["platform.normalized"],         "platform.state"),
    "interaction": ProjectionDependency("interaction",  ["platform.normalized"],         "platform.interaction"),
    "graph":       ProjectionDependency("graph",        ["platform.entity",
                                                         "platform.interaction"],        None),
    "timeline":    ProjectionDependency("timeline",     ["platform.normalized"],         None),
    "dynamic_rel": ProjectionDependency("dynamic_rel",  ["platform.interaction"],        None),
}
```

### 2.8 组件职责

| 层 | 职责 | 不做什么 |
|----|------|----------|
| **Raw Event Store** | 原样保留原始日志，不可变 append-only | 不解析、不处理 |
| **Event Pipeline** | Aggregate / Dedup / Sample / Enrich / Route | 不做 Normalize |
| **Semantic Engine** | Raw → NormalizedEvent（经 Schema Registry） | 不聚合、不去重 |
| **Event Bus** | Multi-topic Event 分发（EventEnvelope） | 不涉及业务逻辑 |
| **Projectors** | 上层 Event → 下层 Event | 不构建存储 |
| **Projections** | 消费 Event → 写入持久化存储 | 不做推理 |
| **Context API** | 统一查询入口，隐藏所有存储 | 不持久化 |
| **Topology Engine** | 纯渲染，消费 Context API | 不查 Projection |
| **Inference Engine** | IncidentContext → Finding | 不生成 Workflow |
| **Planner** | Finding → Workflow（架构预留） | 不做推理 |
| **Workflow Engine** | Workflow → Capability → Action | 不做决策 |
| **Capability Registry** | 执行方式抽象（SSH / k8s / API） | 不做编排 |

---

## 3. Event Schema

### 3.1 EventEnvelope（所有 Event 的统一容器）

```python
@dataclass
class EventEnvelope:
    """
    所有 Event 的通用信封。

    序列化格式（protobuf / msgpack / JSON）由生产者和消费者协商。
    Payload 是序列化后的 bytes，具体格式由 schema_version 决定。
    """
    envelope_version: str = "v1"
    schema_version: int = 1
    event_type: str = ""
    producer: str = ""
    event_id: str = ""
    timestamp: datetime = field(default_factory=datetime.utcnow)
    payload: bytes = b""
    metadata: Dict[str, str] = field(default_factory=dict)
```

### 3.2 RawEvent

```python
# schema_version = 1, event_type = "raw.log"
@dataclass
class RawEvent:
    """真正的不可变 Source of Truth。"""
    raw_id: str                # UUID7
    timestamp: datetime
    source: str                # "fluentbit", "otel-collector"
    data_type: str             # "log", "metric", "trace"
    raw_payload: str           # 原始日志行 / JSON / protobuf（base64）
    content_type: str = "text/plain"
    host: str = ""
    cluster: str = ""
    namespace: str = ""
    pod_name: str = ""
    container_name: str = ""
    service_name: str = ""
    labels_json: str = ""
```

### 3.3 核心类型

```python
class ResourceType(Enum):
    INSTANCE = "INSTANCE"; VOLUME = "VOLUME"; PORT = "PORT"
    IMAGE = "IMAGE"; HOST = "HOST"; NETWORK = "NETWORK"
    POD = "POD"; NODE = "NODE"; PVC = "PVC"
    SERVICE = "SERVICE"; CONTAINER = "CONTAINER"; PROCESS = "PROCESS"
    SWITCH = "SWITCH"; ROUTER = "ROUTER"
    UNKNOWN = "UNKNOWN"


@dataclass
class ResourceIdentity:
    type: ResourceType
    id: str


@dataclass
class EventCategory:
    schema_version: str = "v1"
    category: str = ""
    domain: str = ""
    resource: str = ""
    action: str = ""
    phase: str = ""
    outcome: str = ""
```

### 3.4 NormalizedEvent

```python
# schema_version = 1 (will evolve), event_type = "normalized.event"
@dataclass
class NormalizedEvent:
    """
    第一层 Projection（从 RawEvent 经过 Semantic Engine 产生）。
    可通过 EventEnvelope.schema_version 追溯版本。
    """
    event_id: str              # UUID7
    raw_id: str                # 追溯回原始 RawEvent
    timestamp: datetime
    service_name: str
    pod_name: str = ""
    namespace: str = ""
    host: str = ""
    source_cluster: str = ""
    severity: str = ""
    message: str = ""
    pid: int = 0
    thread: str = ""

    event: EventCategory

    trace_id: str = ""
    span_id: str = ""
    request_id: str = ""
    global_request_id: str = ""

    entities: List[ResourceIdentity] = field(default_factory=list)
    participants: List['EventParticipant'] = field(default_factory=list)

    attributes_json: str = ""
    labels_json: str = ""

    # 加速列
    instance_uuid: str = ""
    volume_id: str = ""
    port_id: str = ""
    image_id: str = ""
    aggregate: str = ""
```

---

## 4. Event Bus

```python
class EventBus(ABC):
    """Multi-topic 事件总线。所有 Event 以 EventEnvelope 传输。"""

    TOPICS = {
        "platform.raw":              {"retention_days": 90, "partitions": 16},
        "platform.normalized":       {"retention_days": 7,  "partitions": 16},
        "platform.entity":           {"retention_days": 90, "partitions": 8},
        "platform.state":            {"retention_days": 1,  "partitions": 8},
        "platform.interaction":      {"retention_days": 90, "partitions": 16},
        "platform.graph":            {"retention_days": 90, "partitions": 8},
        "platform.alert":            {"retention_days": 30, "partitions": 4},
        "platform.workflow.command": {"retention_days": 7,  "partitions": 4},
        "platform.workflow.event":   {"retention_days": 90, "partitions": 4},
        "platform.system":           {"retention_days": 90, "partitions": 4},
    }

    @abstractmethod
    def publish(self, topic: str, envelope: EventEnvelope):
        ...

    @abstractmethod
    def subscribe(self, topic: str, group: str,
                   callback: Callable[[EventEnvelope], None]):
        ...

    @abstractmethod
    def latest_offsets(self, topic: str) -> Dict[int, int]:
        """返回 {partition: latest_offset}——用于计算 Projection lag。"""
        ...
```

---

## 5. Event Pipeline

### 5.1 定位

```
v7.1:  Semantic Engine = normalize + aggregate + dedup + enrich + route
v8:    Semantic Engine = 只做 normalize
       Event Pipeline = aggregate + dedup + sample + enrich + route
```

Event Pipeline 是 **RawEvent 进入 Semantic Engine 前的 Processor 链**。每个 Processor 可以过滤、聚合、变换 RawEvent。

**为什么需要 Pipeline：**

| 场景 | 在 Semantic Engine 做 | 在 Pipeline 做 |
|------|----------------------|----------------|
| Python Traceback 多行日志 | Semantic Engine 要处理状态机 | Aggregate Processor 合并为单行 |
| Nova ERROR x10000 | 产生 10000 个 NormalizedEvent | Dedup Processor → count=10000 |
| INFO 日志 x1M Semantic | Engine 处理 100 万条 | Sample Processor → 保留 1% |
| host→AZ→Rack 注入 | Semantic Engine 要查外部系统 | Enrich Processor 注入 |
| K8s vs OpenStack | 同一管道处理 | Route Processor 分发 |

### 5.2 Processor 接口

```python
class PipelineProcessor(ABC):
    """
    Pipeline Processor 基类。
    输入一个 RawEvent，输出 0 到 N 个 RawEvent。
    """

    @abstractmethod
    def process(self, raw: RawEvent) -> List[RawEvent]:
        ...
```

### 5.3 内置 Processor

```python
class AggregateProcessor(PipelineProcessor):
    """
    多行日志聚合。
    例如 Python Traceback、多行 JSON、多行日志堆栈。

    策略：
      - 按 trace_id / request_id / timestamp window 关联
      - 缓冲区 TTL 5s
      - 超时未匹配的独立行直接发出
    """

    def __init__(self, window_seconds: int = 5):
        self.window = window_seconds
        self._buffer: Dict[str, List[RawEvent]] = {}

    def process(self, raw: RawEvent) -> List[RawEvent]:
        key = self._aggregation_key(raw)
        if key:
            self._buffer.setdefault(key, []).append(raw)
            if self._is_complete(self._buffer[key]):
                batch = self._buffer.pop(key)
                return [self._merge(batch)]
            return []  # 还在等待后续行
        return [raw]

    def flush(self) -> List[RawEvent]:
        """超时 flush 未完成的 buffer。"""
        results = []
        for key, batch in list(self._buffer.items()):
            if self._is_expired(batch):
                results.extend(batch)
                del self._buffer[key]
        return results


class DedupProcessor(PipelineProcessor):
    """
    指数退避去重。
    相同错误模式连续出现时，聚合为 count=N，不产生 N 个重复事件。
    """

    def __init__(self, initial_window_ms: int = 1000,
                 max_window_ms: int = 60000):
        self._seen: Dict[str, DedupState] = {}

    def process(self, raw: RawEvent) -> List[RawEvent]:
        key = self._dedup_key(raw)
        state = self._seen.get(key)

        if state and (datetime.utcnow() - state.last_seen).total_seconds() * 1000 < state.window_ms:
            # 在去重窗口内——增加计数，不产生新事件
            state.count += 1
            state.last_seen = datetime.utcnow()
            state.window_ms = min(state.window_ms * 2, self.max_window_ms)
            return []
        else:
            # 新事件或窗口过期——发出现有计数
            if state and state.count > 1:
                raw.metadata["dedup_count"] = str(state.count)
                raw.metadata["dedup_key"] = key

            self._seen[key] = DedupState(
                count=1,
                window_ms=self.initial_window_ms,
                last_seen=datetime.utcnow(),
            )
            return [raw]


class SampleProcessor(PipelineProcessor):
    """
    采样。

    INFO     → 保留 1%
    WARNING  → 保留 10%
    ERROR    → 保留 100%
    CRITICAL → 保留 100%
    """

    def __init__(self, rates: Dict[str, float] = None):
        self.rates = rates or {
            "INFO": 0.01, "WARNING": 0.1,
            "ERROR": 1.0, "CRITICAL": 1.0,
        }

    def process(self, raw: RawEvent) -> List[RawEvent]:
        rate = self.rates.get(raw.labels_json.get("severity", "INFO"), 1.0)
        if random.random() < rate:
            return [raw]
        return []


class EnrichProcessor(PipelineProcessor):
    """
    上下文注入。

    支持：
      - host → AZ → Rack → IDC
      - IP → Hostname
      - Pod → Node → Cluster

    数据来源：静态映射 / Redis 缓存 / API 查询
    """

    def process(self, raw: RawEvent) -> List[RawEvent]:
        raw.labels_json = json.dumps({
            **json.loads(raw.labels_json or "{}"),
            "az": self._host_to_az(raw.host),
            "rack": self._host_to_rack(raw.host),
        })
        return [raw]


class RouteProcessor(PipelineProcessor):
    """
    路由——打标签，不实际分发。
    Semantic Engine 根据标签选择 Extractor。
    """

    def process(self, raw: RawEvent) -> List[RawEvent]:
        if "nova" in raw.service_name or "neutron" in raw.service_name:
            raw.metadata["platform"] = "openstack"
        elif "kube" in raw.service_name or "container" in raw.service_name:
            raw.metadata["platform"] = "kubernetes"
        return [raw]
```

### 5.4 Pipeline 架构

```python
class EventPipeline:
    """
    RawEvent 进入 Semantic Engine 前的 Processor 链。
    每个 Processor 可以过滤、聚合、变换 RawEvent。

    设计原则：
      - 无状态优先（Aggregate 除外）
      - 每个 Processor 职责单一
      - Processor 之间不共享状态
      - Pipeline 在 Semantic Engine 的 consumer 线程中执行
    """

    def __init__(self, processors: List[PipelineProcessor]):
        self.processors = processors

    def execute(self, raw: RawEvent) -> List[RawEvent]:
        """执行 Pipeline，返回 0 到 N 个处理后的 RawEvent。"""
        events = [raw]
        for processor in self.processors:
            events = [
                processed
                for event in events
                for processed in processor.process(event)
            ]
            if not events:
                return []  # 所有事件被过滤
        return events

    def periodic_flush(self) -> List[RawEvent]:
        """
        周期性 flush 有状态的 Processor（如 AggregateProcessor）。
        由外部定时器调用（如每 5 秒）。
        """
        flushed = []
        for processor in self.processors:
            if hasattr(processor, 'flush'):
                flushed.extend(processor.flush())
        return flushed
```

### 5.5 Pipeline 配置

```yaml
# pipeline_config.yaml
pipeline:
  processors:
    - aggregate:
        window_seconds: 5
    - dedup:
        initial_window_ms: 1000
        max_window_ms: 60000
    - sample:
        rates:
          INFO: 0.01
          WARNING: 0.10
          ERROR: 1.0
          CRITICAL: 1.0
    - enrich:
        host_map:
          source: "redis"
          prefix: "host:az:"
    - route: {}
```

---

## 6. Semantic Engine

### 6.1 架构变化

```
v7.1:  平台.raw → Semantic Engine（normalize + aggregate + dedup + enrich）
v8:    平台.raw → Event Pipeline → Semantic Engine（只做 normalize）

       Semantic Engine 输入已经是 EventEnvelope{raw.event, v1}
       Semantic Engine 输出 EventEnvelope{normalized.event, v1}
```

### 6.2 职责

```python
class SemanticEngine:
    """
    消费经过 Pipeline 处理的 RawEvent，产生 NormalizedEvent。
    全部通过 EventEnvelope 和 Schema Registry 管理。

    只做 normalize：提取事件主体、实体、时间、类别。
    不做：聚合、去重、采样、注入（这些已在 Pipeline 完成）。
    """

    def __init__(self, schema_registry: SchemaRegistry, bus: EventBus):
        self.schema_registry = schema_registry
        self.bus = bus

    def process(self, envelope: EventEnvelope) -> Optional[EventEnvelope]:
        """消费一个 EventEnvelope，产生 NormalizedEvent。"""
        # 1. Schema Registry 反序列化（自动迁移版本）
        raw_event = self.schema_registry.deserialize(envelope)

        # 2. 进行 Normalize
        normalized = self._normalize(raw_event)

        # 3. 序列化为 EventEnvelope（使用 Schema Registry）
        output = self.schema_registry.serialize(
            event_type="normalized.event",
            payload=normalized.__dict__,
            producer="semantic-engine",
            raw_id=raw_event.raw_id,
            trace_id=raw_event.trace_id,
        )
        self.bus.publish("platform.normalized", output)
        return output

    def _normalize(self, raw: RawEvent) -> NormalizedEvent:
        """核心 normalize 逻辑。同 v7，不变。"""
        ...
```

---

## 7. Projection Layer

### 7.1 命名规范

| 组件 | 角色 | 输入 topic | 输出 |
|------|------|-----------|------|
| EntityProjector | Projector | platform.normalized | platform.entity |
| StateProjector | Projector | platform.normalized | platform.state |
| InteractionProjector | Projector | platform.normalized | platform.interaction |
| InventoryProjection | Projection | platform.entity | Neo4j |
| StateProjection | Projection | platform.state | Redis TTL |
| GraphProjection | Projection | platform.entity + platform.interaction | Neo4j |
| DynamicRelProjection | Projection | platform.interaction | ClickHouse |
| TimelineProjection | Projection | platform.normalized | ClickHouse MV |

### 7.2 Projector 模式

```python
class Projector(ABC):
    """
    Projector = event → event 变换器。
    消费一个 topic 的 EventEnvelope，产出另一个 topic 的 EventEnvelope。
    """

    @abstractmethod
    def project(self, envelope: EventEnvelope) -> List[EventEnvelope]:
        ...


class EntityProjector(Projector):
    """
    消费 platform.normalized，产出 platform.entity。
    """

    def project(self, envelope: EventEnvelope) -> List[EventEnvelope]:
        normalized = schema_registry.deserialize(envelope)
        result = []
        for entity in normalized.entities:
            entity_envelope = schema_registry.serialize(
                event_type="entity.seen",
                payload={
                    "entity_type": entity.type.value,
                    "entity_id": entity.id,
                    "timestamp": normalized.timestamp.isoformat(),
                    "service": normalized.service_name,
                    "raw_event_id": normalized.event_id,
                },
                producer="entity-projector",
            )
            result.append(entity_envelope)
        return result
```

### 7.3 Inventory Projection（带 Checkpoint）

```python
class InventoryProjection(CheckpointedProjection):
    """
    从 platform.entity topic 构建。
    可全量重建：清空 Neo4j → 从 Event Stream Replay。
    Checkpoint 基于 partition + offset。
    """
    name = "inventory"
    epoch = "20260701"

    @property
    def upstream_topics(self) -> List[str]:
        return ["platform.entity"]

    def apply(self, envelope: EventEnvelope):
        entity = schema_registry.deserialize(envelope)
        # 写入 Neo4j
        self._neo4j.execute(
            "MERGE (e:Entity {id: $id}) SET e.type = $type, e.updated_at = $ts",
            {"id": entity["entity_id"], "type": entity["entity_type"],
             "ts": entity["timestamp"]},
        )

    def rebuild(self, event_source: EventSource, checkpoint: ProjectionCheckpoint):
        self._clear_all()
        for envelope in event_source.stream("platform.entity"):
            self.apply(envelope)
            checkpoint.update("platform.entity",
                              envelope.metadata["partition"],
                              int(envelope.metadata["offset"]))
```

### 7.4 State Projection

同 v7.1，仅 topic 名更新为 `platform.state`。

### 7.5 Graph Projection（只存拓扑）

同 v7.1，仅 topic 名更新为 `platform.entity` + `platform.interaction`。不存状态。

### 7.6 Timeline Projection（ClickHouse MV）

同 v7.1，仅 topic 更新为 `platform.normalized`。

### 7.7 DynamicRel Projection（ClickHouse，Offset-based Checkpoint）

同 v7.1（ClickHouse 存储，支持时间窗口），增加 offset-based checkpoint。

```python
class DynamicRelProjection(CheckpointedProjection):
    name = "dynamic_rel"
    epoch = "20260701"

    @property
    def upstream_topics(self) -> List[str]:
        return ["platform.interaction"]

    def apply(self, envelope: EventEnvelope):
        interaction = schema_registry.deserialize(envelope)
        self.clickhouse.execute("""
            INSERT INTO dynamic_relationships
            (source, target, interaction_type, timestamp, confidence, request_id)
            VALUES
            (%(source)s, %(target)s, %(type)s, %(ts)s, %(conf)s, %(req)s)
        """, { ... })
```

---

## 8. Correlation Engine

同 v7.1，仅 topic 名更新。InteractionProjector 输出到 `platform.interaction`。

---

## 9. Unified Context API

### 9.1 定位

**v8 核心变更。** 所有外部代码通过 `ContextAPI` 查询——不直接访问任何 Projection。

```
v7.1:  Topology → Graph Projection
       Workflow → Context Builder
       Rule     → State Projection

v8:    Topology → ContextAPI.build(type=topology)
       Workflow → ContextAPI.build(type=workflow)
       Rule     → ContextAPI.build(type=rule)
       AI       → ContextAPI.build(type=incident)
```

**好处：**
- 存储实现可以替换（Neo4j → DGraph → 内存），不影响消费者
- 可以添加全局缓存/限流/审计
- 统一的 ContextType 系统保证接口一致
- Context Snapshot 对所有 ContextType 统一可用

### 9.2 ContextType

```python
class ContextType(Enum):
    """查询的上下文类型。"""
    INCIDENT = "incident"    # AI/Inference Engine
    TOPOLOGY = "topology"    # Topology Engine
    WORKFLOW = "workflow"    # Workflow Engine
    RULE     = "rule"        # Rule Engine


@dataclass
class ContextResult:
    """
    统一查询结果。
    所有 ContextType 共享此结构，context 字段由具体 Builder 填充。
    """
    context_type: ContextType
    resource_type: ResourceType
    resource_id: str
    context: Any                    # IncidentContext / TopologyContext / ...
    snapshot_id: str = ""           # 如果有 snapshot
    created_at: datetime = field(default_factory=datetime.utcnow)
```

### 9.3 ContextAPI（唯一入口）

```python
class ContextAPI:
    """
    统一 Context 查询入口。

    设计原则：
      - 外部所有查询通过此 API——不直接访问 Projection
      - 调用方不需要知道 Neo4j/Redis/ClickHouse 的存在
      - 内部 Builder 可以根据 ContextType 自定义组装逻辑
      - Snapshot 机制对所有 ContextType 统一可用
    """

    def __init__(self, inventory: InventoryProjection,
                 state: StateProjection, graph: GraphProjection,
                 timeline: TimelineProjection, dynamic_rel: DynamicRelProjection,
                 alert_store: AlertStore, cache: Cache):
        self._incident_builder = IncidentContextBuilder(
            inventory, state, graph, timeline, dynamic_rel, alert_store, cache)
        self._topology_builder = TopologyContextBuilder(graph)
        self._workflow_builder = WorkflowContextBuilder(
            inventory, state, dynamic_rel, cache)
        self._rule_builder = RuleContextBuilder(
            state, inventory, timeline, cache)

    def build(self, resource_type: ResourceType, resource_id: str,
              context_type: ContextType = ContextType.INCIDENT,
              time_window: str = "1 HOUR",
              use_snapshot: bool = True) -> ContextResult:
        """
        唯一查询入口。

        参数：
          resource_type: ResourceType.INSTANCE
          resource_id: "abc-123"
          context_type: 查询类型（决定返回的数据范围）
          time_window: 时间窗口
          use_snapshot: 是否创建快照（保证后续查询数据一致）

        返回：
          ContextResult{.context_type, .context, .snapshot_id}
        """
        builder = self._get_builder(context_type)
        ctx = builder.build(resource_type, resource_id, time_window)

        snapshot_id = ""
        if use_snapshot and hasattr(builder, 'create_snapshot'):
            snapshot = builder.create_snapshot(ctx)
            snapshot_id = snapshot.snapshot_id

        return ContextResult(
            context_type=context_type,
            resource_type=resource_type,
            resource_id=resource_id,
            context=ctx,
            snapshot_id=snapshot_id,
        )

    def get_snapshot(self, snapshot_id: str) -> Optional[ContextResult]:
        """获取已创建的快照（任何 ContextType）。"""
        snapshot = self._cache.get(f"snapshot:{snapshot_id}")
        if snapshot:
            return pickle.loads(snapshot)
        return None

    def _get_builder(self, context_type: ContextType):
        builders = {
            ContextType.INCIDENT: self._incident_builder,
            ContextType.TOPOLOGY: self._topology_builder,
            ContextType.WORKFLOW: self._workflow_builder,
            ContextType.RULE: self._rule_builder,
        }
        return builders[context_type]
```

### 9.4 ContextType 详解

```python
@dataclass
class IncidentContext:
    """AI / Inference Engine 的上下文。"""
    resource_type: ResourceType
    resource_id: str
    resource_attributes: Dict[str, str]
    neighbors: List[Dict]
    current_state: Dict[str, str]
    timeline: List[Dict]
    relationships: List[Dict]
    recent_alerts: List[Dict]
    context_id: str
    snapshot_id: str
    created_at: datetime


@dataclass
class TopologyContext:
    """Topology Engine 的上下文——只含拓扑结构。"""
    resource_type: ResourceType
    resource_id: str
    nodes: List[Dict]                    # 节点列表
    edges: List[Dict]                    # 边列表
    depth: int = 2
    # 不包含状态信息——状态由前端通过 State Projection API 查询


@dataclass
class WorkflowContext:
    """Workflow Engine 的上下文——包含执行所需的信息。"""
    resource_type: ResourceType
    resource_id: str
    resource_attributes: Dict[str, str]
    current_state: Dict[str, str]
    available_capabilities: List[str]    # 可用的 Capability
    snapshot_id: str


@dataclass
class RuleContext:
    """Rule Engine 的上下文——轻量级，高频查询。"""
    resource_type: ResourceType
    resource_id: str
    current_state: Dict[str, str]
    recent_events: List[Dict]            # 最近事件摘要
    previous_evaluations: List[Dict]     # 前几次规则评估结果
```

### 9.5 使用示例

```python
# AI Engine ——不知道 ClickHouse，不知道 Neo4j
api = ContextAPI(...)
result = api.build(
    resource_type=ResourceType.INSTANCE,
    resource_id="abc-123",
    context_type=ContextType.INCIDENT,
    use_snapshot=True,
)
ctx: IncidentContext = result.context
# AI 推理在此上下文中进行

# Topology Engine ——不知道 Graph Projection
result = api.build(
    resource_type=ResourceType.INSTANCE,
    resource_id="abc-123",
    context_type=ContextType.TOPOLOGY,
)
topo: TopologyContext = result.context
```

---

## 10. Inference Engine

### 10.1 输入输出

```python
@dataclass
class InferenceInput:
    context: IncidentContext
    query: str = ""
```

### 10.2 Finding（统一输出结构）

同 v7.1。

```python
@dataclass
class Finding:
    id: str
    severity: str                     # "critical", "warning", "info"
    confidence: float                 # 0.0 ~ 1.0
    category: str                     # "anomaly", "dependency", "performance"
    reason: str                       # 人类可读的描述
    supporting_events: List[str]
    affected_entities: List[ResourceIdentity]
    recommended_action: str
    engine_type: str                  # "rule", "llm", "ml"
    created_at: datetime
```

### 10.3 Inference Engine

```python
class InferenceEngine(ABC):
    @abstractmethod
    def infer(self, input: InferenceInput) -> List[Finding]:
        ...


class LLMInferenceEngine(InferenceEngine):
    def infer(self, input: InferenceInput) -> List[Finding]:
        # 消费 input.context（IncidentContext，来自 snapshot）
        # 输出 Finding 列表
        ...


class RuleInferenceEngine(InferenceEngine):
    def __init__(self):
        self.rules: List[Rule] = []

    def infer(self, input: InferenceInput) -> List[Finding]:
        findings = []
        for rule in self.rules:
            if rule.matches(input.context):
                findings.append(rule.to_finding(input.context))
        return findings
```

---

## 11. Topology Engine

```python
class TopologyEngine:
    """
    纯渲染引擎。
    输入：ContextAPI.build(type=TOPOLOGY) — 只含节点的拓扑身份，不含状态
    输出：Layout + Render

    为什么 Topology Engine 通过 Context API 查询：
      - 外部代码不直接访问 Graph Projection
      - Context API 返回的 TopologyContext 已经包含了所有拓扑信息
      - 未来 Graph Projection 从 Neo4j 切换到 DGraph，Topology Engine 不需要改
    """

    def render(self, entity_id: str, depth: int = 2) -> TopologyResult:
        ctx = self.context_api.build(
            resource_type=ResourceType.UNKNOWN,
            resource_id=entity_id,
            context_type=ContextType.TOPOLOGY,
            use_snapshot=False,
        )
        topo: TopologyContext = ctx.context
        layout = self._compute_layout(topo.nodes, topo.edges)
        return TopologyResult(
            nodes=topo.nodes,
            edges=topo.edges,
            layout=layout,
        )

    def _compute_layout(self, nodes, edges):
        # 力导向布局 / 层次布局
        ...
```

---

## 12. Planner（架构预留）

### 12.1 定位

```
v7.1:   Inference → Workflow（直接映射）
v8:     Inference → Finding → Planner → Workflow

Planner 是 Inference 和 Workflow 之间的解耦层。
AI 负责"诊断"（产 Finding），Planner 负责"开药"（生成 Workflow）。
```

### 12.2 架构

```python
class Planner:
    """
    Architecture Reserve（Phase 5+）。

    v1 实现：Finding.recommended_action → 1:1 映射到 Workflow
    v2 实现：LLM Planner（根据 IncidentContext + Finding 生成多步 Workflow）
    v3 实现：Rule-based DSL Planner（预定义的 Playbook）
    """

    v1_ACTION_MAP = {
        "restart_service": Workflow(
            steps=[WorkflowStep("ssh", "{host}", "systemctl restart {service}")],
        ),
        "describe_pod": Workflow(
            steps=[WorkflowStep("kubectl", "", "describe pod {pod_name}")],
        ),
    }

    def plan(self, finding: Finding, context: IncidentContext) -> Workflow:
        """将 Finding 映射为可执行的 Workflow。"""
        action = finding.recommended_action

        # v1: 简单映射
        if action in self.v1_ACTION_MAP:
            wf = copy.deepcopy(self.v1_ACTION_MAP[action])
            wf.workflow_id = uuid4().hex
            wf.name = action
            wf.trigger = "inference"
            # 用 context 中的信息填充占位符
            wf = self._interpolate(wf, context)
            return wf

        # v2: LLM Planner（预留）
        # v3: DSL Playbook（预留）
        raise PlannerError(f"No workflow mapping for action: {action}")
```

---

## 13. Workflow Engine

### 13.1 Capability 抽象

**v8 核心变更。** Workflow 不直接依赖 SSH/kubectl/API，通过 Capability 执行。

```
v7.1:  WorkflowStep(step_type="ssh", target="compute-01", command="systemctl ...")
v8:    WorkflowStep(capability="ssh.execute", params={"host": "compute-01", "command": "..."})
```

```python
@dataclass
class Capability:
    """执行能力注册。"""
    capability_id: str
    name: str                              # "ssh.execute_command"
    provider: str                          # "ssh-executor", "k8s-executor"
    parameters: Dict[str, ParameterDef]
    output_type: type = str
    timeout_seconds: int = 30
    retry_count: int = 0

    @property
    def short_name(self) -> str:
        return self.name.split(".")[-1]    # "ssh.execute_command" → "execute_command"


@dataclass
class ParameterDef:
    name: str
    type: type
    required: bool = True
    default: Any = None
    description: str = ""


class CapabilityRegistry:
    """
    全局 Capability 注册表。
    Workflow 通过名称查找 Capability，不关心具体实现。
    """

    def __init__(self):
        self._capabilities: Dict[str, Capability] = {}

    def register(self, capability: Capability):
        self._capabilities[capability.capability_id] = capability

    def get(self, name: str) -> Optional[Capability]:
        return self._capabilities.get(name)

    def execute(self, capability_id: str,
                params: Dict[str, Any]) -> CapabilityResult:
        """按名称执行，不关心实际执行方式。"""
        cap = self.get(capability_id)
        if not cap:
            raise CapabilityNotFoundError(capability_id)
        return self._providers[cap.provider].execute(cap, params)


# 注册示例
registry = CapabilityRegistry()
registry.register(Capability(
    capability_id="ssh.execute_command",
    name="ssh.execute_command",
    provider="ssh-executor",
    parameters={
        "host": ParameterDef("host", str, True),
        "command": ParameterDef("command", str, True),
    },
))
registry.register(Capability(
    capability_id="k8s.describe_pod",
    name="k8s.describe_pod",
    provider="k8s-executor",
    parameters={
        "namespace": ParameterDef("namespace", str, True),
        "pod": ParameterDef("pod", str, True),
    },
))
```

### 13.2 Workflow 定义

```python
@dataclass
class WorkflowCommand:
    """Command = 意图（"我希望发生"）。"""
    command_id: str
    command_type: str          # "restart_service", "describe_pod"
    target: str
    params: Dict[str, str]
    created_at: datetime
    source_workflow_id: str = ""


@dataclass
class WorkflowEvent:
    """Event = 事实（"已经发生"）。"""
    event_id: str
    event_type: str            # "service_restarted", "action_failed"
    command_id: str
    workflow_id: str
    outcome: str               # "success", "failure"
    details: Dict[str, str]
    timestamp: datetime


@dataclass
class WorkflowStep:
    """Workflow 步骤——通过 Capability 执行，不直接写 SSH。"""
    capability: str             # "ssh.execute_command"
    params: Dict[str, str]     # 参数
    timeout_seconds: int = 30
    retry_count: int = 0
    on_failure: str = "abort"  # "abort", "skip", "retry"


@dataclass
class Workflow:
    workflow_id: str
    name: str
    steps: List[WorkflowStep]
    trigger: str = ""           # "alert", "inference", "manual"
```

### 13.3 Workflow Engine

```python
class WorkflowEngine:
    """
    执行 Workflow。

    流程：
      Planner / API → WorkflowCommand → Engine → Capability → Action → WorkflowEvent
    """

    def __init__(self, bus: EventBus, registry: CapabilityRegistry):
        self.bus = bus
        self.registry = registry

    def execute(self, workflow: Workflow,
                context: WorkflowContext) -> WorkflowEvent:
        # 1. 发布 Command
        cmd = WorkflowCommand(
            command_id=uuid4().hex,
            command_type=workflow.name,
            target=context.resource_id,
            params={},
            source_workflow_id=workflow.workflow_id,
        )
        self.bus.publish("platform.workflow.command",
            schema_registry.serialize("workflow.command", asdict(cmd),
                                       producer="workflow-engine"))

        # 2. 执行每个步骤（通过 Capability）
        results = []
        for step in workflow.steps:
            result = self.registry.execute(step.capability, step.params)
            results.append(result)
            if not result.success and step.on_failure == "abort":
                break

        # 3. 发布 Event（事实）
        outcome = "success" if all(r.success for r in results) else "failure"
        event = WorkflowEvent(
            event_id=uuid4().hex,
            event_type=f"{workflow.name}.completed",
            command_id=cmd.command_id,
            workflow_id=workflow.workflow_id,
            outcome=outcome,
            details={"steps": len(results), "successful": sum(1 for r in results if r.success)},
            timestamp=datetime.utcnow(),
        )
        self.bus.publish("platform.workflow.event",
            schema_registry.serialize("workflow.event", asdict(event),
                                       producer="workflow-engine"))

        # 4. Platform Event
        self._publish_platform_event("workflow_completed",
            workflow.workflow_id, outcome)

        return event
```

---

## 14. Platform Events

同 v7.1，仅 topic 名更新为 `platform.system`。所有平台自身事件（Projection 重建、Inference 开始/完成、Workflow 完成、Snapshot 创建）发布到此 topic。

---

## 15. 置信度模型

（同 v7，不变）

```
base = 0.3
resource_match:  +0.45
request_match:   global +0.35, local +0.20
time_window:     <0.5s +0.10, <0.1s +0.15
message_match:   +0.20
static_relation: +0.30

max = min(base, 0.98)
time_decay: >120min → 0.5^(min/120)
final = max * decay
```

---

## 16. API

```text
# Context API（v8 统一查询入口——所有内部查询走此 API）
GET /api/v1/context/build?type=INSTANCE&id=abc-123&context=incident&time_window=1+HOUR
GET /api/v1/context/build?type=INSTANCE&id=abc-123&context=topology&depth=2
GET /api/v1/context/build?type=INSTANCE&id=abc-123&context=workflow
GET /api/v1/context/build?type=INSTANCE&id=abc-123&context=rule
GET /api/v1/context/snapshot/{snapshot_id}

# Topology（消费 Context API）
GET /api/v1/topology/hybrid?time_window=1+HOUR

# Interaction
GET /api/v1/interactions?source=Nova&target=Neutron&window=1+HOUR

# Inventory Projection
GET /api/v1/inventory/entities?type=INSTANCE&id=abc-123
GET /api/v1/inventory/entities/history?type=INSTANCE&id=abc-123

# State Projection
GET /api/v1/state/current?type=INSTANCE&id=abc-123

# Correlation
GET /api/v1/correlate/services?source=Nova&window=6+HOUR
GET /api/v1/correlate/trend?source=Nova&target=Neutron

# Capability
GET /api/v1/capabilities/platforms
GET /api/v1/capabilities/extractors?resource_type=INSTANCE
GET /api/v1/capabilities/executors    # ← v8 新增

# Projection Management
GET /api/v1/projections/status
POST /api/v1/projections/rebuild?name=inventory
GET /api/v1/projections/dependencies
POST /api/v1/projections/rebuild-chain?name=graph
GET /api/v1/projections/{name}/lag    # ← v8 新增（offset-based lag）

# Schema Registry（v8 新增）
GET  /api/v1/schemas?event_type=normalized.event
POST /api/v1/schemas/register
POST /api/v1/schemas/migrate?from=1&to=2

# Platform Events
GET /api/v1/platform/events?category=projection&limit=100
```

---

## 17. 实施阶段

### Phase 0: Foundation（~2 周）

| 模块 | 内容 |
|------|------|
| Raw Event Store | 本地 WAL + Kafka `platform.raw`，EventEnvelope 封装 |
| Schema Registry | Schema 注册、版本管理、迁移链 |
| EventEnvelope | 统一 Event 容器（schema_version, producer, event_type） |
| Event Bus | 10 topics，domain-first 命名 |
| event_id / raw_id | UUID7 |

### Phase 1: Event Pipeline + Semantic Engine（~1.5 周）

| 模块 | 内容 |
|------|------|
| Event Pipeline | AggregateProcessor, DedupProcessor, SampleProcessor |
| EnrichProcessor | host→AZ→Rack 注入 |
| Semantic Engine | 消费 pipeline 处理后的 RawEvent，只做 normalize |
| Schema Registry 集成 | NormalizedEvent 通过 Envelope 输出 |

### Phase 2: Projection Framework + Inventory/State（~2 周）

| 模块 | 内容 |
|------|------|
| Projection Checkpoint | Partition + Offset 持久化，lag 计算 |
| Projection Base | CheckpointedProjection 抽象类 |
| EntityProjector | `platform.normalized` → `platform.entity` |
| InventoryProjection | `platform.entity` → Neo4j（epoch 化） |
| StateProjection | `platform.normalized` → `platform.state` → Redis TTL |
| Identity Resolution | Alias Registry |
| Timeline MV | ClickHouse Materialized View |

### Phase 3: Interaction + Correlation（~2 周）

| 模块 | 内容 |
|------|------|
| InteractionProjector | `platform.normalized` → `platform.interaction` |
| Correlation Engine | 聚合端点对 → DynamicRel Projection |
| DynamicRel Projection | ClickHouse 存储，offset checkpoint，多窗口查询 |

### Phase 4: Graph + Context API（~2 周）

| 模块 | 内容 |
|------|------|
| Graph Projection | `platform.entity` + `platform.interaction` → Neo4j（只存拓扑） |
| **Context API（v8 核心）** | 4 种 ContextType：incident / topology / workflow / rule |
| Context Snapshot | TTL 10min，统一 snapshot 支持 |
| Platform Events | `platform.system` topic |

### Phase 5: Topology + Inference + Planner（~1.5 周）

| 模块 | 内容 |
|------|------|
| Topology Engine | 通过 Context API 查询，纯渲染 |
| Inference Engine | LLM + Rule，Finding 统一输出 |
| **Planner（v8 新增）** | Finding → Workflow 映射（v1 简单映射） |
| Capability Registry | SSH + kubectl 注册 |

### Phase 6: Workflow Engine + Multi-platform（~2 周）

| 模块 | 内容 |
|------|------|
| Workflow Engine | Command/Event 分离，Capability 抽象 |
| K8s Extractor | Capability 注册 |
| VMware Extractor | Capability 注册 |

---

## 18. 测试策略

```python
def test_event_envelope_schema_version():
    """EventEnvelope 携带 schema_version，支持版本迁移"""
    env = EventEnvelope(
        schema_version=1,
        event_type="normalized.event",
        producer="semantic-engine",
        payload=b"{}",
    )
    assert env.schema_version == 1


def test_schema_migration():
    """Schema Registry 自动迁移 v1→v2"""
    registry = SchemaRegistry()
    registry.register("normalized.event", 1, Schema(
        event_type="normalized.event", version=1,
        fields={"event_id": str, "message": str},
    ))
    registry.register("normalized.event", 2, Schema(
        event_type="normalized.event", version=2,
        fields={"event_id": str, "message": str, "tenant_id": str},
    ))
    registry.register_migration("normalized.event", 1, 2,
        migrate_fn=lambda p: {**p, "tenant_id": ""})

    env = EventEnvelope(schema_version=1, event_type="normalized.event",
                        payload=json.dumps({"event_id": "e1", "message": "test"}).encode())
    payload = registry.deserialize(env)
    assert "tenant_id" in payload  # v1 → v2 迁移自动补全


def test_event_pipeline_aggregate():
    """Event Pipeline 聚合多行日志"""
    pipeline = EventPipeline([AggregateProcessor(window_seconds=5)])
    line1 = RawEvent(raw_payload="Traceback (most recent call last):")
    line2 = RawEvent(raw_payload="  File ...")
    line3 = RawEvent(raw_payload="Exception: OOM")
    assert len(pipeline.execute(line1)) == 0   # buffer
    assert len(pipeline.execute(line2)) == 0   # buffer
    assert len(pipeline.execute(line3)) == 1   # 完整 traceback → 聚合


def test_event_pipeline_dedup():
    """Event Pipeline 去重相同错误"""
    pipeline = EventPipeline([DedupProcessor(initial_window_ms=5000)])
    err = RawEvent(raw_payload="nova-api: ERROR Connection refused")
    assert len(pipeline.execute(err)) == 1     # 第一个 → 发出
    assert len(pipeline.execute(err)) == 0     # 重复 → 聚合
    assert len(pipeline.execute(err)) == 0     # 重复 → 聚合


def test_projecton_checkpoint_offset():
    """Projection Checkpoint 基于 partition+offset"""
    cp = ProjectionCheckpoint(
        projection="inventory", epoch="20260701",
        records={},
    )
    cp.update("platform.entity", 0, 100)
    cp.update("platform.entity", 1, 200)
    assert cp.records["platform.entity"][0] == 100
    assert cp.get_lag("platform.entity", 0, 150) == 50

    # offset 严格递增——不会回退
    cp.update("platform.entity", 0, 90)  # 小于当前 100
    assert cp.records["platform.entity"][0] == 100  # 保持不变


def test_context_api_hides_storage():
    """Context API 隐藏所有存储实现——调用方不知道 Neo4j/Redis/ClickHouse"""
    api = ContextAPI(...)
    result = api.build(ResourceType.INSTANCE, "abc-123",
                       context_type=ContextType.INCIDENT)
    # result.context 不包含任何存储相关的字段
    assert not hasattr(result.context, "neo4j_query")
    assert not hasattr(result.context, "clickhouse_sql")


def test_context_api_multiple_types():
    """Context API 支持多种 ContextType"""
    api = ContextAPI(...)
    incident = api.build(ResourceType.INSTANCE, "abc-123",
                         context_type=ContextType.INCIDENT)
    topo = api.build(ResourceType.INSTANCE, "abc-123",
                     context_type=ContextType.TOPOLOGY)
    assert isinstance(incident.context, IncidentContext)
    assert isinstance(topo.context, TopologyContext)
    # TopologyContext 不包含状态
    assert not hasattr(topo.context, "current_state")


def test_capability_abstraction():
    """Workflow 通过 Capability 执行，不直接依赖 SSH"""
    registry = CapabilityRegistry()
    registry.register(Capability(
        capability_id="ssh.execute_command",
        name="ssh.execute_command",
        provider="mock-ssh",
        parameters={"host": str, "command": str},
    ))
    # Workflow 不知道 SSH——只知道 Capability ID
    step = WorkflowStep(capability="ssh.execute_command",
                        params={"host": "compute-01", "command": "uptime"})
    assert step.capability == "ssh.execute_command"
    assert "ssh" not in str(type(step))  # 不直接引用 SSH


def test_capability_workflow_integration():
    """Workflow Engine 通过 Capability 执行步骤"""
    engine = WorkflowEngine(bus, registry)
    wf = Workflow(
        steps=[WorkflowStep(capability="ssh.execute_command",
                            params={"host": "compute-01", "command": "uptime"})],
    )
    ctx = WorkflowContext(resource_type=ResourceType.HOST, resource_id="compute-01")
    event = engine.execute(wf, ctx)
    assert event.outcome in ("success", "failure")
    assert event.command_id != ""


def test_planner_reserve():
    """Planner 可以预留——v1 简单映射，v2+ 可扩展"""
    planner = Planner()
    finding = Finding(
        severity="warning", confidence=0.85,
        category="dependency",
        reason="Neutron agent 无响应",
        recommended_action="restart_service",
        affected_entities=[ResourceIdentity(ResourceType.HOST, "compute-01")],
    )
    context = IncidentContext(resource_type=ResourceType.HOST, resource_id="compute-01")
    wf = planner.plan(finding, context)
    assert wf is not None
    assert len(wf.steps) > 0


def test_raw_event_is_source_of_truth():
    """Raw Event（EventEnvelope）是真正的 Source of Truth"""
    raw = RawEvent(raw_payload="nova-api: Failed to allocate PCI device")
    envelope = schema_registry.serialize(
        event_type="raw.log",
        payload=asdict(raw),
        producer="ingest",
    )
    assert envelope.event_type == "raw.log"
    assert envelope.schema_version >= 1
    # Raw Event 原样保留
    deserialized = schema_registry.deserialize(envelope)
    assert deserialized["raw_payload"] == "nova-api: Failed to allocate PCI device"


def test_normalized_event_from_raw():
    """NormalizedEvent 可从 raw-events 重建"""
    raw = RawEvent(raw_id="raw-001", raw_payload="nova-api: ...")
    raw_env = schema_registry.serialize("raw.log", asdict(raw), "ingest")
    engine = SemanticEngine(schema_registry, bus)
    norm_env = engine.process(raw_env)
    assert norm_env.event_type == "normalized.event"

    # 模拟升级 Semantic Engine
    engine_v2 = SemanticEngine(schema_registry, bus, version="v2")
    re_normalized = engine_v2.process(raw_env)
    assert re_normalized is not None  # 可以重新解析


def test_graph_no_state():
    """Graph Projection 不存状态"""
    graph = GraphProjection()
    graph.apply_entity(entity_envelope)
    graph.apply_interaction(interaction_envelope)
    subgraph = graph.get_subgraph("INSTANCE:abc-123")
    assert all(hasattr(n, "entity_id") for n in subgraph.nodes)
    assert not any("status" in n.attributes for n in subgraph.nodes)


def test_projection_epoch():
    """Projection Epoch 表示重建时间点"""
    inv = InventoryProjection(epoch="20260701")
    assert inv.epoch == "20260701"


def test_context_snapshot():
    """Context Snapshot 保证推理期间数据一致"""
    api = ContextAPI(...)
    result = api.build(ResourceType.INSTANCE, "abc-123",
                       context_type=ContextType.INCIDENT, use_snapshot=True)
    assert result.snapshot_id != ""

    # AI 使用 snapshot
    snapshot = api.get_snapshot(result.snapshot_id)
    assert snapshot is not None


def test_finding_unified_output():
    """所有 Inference Engine 输出 Finding 结构"""
    rule_engine = RuleInferenceEngine()
    llm_engine = LLMInferenceEngine()

    findings_rule = rule_engine.infer(InferenceInput(context=ctx))
    findings_llm = llm_engine.infer(InferenceInput(context=ctx))

    for finding in findings_rule + findings_llm:
        assert hasattr(finding, "severity")
        assert hasattr(finding, "confidence")
        assert hasattr(finding, "reason")
        assert hasattr(finding, "engine_type")


def test_workflow_command_event_separation():
    """Command 和 Event 使用不同 topic"""
    engine = WorkflowEngine(bus, registry)
    wf = Workflow(steps=[WorkflowStep(capability="mock.echo", params={"msg": "hello"})])
    event = engine.execute(wf, WorkflowContext(
        resource_type=ResourceType.HOST, resource_id="test"))
    assert event.outcome in ("success", "failure")
    assert event.command_id != ""


def test_platform_event_published():
    """平台自身事件发布到 platform.system topic"""
    collected = []
    bus.subscribe("platform.system", "test", lambda e: collected.append(e))
    api = ContextAPI(...)
    api.build(ResourceType.INSTANCE, "abc-123", context_type=ContextType.INCIDENT)
    assert any("snapshot_created" in str(e) for e in collected)


def test_projection_dependency_graph():
    """Projection 依赖图正确排序"""
    orchestrator = RebuildOrchestrator(PROJECTION_DEPENDENCIES)
    order = orchestrator._topological_sort()
    assert order.index("raw") < order.index("normalized")
    assert order.index("normalized") < order.index("inventory")
    assert order.index("normalized") < order.index("state")
    assert order.index("normalized") < order.index("interaction")
    assert order.index("inventory") < order.index("graph")
    assert order.index("interaction") < order.index("graph")


def test_dynamic_rel_time_window():
    """DynamicRel 支持多时间窗口查询"""
    rel_projection = DynamicRelProjection()
    for i in range(100):
        rel_projection.apply(interaction_envelope)
    trend = rel_projection.query_trend("A", "B",
                windows=["1 HOUR", "6 HOUR", "24 HOUR"])
    assert len(trend) == 3


def test_event_sourcing_rebuild():
    """Projection 可以从 Event Stream 重建"""
    events = [...]
    for e in events:
        bus.publish("platform.normalized", e)

    inventory.apply(events)
    assert inventory.query(ResourceType.INSTANCE, "abc") is not None

    inventory._clear_all()
    assert inventory.query(ResourceType.INSTANCE, "abc") is None

    inventory.rebuild(ReplayEventSource(events), checkpoint)
    assert inventory.query(ResourceType.INSTANCE, "abc") is not None


def test_multi_topic_independence():
    """不同 topic 的 Projection 独立重建"""
    inventory.rebuild(event_source, checkpoint)
    assert inventory.query(...) is not None
    assert state.query(...) is not None


def test_topology_via_context_api():
    """Topology Engine 通过 Context API 查询，不直接查 Projection"""
    api = FakeContextAPI()
    engine = TopologyEngine(api)
    result = engine.render("entity-001")
    assert len(result.nodes) > 0
    # 验证 Topology Engine 没有直接访问 Graph Projection
    assert not hasattr(engine, "graph_projection")


def test_state_ttl_expiry():
    """State Projection TTL 自动过期"""
    state.apply(state_event(key="attached", value="true", ttl=60))
    assert state.query(ResourceType.VOLUME, "vol-1", "attached") == "true"
    with freeze_time(now + timedelta(seconds=70)):
        assert state.query(ResourceType.VOLUME, "vol-1", "attached") is None
```

---

## 19. 性能考量

| 场景 | 预期 | 瓶颈 | 扩展方式 |
|------|------|------|----------|
| Raw Event 写入 | 100K events/s | WAL I/O + Kafka | Batch flush, disk RAID |
| Event Pipeline | 100K events/s | CPU（Processor 链） | 水平扩展 consumer |
| Semantic Engine | 50K events/s | CPU（解析 + Schema Registry） | 水平扩展 consumer |
| Event Bus | 100K events/s | Kafka partition | 增加 partitions |
| Schema Registry | 1M deserializations/s | 内存（cache hot schemas） | 本地缓存 |
| Inventory Projection | 10K updates/s | Neo4j write | Batch write |
| State Projection | 50K updates/s | Redis | Cluster mode |
| Graph Projection | 5K updates/s | Neo4j write | Sharding |
| DynamicRel | 10K writes/s | ClickHouse insert | Batch insert |
| Context API | 200 req/s | Multi-Projection read | Snapshot cache |
| Timeline MV | 100K reads/s | ClickHouse | Distributed table |
| Capability 执行 | 100 exec/s | SSH/k8s API | 连接池 |

---

## 20. 向后兼容

| 影响点 | 策略 |
|--------|------|
| 现有 API /hybrid | 保持格式不变 |
| EventEnvelope 迁移 | Phase 0 先穿透（envelope.payload = raw JSON），Phase 1 正式 schema |
| Topic 重命名 | 旧 topic 保留 1 个 retention 周期，consumer 双订阅 |
| Schema Registry 存量 | 存量 event 标记 schema_version=0（未知），不迁移 |
| Checkpoint 迁移 | 现有 last_event_id ↔ offset 双向映射（过渡期） |
| raw_id 历史 | 存量="", 新 UUID7 |
| Graph 删除状态属性 | 存量 Neo4j 数据逐步清理，不影响查询 |
| Builder → Projector | 接口兼容（别名过渡） |

---

## 21. v7.1 → v8 变更对照

| 维度 | v7.1 | v8 |
|------|------|----|
| **Event 容器** | 裸对象传输 | **EventEnvelope**（schema_version, producer, event_type, payload） |
| **Schema 管理** | 无 | **Schema Registry** + SchemaMigration（v1→v2 自动迁移） |
| **Semantic Engine** | normalize + aggregate + dedup + enrich + route（monolith） | **只 normalize**；预处理拆为 Event Pipeline |
| **Event Pipeline** | 无 | **Processor 链**：Aggregate → Dedup → Sample → Enrich → Route |
| **Topic 命名** | `normalized-events`, `raw-events` | **Domain-first**：`platform.normalized`, `platform.raw` |
| **Projection Checkpoint** | last_event_id（不可靠） | **Partition + Offset**（可靠，支持 lag 计算） |
| **查询入口** | 各自直接调 Projection | **Unified Context API**（隐藏 Neo4j/Redis/ClickHouse） |
| **ContextType** | 只有 Incident | **4 种**：Incident / Topology / Workflow / Rule |
| **Workflow 执行** | 直接 SSH/kubectl | **Capability 抽象**（Workflow 不知道执行方式） |
| **Inference → Workflow** | 直接映射 | **Planner**（Finding → Workflow，v1 简单映射） |
| **Schema Evolution** | 不支持 | **Migrations 链**（只加字段，不删不改） |
| **Lag monitoring** | 无 | **Projection.lag** = latest_offset - checkpoint_offset |
| **Capability Executor** | 无 | **CapabilityRegistry**（SSH / k8s / API 统一注册） |
| **Event Bus Topics** | 10 topics（短名） | **10 topics（domain 命名）** |
| **API** | Context Builder API | **+Schema Registry + Capability + Projection Lag** |
| **实施阶段** | 8 phases | **8 phases**（Phase 0 增加 Schema Registry + Envelope） |
| **测试** | 20 个测试 | **28 个测试**（+envelope, schema migration, pipeline, capability, planner, context API） |
