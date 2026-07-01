# Logoscope Data Architecture v1 — Event-driven Observability Platform

> **将 Logoscope 从"日志分析平台"升级为"事件驱动的可观测平台" (Event-driven Observability Platform)。**
>
> 定义从 Raw Log → NormalizedEvent → Event Bus → Knowledge → Graph → Correlation → Inference → Topology
> 的异步事件驱动数据链，统一 OpenStack、Kubernetes、VMware、Linux、网络设备、AI Agent 等多平台的事件模型。

**Status:** Draft v6
**Date:** 2026-07-01
**Authors:** croner01, Claude

---

## 1. Problem

### 1.1 当前拓扑的局限性

Logoscope 现有拓扑计算有 6 种边类型，核心问题在于各自独立、互不感知：

| 边类型 | 置信度 | 依赖字段 | 覆盖率 |
|--------|--------|----------|--------|
| Traces | 1.0 | parent_span_id | 仅限 OpenTelemetry 埋点服务 |
| OpenStack chain | 0.6 | `openstack_global_request_id` | 仅限 oslo.log 传播到的组件 |
| Inferred request_id | 0.80 | message 中的 request_id | 20% hash 采样，有漏检 |
| Inferred trace_id | 0.66 | trace_id | 20% 采样 |
| Inferred message_target | 0.74 | message 中的 URL/KV 模式 | 受限于正则覆盖 |
| Inferred time_window | 0.35 | 0.8s 时间窗口 | 假阳性风险高 |

**根因：** 缺少统一事件模型和知识层，依赖单一字段关联，调用链系统性断裂。

### 1.2 为什么要做 Event-driven Platform

前几版已解决"定义什么数据"——NormalizedEvent、Knowledge Layer、Correlation Engine。但**运行时架构**（数据如何流动、模块如何集成）尚未收敛：

- Semantic Engine 到所有下游是**同步调用**——Entity Builder、Interaction Builder、Fact Builder 全部串联
- Topology Engine 同时负责 Node 发现和画图——职责过重
- Correlation Store 存储可变的关系——难以重新计算
- 每个新增消费者需要改主链路——不满足事件驱动原则

**v6 目标：** 将架构从**同步管道**升级为**异步事件驱动**，引入 Event Bus、Graph Builder、Property Graph 层，使所有模块通过事件解耦。

### 1.3 v5.3 → v6 核心变化

```
v5.3: Semantic → EntityBuilder → Knowledge Layer (同步管道)
v6:   Semantic → Event Bus → Entity Builder | Interaction Extractor | Fact Builder (异步事件驱动)

v5.3: Topology Engine lookup Node
v6:   Graph Builder → Property Graph → Topology (纯渲染) + AI + Rule

v5.3: DynamicRelationship 存储在 Correlation Store
v6:   Correlation Store 存储不可变的 Interaction，DynamicRelationship 可缓存、可重建

v5.3: Fact Registry 包含所有类型的事实
v6:   AttributeFact (镜像/host), StateFact (attached), HistoricalFact (create_completed)
      State Registry 从 Knowledge Layer 独立，适应高频刷新

v5.3: AI 消费 Fact + Evidence
v6:   Inference Engine 消费 Subgraph + Evidence + Timeline

v5.3: Interaction Builder
v6:   Interaction Extractor (只从单 Event 提取可能端点，端到端关联归 Correlation)
```

---

## 2. Architecture

### 2.1 整体架构

```
                Raw Logs
                    |
                    v
            Semantic Engine
                    |
            NormalizedEvent
           (event_id + 4 partitions)
                    |
                    v
               Event Bus
      ┌────────────┼────────────┐
      │            │            │
      v            v            v
 Entity      Interaction    Fact
 Builder      Extractor    Builder
      │            │            │
      └────────┬───┴────────────┘
               v
        Knowledge Layer
 +------+------+------+------+
 |Entity| Alias|Static| State|
 | Reg. | Reg. | Rel. | Reg. |
 +------+------+------+------+
               |
               v
         Graph Builder
               |
         Property Graph
               |
  ┌──────┬────┴────┬──────┐
  │      │         │      │
  v      v         v      v
Correl. Rule     Timeline AI/Inf.
  |      |                |
  v      v                v
 Dynamic Rel.         Alert/Reason
 (cacheable)
```

### 2.2 为什么引入 Event Bus

```
v5.3: Semantic → EntityBuilder → Knowledge (同步)
                    → InteractionBuilder (同步)
                    → FactBuilder (同步)
    每新增一个消费者就要改 Semantic Engine

v6:   Semantic → Event Bus
                  ↓
            Entity Builder (独立订阅)
            Interaction Extractor (独立订阅)
            Fact Builder (独立订阅)
            Rule Engine (独立订阅)
            AI Audit (独立订阅)
            Metrics (独立订阅)
    新增消费者 = 新增订阅者，Semantic Engine 零改动
```

**Event Bus 选型（Phase 0 确定，建议 Kafka）：**

```
Kafka Topic: normalized-events
  Partition Key: event.service_name (保证同一服务的 Event 有序)

消费者组:
  entity-builder-group     — Entity Builder
  interaction-group        — Interaction Extractor
  fact-builder-group       — Fact Builder
  rule-engine-group        — Rule Engine
  ai-audit-group           — AI Audit
  metrics-group            — Metrics
```

### 2.3 为什么引入 Graph Builder + Property Graph

```
v5.3: Topology Engine 直接从 Knowledge + Correlation 查数据
      → 职责过重 (Node 发现 + Edge 构建 + 布局 + 渲染)
      → AI 每次遍历多个 Registry 查询上下文

v6:   Knowledge Layer + Interaction → Graph Builder → Property Graph
                                                          |
                                   Topology (纯渲染) ←───┘
                                   AI (Subgraph)  ←──────┘
                                   Rule Engine    ←──────┘
                                   Timeline       ←──────┘
```

Property Graph 是统一的**查询视图**。AI 不再分别查 Entity、Fact、Interaction——直接从 Property Graph 取 Subgraph。

### 2.4 数据流

```
Raw Log
   v
Semantic Engine → NormalizedEvent(event_id + event/context/entities/attributes)
   v
Event Bus (Kafka: normalized-events)
   |
   +-- Entity Builder
   |     → Knowledge Layer: Entity Registry (versioned) + Static Relationship
   |
   +-- Interaction Extractor
   |     → 从单 Event 提取可能端点 (process/service/host)
   |
   +-- Fact Builder
   |     → Knowledge Layer: State Registry (高频) + Attribute/Historical Fact
   |
   +-- Correlation Engine (聚合 Interaction)
   |     → Correlation Store: Interaction (不可变)
   |     → Dynamic Relationship: 实时聚合 (可缓存、可重建)
   |
   +-- Rule Engine (消费 State + Interaction)
   +-- AI Audit
   +-- Metrics
   |
   v
Graph Builder (消费 Knowledge + Interaction + Relationship)
   |
Property Graph (Neo4j + 索引)
   |
   +-- Topology Engine (纯渲染)
   +-- AI Inference (Subgraph + Evidence + Timeline)
   +-- Rule Engine (Graph Pattern Match)
   +-- Timeline API
```

### 2.5 组件职责

| 层 | 职责 | 不做什么 |
|----|------|----------|
| **Semantic Engine** | 日志理解、标准化 NormalizedEvent | 不关联、不建知识、不建图 |
| **Event Bus** | 异步事件分发（Kafka） | 不涉及业务逻辑 |
| **Entity Builder** | NormalizedEvent → Entity + Static Relationship | 不处理运行时状态 |
| **Interaction Extractor** | 从 NormalizedEvent 提取端点信息 | 不做端到端关联（归 Correlation） |
| **Fact Builder** | NormalizedEvent → AttributeFact/StateFact/HistoricalFact | 不推断状态 |
| **Correlation Engine** | 聚合 Interaction → Dynamic Relationship | 不画图 |
| **Graph Builder** | Knowledge + Interaction → Property Graph | 不做推理 |
| **Topology Engine** | Property Graph → Layout → Render | 不查数据库 |
| **Inference Engine** | Subgraph + Evidence → 推理 | 不聚合数据 |
| **Rule Engine** | State + Graph Pattern → 告警 | 不建图 |
| **Schema Registry** | Schema + Capability 管理 | 不参与运行时 |
| **Capability Registry** | Extractor/ResourceType 注册发现 | 不参与推理 |

---

## 3. Schema + Capability Registry

### 3.1 Schema Registry

所有数据契约在此注册：

| Schema | 版本 | 关键字段 |
|--------|------|----------|
| `EventSchema` | v1 | event_id, timestamp, event, context, entities, attributes |
| `EntitySchema` | v1 | entity_id, type, id, version, valid_from, valid_to, attributes |
| `AttributeFactSchema` | v1 | entity, key, value, valid_from, valid_to, source |
| `StateFactSchema` | v1 | entity, key, value, timestamp, source |
| `HistoricalFactSchema` | v1 | entity, event_id, lifecycle_key, timestamp |
| `InteractionSchema` | v1 | interaction_id, timestamp, source_ep, target_ep, type |
| `RelationshipSchema` | v1 | rel_id, source, target, type, version, status |

### 3.2 Capability Registry

```
Capability Registry

platform       | extractor         | resource_types
---------------|-------------------|----------------
openstack      | openstack_logs    | INSTANCE, VOLUME, PORT, IMAGE, HOST, NETWORK
kubernetes     | k8s_logs          | POD, NODE, PVC, SERVICE, CONTAINER
vmware         | vmware_logs       | VM, HOST, DATASTORE, NETWORK
linux          | syslog_parser     | PROCESS, CONTAINER
network        | network_logs      | SWITCH, ROUTER, INTERFACE

查询:
  GET /api/v1/capabilities/platforms
  → [{platform: "openstack", extractor: "openstack_logs",
      resource_types: ["INSTANCE", "VOLUME", ...]}]

  GET /api/v1/capabilities/extractors?resource_type=INSTANCE
  → [{platform: "openstack", extractor: "openstack_logs", ...}]
```

**为什么需要：**
- AI 查询"什么平台支持 INSTANCE 类型？"
- Schema Registry 新增字段时通知所有消费者
- 插件（extractor）注册即可自动发现

---

## 4. NormalizedEvent 模型

### 4.1 核心数据类型

```python
class ResourceType(Enum):
    INSTANCE  = "INSTANCE"
    VOLUME    = "VOLUME"
    PORT      = "PORT"
    IMAGE     = "IMAGE"
    HOST      = "HOST"
    NETWORK   = "NETWORK"
    POD       = "POD"
    NODE      = "NODE"
    PVC       = "PVC"
    SERVICE   = "SERVICE"
    CONTAINER = "CONTAINER"
    PROCESS   = "PROCESS"
    SWITCH    = "SWITCH"
    ROUTER    = "ROUTER"
    UNKNOWN   = "UNKNOWN"


@dataclass
class ResourceIdentity:
    type: ResourceType
    id: str


@dataclass
class EventParticipant:
    resource: ResourceIdentity
    role: str       # actor, target, source, destination


@dataclass
class EventCategory:
    schema_version: str = "v1"
    category: str = ""      # infra, platform, app, security
    domain: str = ""        # compute, network, volume, k8s, vmware
    resource: str = ""      # INSTANCE, VOLUME, PORT, POD, NODE
    action: str = ""        # CREATE, DELETE, ATTACH, REBOOT
    phase: str = ""         # start, end, error
    outcome: str = ""       # success, failure, unknown
```

### 4.2 NormalizedEvent

```python
@dataclass
class NormalizedEvent:
    """
    统一事件模型（四分区）。
    通过 Event Bus 发布给所有消费者。
    """
    event_id: str                           # UUID7，全局唯一
    timestamp: datetime
    service_name: str
    pod_name: str
    namespace: str
    host: str
    source_cluster: str
    severity: str
    message: str
    pid: int = 0
    thread: str = ""

    # -- event: 事件分类 --
    event: EventCategory

    # -- context: 请求上下文 --
    trace_id: str = ""
    span_id: str = ""
    request_id: str = ""
    global_request_id: str = ""

    # -- entities: 实体列表 --
    entities: List[ResourceIdentity] = field(default_factory=list)
    participants: List[EventParticipant] = field(default_factory=list)

    # -- attributes: 原始保留 --
    attributes_json: str = ""
    labels_json: str = ""

    # -- 查询加速列 --
    instance_uuid: str = ""
    volume_id: str = ""
    port_id: str = ""
    image_id: str = ""
    aggregate: str = ""
```

### 4.3 Event 引用关系

```
所有下游模块通过 event_id 稳定引用 Event：

  Interaction.interaction_id       → 从 event_id 衍生
  HistoricalFact.event_id          → 指向源 Event
  Evidence.observations[i].event_id → 指向源 Event
  Inference.evidence_list           → 引用 event_id 链
```

---

## 5. Semantic Engine

### 5.1 extract_event_category()

```python
_OPERATION_PATTERNS = {
    ("compute.instance.create.start", None):
        ("infra", "compute", "INSTANCE", "CREATE", "start", ""),
    ("compute.instance.create.end", None):
        ("infra", "compute", "INSTANCE", "CREATE", "end", "success"),
    ("compute.instance.create.error", None):
        ("infra", "compute", "INSTANCE", "CREATE", "end", "failure"),
    ("volume.attach.end", None):
        ("infra", "volume", "INSTANCE", "ATTACH_VOLUME", "end", "success"),
    ("volume.detach.end", None):
        ("infra", "volume", "INSTANCE", "DETACH_VOLUME", "end", "success"),
    ("port.create.end", None):
        ("infra", "network", "PORT", "CREATE", "end", "success"),
    (None, "attach"): ("", "", "INSTANCE", "ATTACH_VOLUME", "", ""),
    (None, "detach"): ("", "", "INSTANCE", "DETACH_VOLUME", "", ""),
    (None, "spawn"):  ("", "", "INSTANCE", "SPAWN", "", ""),
}


def extract_event_category(log_data: Dict[str, Any]) -> EventCategory:
    event_type = _candidate_text(log_data.get("event_type") or "")
    action = _candidate_text(log_data.get("action") or "")
    for (ev_prefix, _), result in _OPERATION_PATTERNS.items():
        if ev_prefix and event_type.startswith(ev_prefix):
            return EventCategory(schema_version="v1", *result)
    for (_, act_verb), result in _OPERATION_PATTERNS.items():
        if act_verb and action and act_verb in action.lower():
            return EventCategory(schema_version="v1", *result)
    outcome = ("failure" if str(log_data.get("severity","")).upper()
               in ("ERROR","FATAL","CRITICAL") else "unknown")
    return EventCategory(schema_version="v1", resource="UNKNOWN",
                          action="UNKNOWN", outcome=outcome)
```

### 5.2 extract_entities()

```python
_RESOURCE_FIELD_MAP = {
    ResourceType.INSTANCE: ["instance_uuid","instance_id","server_id","uuid"],
    ResourceType.VOLUME:   ["volume_id","volumeId"],
    ResourceType.PORT:     ["port_id","portId"],
    ResourceType.IMAGE:    ["image_id","imageId","image_uuid"],
}

_TRANSFORMATION_MAP = {
    "device_id":     ResourceType.INSTANCE,
    "consumer_uuid": ResourceType.INSTANCE,
    "domain_uuid":   ResourceType.INSTANCE,
    "qemu_uuid":     ResourceType.INSTANCE,
    "snapshot_id":   ResourceType.VOLUME,
    "attachment_id": ResourceType.VOLUME,
}


def extract_entities(log_data: Dict[str, Any]) -> List[ResourceIdentity]:
    raw_attrs = log_data.get("_raw_attributes", {}) or {}
    message = str(log_data.get("message", ""))
    found = {}
    for res_type, keys in _RESOURCE_FIELD_MAP.items():
        for k in keys:
            v = _find_value(raw_attrs, message, k)
            if v: found[k] = (res_type, v); break
    for alias_key, res_type in _TRANSFORMATION_MAP.items():
        if not any(v[0] == res_type for v in found.values()):
            v = _find_value(raw_attrs, message, alias_key)
            if v: found[alias_key] = (res_type, v)
    seen, entities = set(), []
    for _, (rt, uid) in found.items():
        if uid and uid not in seen:
            seen.add(uid)
            entities.append(ResourceIdentity(type=rt, id=uid))
    return entities
```

### 5.3 normalize_log() → Event Bus

```python
def normalize_log(log_data: Dict[str, Any]) -> NormalizedEvent:
    category = extract_event_category(log_data)
    entities = extract_entities(log_data)
    participants = assign_participants(category, entities)
    event = NormalizedEvent(
        event_id=_generate_uuid7(),
        timestamp=..., service_name=..., host=..., severity=..., message=...,
        event=category, entities=entities, participants=participants,
        trace_id=..., request_id=..., global_request_id=...,
        instance_uuid=entity_uuids.get("instance_uuid",""),
        volume_id=entity_uuids.get("volume_id",""),
        port_id=entity_uuids.get("port_id",""),
        image_id=entity_uuids.get("image_id",""),
        attributes_json=json.dumps(raw_attributes),
    )
    # 写入 Event Bus — 所有消费者异步消费
    event_bus.publish("normalized-events", event)
    return event
```

---

## 6. Event Bus

### 6.1 接口

```python
class EventBus(ABC):
    """事件总线。解耦 Producer 和 N 个 Consumer。"""

    @abstractmethod
    def publish(self, topic: str, event: NormalizedEvent):
        """发布事件到 Topic。"""
        ...

    @abstractmethod
    def subscribe(self, topic: str, group: str,
                   callback: Callable[[NormalizedEvent], None]):
        """订阅 Topic。同 group 内负载均衡，不同 group 独立消费。"""
        ...
```

### 6.2 Topic 设计

```
Topic: normalized-events
  分区: event.service_name (保证同服务有序)

消费者组:
  entity-builder       → Entity Registry + Static Relationship
  interaction-extractor → Interaction
  fact-builder         → Fact Registry (Attribute + State + Historical)
  correlation-engine   → Interaction → Dynamic Relationship
  rule-engine          → Fact + Interaction Pattern Match
  ai-audit             → AI Audit Trail
  metrics              → Observability Metrics
```

---

## 7. Knowledge Layer

### 7.1 结构

```
Knowledge Layer
+-- Entity Registry       # 版本化实体，生命周期：月/年
+-- Alias Registry        # Identity Resolution，稳定映射
+-- Static Relationship   # 资源间固有关系，API/Inventory 来源
+-- State Registry        # 运行时状态，高频刷新，生命周期：秒/分/时
```

**Entity = State 分离：**

| 数据 | 类型 | 生命周期 | 存储 | 刷新频率 |
|------|------|----------|------|----------|
| instance.id=abc | Entity | 月/年 | Entity Registry | 低 |
| instance.host=compute-01 | Entity | 月/年 | Entity (attribute) | 低 |
| instance.image=cirros | Entity | 月/年 | Entity (attribute) | 低 |
| attached=true | State | 秒/分/时 | State Registry | 高 |
| migration_state=running | State | 秒 | State Registry | 极高 |
| lifecycle.create_completed | Historical | 永久 | HistoricalFact | 一次 |

### 7.2 Entity Registry（versioned）

```python
@dataclass
class EntityRecord:
    entity_id: str
    type: ResourceType
    id: str
    version: int = 1
    valid_from: datetime
    valid_to: Optional[datetime] = None
    attributes: Dict[str, str] = field(default_factory=dict)
    source: str = ""

    @property
    def is_current(self) -> bool:
        return self.valid_to is None
```

### 7.3 Fact 三级模型

```python
@dataclass
class AttributeFact:
    """
    属性事实：几乎不变。
    查询模式: entity + key (稳定)
    例子: image=cirros, mac=fa:16:3e:...
    """
    entity: ResourceIdentity
    key: str
    value: str
    valid_from: datetime
    valid_to: Optional[datetime] = None
    source: str = ""
    confidence: float = 1.0

    @property
    def is_current(self) -> bool:
        return self.valid_to is None


@dataclass
class StateFact:
    """
    状态事实：高频刷新，有有效时间。
    查询模式: entity + key + is_current (高频)
    例子: attached=true, migration_state=running
    """
    entity: ResourceIdentity
    key: str
    value: str
    timestamp: datetime          # 刷新时间
    ttl_seconds: int = 60        # TTL 过期自动失效
    source: str = ""
    confidence: float = 1.0

    @property
    def is_current(self) -> bool:
        return (datetime.utcnow() - self.timestamp).total_seconds() < self.ttl_seconds


@dataclass
class HistoricalFact:
    """
    历史事实：不可变，一次写入永久保留。
    查询模式: entity + time_range (时序)
    例子: lifecycle.create_completed, migration.started
    """
    entity: ResourceIdentity
    event_id: str               # 指向源 Event
    key: str                    # "lifecycle.create_completed"
    value: str                  # "true"
    timestamp: datetime
    source: str = ""
```

### 7.4 Alias Registry（Identity Resolution）

```python
@dataclass
class AliasRecord:
    alias_key: str            # "device_id"
    alias_value: str          # "abc-123"
    canonical_type: ResourceType
    canonical_id: str
    source: str               # "alias_map", "api", "notification"
    confidence: float = 1.0


class IdentityResolver:
    """集中式身份解析。所有模块通过此接口查询。"""

    def resolve(self, alias_key: str, alias_value: str
                ) -> Optional[ResourceIdentity]:
        record = self.alias_registry.lookup(alias_key, alias_value)
        if record:
            return ResourceIdentity(record.canonical_type, record.canonical_id)
        return None

    def resolve_event_entities(self, event: NormalizedEvent) -> List[ResourceIdentity]:
        """批量解析 Event 中的实体。"""
        results = list(event.entities)
        for p in event.participants:
            results.append(p.resource)
        return results
```

### 7.5 Knowledge Layer API

```text
GET /api/v1/knowledge/entities?type=INSTANCE&id=abc-123
GET /api/v1/knowledge/entities/history?type=INSTANCE&id=abc-123

GET /api/v1/knowledge/attributes?type=INSTANCE&id=abc-123
GET /api/v1/knowledge/state?type=INSTANCE&id=abc-123&current=true
GET /api/v1/knowledge/history?type=INSTANCE&id=abc-123&time_range=1h

GET /api/v1/knowledge/resolve?alias=device_id&value=abc-123
GET /api/v1/knowledge/relationships?type=INSTANCE&id=abc-123
```

---

## 8. Correlation Engine

### 8.1 架构

```
Event Bus → Correlation Engine
                |
            Interaction Extractor (提取端点)
                |
            Interaction (不可变, 存 Correlation Store)
                |
            Correlation Provider 聚合
                |
            Dynamic Relationship (可缓存、可重建)
```

**关键原则：Interaction 是 Source of Truth，Dynamic Relationship 可重建。**

```
Interaction = 不可变记录（一次写入永久保留）
Dynamic Relationship = 可变聚合（基于 Interaction 实时计算）
```

### 8.2 Interaction（不可变）

```python
@dataclass
class InteractionEndpoint:
    entity: ResourceIdentity   # 可以是 SERVICE / INSTANCE / HOST / POD ...
    role: str = ""


@dataclass
class Interaction:
    """
    运行时交互：两个 Endpoint 之间的单次交互。
    不可变 — 写入 Correlation Store 后不会被修改。

    不限于 Service→Service:
      Service → Service, Instance → Host, Pod → Node, Container → Runtime
    """
    interaction_id: str
    timestamp: datetime
    source_endpoint: InteractionEndpoint
    target_endpoint: InteractionEndpoint
    interaction_type: str       # "calls", "rpc", "api_call", "attaches", "schedules"
    duration_ms: float = 0.0
    request_id: str = ""
    outcome: str = ""
```

### 8.3 Interaction Extractor

```python
class InteractionExtractor:
    """
    从单 NormalizedEvent 提取可能的端点。
    只提取"这个 Event 中出现了哪些端点"，不做端到端关联。

    NormalizedEvent(service=A, trace_id=..., request_id=...)
      → InteractionEndpoint SERVICE A (source)
      → InteractionEndpoint INSTANCE abc (entity)

    端到端关联 (A → B) 在 Correlation Engine 完成。
    """

    def extract(self, event: NormalizedEvent) -> List[InteractionEndpoint]:
        endpoints = []
        # Event 的服务本身是一个端点
        endpoints.append(InteractionEndpoint(
            entity=ResourceIdentity(ResourceType.SERVICE, event.service_name),
            role="source",
        ))
        # Event 中的 entities 也是潜在端点
        for entity in event.entities:
            endpoints.append(InteractionEndpoint(entity=entity, role="target"))
        for p in event.participants:
            endpoints.append(InteractionEndpoint(
                entity=p.resource, role=p.role))
        return endpoints
```

### 8.4 Evidence 链（Observation → Transformation → Assertion → Fact → Evidence → Inference）

```python
@dataclass
class Observation:
    event_id: str
    service_name: str
    timestamp: datetime
    observed_field: str       # "device_id"
    observed_value: str       # "abc-123"
    confidence: float = 0.8
    source_detail: str = ""


@dataclass
class Transformation:
    """
    语义转换：Observation → 目标类型。
    仍未经 Knowledge 验证。
    """
    transformation_type: str    # "alias_resolution"
    observation: Observation
    resolved_type: ResourceType
    resolved_id: str
    confidence: float = 1.0


@dataclass
class Assertion:
    """
    断言：Transformation 经过 Knowledge 验证后。
    Transformation: "device_id → INSTANCE" (未经确认)
    Assertion: "device_id → INSTANCE" (经 IdentityResolver 确认)
    """
    assertion_type: str             # "alias_confirmed", "entity_exists"
    transformation: Transformation
    verified_by: str                # "knowledge_layer", "api"
    confidence: float = 1.0


@dataclass
class Fact:
    """经过验证的事实。"""
    fact_type: str
    subject_type: ResourceType
    subject_value: str
    object_type: ResourceType
    object_value: str
    assertion: Assertion            # 引用验证链
    source: str = ""


@dataclass
class Evidence:
    """同一条事实出现在两个服务中。"""
    source_service: str
    target_service: str
    evidence_type: str
    match_value: str
    weight: float
    assertions: List[Assertion] = field(default_factory=list)


@dataclass
class Inference:
    """推断：两个服务之间存在行为关系。"""
    source: str
    target: str
    evidence_list: List[Evidence]
    inferred_relationship: str
```

### 8.5 Dynamic Relationship（可缓存、可重建）

```python
class RelationshipStatus(Enum):
    ACTIVE = "ACTIVE"
    STALE = "STALE"
    EXPIRED = "EXPIRED"


@dataclass
class DynamicRelationship:
    """
    动态关系：基于 Interactions 实时聚合。
    可以随时从 Interaction 重建——不是 Source of Truth。

    缓存策略:
      - 读时缓存 (Cache-aside): Topology 查询时缓存
      - TTL: 30s (高频场景) / 5min (低频场景)
      - 重建: 清缓存后从 Interaction 聚合
    """
    relationship_id: str
    version: int = 1
    created_at: datetime = field(default_factory=datetime.utcnow)
    source: str
    target: str
    relationship_type: str = "calls"
    confidence: float

    first_seen: datetime
    last_seen: datetime
    expire_after_minutes: int = 30
    status: RelationshipStatus = RelationshipStatus.ACTIVE

    interaction_ids: List[str] = field(default_factory=list)
    call_count: int = 0
    inferences: List[Inference] = field(default_factory=list)
    data_sources: List[str] = field(default_factory=list)
```

**为什么 DynamicRelationship 可重建：**
- Interaction 是唯一的 Source of Truth
- DynamicRelationship = `SELECT source, target, count(*) FROM interactions GROUP BY source, target`
- 缓存清空后自动重建，不影响下游
- 支持版本化：v1 和 v2 可以并行聚合对比

### 8.6 Correlation Store

```python
class CorrelationStore(ABC):
    """存储 Interaction（不可变）+ 缓存 Dynamic Relationship（可重建）。"""

    @abstractmethod
    def store_interaction(self, interaction: Interaction):
        """写入 Interaction（append-only）。"""
        ...

    @abstractmethod
    def query_interactions(self, time_window: str,
                            filters: Optional[Dict] = None) -> List[Interaction]:
        """查询 Interaction（不可变数据）。"""
        ...

    @abstractmethod
    def get_relationship(self, source: str, target: str
                          ) -> Optional[DynamicRelationship]:
        """获取 Dynamic Relationship（缓存，可重建）。"""
        ...
```

### 8.7 Inference Engine（输入：Subgraph + Evidence + Timeline）

```python
@dataclass
class InferenceInput:
    """
    推理引擎输入。
    不再接收原始 Fact — 而是结构化的 Subgraph + Evidence + Timeline。
    """
    subgraph: PropertyGraph           # 邻居子图
    evidence: List[Evidence]          # 相关证据
    timeline: List[HistoricalFact]    # 相关历史


class InferenceEngine(ABC):
    """
    推理引擎。

    输入：Subgraph + Evidence + Timeline（不是原始 Fact）
    输出：推理结果

    Implementations:
      LLMInferenceEngine — 大模型推理
      RuleInferenceEngine — 规则推理
      GraphInferenceEngine — 图模式匹配
    """

    @abstractmethod
    def infer(self, input: InferenceInput) -> List[Inference]:
        ...
```

---

## 9. Graph Builder + Property Graph

### 9.1 定位

```
Knowledge Layer ──┐
Interaction  ─────┤──→ Graph Builder → Property Graph
Dynamic Rel. ─────┘
```

Graph Builder 消费 Knowledge Layer（Entity + State + Static Relationship）+ Interaction + Dynamic Relationship，构建统一的 Property Graph。

```
Property Graph
+-- Node: Entity (versioned attributes + current state)
+-- Edge: Static Relationship (KNOWS, RUNS_ON, HAS_PORT)
+-- Edge: Dynamic Relationship (CALLS, DEPENDS_ON)
+-- Edge: Interaction (单次运行时交互)
```

### 9.2 为什么需要 Property Graph

```
v5.3: Topology  → 直接查 Knowledge + Correlation
       AI       → 分别查 Entity + Fact + State + Interaction
       Rule     → 分别查 State + Static Relationship

v6:   Topology  → Property Graph (只取子图，纯渲染)
       AI       → Property Graph (取 Neighborhood Subgraph)
       Rule     → Property Graph (Graph Pattern Match)
       Timeline → Property Graph (时序子图)
```

所有消费者从 Property Graph 获取数据，而不是各自查多个 Registry。

### 9.3 Graph Builder 实现

```python
class GraphBuilder:
    """
    构建 Property Graph。
    增量更新：每次收到新事件时更新子图，而非全量重建。
    """

    def build_full(self) -> PropertyGraph:
        """全量重建（启动时 / 定时）。"""
        entities = self.knowledge_layer.get_all_entities()
        states = self.knowledge_layer.get_all_states()
        static_rels = self.knowledge_layer.get_all_relationships()
        interactions = self.correlation_store.query_interactions("1 HOUR")
        dynamic_rels = self.correlation_store.query_relationships("1 HOUR")

        graph = PropertyGraph()
        for e in entities:
            graph.add_node(e.entity_id, e.type.value, e.attributes)
        for s in states:
            graph.update_node_state(s.entity.id, s.key, s.value)
        for r in static_rels:
            graph.add_edge(r.subject, r.object, "STATIC:" + r.relation_type)
        for ia in interactions:
            graph.add_edge(ia.source_endpoint.entity.id,
                           ia.target_endpoint.entity.id,
                           "INTERACTION:" + ia.interaction_type)
        for dr in dynamic_rels:
            graph.add_edge(dr.source, dr.target, "DYNAMIC:" + dr.relationship_type)
        return graph

    def apply_event(self, event: NormalizedEvent):
        """增量更新：收到 Event Bus 事件时局部更新。"""
        ...


@dataclass
class PropertyGraph:
    """属性图。所有消费者通过此接口查询。"""

    def get_subgraph(self, entity_id: str, depth: int = 2,
                     edge_types: Optional[List[str]] = None) -> "PropertyGraph":
        """获取指定实体的邻居子图。AI/Topology/Rule 都调此接口。"""
        ...

    def get_timeline(self, entity_id: str, time_range: str
                     ) -> List[GraphEvent]:
        """获取指定实体的时间线子图。"""
        ...

    def match_pattern(self, pattern: GraphPattern) -> List[GraphMatch]:
        """图模式匹配。Rule Engine 调用。"""
        ...
```

### 9.4 Topology Engine（纯渲染）

```python
class TopologyEngine:
    """
    纯渲染引擎。
    不查数据库、不做 Node 发现、不计算关系。
    输入：PropertyGraph Subgraph → Layout → Render
    """

    def render(self, subgraph: PropertyGraph) -> TopologyResult:
        nodes = [Node(id=n.id, name=n.name, type=n.type,
                      attributes=n.attributes)
                 for n in subgraph.nodes]
        edges = [Edge(source=e.source, target=e.target,
                      type=e.type, confidence=e.confidence)
                 for e in subgraph.edges]
        layout = self._compute_layout(nodes, edges)
        return TopologyResult(nodes=nodes, edges=edges, layout=layout)
```

---

## 10. 置信度模型

```
base = 0.3

resource_match:  +0.45
request_match:   global_request_id +0.35, request_id +0.20
time_window:     < 0.5s +0.10, < 0.1s +0.15
message_match:   +0.20
static_relation: +0.30

max = min(base, 0.98)
time_decay: >120min → decay = 0.5^(min/120)
final = max * decay
```

---

## 11. API

```text
# 拓扑（从 Property Graph 渲染）
GET /api/v1/topology/hybrid?time_window=1+HOUR

# 交互详情
GET /api/v1/interactions?source=Nova&target=Neutron

# 证据详情
GET /api/v1/evidence?source=Nova&target=Neutron

# 知识层
GET /api/v1/knowledge/entities?type=INSTANCE&id=abc-123
GET /api/v1/knowledge/entities/history?type=INSTANCE&id=abc-123
GET /api/v1/knowledge/attributes?type=INSTANCE&id=abc-123
GET /api/v1/knowledge/state?type=INSTANCE&id=abc-123&current=true
GET /api/v1/knowledge/history?type=INSTANCE&id=abc-123

# Property Graph
GET /api/v1/graph/subgraph?entity_id=abc-123&depth=2
GET /api/v1/graph/timeline?entity_id=abc-123&time_range=1h

# Capability
GET /api/v1/capabilities/platforms
GET /api/v1/capabilities/extractors?resource_type=INSTANCE

# Correlation
GET /api/v1/correlate/services?source=Nova
```

---

## 12. 实施阶段

### Phase 0: Foundation（~1 周）

| 模块 | 内容 |
|------|------|
| Schema Registry | EventSchema v1, EntitySchema v1, FactSchema 三级, InteractionSchema v1 |
| Capability Registry | 平台 + Extractor 注册 |
| Event Bus 接口 | Kafka topic: normalized-events |
| event_id 生成 | UUID7 |

### Phase 1: Semantic Engine + Event Bus（~1 周）

| 模块 | 内容 |
|------|------|
| Semantic Engine | extract_event_category(), extract_entities() |
| Event Bus 集成 | Semantic → Kafka normalized-events |

### Phase 2: Knowledge Layer（~2 周）

| 模块 | 内容 |
|------|------|
| Entity Registry | versioned, valid_from/valid_to |
| Alias Registry | Identity Resolution |
| State Registry | StateFact with TTL |
| Fact Registry | AttributeFact + HistoricalFact |
| Entity/Fact Builder | Event Bus 消费者 |

### Phase 3: Interaction + Correlation（~2 周）

| 模块 | 内容 |
|------|------|
| Interaction Extractor | Event Bus 消费者，提取端点 |
| Correlation Engine | Interaction → Dynamic Relationship |
| Correlation Store | Interaction (immutable) + Dynamic Rel (cache) |

### Phase 4: Graph Builder + Property Graph（~1 周）

| 模块 | 内容 |
|------|------|
| Graph Builder | 全量+增量构建 |
| Property Graph | Subgraph, Timeline, Pattern Match |

### Phase 5: Topology + Inference Engine（~1 周）

| 模块 | 内容 |
|------|------|
| Topology Engine | 纯渲染，消费 Property Graph Subgraph |
| Inference Engine | LLM/Rule/ML 实现，消费 Subgraph + Evidence + Timeline |

### Phase 6: Rule Engine + Multi-platform（~2 周）

| 模块 | 内容 |
|------|------|
| Rule Engine | Graph Pattern + State 匹配 |
| K8s/VMware | Extractor + Capability 注册 |

---

## 13. 测试策略

```python
def test_event_bus_async():
    """Semantic → Event Bus → N consumers"""
    bus = FakeEventBus()
    engine = CorrelationEngine(bus)
    bus.publish("normalized-events", event)
    assert engine.consumed_count == 1

def test_interaction_immutable():
    """Interaction 写入后不可修改"""
    ia = Interaction(...)
    store.store_interaction(ia)
    with pytest.raises(AttributeError):
        ia.source_endpoint = InteractionEndpoint(...)

def test_dynamic_relationship_rebuildable():
    """DynamicRelationship 可从 Interaction 重建"""
    interactions = [Interaction(...), Interaction(...)]
    rel = aggregator.build("A", "B", interactions)
    assert rel.call_count == 2
    # 重建
    rel2 = aggregator.build("A", "B", interactions)
    assert rel2.call_count == 2  # 结果一致

def test_property_graph_subgraph():
    """Property Graph 子图查询"""
    graph = GraphBuilder().build_full()
    sub = graph.get_subgraph("entity-001", depth=1)
    assert len(sub.nodes) > 0

def test_assertion_layer():
    """Assertion 验证 Transformation"""
    trans = Transformation("alias_resolution", obs,
                           ResourceType.INSTANCE, "abc-123")
    assertion = Assertion("alias_confirmed", trans,
                          verified_by="knowledge_layer")
    assert assertion.confidence == 1.0

def test_state_ttl():
    """StateFact 自动过期"""
    state = StateFact(entity=..., key="attached", value="true",
                      timestamp=now, ttl_seconds=60)
    assert state.is_current is True
    # 模拟 70 秒后
    with freeze_time(now + timedelta(seconds=70)):
        assert state.is_current is False

def test_topology_no_db():
    """Topology Engine 不直接查数据库"""
    graph = FakePropertyGraph()
    engine = TopologyEngine()
    result = engine.render(graph.get_subgraph("abc"))
    assert len(result.nodes) > 0
    # 不调用 knowledge_layer / correlation_store
```

---

## 14. v5.3 → v6 变更对照

| 维度 | v5.3 | v6 |
|------|----|------|
| **集成方式** | 同步管道 | Event Bus 异步事件驱动 |
| **Interaction** | Builder（端到端关联） | Extractor（只提取端点） |
| **Fact 模型** | 统一 Fact | AttributeFact / StateFact / HistoricalFact |
| **State** | Fact Registry 的一部分 | State Registry 独立（高频刷新） |
| **Evidence 链** | O→T→F→E→I | O→T→**Assertion**→F→E→I |
| **Node 发现** | Topology Engine | Graph Builder → Property Graph |
| **Topology** | 查数据库 | 纯渲染（消费 Property Graph）|
| **AI 输入** | Fact + Evidence | Subgraph + Evidence + Timeline |
| **Correlation Store** | DynamicRelationship | Interaction（不可变）+ Rel（可缓存）|
| **Schema 治理** | Schema Registry | Schema + Capability Registry |
| **Extractor** | 无 | Capability Registry 注册发现 |

---

## 15. 向后兼容

| 影响点 | 策略 |
|--------|------|
| 现有 API /hybrid | 保持输出格式不变 |
| ClickHouse 存量 | ALTER TABLE ADD COLUMN |
| event_id 历史 | 存量="", 新数据 UUID7 |
| Entity 无版本 | 存量 version=1, valid_from=now() |
| State 迁移 | 存量走 State Registry, valid_to=None |
