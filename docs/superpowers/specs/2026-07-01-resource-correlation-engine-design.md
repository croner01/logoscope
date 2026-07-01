# Logoscope Data Architecture v1

> **将 Logoscope 从"日志分析平台"升级为"事件知识平台" (Event Knowledge Platform)。**
>
> 定义从 Raw Log → Normalized Event → Interaction → Knowledge → Correlation → Graph → AI
> 的完整数据链，统一 OpenStack、Kubernetes、VMware、Linux、网络设备、AI Agent 等多平台的事件模型。
>
> **Key design decisions:**
> - NO `primary_resource` — all resources equal, expressed as `entities[ResourceIdentity]` + `participants[EventParticipant]`
> - NO hardcoded resource types in Correlation Engine — uses generic `ResourceIdentity` matching
> - **Knowledge Layer: Entity + Fact + Static Relationship** (no Dynamic Relationship, no Metadata Registry)
> - **Correlation Store: Dynamic Relationship** separately owned by Correlation Engine (Single Source of Truth)
> - **Interaction before Correlation**: NormalizedEvent → Interaction → Correlation → Dynamic Relationship
> - **Static Relationship** (resource-to-resource, from API/Inventory) in Knowledge Layer
> - **Dynamic Relationship** (service-to-service behavior) in Correlation Store
> - Correlation Engine consumes **EventSource** (event stream abstraction), not storage directly
> - Observation → **Transformation** → Fact → Evidence → Inference five-layer evidence chain
> - NormalizedEvent split into `event` / `context` / `entities` / `attributes` four sections
> - Topology Engine looks up nodes from Knowledge Layer for **Infrastructure Entity** and **Execution Entity**
> - EventCategory: `schema_version/category/domain/resource/action/phase/outcome` seven-dimension
> - `ResourceType` enum instead of raw strings
> - AI is **Inference Engine** (not a Correlator Provider) — Rule, Graph, ML, LLM all implement same interface
> - **Fact Builder is conservative**: derives only what is directly observable, never infers resource state

**Status:** Draft v5.2
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

**根因：** 依赖单一字段（request_id / global_request_id）进行关联，而 OpenStack 中存在大量没有 request_id 传播的组件（Libvirt、QEMU、os-brick、multipathd），导致调用链系统性断裂。

### 1.2 为什么要做 Data Architecture

这个问题的根源比"拓扑计算"更深——Logoscope 缺少一个**统一的数据模型**。

- Semantic Engine 输出的是"日志"，而不是"事件"
- Correlation Engine 直接写 ClickHouse SQL
- Topology Engine 同时负责计算置信度、推断关系、画图
- Knowledge Layer 只存在于 Topology 中，其他模块无法共享
- AI Service 没有标准化的事件模型可用
- 新增平台（K8s、VMware、Bare Metal）需要重复实现整条链

**目标：** 定义 Logoscope 的**数据架构 v1**，使每一层的职责、接口、数据模型清晰，平台扩展时只需新增 extractor + entity builder，核心链路零改动。

### 1.3 从「资源关联」到「知识关联」

OpenStack 日志中实际存在比 request_id 更稳定的关联键——**资源 UUID**：

| 资源 UUID | 出现组件 | 覆盖场景 |
|-----------|----------|----------|
| `instance_uuid` | Nova API/Compute/Sched, Neutron, Cinder, Glance, Libvirt | VM 全生命周期 |
| `volume_id` | Cinder API/Volume, Nova Compute, os-brick | 存储操作 |
| `port_id` | Neutron, Nova Compute, OVS Agent | 网络操作 |
| `image_id` | Glance API, Nova Compute | 镜像操作 |

但这些 UUID 的关联是**表层的**——真正需要的是**知识层关联**：

```
日志:  "device_id=abc-123"              (Observation)
  -> "device_id 是 instance_uuid"       (Transformation)
  -> "instance abc-123 存在"            (Fact)
  -> "instance 在 compute-01 上"        (Knowledge: Static Relationship)
  -> "Nova 和 Neutron 都有此 instance"  (Evidence)
  -> "Nova calls Neutron"               (Dynamic Relationship)
```

---

## 2. Architecture

### 2.1 整体数据链

```
                    Raw Logs
                       |
                       v
               Semantic Engine
              (normalize + classify)
                       |
                       v
               Normalized Event
           +------+---+---+------+
           |      |       |      |
           v      v       v      v
    Interaction  Entity  Fact   Event
     Builder    Builder Builder Store
           |      |       |      |
           |      +---+---+      |
           |          v          |
           |   Knowledge Layer   |
           |  (Entity + Fact +   |
           |   Static Relation)  |
           |          |          |
           +----+-----+          |
                |                |
                v                v
          Correlation Engine + EventSource
                |
          Dynamic Relationship
                |
          Correlation Store
                |
      +---------+---------+
      |                   |
      v                   v
 Topology Engine     Inference Engine
 (Graph + Layout)   (AI / Rule / ML / Graph)
      |                   |
      v                   v
   Frontend / API     AI Service / Alerting
```

### 2.2 数据流

```
Raw Log
   v
Semantic Engine
   v
NormalizedEvent { event, context, entities[], attributes }
   |
   +-- EventSource --> [InteractionBuilder] --> Interaction
   |                                               |
   +-- EntityBuilder --> Knowledge Layer           |
   |   (Entity + Static Relationship)              |
   |                                               |
   +-- FactBuilder --> Knowledge Layer             |
       (Fact Registry, conservative only)          |
                                                   v
                                          Correlation Engine
                                           (aggregates Interactions
                                            into Dynamic Relationships)
                                                   |
                                                   v
                                          Correlation Store
                                        (Dynamic Relationship ONLY)
                                                   |
                                    +--------------+
                                    |
                                    v
                             Topology Engine
                        (lookup Entity from Knowledge,
                         render Interaction + Dynamic Relationship)
                                    |
                                    v
                             API / Frontend / AI
```

**核心变更（v5.2）对比：**

```
v5:  NormalizedEvent -> EventSource -> Correlation -> Relationship -> Interaction -> Topology
v5.2: NormalizedEvent -> InteractionBuilder -> Interaction -> Correlation -> Dynamic Relationship -> Topology
       ^ Interaction 先于 Correlation 生成，Correlation 聚合 Interaction 为 Dynamic Relationship

v5:  Knowledge Layer = Entity + Relationship + Fact + Metadata
v5.2: Knowledge Layer = Entity + Fact + Static Relationship ONLY
       ^ 动态关系（Dynamic Relationship）归 Correlation Store 独有

v5:  AI = CorrelationProvider
v5.2: AI = InferenceEngine implementation（与 Rule/ML/Graph 同级）
```

### 2.3 组件职责

| 层 | 职责 | 不做什么 |
|----|------|----------|
| **Semantic Engine** | 日志理解、分类、标准化 NormalizedEvent | 不计算拓扑、不做关联、不建知识 |
| **Interaction Builder** | 从 NormalizedEvent 构造 Interaction | 不推断关系、不聚合 |
| **Entity Builder** | 从 NormalizedEvent 提取 Entity/Static Relationship 写入 Knowledge | 不推断动态关系 |
| **Fact Builder** | 从 NormalizedEvent 构造保守 Fact 写入 Knowledge | 不推断资源状态（如 ACTIVE） |
| **Event Store** | 持久化 NormalizedEvent，索引、预聚合 | 不做业务逻辑 |
| **EventSource** | 抽象事件流接口（ClickHouse/Kafka/Iceberg/Replay） | 不涉及具体字段含义 |
| **Knowledge Layer** | Entity + Fact + Static Relationship 三个注册表 | 不存储动态运行时关系 |
| **Correlation Store** | 存储 Dynamic Relationship | 不涉及资源模型、不做推理 |
| **Correlation Engine** | 聚合 Interaction -> Dynamic Relationship | 不画图、不涉及资源属性 |
| **Inference Engine** | 对 Evidence 做推理（AI/Rule/ML/Graph） | 不聚合、不存储 |
| **Topology Engine** | Interaction + Dynamic Relationship -> lookup Node -> Edge -> Layout | 不计算置信度、不推断关系 |
| **AI Service** | 推理补全不可证明的关系 | 不覆盖已有证据的关系 |
| **Rule Engine** | 消费 Fact + Interaction 做规则匹配告警 | 不画图、不关联 |

### 2.4 为什么取消 primary_resource

`primary_resource` 试图回答"哪个资源最重要"，但在多资源事件（attach_volume、live_migration）中，这个选择是武断的。改为 `entities[ResourceIdentity]` + `participants[EventParticipant]`：

- **attach_volume**: `participants = [{INSTANCE, actor}, {VOLUME, target}]`
- **live_migration**: `participants = [{INSTANCE, actor}, {HOST, source}, {HOST, target}]`
- **create_port**: `participants = [{PORT, actor}, {NETWORK, target}]`

角色是**事件参与角色**，不是资源属性。资源本身的属性（host、zone、label）在 Knowledge Layer。

### 2.5 为什么 Correlation Engine 不用硬编码资源类型

ResourceCorrelator 不关心 `INSTANCE` vs `VOLUME` vs `POD`。它只匹配 `(type, id)` 对：

```python
# 匹配任何在两个服务中出现相同 (type, id) 的资源
# OpenStack: (INSTANCE, abc-123) -> Nova + Neutron 都出现 -> 产生边
# Kubernetes: (POD, xyz-789) -> kube-apiserver + kubelet 都出现 -> 产生边
```

新增平台只需在 Semantic Engine 的 extractors 中注册新资源类型，Correlation Engine 零改动。

### 2.6 为什么 Correlation 不直接 Query Storage

```
v4: Provider -> EventRepository -> ClickHouse
v5: Provider -> EventSource -> ClickHouseEventSource | KafkaEventSource | IcebergEventSource
```

Correlation 关心的不是**存储**，而是**事件流**：

| 场景 | EventSource 实现 | 用途 |
|------|-----------------|------|
| 实时关联 | `KafkaEventSource.stream_events()` | 秒级流式关联 |
| 历史查询 | `ClickHouseEventSource.query_events()` | 回溯分析 |
| 离线分析 | `IcebergEventSource.query_events()` | 数据仓库 |
| 测试 | `ReplayEventSource.from_fixtures()` | 回放测试 |

EventSource 接口仅定义流式/查询操作，Provider 不碰任何 SQL。

---

## 3. Normalized Event 模型

### 3.1 为什么需要统一 Event 模型

NormalizedEvent 是整个平台的**数据契约**。所有下游围绕同一模型工作。

**拆分为四个逻辑组：** `event` / `context` / `entities` / `attributes`，避免 Fat Event 问题。

### 3.2 核心数据类型

```python
class ResourceType(Enum):
    """平台无关的资源类型枚举。所有 Extractor 注册到这里。"""
    INSTANCE = "INSTANCE"
    VOLUME   = "VOLUME"
    PORT     = "PORT"
    IMAGE    = "IMAGE"
    HOST     = "HOST"
    NETWORK  = "NETWORK"
    POD      = "POD"
    NODE     = "NODE"
    PVC      = "PVC"
    SERVICE  = "SERVICE"
    CONTAINER = "CONTAINER"
    UNKNOWN  = "UNKNOWN"


@dataclass
class ResourceIdentity:
    """
    资源标识：Type + ID。
    枚举化的 ResourceType 避免字符串碎片化。
    资源完整属性存放在 Knowledge Layer Entity Registry。
    """
    type: ResourceType
    id: str


@dataclass
class EventParticipant:
    """
    事件参与角色。
    role 不是 Resource 的属性，而是事件中的角色。
    同一个资源在不同事件中可以有不同的 role。
    """
    resource: ResourceIdentity
    role: str       # actor, target, source, destination, ...


@dataclass
class EventCategory:
    """
    事件类别：七维模型。
    schema_version 确保 EventCategory 可向前兼容。

    example: v1 / infra / compute / INSTANCE / CREATE / end / success
    """
    schema_version: str = "v1"
    category: str = ""      # infra, platform, app, security, ...
    domain: str = ""        # compute, network, volume, image, k8s, vmware, ...
    resource: str           # INSTANCE, VOLUME, PORT, POD, NODE, ...
    action: str             # CREATE, DELETE, ATTACH, DETACH, REBOOT, ...
    phase: str = ""         # start, end, error
    outcome: str = ""       # success, failure, unknown


@dataclass
class NormalizedEvent:
    """Semantic Engine 输出的统一 Event 模型（四分区结构）"""

    # 时间 & 标识
    timestamp: datetime
    service_name: str
    pod_name: str
    namespace: str
    host: str
    source_cluster: str

    severity: str               # INFO, WARN, ERROR, FATAL
    message: str
    pid: int = 0
    thread: str = ""

    # -- event: 事件分类信息 --
    event: EventCategory

    # -- context: 请求上下文 --
    trace_id: str = ""
    span_id: str = ""
    request_id: str = ""
    global_request_id: str = ""

    # -- entities: 实体列表 --
    entities: List[ResourceIdentity] = field(default_factory=list)

    # -- participants: 参与角色（可选，丰富关联语义） --
    participants: List[EventParticipant] = field(default_factory=list)

    # -- attributes: 原始保留 --
    attributes_json: str = ""
    labels_json: str = ""

    # -- 查询加速列（ClickHouse 索引，与 entities 保持同步） --
    instance_uuid: str = ""
    volume_id: str = ""
    port_id: str = ""
    image_id: str = ""
    aggregate: str = ""
```

### 3.3 示例

```python
# Attach volume 日志解析后的 NormalizedEvent
event = NormalizedEvent(
    timestamp="2026-01-01T00:00:00Z",
    service_name="nova-compute",
    host="compute-01",
    severity="INFO",
    message="Attaching volume vol-456 to instance abc-123",
    # event
    event=EventCategory(
        schema_version="v1",
        category="infra",
        domain="volume",
        resource="INSTANCE",
        action="ATTACH_VOLUME",
        phase="end",
        outcome="success",
    ),
    # context
    trace_id="trace-xxx",
    request_id="req-abc-123",
    # entities
    entities=[
        ResourceIdentity(ResourceType.INSTANCE, "abc-123"),
        ResourceIdentity(ResourceType.VOLUME, "vol-456"),
    ],
    # participants
    participants=[
        EventParticipant(ResourceIdentity(ResourceType.INSTANCE, "abc-123"), role="actor"),
        EventParticipant(ResourceIdentity(ResourceType.VOLUME, "vol-456"), role="target"),
    ],
    instance_uuid="abc-123",
    volume_id="vol-456",
)
```

### 3.4 Event 消费关系

```
NormalizedEvent
    |
    +-- Event Store (ClickHouse)    -> event / context / entities / attributes
    +-- Interaction Builder         -> entities + context -> Interaction
    +-- Entity Builder              -> entities -> Knowledge Entity + Static Relationship
    +-- Fact Builder                -> event + entities -> Knowledge Fact (conservative)
    +-- EventSource                 -> stream to Correlation Engine
    +-- Topology Engine             -> service_name + entities -> Node lookup
    +-- AI Service                  -> message + entities + event -> reasoning
    +-- Rule Engine                 -> event.category + severity -> rule matching
```

---

## 4. Semantic Engine 改造

### 4.1 新增 extract_event_category()

```python
# semantic-engine/normalize/operation.py

_OPERATION_PATTERNS = {
    # (event_type_prefix, action_verb)
    #   -> (category, domain, resource, action, phase, outcome)
    ("compute.instance.create.start", None):
        ("infra", "compute", "INSTANCE", "CREATE", "start", ""),
    ("compute.instance.create.end", None):
        ("infra", "compute", "INSTANCE", "CREATE", "end", "success"),
    ("compute.instance.create.error", None):
        ("infra", "compute", "INSTANCE", "CREATE", "end", "failure"),
    ("compute.instance.delete.end", None):
        ("infra", "compute", "INSTANCE", "DELETE", "end", "success"),
    ("compute.instance.rebuild.end", None):
        ("infra", "compute", "INSTANCE", "REBUILD", "end", "success"),
    ("volume.attach.end", None):
        ("infra", "volume", "INSTANCE", "ATTACH_VOLUME", "end", "success"),
    ("volume.detach.end", None):
        ("infra", "volume", "INSTANCE", "DETACH_VOLUME", "end", "success"),
    ("port.create.end", None):
        ("infra", "network", "PORT", "CREATE", "end", "success"),
    ("image.create.end", None):
        ("infra", "image", "IMAGE", "CREATE", "end", "success"),

    # Phase 2: message text fallback
    (None, "attach"):           ("", "", "INSTANCE", "ATTACH_VOLUME", "", ""),
    (None, "detach"):           ("", "", "INSTANCE", "DETACH_VOLUME", "", ""),
    (None, "spawn"):            ("", "", "INSTANCE", "SPAWN", "", ""),
    (None, "create_server"):    ("", "", "INSTANCE", "CREATE", "", ""),
}


def extract_event_category(log_data: Dict[str, Any]) -> EventCategory:
    """从日志中提取标准化 EventCategory"""
    event_type = _candidate_text(log_data.get("event_type") or "")
    action = _candidate_text(log_data.get("action") or "")

    for (ev_prefix, act_verb), result in _OPERATION_PATTERNS.items():
        if ev_prefix and event_type.startswith(ev_prefix):
            return EventCategory(schema_version="v1", *result)

    for (ev_prefix, act_verb), result in _OPERATION_PATTERNS.items():
        if act_verb and action and act_verb in action.lower():
            return EventCategory(schema_version="v1", *result)

    outcome = _detect_outcome_from_severity(log_data.get("severity", ""))
    return EventCategory(schema_version="v1",
                         resource="UNKNOWN", action="UNKNOWN", outcome=outcome)


def _detect_outcome_from_severity(severity: str) -> str:
    if severity.upper() in ("ERROR", "FATAL", "CRITICAL"):
        return "failure"
    return "unknown"
```

### 4.2 新增 extract_entities()

从 `_raw_attributes` 和 message 中提取实体。输出 `List[ResourceIdentity]`。

```python
# semantic-engine/normalize/resource.py

_RESOURCE_FIELD_MAP = {
    ResourceType.INSTANCE: ["instance_uuid", "instance_id", "server_id", "uuid"],
    ResourceType.VOLUME:   ["volume_id", "volumeId"],
    ResourceType.PORT:     ["port_id", "portId"],
    ResourceType.IMAGE:    ["image_id", "imageId", "image_uuid"],
}

# Transformation Map（Observation -> Transformation 间的语义解析）
_TRANSFORMATION_MAP = {
    "device_id":     ResourceType.INSTANCE,
    "consumer_uuid": ResourceType.INSTANCE,
    "domain_uuid":   ResourceType.INSTANCE,
    "qemu_uuid":     ResourceType.INSTANCE,
    "snapshot_id":   ResourceType.VOLUME,
    "attachment_id": ResourceType.VOLUME,
}

_PARTICIPANT_ROLES = {
    (ResourceType.INSTANCE, "ATTACH_VOLUME"): {ResourceType.INSTANCE: "actor",
                                                ResourceType.VOLUME: "target"},
    (ResourceType.INSTANCE, "DETACH_VOLUME"): {ResourceType.INSTANCE: "actor",
                                                ResourceType.VOLUME: "target"},
    (ResourceType.INSTANCE, "CREATE"):        {ResourceType.INSTANCE: "actor"},
    (ResourceType.VOLUME, "CREATE"):          {ResourceType.VOLUME: "actor"},
    (ResourceType.PORT, "CREATE"):            {ResourceType.PORT: "actor",
                                                ResourceType.NETWORK: "target"},
    (ResourceType.INSTANCE, "LIVE_MIGRATE"):  {ResourceType.INSTANCE: "actor",
                                                ResourceType.HOST: "source"},
}


def extract_entities(log_data: Dict[str, Any]) -> List[ResourceIdentity]:
    """提取实体列表。返回 dedup 后的 ResourceIdentity 列表。"""
    raw_attrs = log_data.get("_raw_attributes", {}) or {}
    message = str(log_data.get("message", ""))

    found = {}
    for res_type, source_keys in _RESOURCE_FIELD_MAP.items():
        for source_key in source_keys:
            value = _find_value(raw_attrs, message, source_key)
            if value:
                found[source_key] = (res_type, value)
                break

    # Transformation: 归一化别名
    for alias_key, res_type in _TRANSFORMATION_MAP.items():
        if not any(v[0] == res_type for v in found.values()):
            value = _find_value(raw_attrs, message, alias_key)
            if value:
                found[alias_key] = (res_type, value)

    seen = set()
    entities = []
    for field_name, (res_type, uuid) in found.items():
        if not uuid or uuid in seen:
            continue
        seen.add(uuid)
        entities.append(ResourceIdentity(type=res_type, id=uuid))
    return entities


def assign_participants(category: EventCategory,
                         entities: List[ResourceIdentity]) -> List[EventParticipant]:
    """根据 EventCategory 为实体分配参与角色。"""
    res_type = ResourceType(category.resource) if category.resource else None
    role_rules = _PARTICIPANT_ROLES.get((res_type, category.action), {})

    participants = []
    for entity in entities:
        role = role_rules.get(entity.type, "")
        participants.append(EventParticipant(resource=entity, role=role))
    return participants
```

### 4.3 normalize_log() 集成

```python
def normalize_log(log_data: Dict[str, Any]) -> NormalizedEvent:
    category = extract_event_category(log_data)
    entities = extract_entities(log_data)
    participants = assign_participants(category, entities)

    entity_uuids = _extract_flat_uuids(entities)

    return NormalizedEvent(
        timestamp=...,
        service_name=...,
        host=...,
        severity=...,
        message=...,
        event=category,
        entities=entities,
        participants=participants,
        trace_id=...,
        request_id=...,
        global_request_id=...,
        instance_uuid=entity_uuids.get("instance_uuid", ""),
        volume_id=entity_uuids.get("volume_id", ""),
        port_id=entity_uuids.get("port_id", ""),
        image_id=entity_uuids.get("image_id", ""),
        attributes_json=json.dumps(raw_attributes),
    )
```

---

## 5. Event Source 接口

### 5.1 接口定义

```python
# correlation-engine/event_source.py

class EventSource(ABC):
    """
    事件源抽象接口。
    Correlation Engine 只消费事件流，不关心存储后端。

    Implementations:
        ClickHouseEventSource   - 历史查询
        KafkaEventSource        - 实时流
        IcebergEventSource      - 离线分析
        ReplayEventSource       - 测试回放
    """

    @abstractmethod
    def get_events(self, time_window: str,
                   filters: Optional[Dict[str, str]] = None) -> List[NormalizedEvent]:
        """查询历史事件（批量）。"""
        ...

    @abstractmethod
    def stream_events(self, time_window: str,
                       filters: Optional[Dict[str, str]] = None) -> Iterator[NormalizedEvent]:
        """流式读取事件。"""
        ...

    @abstractmethod
    def subscribe(self, callback: Callable[[NormalizedEvent], None],
                   filters: Optional[Dict[str, str]] = None) -> str:
        """
        订阅实时事件流（推模式）。
        返回 subscription_id，可用于取消订阅。
        适配 Kafka / NATS / RabbitMQ / WebSocket。
        """
        ...

    @abstractmethod
    def unsubscribe(self, subscription_id: str):
        """取消订阅。"""
        ...

    @abstractmethod
    def timeline(self, entity: ResourceIdentity,
                 time_window: str) -> List[NormalizedEvent]:
        """查询单个实体的时间线。"""
        ...

    @abstractmethod
    def get_participating_services(self, time_window: str
        ) -> List[Tuple[ResourceIdentity, str, datetime]]:
        """查询所有 (entity, service_name, timestamp) 三元组。"""
        ...
```

### 5.2 实现示例

```python
class ClickHouseEventSource(EventSource):
    def stream_events(self, time_window, filters=None):
        yield from self._query_stream("""
            SELECT ... FROM logs.logs
            PREWHERE timestamp > now() - INTERVAL {time_window}
            ...
        """)

    def subscribe(self, callback, filters=None):
        # ClickHouse 不支持推模式，通过轮询实现
        subscription_id = uuid4().hex
        self._subscriptions[subscription_id] = (callback, filters)
        return subscription_id


class KafkaEventSource(EventSource):
    def subscribe(self, callback, filters=None):
        # 原生推模式
        self._consumer.subscribe(self._topic)
        subscription_id = uuid4().hex
        self._start_poll_loop(subscription_id, callback, filters)
        return subscription_id


class ReplayEventSource(EventSource):
    def __init__(self, fixtures: List[NormalizedEvent]):
        self._fixtures = fixtures

    def get_events(self, time_window, filters=None):
        return self._fixtures

    def subscribe(self, callback, filters=None):
        for event in self._fixtures:
            callback(event)
        return "replay-done"
```

---

## 6. Knowledge Layer

### 6.1 定位

Knowledge Layer 是 Logoscope 的**静态知识存储**——只保存从 API / Inventory / 通知中确认的实体和事实，**不保存运行时动态关系**。

```
Knowledge Layer
+-- Entity Registry           # "有什么" — 资源实例及其属性
|     Instance, Volume, Port, Host, Pod, Node
|     (Neo4j 节点 + 属性)
|
+-- Fact Registry             # "是什么" — 实体的保守属性事实
|     instance.image=cirros, port.mac=fa:16:3e:...
|     (KV + 时间戳 + 置信度 + 来源)
|     供 Rule Engine 做条件匹配、AI 做推理上下文
|
+-- Static Relationship Reg.  # "怎么连" — 实体间的静态关系
      Instance -> Host, Port -> Network, Volume -> Instance
      (Neo4j 边，来源：API / Inventory / Resource Model)
```

**Knowledge Layer 不包含：**
- 运行时调用关系（Nova calls Neutron）— 归 **Correlation Store**
- 不确定的推导状态（instance=ACTIVE）— Fact Builder 只做保守推断
- Metadata Registry — 全部归入 **Fact Registry**

**Static Relationship ≠ Dynamic Relationship：**

| 类型 | 例子 | 来源 | 属于 |
|------|------|------|------|
| Static | Instance -> Host | API / Inventory | Knowledge Layer |
| Static | Port -> Network | Neutron API | Knowledge Layer |
| Static | Volume -> Instance | Cinder API | Knowledge Layer |
| Dynamic | Nova calls Neutron | Log Correlation | Correlation Store |
| Dynamic | Nova depends on DB | Log Correlation | Correlation Store |

**为什么分开：**
- 所有权清晰：Knowledge Layer 拥有资源模型，Correlation Store 拥有行为模型
- AI 不会混淆 `attached_to`（资源关系）和 `calls`（行为关系）
- 各自的 schema 和生命周期不同——Static Relationship 更新慢（分钟级），Dynamic Relationship 变化快（秒级）

### 6.2 消费关系

```
                  +-----------------------+
                  |     Knowledge Layer     |
                  |                         |
                  |  Entity Registry        |
                  |  Fact Registry          |
                  |  Static Relationship    |
                  +-----------------------+
                        ^     ^      ^
                        |     |      |
              +---------+     |      +----------+
              v               v                 v
       Correlation       Topology              AI
       (static rel       (Node 查找)       (推理上下文)
        as evidence)                       
              ^
              |
       Rule Engine
       (Fact 匹配)
```

### 6.3 同步策略

```
主同步: NormalizedEvent（平台无关）
    Semantic Engine -> NormalizedEvent
        |
    Entity Builder -> Entity Registry (MERGE)
    Fact Builder   -> Fact Registry (UPSERT, conservative only)
        |
    Neo4j updated
```

```
冷同步: API 定期轮询（降级方案）
    OpenStack / K8s / VMware API
        |
    Entity Builder (适配器模式)
        |
    Neo4j MERGE
```

### 6.4 Fact Builder（保守策略）

Fact Builder **不推测资源状态**。它只做两件事：
1. 直接从 NormalizedEvent 提取的事实（CREATE.end → lifecycle.create_completed）
2. 通过 Transformation 确认的语义等价关系（device_id = instance_uuid）

```python
# knowledge-layer/builders/fact_builder.py

class FactBuilder:
    """
    保守 Fact Builder。
    只产出可直接观测的事实，不推断资源状态。
    例如：CREATE.end 产出 lifecycle.create_completed
          不产出 state=ACTIVE（中间可能有 BUILD/spawn/network）
    """

    _LIFECYCLE_FACTS = {
        ("INSTANCE", "CREATE", "end", "success"):   "lifecycle.create_completed",
        ("INSTANCE", "DELETE", "end", "success"):   "lifecycle.delete_completed",
        ("INSTANCE", "REBOOT", "end", "success"):   "lifecycle.reboot_completed",
        ("VOLUME", "CREATE", "end", "success"):     "lifecycle.create_completed",
        ("PORT", "CREATE", "end", "success"):       "lifecycle.create_completed",
    }

    def process_event(self, event: NormalizedEvent):
        for entity in event.entities:
            # Conservative: only directly observable lifecycle facts
            fact_key = self._derive_lifecycle(event)
            if fact_key:
                self.fact_registry.upsert(Fact(
                    entity=entity,
                    key=fact_key,
                    value="true",
                    timestamp=event.timestamp,
                    source="derived",
                    confidence=0.9,
                ))

    def _derive_lifecycle(self, event) -> Optional[str]:
        key = (event.event.resource, event.event.action,
               event.event.phase, event.event.outcome)
        return self._LIFECYCLE_FACTS.get(key)
```

### 6.5 Fact Registry 数据模型

```python
@dataclass
class Fact:
    """
    事实：实体的某种属性。
    保守推断，不推测资源状态。

    Rule Engine: fact.key == "lifecycle.create_completed"
    AI: fact.source == "derived" AND fact.confidence > 0.8
    """
    entity: ResourceIdentity
    key: str                  # "lifecycle.create_completed", "image", "mac"
    value: str                # "true", "cirros", "fa:16:3e:..."
    timestamp: datetime
    source: str               # "derived", "api", "knowledge", "ai"
    confidence: float = 1.0
```

### 6.6 Knowledge Layer API

```text
# 查询一个实体的所有 Static Relationships
GET /api/v1/knowledge/relationships?type=INSTANCE&id=abc-123
-> {
    "entity": {"type": "INSTANCE", "id": "abc-123"},
    "static_relationships": [
        {"relation": "RUNS_ON",    "target": {"type": "HOST", "id": "compute-01"}},
        {"relation": "HAS_PORT",   "target": {"type": "PORT", "id": "port-xyz"}},
        {"relation": "ATTACHED_TO","target": {"type": "VOLUME", "id": "vol-789"}},
    ]
}

# 查询一个实体的 Facts
GET /api/v1/knowledge/facts?type=INSTANCE&id=abc-123
-> {
    "facts": [
        {"key": "lifecycle.create_completed", "value": "true",
         "source": "derived", "confidence": 0.9},
        {"key": "image", "value": "cirros",
         "source": "api", "confidence": 1.0},
    ]
}

# 路径查询
GET /api/v1/knowledge/path?source=PORT:port-xyz&target=INSTANCE
-> {"path": [{type, id, relation}, ...]}
```

---

## 7. Correlation Engine

### 7.1 架构

独立模块。消费 Interaction（来自 Interaction Builder），输出 Dynamic Relationship 到 Correlation Store。

```
correlation-engine/
+-- __init__.py
+-- engine.py              # CorrelationEngine 入口
+── base.py                # CorrelationProvider 抽象基类
+── event_source.py        # EventSource 抽象接口
+── models.py              # Observation, Transformation, Fact, Evidence, Inference, DynamicRelationship
+── merger.py              # EvidenceMerger 多源证据融合
+── store.py               # CorrelationStore 抽象接口（Dynamic Relationship 持久化）
|
+-- providers/
|   +-- request_correlator.py     # request_id 分组
|   +-- resource_correlator.py    # ResourceIdentity (type-agnostic)
|   +-- time_correlator.py        # host + time_window
|   +-- host_correlator.py        # host + pid + thread (Phase 2)
|
+-- tests/
```

### 7.2 五层证据链（Observation -> Transformation -> Fact -> Evidence -> Inference）

```python
# correlation-engine/models.py

@dataclass
class Observation:
    """
    原始观测：一条日志中出现的事实。
    自带 confidence，来源不同置信度不同：
      - Regex: 0.8
      - LLM:   0.6
      - API:   1.0
    """
    log_id: str
    service_name: str
    timestamp: datetime
    observed_field: str           # "device_id"
    observed_value: str           # "abc-123"
    confidence: float = 0.8       # 来源置信度
    source_detail: str = ""       # "regex: _raw_attributes"


@dataclass
class Transformation:
    """
    语义转换：将 Observation 解析为目标类型。

    Observation = "device_id=abc"         (日志原文, confidence=0.8)
    Transformation = "INSTANCE=abc-123"   (已解析, confidence=1.0)
    """
    transformation_type: str    # "alias_resolution", "field_extraction"
    observation: Observation
    resolved_type: ResourceType
    resolved_id: str
    confidence: float = 1.0


@dataclass
class Fact:
    """
    事实：经过 Transformation + Knowledge 确认后的确定性信息。

    Fact = "instance=abc 在知识层存在" (已确认)
    """
    fact_type: str            # "entity_exists", "alias_confirmed"
    subject_type: ResourceType
    subject_value: str
    object_type: ResourceType
    object_value: str
    source: str               # "transformation", "knowledge_layer", "api"
    confidence: float = 1.0


@dataclass
class Evidence:
    """
    证据：同一条事实出现在两个服务中。
    例如：instance abc-123 出现在 nova-api 和 nova-compute。
    """
    source_service: str
    target_service: str
    evidence_type: str            # "resource_match", "request_match"
    match_value: str              # "INSTANCE:abc-123"
    weight: float
    transformations: List[Transformation] = field(default_factory=list)


@dataclass
class Inference:
    """
    推断：根据证据推断两个服务之间存在行为关系。
    """
    source: str
    target: str
    evidence_list: List[Evidence]
    inferred_relationship: str    # "calls", "depends_on", "runs_on"
```

### 7.3 DynamicRelationship（Correlation Engine 输出）

```python
@dataclass
class DynamicRelationship:
    """
    动态关系：Correlation Engine 的最终输出。
    描述两个服务之间"存在某种行为关系"——通过聚合多条 Interaction 得出。

    属于 Correlation Store，不属于 Knowledge Layer。

    不可变记录：relationship_id / version / created_at。
    """
    relationship_id: str
    version: int = 1
    created_at: datetime = field(default_factory=datetime.utcnow)
    source: str
    target: str
    relationship_type: str = "calls"  # calls, depends_on, runs_on, peers
    confidence: float
    interaction_ids: List[str]        # 聚合的 Interaction ID 列表
    call_count: int
    inferences: List[Inference]       # 完整推断链
    data_sources: List[str]
```

### 7.4 CorrelationProvider 接口

```python
# correlation-engine/base.py

class CorrelationProvider(ABC):
    """关联提供者：从一种维度发现候选关系。"""

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    def correlate(self, interactions: List[Interaction], **kwargs
                  ) -> Tuple[List[Fact], List[Inference]]:
        """
        从已知的 Interaction 中聚合关联。

        Args:
            interactions: 来自 Interaction Builder 的交互记录
        Returns:
            (facts, inferences)
        """
        ...
```

### 7.5 ResourceCorrelator（type-agnostic）

```python
class ResourceCorrelator(CorrelationProvider):
    """
    按 ResourceIdentity 分组发现关联。
    不硬编码任何资源类型 — 匹配任何在两个服务中出现的 (type, id) 对。

    新增 Kubernetes：只需 Semantic Engine 提取 POD/PVC 等 entity，
    此 Provider 零改动。
    """
    name = "resource_correlator"
    RESOURCE_MATCH_WEIGHT = 0.45

    def correlate(self, interactions: List[Interaction], **kwargs
                  ) -> List[Inference]:
        # 对每个 Interaction，检查里面的 entity
        # 相同 (type, id) 出现在不同 service 的多个 Interaction 中
        # -> Observation -> Transformation -> Fact -> Evidence -> Inference
        ...
```

### 7.6 EvidenceMerger

```python
class EvidenceMerger:
    """
    多源证据融合。

    保留完整追溯链：
    DynamicRelationship.inferences[i].evidence_list[j].transformations[k].observation
    """

    EVIDENCE_BASE_WEIGHTS = {
        "request_match":  0.20,
        "resource_match": 0.45,
        "time_window":    0.10,
        "message_target": 0.20,
        "host_match":     0.15,
        "ai_inferred":    0.10,
        "static_relation": 0.30,   # Knowledge Layer 确认的静态关系
    }

    WEIGHT_DECAY_MINUTES = 120

    def merge(self, inferences: List[Inference],
              facts: Optional[List[Fact]] = None
              ) -> Dict[Tuple[str, str], DynamicRelationship]:
        """
        合并所有 Inference -> DynamicRelationship。
        relationship_id 为不可变 UUID。
        """
        ...
```

### 7.7 Inference Engine（AI / Rule / ML / Graph）

AI **不是** CorrelationProvider。它是 Inference Engine 的一种实现。

```python
# inference-engine/base.py

class InferenceEngine(ABC):
    """
    推理引擎：对 Evidence 做推理，辅助或替代 Correlation Provider。

    Implementations:
        AICorrelationEngine    — LLM 推理
        RuleEngine             — 规则推理
        GraphEngine            — 图推理
        MLEngine               — 机器学习推理
    """

    @abstractmethod
    def infer(self, evidence: List[Evidence],
              facts: List[Fact]) -> List[Inference]:
        """
        对一组证据做推理，产出可能的 Inference。
        """
        ...
```

**为什么独立：**
- AI 本质不是"关联提供者"——它不知道 `request_id` 或 `ResourceIdentity`，它只是对证据做推断
- Rule、Graph、ML 都可以实现 InferenceEngine 接口，各自有自己的置信度模型
- Correlation Engine 可以集成 InferenceEngine 的结果，但不依赖它

---

## 8. Interaction（运行时交互）

### 8.1 数据流方向（v5 反了，v5.2 修正）

```
v5 (错误):
  Relationship -> Interaction -> Topology
  ^ Relationship 在前，Interaction 在后，方向反了

v5.2 (正确):
  NormalizedEvent -> Interaction -> Correlation -> Dynamic Relationship
  ^ Interaction 先于 Correlation，聚合出 Dynamic Relationship
```

**为什么 v5 是错的：**
- Interaction 是单次运行时交互（"09:00 Nova 调用了 Neutron"）
- Dynamic Relationship 是多次 Interaction 的聚合（"过去一小时 Nova 调了 100 次 Neutron"）
- 聚合的方向是 Interaction → Dynamic Relationship，不是反过来

### 8.2 Interaction 模型

```python
# interaction-builder/models.py

@dataclass
class Interaction:
    """
    运行时交互：一次具体的服务间调用。
    直接从 NormalizedEvent 构建，不依赖 Relationship。

    Interaction 是"原子交互记录"：
    - "2026-01-01T00:00:00Z Nova -> Neutron"
    - 单次调用，不聚合
    """
    interaction_id: str
    timestamp: datetime
    source: str
    target: str
    interaction_type: str       # "calls", "rpc", "api_call", "message"
    duration_ms: float = 0.0
    request_id: str = ""
    entities: List[ResourceIdentity] = field(default_factory=list)
    outcome: str = ""           # "success", "failure", "timeout"
```

### 8.3 InteractionBuilder

从 NormalizedEvent 构建 Interaction。每个 NormalizedEvent 可能对应 0-N 个 Interaction（取决于是否存在于两个服务之间的调用证据）。

```python
# interaction-builder/builder.py

class InteractionBuilder:
    """
    从 NormalizedEvent 构建 Interaction。
    规则：
    - 同一 request_id 出现在两个服务中 -> Interaction
    - 同一 trace_id + span_id 跨服务 -> Interaction
    - 同 (entity, service) 在时间窗口内 -> Interaction
    """

    def build(self, events: List[NormalizedEvent]) -> List[Interaction]:
        """
        从一批 NormalizedEvent 中构建 Interaction。
        不是从 Relationship 反推。
        """
        interactions = []

        # Method 1: request_id 匹配
        request_groups = self._group_by_request_id(events)
        for req_id, group in request_groups.items():
            if len(group) >= 2:
                for source_event, target_event in self._pair_services(group):
                    interactions.append(Interaction(
                        interaction_id=uuid4().hex,
                        timestamp=target_event.timestamp,
                        source=source_event.service_name,
                        target=target_event.service_name,
                        interaction_type="calls",
                        request_id=req_id,
                        entities=self._merge_entities(source_event, target_event),
                    ))

        # Method 2: trace_id 匹配
        # Method 3: entity 时间窗口匹配
        ...

        return interactions
```

---

## 9. Topology Engine

### 9.1 精简后的职责

Topology Engine 从 Correlation Store 读取 Dynamic Relationship + Interaction，从 Knowledge Layer 查找 Node 属性。

```
TopologyEngine
+-- 1. 消费 DynamicRelationship[] + Interaction[]
+-- 2. Node 查找：两类实体
|     Infrastructure Entity — INSTANCE, HOST, PORT, VOLUME (从 Knowledge Layer)
|     Execution Entity      — SERVICE, POD, PROCESS (从 Knowledge Layer + Cluster)
+-- 3. Layout + Render
```

### 9.2 Node 类型

Topology Engine 的 Node 分为两类：

| 类型 | 例子 | 来源 | 属性 |
|------|------|------|------|
| **Infrastructure Entity** | Instance, Host, Port, Volume, Network | Knowledge Layer Entity Registry | type, id, name, zone, status |
| **Execution Entity** | nova-api, nova-compute, kubelet | Knowledge Layer + Cluster API | service_name, version, host, deployment |

```python
@dataclass
class Node:
    node_id: str
    name: str
    node_type: NodeType  # INFRASTRUCTURE, EXECUTION
    entity_type: Optional[ResourceType] = None  # INSTANCE, SERVICE, ...
    attributes: Dict[str, str] = field(default_factory=dict)
```

### 9.3 实现

```python
class TopologyEngine:
    """
    拓扑引擎：纯构图。
    Node 从 Knowledge Layer 查找，分 Infrastructure / Execution 两类。
    """

    def __init__(self, knowledge_layer: KnowledgeLayer,
                 correlation_store: CorrelationStore):
        self.knowledge_layer = knowledge_layer
        self.correlation_store = correlation_store

    def build(self, time_window: str) -> TopologyResult:
        # 1. 获取 Dynamic Relationship + Interaction
        relationships = self.correlation_store.query(time_window)
        interactions = self.correlation_store.query_interactions(time_window)

        # 2. Node 查找
        nodes = self._lookup_nodes(relationships, interactions)

        # 3. Interaction -> Edge（聚合）
        edges = self._interactions_to_edges(interactions)

        # 4. 布局
        layout = self._compute_layout(nodes, edges)

        return TopologyResult(nodes=nodes, edges=edges, layout=layout)

    def _lookup_nodes(self, relationships: List[DynamicRelationship],
                       interactions: List[Interaction]) -> List[Node]:
        """
        从 Knowledge Layer 查找所有 Node。
        Infrastructure Entity -> Knowledge Entity Registry
        Execution Entity      -> Knowledge SERVICE Entity
        """
        service_names = set()
        for rel in relationships:
            service_names.add(rel.source)
            service_names.add(rel.target)
        for ia in interactions:
            service_names.add(ia.source)
            service_names.add(ia.target)

        nodes = []
        for svc in sorted(service_names):
            entity = self.knowledge_layer.find_entity(ResourceType.SERVICE, svc)
            if entity:
                nodes.append(Node(
                    node_id=svc, name=svc,
                    node_type=NodeType.EXECUTION,
                    entity_type=ResourceType.SERVICE,
                    attributes=entity.attributes,
                ))
            else:
                nodes.append(Node(
                    node_id=svc, name=svc,
                    node_type=NodeType.EXECUTION,
                ))
        return nodes

    def _interactions_to_edges(self, interactions: List[Interaction]) -> List[Edge]:
        """Interaction -> Edge。聚合相同 (source, target, interaction_type)。"""
        # Group by (source, target, interaction_type)
        # Agg: call_count, avg_duration, last_seen
        ...
```

---

## 10. 置信度模型

### 10.1 详细计算公式

```
For each (source, target) pair:

  base = 0.3                        # 起始基础值

  resource_match:
    if (type, id) in both services:
      base += 0.45

  request_match:
    if global_request_id matches:   base += 0.35
    if request_id matches:          base += 0.20

  time_window:
    if < 0.5s same host:            base += 0.10
    if < 0.1s same host:            base += 0.15

  message_match:
    if has target match:            base += 0.20
    if inbound+outbound:            base += 0.15

  static_relation:
    if Knowledge Layer confirms:    base += 0.30

  max_confidence = min(base, 0.98)

  time_decay:
    if minutes_since_last_seen > 120:
      decay = 0.5 ^ (minutes / 120)
    else: decay = 1.0

  final = max_confidence * decay
```

### 10.2 O-T-F-E-I 追溯示例

```json
{
  "relationship_id": "rel-abc-123",
  "source": "nova-api",
  "target": "neutron-server",
  "confidence": 0.90,
  "inferences": [
    {
      "inferred_relationship": "calls",
      "evidence_list": [
        {
          "evidence_type": "resource_match",
          "match_value": "INSTANCE:abc-123",
          "weight": 0.45,
          "transformations": [
            {
              "transformation_type": "alias_resolution",
              "observation": {
                "log_id": "nova-api-log-001",
                "service_name": "nova-api",
                "observed_field": "instance_uuid",
                "observed_value": "abc-123",
                "confidence": 0.8
              },
              "resolved_type": "INSTANCE",
              "resolved_id": "abc-123",
              "confidence": 1.0
            },
            {
              "transformation_type": "alias_resolution",
              "observation": {
                "log_id": "neutron-log-042",
                "service_name": "neutron-server",
                "observed_field": "device_id",
                "observed_value": "abc-123",
                "confidence": 0.8
              },
              "resolved_type": "INSTANCE",
              "resolved_id": "abc-123",
              "confidence": 1.0
            }
          ]
        }
      ]
    }
  ]
}
```

---

## 11. API 变化

### 11.1 新端点

```text
# 拓扑查询
GET /api/v1/topology/hybrid?time_window=1+HOUR
-> {
    "nodes": [
      {"id": "Nova", "node_type": "EXECUTION", "attributes": {...}},
      {"id": "neutron-server", "node_type": "EXECUTION", "attributes": {...}},
    ],
    "edges": [{
      "edge_id": "edge-xyz",
      "source": "Nova",
      "target": "neutron-server",
      "confidence": 0.90,
      "relationship_id": "rel-abc-123",
      "call_count": 42
    }]
  }

# 证据详情（Correlation Engine 直接输出）
GET /api/v1/evidence?source=Nova&target=Neutron&time_window=1+HOUR
-> {
    "relationships": [{
      "relationship_id": "rel-abc-123",
      "source": "Nova",
      "target": "Neutron",
      "confidence": 0.90,
      "inferences": [...],
    }]
  }

# 交互详情
GET /api/v1/interactions?source=Nova&target=Neutron&time_window=1+HOUR
-> {
    "interactions": [{
      "interaction_id": "int-001",
      "timestamp": "2026-01-01T00:00:00Z",
      "interaction_type": "calls",
      "duration_ms": 42,
      "outcome": "success",
      "request_id": "req-abc-123"
    }]
  }

# 知识层
GET /api/v1/knowledge/relationships?type=INSTANCE&id=abc-123
GET /api/v1/knowledge/facts?type=INSTANCE&id=abc-123

# 关联查询
GET /api/v1/correlate/services?source=Nova&time_window=1+HOUR

# 运行时交互订阅
GET /api/v1/interactions/subscribe
-> WebSocket / SSE stream
```

### 11.2 现有端点兼容

`GET /api/v1/topology/hybrid` 保持现有返回格式不变，仅在 `metrics` 内新增字段。

---

## 12. 实施阶段

### Phase 1: Core Data Architecture（约 3 周）

**目标：** NormalizedEvent v5.2 -> EventCategory 七维 -> EventSource -> InteractionBuilder -> Resource/Request Correlator -> EvidenceMerger

| 模块 | 文件 | 改动 |
|------|------|------|
| **New** | `semantic-engine/normalize/operation.py` | extract_event_category() |
| **New** | `semantic-engine/normalize/resource.py` | extract_entities(), assign_participants() |
| Modify | `semantic-engine/normalize/normalizer.py` | 集成新 extractors |
| Modify | `shared_src/logoscope_storage/adapter.py` | 新增列 DDL |
| **New** | `correlation-engine/` | 新建包 |
| **New** | `correlation-engine/models.py` | Observation, Transformation, Fact, Evidence, Inference, DynamicRelationship |
| **New** | `correlation-engine/base.py` | CorrelationProvider 基类 |
| **New** | `correlation-engine/event_source.py` | EventSource + ClickHouse/Kafka/Replay 实现 |
| **New** | `correlation-engine/engine.py` | CorrelationEngine |
| **New** | `correlation-engine/merger.py` | EvidenceMerger |
| **New** | `correlation-engine/store.py` | CorrelationStore 抽象 |
| **New** | `interaction-builder/builder.py` | InteractionBuilder |
| **New** | `inference-engine/base.py` | InferenceEngine 抽象基类 |
| Modify | `topology-service/graph/topology_engine.py` | 重写：两类 Node + Interaction->Edge |
| Modify | `topology-service/api/topology_routes.py` | 新增端点 |
| Tests | 5+ 新测试文件 | |

### Phase 2: Knowledge Layer（约 2 周）

| 模块 | 文件 | 改动 |
|------|------|------|
| **New** | `knowledge-layer/` | 新建服务 |
| **New** | `knowledge-layer/registry/entity_registry.py` | Entity Registry |
| **New** | `knowledge-layer/registry/fact_registry.py` | Fact Registry |
| **New** | `knowledge-layer/registry/static_relationship_registry.py` | 静态关系 |
| **New** | `knowledge-layer/builders/entity_builder.py` | NormalizedEvent -> Entity |
| **New** | `knowledge-layer/builders/fact_builder.py` | NormalizedEvent -> Fact (保守) |
| Modify | `topology_engine.py` | Node 查找从 Knowledge Layer |

### Phase 3: Host + Infrastructure（约 1 周）

| 模块 | 文件 | 改动 |
|------|------|------|
| **New** | `correlation-engine/providers/host_correlator.py` | host + pid + thread |

### Phase 4: Inference Engine + Multi-platform（约 2 周）

| 模块 | 文件 | 改动 |
|------|------|------|
| **New** | `inference-engine/providers/ai_engine.py` | AI Inference Engine |
| **New** | `inference-engine/providers/rule_engine.py` | Rule Inference Engine |
| Modify | `semantic-engine/normalize/resource.py` | K8s 实体提取 |
| Modify | `knowledge-layer/builders/` | 平台无关适配 |

---

## 13. 测试策略

### 13.1 Semantic Engine 测试

```python
def test_extract_instance_uuid():
    log = {"_raw_attributes": {"instance_uuid": "abc-123-def-456"}}
    entities = extract_entities(log)
    assert len(entities) == 1
    assert entities[0].type == ResourceType.INSTANCE

def test_device_id_transformation():
    """device_id -> INSTANCE 的 Transformation"""
    log = {"_raw_attributes": {"device_id": "abc-123-def-456"}}
    entities = extract_entities(log)
    assert entities[0].type == ResourceType.INSTANCE

def test_event_category_schema_version():
    """EventCategory 包含 schema_version"""
    log = {"_raw_attributes": {"event_type": "volume.attach.end"}}
    event = normalize_log(log)
    assert event.event.schema_version == "v1"
    assert event.event.category == "infra"
    assert event.event.action == "ATTACH_VOLUME"

def test_normalized_event_four_sections():
    """NormalizedEvent 四分区"""
    log = {"_raw_attributes": {"event_type": "volume.attach.end",
                                "instance_uuid": "abc-123"}}
    event = normalize_log(log)
    assert event.event is not None
    assert event.trace_id is not None  # context
    assert len(event.entities) > 0
```

### 13.2 Interaction Builder 测试

```python
def test_interaction_from_request_id():
    """同一 request_id 的两个 Event -> Interaction"""
    events = [
        NormalizedEvent(timestamp=..., service_name="A",
                        trace_id="trace-1", request_id="req-1",
                        entities=[ResourceIdentity(ResourceType.INSTANCE, "abc")]),
        NormalizedEvent(timestamp=..., service_name="B",
                        trace_id="trace-1", request_id="req-1",
                        entities=[ResourceIdentity(ResourceType.INSTANCE, "abc")]),
    ]
    builder = InteractionBuilder()
    interactions = builder.build(events)
    assert len(interactions) >= 1
    assert interactions[0].source == "A"
    assert interactions[0].target == "B"

def test_interaction_does_not_depend_on_relationship():
    """Interaction 不依赖 Relationship"""
    events = [NormalizedEvent(...)]
    builder = InteractionBuilder()
    interactions = builder.build(events)
    # OK — doesn't require any Relationship to exist
```

### 13.3 Correlation Engine 测试

```python
def test_interaction_aggregated_into_relationship():
    """多条 Interaction 聚合为一条 DynamicRelationship"""
    interactions = [
        Interaction("i1", datetime(...), "A", "B", "calls", request_id="r1"),
        Interaction("i2", datetime(...), "A", "B", "calls", request_id="r2"),
        Interaction("i3", datetime(...), "A", "B", "calls", request_id="r3"),
    ]
    source = ReplayEventSource([])
    correlator = ResourceCorrelator(source)
    result = correlator.correlate(interactions)
    # Should produce 1 relationship with call_count=3
    ...

def test_dynamic_relationship_has_interaction_ids():
    """DynamicRelationship 引用其聚合的 Interaction"""
    rel = DynamicRelationship(
        relationship_id="rel-1",
        source="A", target="B",
        interaction_ids=["i1", "i2", "i3"],
        call_count=3, confidence=0.9,
    )
    assert len(rel.interaction_ids) == 3

def test_transformation_preserved_in_chain():
    """Transformation 层在证据链中保留"""
    obs = Observation("log-1", "A", datetime(2026, 1, 1),
                      "device_id", "abc-123", confidence=0.8)
    trans = Transformation("alias_resolution", obs,
                           ResourceType.INSTANCE, "abc-123")
    evidence = Evidence("A", "B", "resource_match", "INSTANCE:abc", 0.45,
                        transformations=[trans])
    inference = Inference("A", "B", [evidence], "calls")
    merger = EvidenceMerger()
    result = merger.merge([inference])
    assert ("A", "B") in result
    rel = result[("A", "B")]
    assert rel.relationship_id != ""
    assert rel.inferences[0].evidence_list[0].transformations[0].transformation_type \
        == "alias_resolution"
```

### 13.4 Topology Engine 测试

```python
def test_topology_two_node_types():
    """Topology Engine 区分两类 Node"""
    kl = FakeKnowledgeLayer({
        ResourceType.SERVICE: {
            "A": {"version": "1.0"},
        }
    })
    engine = TopologyEngine(kl, FakeCorrelationStore())
    rels = [DynamicRelationship("rel-1", source="A", target="B",
                                relationship_type="calls", confidence=0.9)]
    result = engine.build(rels, [])
    assert any(n.node_type == NodeType.EXECUTION for n in result.nodes)

def test_topology_edge_from_interaction():
    """Interaction 聚合为 Edge"""
    kl = FakeKnowledgeLayer({})
    engine = TopologyEngine(kl, FakeCorrelationStore())
    interactions = [
        Interaction("i1", datetime(...), "A", "B", "calls", outcome="success"),
        Interaction("i2", datetime(...), "A", "B", "calls", outcome="success"),
    ]
    result = engine.build([], interactions)
    assert len(result.edges) == 1
    assert result.edges[0].call_count == 2

def test_topology_edge_relationship_id():
    """Topology Edge 引用 relationship_id"""
    kl = FakeKnowledgeLayer({})
    engine = TopologyEngine(kl, FakeCorrelationStore())
    rels = [DynamicRelationship("rel-1", source="A", target="B",
                                 relationship_type="calls", confidence=0.9,
                                 interaction_ids=["i1"])]
    result = engine.build(rels, [])
    assert result.edges[0].relationship_id == "rel-1"
```

### 13.5 Fact Builder 测试

```python
def test_fact_builder_conservative():
    """Fact Builder 不推断 state=ACTIVE"""
    event = NormalizedEvent(
        event=EventCategory(resource="INSTANCE", action="CREATE",
                             phase="end", outcome="success"),
    )
    builder = FactBuilder()
    facts = builder._derive_lifecycle(event)
    assert facts == "lifecycle.create_completed"  # not "state=ACTIVE"

def test_fact_builder_no_state_derivation():
    """CREATE.end 不等于 ACTIVE"""
    event = NormalizedEvent(
        event=EventCategory(resource="INSTANCE", action="CREATE",
                             phase="end", outcome="success"),
    )
    builder = FactBuilder()
    fact = builder.process_event(event)
    # 不会包含 state=ACTIVE
```

---

## 14. 性能考量

| 场景 | 当前 | 优化后 | 改善 |
|------|------|--------|------|
| 单次拓扑查询（1h 窗口） | ~800ms | ~500ms | Bloom filter + 独立列 |
| Interaction 构建 | 无 | ~100ms/1k | 批量处理 |
| Correlation 聚合 | 无 | ~200ms/10k | 按 Interaction 分组 |
| Knowledge Entity 查询 | 无 | ~5ms | Neo4j 索引 |
| Fact Registry 查询 | 无 | ~10ms | KV 索引 |
| EventSource 流式 | 无 | 实时 | Kafka |
| EventSource subscribe | 无 | 实时 | 推模式 |

---

## 15. 向后兼容

| 影响点 | 兼容策略 |
|--------|----------|
| ClickHouse 存量数据 | ALTER TABLE ADD COLUMN ... DEFAULT '' |
| 现有 API /hybrid | 保持输出格式不变，仅新增字段 |
| 现有测试 | 不改现有测试，新增模块测试 |
| 前端 | 新增字段不影响现有渲染逻辑 |
| Topology Service | Correlation Engine 可独立部署 |
| schema_version | 旧数据默认 schema_version="v0" |

---

## 16. Static vs Dynamic Relationship 对照

| 维度 | Static Relationship | Dynamic Relationship |
|------|--------------------|---------------------|
| **含义** | 资源间的固有关系 | 服务间的行为关系 |
| **例子** | Instance -> Host | Nova calls Neutron |
| **来源** | API / Inventory / CMDB | Log Correlation |
| **生命周期** | 分钟级更新 | 秒级更新 |
| **存储** | Knowledge Layer (Neo4j) | Correlation Store |
| **拥有者** | Entity Builder (Knowledge) | Correlation Engine |
| **变更频率** | 低（资源创建/删除） | 高（每次调用） |
| **消费者** | Topology, AI, Rule | Topology, AI, Timeline |
| **Schema** | 固定的资源关系类型 | 可扩展的行为关系类型 |
| **置信度** | 1.0（API 确认） | 0.3~0.98（多证据融合） |
