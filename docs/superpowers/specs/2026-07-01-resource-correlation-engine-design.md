# Logoscope Data Architecture v1

> **将 Logoscope 从"日志分析平台"升级为"事件知识平台" (Event Knowledge Platform)。**
>
> 定义从 Raw Log → Normalized Event → Interaction → Knowledge → Correlation → Graph → AI
> 的完整数据链，统一 OpenStack、Kubernetes、VMware、Linux、网络设备、AI Agent 等多平台的事件模型。
>
> **Key design decisions:**
> - NO `primary_resource` — all resources equal, expressed as `entities[ResourceIdentity]` + `participants[EventParticipant]`
> - NO hardcoded resource types in Correlation Engine — uses generic `ResourceIdentity` matching
> - **Knowledge Layer: Entity + Fact + Static Relationship + Identity Resolution (Alias Registry)**
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
> - **Every NormalizedEvent has a globally unique event_id** — all downstream references are stable
> - **Entity Registry has versioned records** (valid_from/valid_to) for temporal queries
> - **Fact has validity window** (valid_from/valid_to) for fact expiration
> - **Interaction is Endpoint→Endpoint**, not Service→Service — platform-agnostic
> - **Identity Resolution (Alias Registry)** centralizes canonical ID mapping in Knowledge Layer
> - **Dynamic Relationship has lifecycle** (first_seen/last_seen/status/expire_after)
> - **Schema Registry + Contract Layer**: all modules reference contracts, not implementations

**Status:** Draft v5.3
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

### 1.3 Schema Governance 前置条件

这份文档定义的每个数据结构都是一份**数据契约**。一旦代码开始编写，以下字段结构、类型、约束将极难修改。因此所有 Schema 在 v1 定稿时必须满足：

- **event_id**：全局唯一，所有下游模块稳定引用
- **version 字段**：所有可变记录带版本号
- **validity 窗口**：Fact 和 Entity 带有效时间
- **枚举化**：ResourceType、RelationshipType、Status 等用 Enum
- **Schema Registry**：所有 Schema 在注册表中记录版本和变更历史

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
        +--------------+--------------+
        |              |              |
        v              v              v
  Schema Registry  NormalizedEvent  Contract Layer
  (schemas)        (event_id + 4     (validation)
                    partitions)
                       |
           +-----------+---+--------+
           |           |            |
           v           v            v
   Interaction   Entity/Fact   EventSource
    Builder      Builder
           |           |            |
           +-----+-----+            |
                 |                  |
                 v                  v
          Knowledge Layer        Correlation Engine
   (Entity+Fact+Identity+Static)  (aggregates)
                 |                  |
                 +--------+---------+
                          |
                          v
                  Correlation Store
              (Dynamic Relationship)
                          |
                +---------+---------+
                |                   |
                v                   v
         Topology Engine     Inference Engine
         (Graph + Layout)    (AI/Rule/ML/Graph)
                |                   |
                v                   v
            Frontend / API     AI / Alerting
```

### 2.2 Contract Layer

所有模块不直接引用 Python dataclass，而是通过 **Schema Registry** 引用的契约接口：

```
Contract Layer
+-- EventSchema       # NormalizedEvent 结构定义
+-- EntitySchema      # Entity Registry 结构定义
+-- FactSchema        # Fact 结构定义
+-- RelationshipSchema # Static + Dynamic Relationship 结构定义
+-- InteractionSchema # Interaction 结构定义
```

```
每个模块的输出 → Schema Registry 校验 → 下游模块消费
                    (contract enforcement)
```

这并非要求第一版就用 Protobuf/Avro——可以是 Python dataclass + 单元测试级别的 schema 校验。关键是**结构变更必须经过 Contract Review**。

### 2.3 数据流

```
Raw Log
   v
Semantic Engine
   v
NormalizedEvent (event_id + event/context/entities/attributes)
   |
   +-- EntityBuilder --> Knowledge Layer
   |                     (Entity versioned, Fact with validity,
   |                      Alias Registry, Static Relationship)
   |
   +-- InteractionBuilder --> Interaction (Endpoint->Endpoint)
   |
   +-- EventSource --> Correlation Engine (aggregates Interactions)
                        |
                        v
                  Correlation Store
                  (Dynamic Relationship with lifecycle)
                        |
                        v
                  Topology Engine + Inference Engine
```

### 2.4 组件职责

| 层 | 职责 | 不做什么 |
|----|------|----------|
| **Schema Registry** | 管理所有数据契约、版本、迁移 | 不参与运行时数据流 |
| **Semantic Engine** | 日志理解、分类、标准化 NormalizedEvent | 不计算拓扑、不做关联 |
| **Interaction Builder** | 从 NormalizedEvent 构造 Endpoint→Endpoint Interaction | 不推断关系、不聚合 |
| **Entity Builder** | NormalizedEvent → Entity（versioned）+ Static Relationship + Alias | 不推断动态行为 |
| **Fact Builder** | NormalizedEvent → Fact（validity window, conservative only） | 不推断资源状态 |
| **EventSource** | 抽象事件流：get/stream/subscribe | 不涉及字段含义 |
| **Knowledge Layer** | Entity + Fact + Static Relationship + Identity Resolution | 不存储运行时关系 |
| **Correlation Store** | 存储 Dynamic Relationship（带 lifecycle） | 不涉及资源模型 |
| **Correlation Engine** | 聚合 Interaction → Dynamic Relationship | 不画图、不推理 |
| **Inference Engine** | 对 Evidence 做推理（AI/Rule/ML/Graph） | 不聚合、不存储 |
| **Topology Engine** | Interaction → lookup Node → Edge → Layout | 不计算置信度 |
| **Rule Engine** | 消费 Fact + Interaction 做规则匹配 | 不画图、不关联 |

---

## 3. Schema Registry + Contract Layer

### 3.1 定位

Schema Registry 是 Logoscope 的**数据契约中心**。它不参与运行时数据流，但定义所有模块之间传递的数据结构。

```
+-------------------------------+
|       Schema Registry         |
|                               |
|  EventSchema       v1 → v2   |
|  EntitySchema      v1        |
|  FactSchema        v1        |
|  RelationshipSchema v1       |
|  InteractionSchema v1        |
|                               |
|  每个 Schema 包含:             |
|  - 字段名 + 类型 + 约束       |
|  - 版本 + 变更历史            |
|  - 兼容性规则 (backward)      |
|  - 迁移路径                   |
+-------------------------------+
       ^
       |
所有模块输出 → Schema 校验 → 下游
```

### 3.2 第一版 Schema 清单

| Schema | 版本 | 关键字段 | 稳定承诺 |
|--------|------|----------|----------|
| `EventSchema` | v1 | event_id, timestamp, event, context, entities, attributes | event_id 永远不为空 |
| `EntitySchema` | v1 | entity_id, type, id, version, valid_from, valid_to, attributes | 实体带版本 |
| `FactSchema` | v1 | entity, key, value, valid_from, valid_to, source, confidence | Fact 带窗口 |
| `RelationshipSchema` | v1 | rel_id, source, target, type, version, status | 区分 Static/Dynamic |
| `InteractionSchema` | v1 | interaction_id, source_endpoint, target_endpoint, timestamp, type | Endpoint→Endpoint |

### 3.3 迁移策略

```
Schema Evolution:
  v1 → v2: 只加字段 (backward compatible)
  v2 → v3: 不删字段，不改类型 (除非 major version，通知所有消费者)

迁移流程:
  1. Schema Registry 注册新版本
  2. 所有消费者确认兼容
  3. Producer 开始写入新格式
  4. 双写期 (old + new) 持续 1 个 release cycle
  5. 下线旧格式
```

---

## 4. Normalized Event 模型

### 4.1 为什么需要统一 Event 模型

NormalizedEvent 是整个平台的**数据契约**。所有下游围绕同一模型工作。

**拆分为四个逻辑组：** `event` / `context` / `entities` / `attributes`，避免 Fat Event 问题。

**全局唯一 event_id：** 所有下游模块（Interaction、Fact、Evidence、AI、Rule、Timeline）通过 event_id 稳定引用 Event。不可用时戳+service+message 组合。

### 4.2 核心数据类型

```python
class ResourceType(Enum):
    """平台无关的资源类型枚举。所有 Extractor 注册到这里。"""
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
    UNKNOWN   = "UNKNOWN"


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
    """事件参与角色。role 不是 Resource 的属性。"""
    resource: ResourceIdentity
    role: str       # actor, target, source, destination, ...


@dataclass
class EventCategory:
    """
    事件类别：七维模型。
    schema_version 确保可向前兼容。
    """
    schema_version: str = "v1"
    category: str = ""      # infra, platform, app, security, ...
    domain: str = ""        # compute, network, volume, k8s, vmware ...
    resource: str           # INSTANCE, VOLUME, PORT, POD, NODE, ...
    action: str             # CREATE, DELETE, ATTACH, DETACH, REBOOT, ...
    phase: str = ""         # start, end, error
    outcome: str = ""       # success, failure, unknown


def _generate_event_id(timestamp: datetime, service: str,
                        host: str, seq: int = 0) -> str:
    """
    生成全局唯一 event_id。
    使用 UUID7（时间有序）保证全局唯一 + 时间排序。
    降级方案：sha256(timestamp + service + host + seq).
    """
    import uuid
    return uuid.uuid7().hex  # Python 3.14+ or uuid6/uuid7 lib


@dataclass
class NormalizedEvent:
    """
    Semantic Engine 输出的统一 Event 模型（四分区结构）。

    每个 Event 有全局唯一的 event_id。
    所有下游模块通过 event_id 引用此 Event。
    """
    event_id: str                           # 全局唯一，UUID7

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

    # -- participants: 参与角色 --
    participants: List[EventParticipant] = field(default_factory=list)

    # -- attributes: 原始保留 --
    attributes_json: str = ""
    labels_json: str = ""

    # -- 查询加速列（ClickHouse） --
    instance_uuid: str = ""
    volume_id: str = ""
    port_id: str = ""
    image_id: str = ""
    aggregate: str = ""
```

### 4.3 示例

```python
event = NormalizedEvent(
    event_id="018f9a2b-3c4d-5e6f-7a8b-9c0d1e2f3a4b",
    timestamp="2026-01-01T00:00:00Z",
    service_name="nova-compute",
    host="compute-01",
    severity="INFO",
    message="Attaching volume vol-456 to instance abc-123",
    event=EventCategory(
        schema_version="v1", category="infra", domain="volume",
        resource="INSTANCE", action="ATTACH_VOLUME",
        phase="end", outcome="success",
    ),
    trace_id="trace-xxx",
    request_id="req-abc-123",
    entities=[
        ResourceIdentity(ResourceType.INSTANCE, "abc-123"),
        ResourceIdentity(ResourceType.VOLUME, "vol-456"),
    ],
    participants=[
        EventParticipant(ResourceIdentity(ResourceType.INSTANCE, "abc-123"), "actor"),
        EventParticipant(ResourceIdentity(ResourceType.VOLUME, "vol-456"), "target"),
    ],
    instance_uuid="abc-123", volume_id="vol-456",
)
```

### 4.4 Event 引用关系

```
所有下游通过 event_id 引用 Event：

Interaction.interaction_id → 由 event_id 衍生
Fact.entity → 指向 Entity 的 entity_id
Evidence.observations[i].log_id → 实际是 event_id
Inference → 引用 Evidence 中的 event_id 链
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
    ("compute.instance.delete.end", None):
        ("infra", "compute", "INSTANCE", "DELETE", "end", "success"),
    ("volume.attach.end", None):
        ("infra", "volume", "INSTANCE", "ATTACH_VOLUME", "end", "success"),
    ("volume.detach.end", None):
        ("infra", "volume", "INSTANCE", "DETACH_VOLUME", "end", "success"),
    ("port.create.end", None):
        ("infra", "network", "PORT", "CREATE", "end", "success"),
    ("image.create.end", None):
        ("infra", "image", "IMAGE", "CREATE", "end", "success"),
    (None, "attach"):  ("", "", "INSTANCE", "ATTACH_VOLUME", "", ""),
    (None, "detach"):  ("", "", "INSTANCE", "DETACH_VOLUME", "", ""),
    (None, "spawn"):   ("", "", "INSTANCE", "SPAWN", "", ""),
}


def extract_event_category(log_data: Dict[str, Any]) -> EventCategory:
    event_type = _candidate_text(log_data.get("event_type") or "")
    action = _candidate_text(log_data.get("action") or "")
    for (ev_prefix, act_verb), result in _OPERATION_PATTERNS.items():
        if ev_prefix and event_type.startswith(ev_prefix):
            return EventCategory(schema_version="v1", *result)
    for (ev_prefix, act_verb), result in _OPERATION_PATTERNS.items():
        if act_verb and action and act_verb in action.lower():
            return EventCategory(schema_version="v1", *result)
    outcome = "failure" if str(log_data.get("severity","")).upper() in ("ERROR","FATAL","CRITICAL") else "unknown"
    return EventCategory(schema_version="v1", resource="UNKNOWN", action="UNKNOWN", outcome=outcome)
```

### 5.2 extract_entities()

```python
_RESOURCE_FIELD_MAP = {
    ResourceType.INSTANCE: ["instance_uuid", "instance_id", "server_id", "uuid"],
    ResourceType.VOLUME:   ["volume_id", "volumeId"],
    ResourceType.PORT:     ["port_id", "portId"],
    ResourceType.IMAGE:    ["image_id", "imageId", "image_uuid"],
}

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
}


def extract_entities(log_data: Dict[str, Any]) -> List[ResourceIdentity]:
    raw_attrs = log_data.get("_raw_attributes", {}) or {}
    message = str(log_data.get("message", ""))
    found = {}
    for res_type, source_keys in _RESOURCE_FIELD_MAP.items():
        for source_key in source_keys:
            value = _find_value(raw_attrs, message, source_key)
            if value:
                found[source_key] = (res_type, value)
                break
    for alias_key, res_type in _TRANSFORMATION_MAP.items():
        if not any(v[0] == res_type for v in found.values()):
            value = _find_value(raw_attrs, message, alias_key)
            if value:
                found[alias_key] = (res_type, value)
    seen, entities = set(), []
    for _, (res_type, uuid) in found.items():
        if uuid and uuid not in seen:
            seen.add(uuid)
            entities.append(ResourceIdentity(type=res_type, id=uuid))
    return entities


def assign_participants(category: EventCategory,
                         entities: List[ResourceIdentity]) -> List[EventParticipant]:
    res_type = ResourceType(category.resource) if category.resource else None
    role_rules = _PARTICIPANT_ROLES.get((res_type, category.action), {})
    return [EventParticipant(resource=e, role=role_rules.get(e.type, "")) for e in entities]
```

### 5.3 normalize_log() 集成

```python
def normalize_log(log_data: Dict[str, Any]) -> NormalizedEvent:
    category = extract_event_category(log_data)
    entities = extract_entities(log_data)
    participants = assign_participants(category, entities)
    entity_uuids = _extract_flat_uuids(entities)
    return NormalizedEvent(
        event_id=_generate_event_id(timestamp, service, host),
        timestamp=..., service_name=..., host=..., severity=..., message=...,
        event=category, entities=entities, participants=participants,
        trace_id=..., request_id=..., global_request_id=...,
        instance_uuid=entity_uuids.get("instance_uuid", ""),
        volume_id=entity_uuids.get("volume_id", ""),
        port_id=entity_uuids.get("port_id", ""),
        image_id=entity_uuids.get("image_id", ""),
        attributes_json=json.dumps(raw_attributes),
    )
```

---

## 6. EventSource

### 6.1 接口定义

```python
class EventSource(ABC):
    """
    事件源抽象接口。
    不关心存储后端。

    Implementations:
        ClickHouseEventSource — 历史查询 + 轮询
        KafkaEventSource      — 实时流 + 推模式
        IcebergEventSource    — 离线分析
        ReplayEventSource     — 测试回放
    """

    @abstractmethod
    def get_events(self, time_window: str,
                   filters: Optional[Dict[str, str]] = None) -> List[NormalizedEvent]:
        ...

    @abstractmethod
    def stream_events(self, time_window: str,
                       filters: Optional[Dict[str, str]] = None) -> Iterator[NormalizedEvent]:
        ...

    @abstractmethod
    def subscribe(self, callback: Callable[[NormalizedEvent], None],
                   filters: Optional[Dict[str, str]] = None) -> str:
        """推模式订阅。返回 subscription_id。"""
        ...

    @abstractmethod
    def unsubscribe(self, subscription_id: str):
        ...

    @abstractmethod
    def timeline(self, entity: ResourceIdentity,
                 time_window: str) -> List[NormalizedEvent]:
        ...

    @abstractmethod
    def get_participating_services(self, time_window: str
        ) -> List[Tuple[ResourceIdentity, str, datetime]]:
        ...
```

### 6.2 实现示例

```python
class KafkaEventSource(EventSource):
    def subscribe(self, callback, filters=None):
        self._consumer.subscribe(self._topic)
        sub_id = uuid4().hex
        self._start_poll_loop(sub_id, callback, filters)
        return sub_id


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

## 7. Knowledge Layer

### 7.1 定位

Knowledge Layer 是 Logoscope 的**静态知识存储**。包含四个注册表：

```
Knowledge Layer
+-- Entity Registry           # "有什么" — 资源实例 (versioned)
+-- Fact Registry             # "是什么" — 属性事实 (validity window)
+-- Identity Resolution       # "谁是谁" — Alias → Canonical ID
+-- Static Relationship Reg.  # "怎么连" — 资源间固有关系
```

**不包含：** 运行时动态关系（归 Correlation Store）、Metadata Registry（归 Fact Registry）。

### 7.2 Entity Registry（versioned）

```python
@dataclass
class EntityRecord:
    """
    实体记录：带版本和有效时间。
    Knowledge Layer 每次更新产生新版本，旧版本保留。
    """
    entity_id: str            # 不可变 ID
    type: ResourceType
    id: str                   # 资源 UUID
    version: int = 1          # 递增版本号
    valid_from: datetime      # 此版本生效时间
    valid_to: Optional[datetime] = None  # 此版本过期时间（None = 当前有效）
    attributes: Dict[str, str] = field(default_factory=dict)
    source: str = ""          # "api", "notification", "derived"

    @property
    def is_current(self) -> bool:
        return self.valid_to is None
```

**为什么 versioned：**
- Timeline 需要回放历史："昨天 instance 在 host-01，今天在 host-02"
- AI 需要上下文："迁移前的 instance 配置是什么？"
- Entity Builder 每次更新产生新版本，旧记录 `valid_to=now()`

### 7.3 Fact Registry（validity window）

```python
@dataclass
class Fact:
    """
    事实：实体的属性。
    带有效性窗口，可过期。

    Rule Engine: fact.key == "lifecycle.create_completed" AND fact.is_current
    """
    entity: ResourceIdentity
    key: str                  # "lifecycle.create_completed", "image", "mac"
    value: str                # "true", "cirros", "fa:16:3e:..."
    valid_from: datetime      # 有效起始时间
    valid_to: Optional[datetime] = None  # 有效截止时间（None = 当前有效）
    source: str               # "derived", "api", "knowledge", "ai"
    confidence: float = 1.0

    @property
    def is_current(self) -> bool:
        return self.valid_to is None
```

**为什么 validity window：**
- `attached=true` 在 detach 后变为 `attached=false`
- Rule Engine 必须知道哪些 Fact 已过期
- AI 必须知道时间上下文

**Fact Builder（保守策略）：**

```python
class FactBuilder:
    """只产出可直接观测的事实，不推断资源状态。"""

    _LIFECYCLE_FACTS = {
        ("INSTANCE", "CREATE", "end", "success"):   "lifecycle.create_completed",
        ("INSTANCE", "DELETE", "end", "success"):   "lifecycle.delete_completed",
        ("INSTANCE", "REBOOT", "end", "success"):   "lifecycle.reboot_completed",
        ("VOLUME", "CREATE", "end", "success"):     "lifecycle.create_completed",
        ("VOLUME", "ATTACH", "end", "success"):     "lifecycle.attach_completed",
        ("VOLUME", "DETACH", "end", "success"):     "lifecycle.detach_completed",
    }

    def process_event(self, event: NormalizedEvent):
        for entity in event.entities:
            fact_key = self._derive_lifecycle(event)
            if fact_key:
                self.fact_registry.upsert(Fact(
                    entity=entity,
                    key=fact_key, value="true",
                    valid_from=event.timestamp,
                    source="derived", confidence=0.9,
                ))

    def _derive_lifecycle(self, event) -> Optional[str]:
        key = (event.event.resource, event.event.action,
               event.event.phase, event.event.outcome)
        return self._LIFECYCLE_FACTS.get(key)
```

### 7.4 Identity Resolution（Alias Registry）

**问题：** `device_id=abc`, `consumer_uuid=abc`, `domain_uuid=abc` 都是同一个 `INSTANCE=abc`。当前每个 Provider 自己做 Transformation，未来 AI/Rule 需要重复解析。

**方案：** Knowledge Layer 维护 **Identity Resolution Registry**（Alias Table）：

```
Alias Registry
alias_key   | alias_value  | canonical_type  | canonical_id  | source
------------|--------------|-----------------|---------------|-------
device_id   | abc-123      | INSTANCE        | abc-123       | alias_map
consumer_uuid| def-456     | INSTANCE        | def-456       | alias_map
port_id     | port-xyz     | PORT            | port-xyz      | api
```

```python
# knowledge-layer/registry/identity_resolution.py

@dataclass
class AliasRecord:
    """
    别名记录：从非标准字段名到规范 ResourceIdentity 的映射。
    所有模块通过 Knowledge Layer 查询，不需要重复实现 Transformation。
    """
    alias_key: str            # "device_id", "consumer_uuid"
    alias_value: str          # "abc-123"
    canonical_type: ResourceType
    canonical_id: str
    source: str               # "alias_map", "api", "notification"
    confidence: float = 1.0


class IdentityResolver:
    """
    集中式身份解析。
    Semantic Engine 和 Correlation Engine 都调用此接口。
    """

    def resolve(self, alias_key: str, alias_value: str) -> Optional[ResourceIdentity]:
        """查询别名 → 规范 ID"""
        record = self.alias_registry.lookup(alias_key, alias_value)
        if record:
            return ResourceIdentity(record.canonical_type, record.canonical_id)
        return None

    def register_alias(self, alias_key: str, alias_value: str,
                        canonical_type: ResourceType, canonical_id: str):
        """注册新别名映射"""
        self.alias_registry.upsert(AliasRecord(
            alias_key=alias_key,
            alias_value=alias_value,
            canonical_type=canonical_type,
            canonical_id=canonical_id,
            source="derived",
        ))
```

**为什么放在 Knowledge Layer 而不是 Semantic Engine：**
- 多个模块（Correlation、AI、Rule）都需要别名解析
- 别名映射可以来自 API（Neutron 确认 device_id=instance_uuid），也可以来自预配置
- 集中一处维护，变更对所有消费者立即可见

### 7.5 Static Relationship Registry

```cypher
// 节点
(:Entity {entity_id: "ent-001", type: "INSTANCE", id: "abc-123"})
(:Entity {entity_id: "ent-002", type: "HOST", id: "compute-01"})

// 静态关系
(:INSTANCE {entity_id: "ent-001"})-[:RUNS_ON]->(:HOST {entity_id: "ent-002"})
(:INSTANCE {entity_id: "ent-001"})-[:HAS_PORT]->(:PORT {entity_id: "ent-003"})
(:PORT     {entity_id: "ent-003"})-[:BELONGS_TO]->(:NETWORK {entity_id: "ent-004"})
(:VOLUME   {entity_id: "ent-005"})-[:ATTACHED_TO]->(:INSTANCE {entity_id: "ent-001"})
```

### 7.6 Knowledge Layer API

```text
# 实体查询
GET /api/v1/knowledge/entities?type=INSTANCE&id=abc-123
-> {"entity_id": "ent-001", "type": "INSTANCE", "version": 3,
    "valid_from": "...", "valid_to": null, "attributes": {...}}

# 实体历史
GET /api/v1/knowledge/entities/history?type=INSTANCE&id=abc-123
-> [{"version": 1, "valid_from": "T0", "valid_to": "T1", ...},
    {"version": 2, "valid_from": "T1", "valid_to": "T2", ...},
    {"version": 3, "valid_from": "T2", "valid_to": null, ...}]

# 事实查询（过滤过期）
GET /api/v1/knowledge/facts?type=INSTANCE&id=abc-123&current=true
-> {"facts": [{"key": "lifecycle.create_completed", "value": "true", ...}]}

# 别名解析
GET /api/v1/knowledge/resolve?alias=device_id&value=abc-123
-> {"canonical_type": "INSTANCE", "canonical_id": "abc-123"}

# 静态关系
GET /api/v1/knowledge/relationships?type=INSTANCE&id=abc-123
-> {"static_relationships": [...]}
```

---

## 8. Correlation Engine

### 8.1 架构

```
correlation-engine/
+-- engine.py              # CorrelationEngine
+-- base.py                # CorrelationProvider
+-- event_source.py        # EventSource 接口
+-- models.py              # Observation, Transformation, Fact, Evidence, Inference, DynamicRelationship
+-- merger.py              # EvidenceMerger
+-- store.py               # CorrelationStore
+-- providers/
|   +-- request_correlator.py
|   +-- resource_correlator.py
|   +-- time_correlator.py
|   +-- host_correlator.py (P2)
+-- tests/
```

### 8.2 证据链模型

```python
@dataclass
class Observation:
    """原始观测。自带置信度。"""
    event_id: str
    service_name: str
    timestamp: datetime
    observed_field: str       # "device_id"
    observed_value: str       # "abc-123"
    confidence: float = 0.8   # Regex=0.8, LLM=0.6, API=1.0
    source_detail: str = ""

@dataclass
class Transformation:
    """语义转换：Observation → 目标类型。"""
    transformation_type: str    # "alias_resolution"
    observation: Observation
    resolved_type: ResourceType
    resolved_id: str
    confidence: float = 1.0

@dataclass
class Fact:
    """经过 Transformation + Knowledge 确认的事实。"""
    fact_type: str
    subject_type: ResourceType
    subject_value: str
    object_type: ResourceType
    object_value: str
    source: str            # "transformation", "knowledge_layer"
    confidence: float = 1.0

@dataclass
class Evidence:
    """同一条事实出现在两个服务中。"""
    source_service: str
    target_service: str
    evidence_type: str              # "resource_match", "request_match"
    match_value: str                # "INSTANCE:abc-123"
    weight: float
    transformations: List[Transformation] = field(default_factory=list)

@dataclass
class Inference:
    """推断：两个服务之间存在行为关系。"""
    source: str
    target: str
    evidence_list: List[Evidence]
    inferred_relationship: str      # "calls", "depends_on"
```

### 8.3 Dynamic Relationship（带生命周期）

```python
class RelationshipStatus(Enum):
    ACTIVE  = "ACTIVE"
    STALE   = "STALE"    # 超过 expire_after 未更新
    EXPIRED = "EXPIRED"  # 超过 2×expire_after


@dataclass
class DynamicRelationship:
    """
    动态关系：聚合多条 Interaction 得出。
    不可变记录 + 生命周期管理。

    生命周期：
      first_seen → (不断更新 last_seen) → expire_after → STALE
      → 超过 2×expire_after → EXPIRED → 可以被清理
    """
    relationship_id: str
    version: int = 1
    created_at: datetime = field(default_factory=datetime.utcnow)

    source: str
    target: str
    relationship_type: str = "calls"
    confidence: float

    # 生命周期
    first_seen: datetime          # 首次发现
    last_seen: datetime           # 最后出现
    expire_after_minutes: int = 30  # 多久不更新标记为 STALE
    status: RelationshipStatus = RelationshipStatus.ACTIVE

    # 聚合数据
    interaction_ids: List[str] = field(default_factory=list)
    call_count: int = 0
    inferences: List[Inference] = field(default_factory=list)
    data_sources: List[str] = field(default_factory=list)
```

**生命周期：**
```
first_seen=T0 → (Interactions arrive) → last_seen=T1
                                           ↓ after expire_after (30 min)
                                        STALE
                                           ↓ after 2×expire_after (60 min)
                                        EXPIRED → cleanup
```

### 8.4 CorrelationProvider

```python
class CorrelationProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    def correlate(self, interactions: List[Interaction], **kwargs
                  ) -> Tuple[List[Fact], List[Inference]]:
        """
        从 Interaction 中聚合关联。
        不直接查询 EventStore — 通过 EventSource 消费 Interaction。
        """
        ...
```

### 8.5 EvidenceMerger

```python
class EvidenceMerger:
    """Observation → Transformation → Fact → Evidence → Inference → DynamicRelationship"""

    EVIDENCE_WEIGHTS = {
        "request_match":   0.20,
        "resource_match":  0.45,
        "time_window":     0.10,
        "host_match":      0.15,
        "static_relation": 0.30,
    }
    WEIGHT_DECAY_MINUTES = 120

    def merge(self, inferences: List[Inference],
              facts: Optional[List[Fact]] = None
              ) -> Dict[Tuple[str, str], DynamicRelationship]:
        """合并 Inference → DynamicRelationship。"""
        ...
```

### 8.6 Inference Engine（AI / Rule / ML / Graph）

AI 不是 CorrelationProvider。

```python
# inference-engine/base.py

class InferenceEngine(ABC):
    """
    推理引擎：对 Evidence 做推理。

    Implementations:
        AICorrelationEngine    — LLM
        RuleEngine             — 规则
        GraphEngine            — 图推理
        MLEngine               — 机器学习
    """

    @abstractmethod
    def infer(self, evidence: List[Evidence],
              facts: List[Fact]) -> List[Inference]:
        ...
```

---

## 9. Interaction（Endpoint→Endpoint）

### 9.1 模型

```python
@dataclass
class InteractionEndpoint:
    """
    交互端点。
    不限于 Service — 可以是 Service / Process / Pod / Host / VM / Container / Switch。
    """
    entity: ResourceIdentity
    role: str = ""          # "source", "target", "client", "server"


@dataclass
class Interaction:
    """
    运行时交互：两个 Endpoint 之间的单次交互。
    直接从 NormalizedEvent 构建，不依赖 Relationship。

    不限于 Service→Service:
      - Service → Service    (Nova calls Neutron)
      - Process → Service    (python process calls API)
      - VM → Volume          (instance attaches volume)
      - Pod → Node           (pod scheduled on node)
      - Container → Runtime  (container runtime operation)
    """
    interaction_id: str
    timestamp: datetime
    source_endpoint: InteractionEndpoint
    target_endpoint: InteractionEndpoint
    interaction_type: str       # "calls", "rpc", "api_call", "attaches", "schedules"
    duration_ms: float = 0.0
    request_id: str = ""
    outcome: str = ""           # "success", "failure", "timeout"
```

### 9.2 InteractionBuilder

```python
class InteractionBuilder:
    """
    从 NormalizedEvent 构建 Interaction。
    Endpoint→Endpoint 模型，平台无关。
    """

    def build(self, events: List[NormalizedEvent]) -> List[Interaction]:
        interactions = []

        # Method 1: request_id 匹配
        for req_id, group in self._group_by_request_id(events).items():
            if len(group) >= 2:
                for src_ev, tgt_ev in self._pair_services(group):
                    interactions.append(Interaction(
                        interaction_id=uuid4().hex,
                        timestamp=tgt_ev.timestamp,
                        source_endpoint=InteractionEndpoint(
                            entity=ResourceIdentity(ResourceType.SERVICE, src_ev.service_name),
                            role="source",
                        ),
                        target_endpoint=InteractionEndpoint(
                            entity=ResourceIdentity(ResourceType.SERVICE, tgt_ev.service_name),
                            role="target",
                        ),
                        interaction_type="calls",
                        request_id=req_id,
                    ))

        # Method 2: trace_id + span_id
        # Method 3: entity time-window
        return interactions
```

### 9.3 与 v5.1 对比

```
v5.1: source=str, target=str          # 只能是 service name
v5.3: source_endpoint=InteractionEndpoint, target_endpoint=InteractionEndpoint
      # endpoint.entity = ResourceIdentity — 可以是任何资源类型

v5.1: interaction_type=["calls"]      # 限定
v5.3: interaction_type=["calls","attaches","schedules","runs_on"]
      # 平台无关
```

---

## 10. Topology Engine

### 10.1 职责

```
TopologyEngine
+-- 消费 DynamicRelationship[] + Interaction[]
+-- Node 查找：两类实体
|     Infrastructure Entity — Knowledge Layer (versioned)
|     Execution Entity      — Knowledge Layer + Cluster
+-- Layout + Render
```

### 10.2 Node 类型

```python
class NodeType(Enum):
    INFRASTRUCTURE = "INFRASTRUCTURE"
    EXECUTION      = "EXECUTION"


@dataclass
class Node:
    node_id: str
    name: str
    node_type: NodeType
    entity_type: Optional[ResourceType] = None
    attributes: Dict[str, str] = field(default_factory=dict)
```

```python
class TopologyEngine:
    def __init__(self, knowledge_layer: KnowledgeLayer,
                 correlation_store: CorrelationStore):
        ...

    def build(self, time_window: str) -> TopologyResult:
        rels = self.correlation_store.query(time_window)
        interactions = self.correlation_store.query_interactions(time_window)
        nodes = self._lookup_nodes(rels, interactions)
        edges = self._interactions_to_edges(interactions)
        return TopologyResult(nodes=nodes, edges=edges,
                               layout=self._compute_layout(nodes, edges))

    def _lookup_nodes(self, rels, interactions) -> List[Node]:
        services = set()
        for r in rels:
            services.add(r.source); services.add(r.target)
        for ia in interactions:
            # endpoints 可以不是 SERVICE — 也可能是 INSTANCE, HOST, POD
            for ep in [ia.source_endpoint, ia.target_endpoint]:
                if ep.entity.id not in services:
                    # lookup infrastructure entity
                    record = self.knowledge_layer.find_entity(
                        ep.entity.type, ep.entity.id)
                    if record:
                        nodes.append(Node(
                            node_id=record.entity_id, name=record.id,
                            node_type=NodeType.INFRASTRUCTURE,
                            entity_type=record.type,
                            attributes=record.attributes,
                        ))
        ...
```

---

## 11. 置信度模型

```
base = 0.3

resource_match:  (type, id) in both  → +0.45
request_match:   global_request_id   → +0.35
                 request_id          → +0.20
time_window:     < 0.5s same host    → +0.10
                 < 0.1s same host    → +0.15
message_match:   target match        → +0.20
static_relation: Knowledge confirms  → +0.30

max_confidence = min(base, 0.98)
time_decay:     >120 min → decay = 0.5^(min/120)
final = max_confidence * decay
```

---

## 12. API

```text
# 拓扑查询
GET /api/v1/topology/hybrid?time_window=1+HOUR

# 证据详情
GET /api/v1/evidence?source=Nova&target=Neutron&time_window=1+HOUR

# 交互详情
GET /api/v1/interactions?source=Nova&target=Neutron&time_window=1+HOUR

# 运行时交互订阅
GET /api/v1/interactions/subscribe  (WebSocket/SSE)

# 知识层
GET /api/v1/knowledge/entities?type=INSTANCE&id=abc-123
GET /api/v1/knowledge/entities/history?type=INSTANCE&id=abc-123
GET /api/v1/knowledge/facts?type=INSTANCE&id=abc-123&current=true
GET /api/v1/knowledge/resolve?alias=device_id&value=abc-123
GET /api/v1/knowledge/relationships?type=INSTANCE&id=abc-123

# 关联查询
GET /api/v1/correlate/services?source=Nova&time_window=1+HOUR
```

---

## 13. 实施阶段（重新排序）

### Phase 0: Schema Registry + Foundation（~1 周）

| 模块 | 内容 |
|------|------|
| **Schema Registry** | 定义 EventSchema v1, EntitySchema v1, FactSchema v1, InteractionSchema v1, RelationshipSchema v1 |
| **Contract Layer** | dataclass → Schema 校验，单元测试级别 |
| **event_id** | UUID7 生成器，NormalizedEvent 基础结构 |

### Phase 1: Semantic Engine（~1 周）

| 模块 | 内容 |
|------|------|
| `operation.py` | extract_event_category() |
| `resource.py` | extract_entities(), assign_participants() |
| `normalizer.py` | 集成 + event_id 生成 |

### Phase 2: Interaction Builder（~1 周）

| 模块 | 内容 |
|------|------|
| `interaction-builder/` | InteractionBuilder，request_id/trace_id 匹配 |
| EventSource 基础 | ReplayEventSource + ClickHouse 基础实现 |

### Phase 3: Knowledge Layer（~2 周）

| 模块 | 内容 |
|------|------|
| `entity_registry.py` | Entity Registry（versioned）|
| `fact_registry.py` | Fact Registry（validity window）|
| `identity_resolution.py` | Alias Registry |
| `static_relationship_registry.py` | Static Relationship |
| `entity_builder.py` | NormalizedEvent → Entity |
| `fact_builder.py` | NormalizedEvent → Fact（保守）|

### Phase 4: Correlation Engine（~2 周）

| 模块 | 内容 |
|------|------|
| `correlation-engine/` | 完整包 |
| `models.py` | Observation → Transformation → Fact → Evidence → Inference |
| `merger.py` | EvidenceMerger |
| `store.py` | CorrelationStore（Dynamic Relationship + lifecycle）|
| `providers/` | resource_correlator + request_correlator |

### Phase 5: Topology Engine（~1 周）

| 模块 | 内容 |
|------|------|
| `topology_engine.py` | 重写：两类 Node + Interaction→Edge |
| `api/topology_routes.py` | 新增端点 |

### Phase 6: Inference Engine（~1 周）

| 模块 | 内容 |
|------|------|
| `inference-engine/base.py` | InferenceEngine 抽象 |
| `inference-engine/providers/ai_engine.py` | AI 实现 |
| `inference-engine/providers/rule_engine.py` | Rule 实现 |

### Phase 7: Rule Engine + Multi-platform（~2 周）

| 模块 | 内容 |
|------|------|
| Rule Engine | 消费 Fact + Interaction |
| K8s extractor | POD, NODE, PVC Entity 提取 |
| VMware adapter | platform adapter |

---

## 14. 测试策略

### 14.1 event_id 测试

```python
def test_event_id_unique():
    """每个 Event 有全局唯一 event_id"""
    event = normalize_log({"_raw_attributes": {"instance_uuid": "abc"}})
    assert event.event_id != ""
    assert len(event.event_id) > 10

def test_event_id_based_on_event():
    """同源 Event 生成不同 event_id（UUID7 单调递增）"""
    e1 = normalize_log({...})
    e2 = normalize_log({...})
    assert e1.event_id != e2.event_id
```

### 14.2 Entity versioning 测试

```python
def test_entity_version_increments():
    """Entity 更新产生新版本"""
    registry = EntityRegistry()
    e1 = registry.upsert(ResourceType.INSTANCE, "abc", {"host": "h1"})
    e2 = registry.upsert(ResourceType.INSTANCE, "abc", {"host": "h2"})
    assert e1.version == 1
    assert e2.version == 2
    assert e1.valid_to is not None  # 旧版本关闭
    assert e2.valid_to is None       # 新版本当前有效

def test_entity_temporal_query():
    """按时间查询旧版本"""
    e = registry.query_at(ResourceType.INSTANCE, "abc", timestamp=T0)
    assert e.host == "h1"
```

### 14.3 Fact validity 测试

```python
def test_fact_expiration():
    """Fact 过期后不再被 Rule Engine 匹配"""
    fact = Fact(entity=..., key="attached", value="true",
                valid_from=T0, valid_to=T1)
    assert fact.is_current is False  # T1 已过
```

### 14.4 Interaction Endpoint 测试

```python
def test_interaction_endpoint_not_limited_to_service():
    """Interaction Endpoint 可以是任意 ResourceType"""
    ia = Interaction(
        interaction_id="i1",
        timestamp=datetime(...),
        source_endpoint=InteractionEndpoint(
            entity=ResourceIdentity(ResourceType.INSTANCE, "abc-123"),
            role="source",
        ),
        target_endpoint=InteractionEndpoint(
            entity=ResourceIdentity(ResourceType.VOLUME, "vol-456"),
            role="target",
        ),
        interaction_type="attaches",
    )
    assert ia.source_endpoint.entity.type == ResourceType.INSTANCE
    assert ia.target_endpoint.entity.type == ResourceType.VOLUME

def test_topology_infrastructure_node():
    """Topology Engine 支持 Infra Entity Node"""
    kl = FakeKnowledgeLayer(...)
    engine = TopologyEngine(kl, FakeCorrelationStore())
    interactions = [
        Interaction("i1", datetime(...),
            source_endpoint=InteractionEndpoint(ResourceIdentity(ResourceType.INSTANCE, "abc")),
            target_endpoint=InteractionEndpoint(ResourceIdentity(ResourceType.HOST, "h1")),
            interaction_type="runs_on"),
    ]
    result = engine.build([], interactions)
    assert any(n.node_type == NodeType.INFRASTRUCTURE for n in result.nodes)
```

### 14.5 Identity Resolution 测试

```python
def test_identity_resolution():
    """Alias → Canonical ID"""
    resolver = IdentityResolver()
    resolver.register_alias("device_id", "abc-123",
                             ResourceType.INSTANCE, "abc-123")
    result = resolver.resolve("device_id", "abc-123")
    assert result == ResourceIdentity(ResourceType.INSTANCE, "abc-123")

def test_identity_resolution_centralized():
    """所有模块共用 IdentityResolver"""
    # Semantic Engine 和 Correlation Engine 调同一个接口
    ...
```

### 14.6 Dynamic Relationship lifecycle 测试

```python
def test_relationship_lifecycle():
    """DynamicRelationship 生命周期"""
    rel = DynamicRelationship(
        relationship_id="r1", source="A", target="B",
        first_seen=T0, last_seen=T0,
        expire_after_minutes=30,
    )
    assert rel.status == RelationshipStatus.ACTIVE

    # 35 分钟后
    rel.status = rel.compute_status(now=T0+35min)
    assert rel.status == RelationshipStatus.STALE

    # 65 分钟后
    rel.status = rel.compute_status(now=T0+65min)
    assert rel.status == RelationshipStatus.EXPIRED
```

---

## 15. 性能考量

| 场景 | 当前 | 优化后 |
|------|------|--------|
| 单次拓扑查询 | ~800ms | ~500ms |
| Interaction 构建 | 无 | ~100ms/1k |
| Correlation 聚合 | 无 | ~200ms/10k |
| Entity 查询 (versioned) | 无 | ~10ms (Neo4j) |
| Fact 查询 (validity) | 无 | ~10ms (KV + index) |
| Identity Resolution | 无 | ~2ms (cache) |
| EventSource subscribe | 无 | 实时 (Kafka push) |

---

## 16. 向后兼容

| 影响点 | 策略 |
|--------|------|
| ClickHouse 存量数据 | ALTER TABLE ADD COLUMN ... DEFAULT '' |
| 现有 API /hybrid | 保持输出格式不变，新增字段 |
| 现有测试 | 不改现有测试，新增模块测试 |
| event_id 历史数据 | 存量 event_id="", 新数据生成 UUID7 |
| schema_version | 旧数据默认 schema_version="v0" |
| Entity 历史 | 存量 version=1, valid_from=now(), valid_to=None |

---

## 17. Static vs Dynamic Relationship

| 维度 | Static Relationship | Dynamic Relationship |
|------|--------------------|---------------------|
| 含义 | 资源间固有关系 | 服务间行为关系 |
| 例子 | Instance → Host | Nova calls Neutron |
| 来源 | API / Inventory / CMDB | Log Correlation |
| 生命周期 | 分钟级 | 秒级，带 first_seen/last_seen/expire |
| 存储 | Knowledge Layer (Neo4j) | Correlation Store |
| 拥有者 | Entity Builder | Correlation Engine |
| 置信度 | 1.0 (API) | 0.3~0.98 |
| 版本 | versioned (Entity) | version + status |
| Schema | ResourceSchema | RelationshipSchema |
