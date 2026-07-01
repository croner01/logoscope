# Logoscope Data Architecture v1

> **将 Logoscope 从"日志分析平台"升级为"事件知识平台" (Event Knowledge Platform)。**
>
> 定义从 Raw Log → Normalized Event → Knowledge → Correlation → Interaction → Graph → AI
> 的完整数据链，统一 OpenStack、Kubernetes、VMware、Linux、网络设备、AI Agent 等多平台的事件模型。
>
> **Key design decisions:**
> - NO `primary_resource` — all resources equal, expressed as `entities[ResourceIdentity]` + `participants[EventParticipant]`
> - NO hardcoded resource types in Correlation Engine — uses generic `ResourceIdentity` matching
> - Knowledge Layer is platform-wide: Entity + Relationship + Fact + Metadata four registries
> - Correlation Engine consumes **EventSource** (event stream abstraction), not storage directly
> - Correlation Engine outputs **Relationship** → **Interaction** → Topology Engine converts to Edge
> - `relationship_id` (not `edge_id`) in Correlation; `edge_id` only in Topology Engine
> - Observation → **Extraction** → Fact → Evidence → Inference five-layer evidence chain
> - NormalizedEvent split into `event` / `context` / `entities` / `attributes` four sections
> - Knowledge Layer sync via NormalizedEvent, not raw platform notifications
> - Topology Engine looks up nodes from Knowledge Layer, does not `new Node()` from edges
> - EventType upgraded to `category/domain/resource/action/phase/outcome` six-dimension model

**Status:** Draft v5
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
  → "device_id 是 instance_uuid"       (Extraction)
  → "instance abc-123 存在"            (Fact)
  → "instance 在 compute-01 上"        (Knowledge: Entity)
  → "Nova 和 Neutron 都有此 instance"  (Evidence)
  → "Nova calls Neutron"               (Inference)
  → "Nova ↔ Neutron calls"             (Relationship)
  → "2026-01-01 12:00:00 call"         (Interaction)
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
          +------+--+--+------+
          |      |     |      |
          v      v     v      v
   Entity    Fact   Event   Interaction
  Builder  Builder  Store    Builder
          |      |     |      |
          +--+---+     |      |
             v          |      |
      Knowledge Layer    |      |
  (Entity+Fact+Rel+Meta) |      |
             |           |      |
             +-----+-----+      |
                   |            |
                   v            |
          Correlation Engine     |
                   |            |
              Relationship      |
                   |            |
              Interaction  +----+
                   |
                   v
            Topology Engine
                   |
          +--------+--------+
          v        v        v
         AI    Rule Engine  Timeline
```

### 2.2 数据流

```
Raw Log
   v Semantic Engine
NormalizedEvent { event, context, entities[], attributes }
   v
+-- Entity Builder ------+    +-- Event Store (ClickHouse) --+
| Entity -> Knowledge    |    | event_* / context_*          |
| Fact   -> Knowledge    |    | entities_json / attributes   |
+------------------------+    +------------------------------+
   v                              v
Knowledge Layer             EventSource (抽象接口)
  (Neo4j)                      |
   v                           v
Correlation Engine <---- EventSource.stream_events()
   |
Relationship + Interaction
   |
Topology Engine -> Graph -> API / Frontend / AI
```

### 2.3 组件职责

| 层 | 职责 | 不做什么 |
|----|------|----------|
| **Semantic Engine** | 日志理解、分类、标准化 NormalizedEvent | 不计算拓扑、不做关联、不建知识 |
| **Entity Builder** | 从 NormalizedEvent 提取 Entity/Fact 写入 Knowledge | 不分析日志原文 |
| **Fact Builder** | 从 Extraction 结果构造 Fact 写入 Knowledge | 不推断关系 |
| **Event Store** | 持久化 NormalizedEvent，索引、预聚合 | 不做业务逻辑 |
| **EventSource** | 抽象事件流接口（ClickHouse/Kafka/Iceberg/Replay） | 不涉及具体字段含义 |
| **Knowledge Layer** | Entity + Relationship + Fact + Metadata 四注册表 | 不分析运行时数据 |
| **Correlation Engine** | O-X-F-E-I 链，输出 Relationship | 不交互画图、不存储 |
| **Interaction Builder** | 从 Relationship + EventStore 构造运行时交互记录 | 不推断静态关系 |
| **Topology Engine** | Interaction -> lookup Node -> Edge -> Layout | 不计算置信度、不推断关系 |
| **AI Service** | 推理补全不可证明的关系，消费 Interaction | 不覆盖已有证据的关系 |
| **Rule Engine** | 消费 NormalizedEvent + Knowledge Facts 做规则匹配告警 | 不画图、不关联 |

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

NormalizedEvent 是整个平台的**数据契约**。所有下游（Correlation、Entity Builder、Fact Builder、Topology、AI、Rule Engine）围绕同一模型工作。

**拆分为四个逻辑组：** `event` / `context` / `entities` / `attributes`，避免 Fat Event 问题。

### 3.2 核心数据类型

```python
@dataclass
class ResourceIdentity:
    """
    资源标识：type + id。
    这是最轻量的资源引用，仅用于关联匹配。
    资源完整属性存放在 Knowledge Layer Entity Registry。
    """
    type: str       # INSTANCE, VOLUME, PORT, POD, NODE, PVC, ...
    id: str         # 资源 UUID

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
    事件类别：六维模型。
    category -> domain -> resource -> action -> phase -> outcome

    example: infra / compute / INSTANCE / CREATE / end / success
    """
    category: str = ""    # infra, platform, app, security, ...
    domain: str = ""      # compute, network, volume, image, k8s, vmware, ...
    resource: str         # INSTANCE, VOLUME, PORT, POD, NODE, ...
    action: str           # CREATE, DELETE, ATTACH, DETACH, REBOOT, ...
    phase: str = ""       # start, end, error
    outcome: str = ""     # success, failure, unknown

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
        ResourceIdentity("INSTANCE", "abc-123"),
        ResourceIdentity("VOLUME", "vol-456"),
    ],
    # participants
    participants=[
        EventParticipant(ResourceIdentity("INSTANCE", "abc-123"), role="actor"),
        EventParticipant(ResourceIdentity("VOLUME", "vol-456"), role="target"),
    ],
    instance_uuid="abc-123",
    volume_id="vol-456",
)
```

### 3.4 Event 消费关系

```
NormalizedEvent
    |
    +-- Event Store (ClickHouse)  -> event / context / entities / attributes
    +-- Entity Builder            -> entities -> Knowledge Entity Registry
    +-- Fact Builder              -> event + entities -> Knowledge Fact Registry
    +-- EventSource               -> Correlation Engine 消费事件流
    +-- Topology Engine           -> service_name + entities -> Node 查找
    +-- AI Service                -> message + entities + event 做推理
    +-- Rule Engine               -> event.category + event.action + severity 做规则匹配
```

### 3.5 NormalizedEvent 四分区结构详解

```
NormalizedEvent
+-- event       -> 分类维度 (category/domain/resource/action/phase/outcome)
|                  Rule Engine 直接匹配 rule.event.action=CREATE
|                  AI 可以问 "哪些 infra 级别的 CREATE 操作失败了？"
|
+-- context     -> 请求上下文 (trace_id/request_id/host/cluster)
|                  Correlation Engine 做请求级别匹配
|                  Topology Engine 做 host 级别聚合
|
+-- entities    -> 实体列表 (ResourceIdentity[])
|                  Correlation Engine 做 ResourceIdentity 匹配
|                  Entity Builder 写入 Knowledge Entity Registry
|
+-- attributes  -> 原始保留 (raw_json)
                   AI Service 做语义理解
                   调试和回放
```

### 3.6 EventType -> EventCategory 升级

v4 的 `EventType(domain, resource, verb, phase)` 升级为：

```python
@dataclass
class EventCategory:
    category: str     # infra, platform, app, security, network, storage
    domain: str       # compute, network, volume, image, k8s, vmware
    resource: str     # INSTANCE, VOLUME, PORT, POD, NODE
    action: str       # CREATE, DELETE, ATTACH, DETACH, REBOOT (v4 verb 改名)
    phase: str        # start, end, error
    outcome: str      # success, failure, unknown
```

**为什么加 category：**
- Rule Engine 可以写 `category=infra AND action=CREATE AND outcome=failure` 触发告警
- AI 可以问 "最近有哪些 app 级别的异常操作？"
- 跨平台过滤：只看 `domain=k8s` 或 `domain=openstack`

**为什么 verb -> action：**
- "action" 在 AI 和 Rule 上下文中更自然
- 避免和 HTTP verb 混淆

**为什么加 outcome：**
- 消除 phase=end 时还要从 severity 推断成功/失败的歧义
- Rule Engine 可以直接 `outcome=failure` 匹配

---

## 4. Semantic Engine 改造

### 4.1 新增 extract_event_category()

```python
# semantic-engine/normalize/operation.py

_OPERATION_PATTERNS = {
    # (event_type_prefix, action_verb) -> (category, domain, resource, action, phase, outcome)
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
    """从日志中提取标准化 EventCategory。优先级: event_type > action > message text"""
    event_type = _candidate_text(log_data.get("event_type") or "")
    action = _candidate_text(log_data.get("action") or "")

    for (ev_prefix, act_verb), result in _OPERATION_PATTERNS.items():
        if ev_prefix and event_type.startswith(ev_prefix):
            return EventCategory(*result)

    for (ev_prefix, act_verb), result in _OPERATION_PATTERNS.items():
        if act_verb and action and act_verb in action.lower():
            return EventCategory(*result)

    outcome = _detect_outcome_from_severity(log_data.get("severity", ""))
    return EventCategory(resource="UNKNOWN", action="UNKNOWN", outcome=outcome)


def _detect_outcome_from_severity(severity: str) -> str:
    if severity.upper() in ("ERROR", "FATAL", "CRITICAL"):
        return "failure"
    return "unknown"
```

### 4.2 新增 extract_entities()

从 `_raw_attributes` 和 message 中提取实体。输出 `List[ResourceIdentity]`。

```python
# semantic-engine/normalize/resource.py

# Phase 1 Core Resources
_RESOURCE_FIELD_MAP = {
    "instance_uuid": ["instance_uuid", "instance_id", "server_id", "uuid"],
    "volume_id":     ["volume_id", "volumeId"],
    "port_id":       ["port_id", "portId"],
    "image_id":      ["image_id", "imageId", "image_uuid"],
}

# Phase 2 Extraction Map（Observation -> Extraction 间的语义解析）
_EXTRACTION_MAP = {
    "device_id":     "instance_uuid",
    "consumer_uuid": "instance_uuid",
    "domain_uuid":   "instance_uuid",
    "qemu_uuid":     "instance_uuid",
    "snapshot_id":   "volume_id",
    "attachment_id": "volume_id",
}

# 参与角色规则（Participant role 从 Event 语义推断，不附着在 Resource 上）
_PARTICIPANT_ROLES = {
    ("INSTANCE", "ATTACH_VOLUME"): {"INSTANCE": "actor", "VOLUME": "target"},
    ("INSTANCE", "DETACH_VOLUME"): {"INSTANCE": "actor", "VOLUME": "target"},
    ("INSTANCE", "CREATE"):        {"INSTANCE": "actor"},
    ("VOLUME", "CREATE"):          {"VOLUME": "actor"},
    ("PORT", "CREATE"):            {"PORT": "actor", "NETWORK": "target"},
    ("INSTANCE", "LIVE_MIGRATE"):  {"INSTANCE": "actor",
                                     "HOST": "source", "HOST.dest": "target"},
}


def extract_entities(log_data: Dict[str, Any]) -> List[ResourceIdentity]:
    """提取实体列表。返回 dedup 后的 ResourceIdentity 列表。"""
    raw_attrs = log_data.get("_raw_attributes", {}) or {}
    message = str(log_data.get("message", ""))

    # Phase 1: 直接提取
    found = {}
    for target_key, source_keys in _RESOURCE_FIELD_MAP.items():
        for source_key in source_keys:
            value = _find_value(raw_attrs, message, source_key)
            if value:
                found[target_key] = value
                break

    # Phase 2: Extraction（归一化别名）
    for alias_key, target_key in _EXTRACTION_MAP.items():
        if target_key not in found:
            value = _find_value(raw_attrs, message, alias_key)
            if value:
                found[target_key] = value

    # 去重 + 转 ResourceIdentity
    seen = set()
    entities = []
    for field_name, uuid in found.items():
        if not uuid or uuid in seen:
            continue
        seen.add(uuid)
        entities.append(ResourceIdentity(
            type=_resource_type_from_field(field_name),
            id=uuid,
        ))
    return entities


def assign_participants(
    category: EventCategory,
    entities: List[ResourceIdentity],
) -> List[EventParticipant]:
    """根据 EventCategory 为实体分配参与角色。"""
    role_rules = _PARTICIPANT_ROLES.get((category.resource, category.action), {})

    participants = []
    for entity in entities:
        role = role_rules.get(entity.type, "")
        participants.append(EventParticipant(resource=entity, role=role))
    return participants


def _resource_type_from_field(field_name: str) -> str:
    mapping = {
        "instance_uuid": "INSTANCE",
        "volume_id": "VOLUME",
        "port_id": "PORT",
        "image_id": "IMAGE",
    }
    return mapping.get(field_name, field_name.upper())
```

### 4.3 normalize_log() 集成

```python
def normalize_log(log_data: Dict[str, Any]) -> NormalizedEvent:
    # ... existing request_id / trace_id extraction ...

    category = extract_event_category(log_data)
    entities = extract_entities(log_data)
    participants = assign_participants(category, entities)

    # Flat columns for storage acceleration
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

### 5.1 为什么需要 EventSource

v4 中的 `EventRepository` 虽然抽象了存储，但语义仍是"数据库查询接口"——`query_events()`, `query_resource_pairs()`。

v5 改为 **EventSource**——语义是"事件流接口"。Correlation Engine 不关心数据存在哪里，只关心"给我这个时间窗口的事件流"。

### 5.2 接口定义

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
        """
        查询历史事件（批量）。
        用于回溯分析和定时任务。
        """
        ...

    @abstractmethod
    def stream_events(self, time_window: str,
                       filters: Optional[Dict[str, str]] = None) -> Iterator[NormalizedEvent]:
        """
        流式读取事件。
        用于实时关联。
        """
        ...

    @abstractmethod
    def timeline(self, entity: ResourceIdentity,
                 time_window: str) -> List[NormalizedEvent]:
        """
        查询单个实体的时间线。
        """
        ...

    @abstractmethod
    def get_participating_services(self, time_window: str) -> List[Tuple[ResourceIdentity, str, datetime]]:
        """
        查询所有 (entity, service_name, timestamp) 三元组。
        供 ResourceCorrelator 使用。
        """
        ...


class ClickHouseEventSource(EventSource):
    """ClickHouse 实现，使用独立索引列加速"""

    def stream_events(self, time_window, filters=None):
        # 使用 PREWHERE 索引列加速
        yield from self._query_stream("""
            SELECT ... FROM logs.logs
            PREWHERE timestamp > now() - INTERVAL {time_window}
            ...
        """)

    def get_participating_services(self, time_window):
        # 使用 Bloom filter 索引
        ...


class KafkaEventSource(EventSource):
    """Kafka 实时事件流"""

    def stream_events(self, time_window, filters=None):
        # 从 Kafka topic 消费实时事件
        ...


class ReplayEventSource(EventSource):
    """测试用：从 fixture 文件回放"""

    def __init__(self, fixtures: List[NormalizedEvent]):
        self._fixtures = fixtures

    def get_events(self, time_window, filters=None):
        return self._fixtures
```

### 5.3 Provider -> EventSource 关系

```
CorrelationProvider.correlate()
    |
    +-- self.source.get_events(time_window, filters)
    |       -> List[NormalizedEvent]
    |
    +-- self.source.stream_events(time_window, filters)
            -> Iterator[NormalizedEvent]
```

Provider 只调 EventSource，不碰任何存储细节。

---

## 6. Knowledge Layer

### 6.1 定位

Knowledge Layer 是 Logoscope 的**共享知识存储**——Entity、Fact、Relationship、Metadata 四个注册表，统一由 NormalizedEvent 驱动同步。

```
Knowledge Layer
+-- Entity Registry       # "有什么" - 资源实例及其属性
|     Instance, Volume, Port, Host, Pod, Node
|     (Neo4j 节点 + 属性)
|
+-- Relationship Registry # "怎么连" - 实体间的静态关系
|     Instance -> Port, Instance -> Host, Volume -> Instance
|     (Neo4j 边)
|
+-- Fact Registry         # "是什么" - 实体的属性事实
|     instance.state=ACTIVE, numa=4, hugepage=true
|     (KV + 时间戳 + 置信度)
|     供 Rule Engine 做条件匹配、AI 做推理上下文
|
+-- Metadata Registry     # "运行时" - 运行时元数据 (Phase 2+)
      CPU pinning, NUMA topology, PCI passthrough
```

**Fact 不是 Relationship。** 举例：

| 数据 | 类型 | 存储位置 |
|------|------|----------|
| `instance abc-123` 存在 | Entity | Entity Registry |
| `instance abc-123 在 host compute-01` | Relationship | Relationship Registry |
| `instance abc-123 state=ACTIVE` | Fact | Fact Registry |
| `instance abc-123 numa=4` | Fact | Fact Registry |

**为什么 Fact 独立：**
- Rule Engine 消费 Fact：`state=ACTIVE AND hugepage=true` 触发告警
- AI 需要 Fact 做推理上下文
- Fact 有时间和置信度属性，关系型 Registry 不适合

### 6.2 消费关系

```
                  +-----------------------+
                  |   Knowledge Layer      |
                  |                       |
                  |  Entity  Registry      |
                  |  Relationship Registry |
                  |  Fact     Registry     |
                  |  Metadata Registry     |
                  +-----------------------+
                        ^     ^     ^
                        |     |     |
              +---------+     |     +----------+
              v               v                v
       Correlation       Topology            AI
       (关系佐证)       (Node 查找)        (推理上下文)
              ^                              ^
              |                              |
       Rule Engine                     Fact Registry
       (Fact 匹配)                     (Fact 查询)
```

### 6.3 同步策略（消费 NormalizedEvent）

```
主同步: NormalizedEvent（平台无关）
    Semantic Engine -> NormalizedEvent
        |
    Entity Builder -> Entity Registry (MERGE)
    Fact Builder   -> Fact Registry (UPSERT)
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

优先级: NormalizedEvent > API。事件是最新的，API 用于补全遗漏。
```

```python
# knowledge-layer/builders/entity_builder.py

class EntityBuilder:
    """
    从 NormalizedEvent 构建/更新 Knowledge Layer Entity + Relationship。
    平台无关，不直接消费 OpenStack 通知。
    """

    def process_event(self, event: NormalizedEvent):
        for entity in event.entities:
            self._ensure_entity(entity)

        for participant in event.participants:
            self._ensure_participant_relationship(participant)

    def _ensure_entity(self, identity: ResourceIdentity):
        # Neo4j MERGE 节点
        ...

    def _ensure_participant_relationship(self, participant: EventParticipant):
        # 如果 participant.role == "actor":
        #   MERGE (actor)-[:PARTICIPATED_IN]->(event_anchor)
        ...


# knowledge-layer/builders/fact_builder.py

class FactBuilder:
    """
    从 NormalizedEvent + Extraction 结果构建 Fact。
    Fact 是 AI 和 Rule Engine 最关心的数据。
    """

    def process_event(self, event: NormalizedEvent):
        for entity in event.entities:
            # 例如：instance 出现在 CREATE.end 且 outcome=success
            # -> FACT: instance.state=ACTIVE
            fact = self._derive_facts(event, entity)
            if fact:
                self.fact_registry.upsert(fact)

    def _derive_facts(self, event, entity) -> Optional[Fact]:
        if event.action == "CREATE" and event.phase == "end" and event.outcome == "success":
            return Fact(entity=entity, key="state", value="ACTIVE",
                        source="derived", confidence=0.9, timestamp=event.timestamp)
        ...
```

### 6.4 Fact Registry 数据模型

```python
@dataclass
class Fact:
    """
    事实：实体的某种属性。
    Fact 有时效性（timestamp）、置信度、来源。

    Rule Engine: fact.key == "state" AND fact.value == "ACTIVE"
    AI: fact.source == "derived" AND fact.confidence > 0.8
    """
    entity: ResourceIdentity  # 事实主体
    key: str                  # "state", "numa", "vcpu", "hugepage"
    value: str                # "ACTIVE", "4", "64", "true"
    timestamp: datetime
    source: str               # "derived", "api", "knowledge", "ai"
    confidence: float = 1.0
```

### 6.5 Knowledge Layer API

```text
# 查询一个实体的所有关联关系
GET /api/v1/knowledge/relationships?type=INSTANCE&id=abc-123
-> {
    "entity": {"type": "INSTANCE", "id": "abc-123"},
    "relationships": [
        {"relation": "HAS_PORT",   "target": {"type": "PORT", "id": "port-xyz"}},
        {"relation": "RUNS_ON",    "target": {"type": "HOST", "id": "compute-01"}},
        {"relation": "ATTACHED_TO","target": {"type": "VOLUME", "id": "vol-789"}},
    ]
}

# 查询一个实体的 Facts
GET /api/v1/knowledge/facts?type=INSTANCE&id=abc-123
-> {
    "facts": [
        {"key": "state", "value": "ACTIVE", "source": "derived", "confidence": 0.9},
        {"key": "vcpu",  "value": "64",     "source": "api",     "confidence": 1.0},
    ]
}

# 路径查询（用于 AI 推理）
GET /api/v1/knowledge/path?source=PORT:port-xyz&target=INSTANCE
-> {"path": [{type, id, relation}, ...]}
```

---

## 7. Correlation Engine

### 7.1 架构

独立模块。不隶属于 Topology Service，AI Service 可直接调用。

```
correlation-engine/
+-- __init__.py
+-- engine.py              # CorrelationEngine 入口
+-- base.py                # CorrelationProvider 抽象基类
+-- event_source.py        # EventSource 抽象接口（流式/批量）
+-- models.py              # Observation, Extraction, Fact, Evidence, Inference, Relationship
+-- merger.py              # EvidenceMerger 多源证据融合
|
+-- providers/
|   +-- request_correlator.py     # request_id 分组
|   +-- resource_correlator.py    # ResourceIdentity (type-agnostic)
|   +-- time_correlator.py        # host + time_window
|   +-- host_correlator.py        # host + pid + thread (Phase 2)
|   +-- ai_correlator.py          # AI 推理 (Phase 4)
|
+-- tests/
```

### 7.2 五层证据链（Observation -> Extraction -> Fact -> Evidence -> Inference）

```python
# correlation-engine/models.py

@dataclass
class Observation:
    """
    原始观测：一条日志中出现的事实。
    最底层数据，可追溯至具体日志行。
    """
    log_id: str
    service_name: str
    timestamp: datetime
    observed_field: str           # "device_id", "instance_uuid"
    observed_value: str           # "abc-123"
    source_detail: str = ""       # "从 _raw_attributes 提取"

@dataclass
class Extraction:
    """
    语义提取：将 Observation 解析为语义上等价的信息。

    Observation = "device_id=abc"        (日志原文)
    Extraction  = "device_id = instance_uuid" (语义解析)
    """
    extraction_type: str    # "alias_resolution", "field_extraction"
    observation: Observation
    resolved_type: str      # "instance_uuid"
    resolved_value: str     # "abc-123"
    confidence: float = 1.0

@dataclass
class Fact:
    """
    事实：经过 Extraction + Knowledge 确认后的确定性信息。

    Fact = "instance=abc 存在" (已确认)
    """
    fact_type: str       # "alias_resolution", "entity_exists", "api_known"
    subject_type: str
    subject_value: str
    object_type: str
    object_value: str
    source: str          # "extraction", "knowledge_layer", "api"
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
    extractions: List[Extraction] = field(default_factory=list)

@dataclass
class Inference:
    """
    推断：根据证据推断两个服务之间存在调用关系。
    同一对 (source, target) 可能有多个不同证据类型的 Inference。
    """
    source: str
    target: str
    evidence_list: List[Evidence]
    inferred_relationship: str    # "calls", "depends_on", "runs_on"
```

### 7.3 Relationship（Correlation Engine 输出）

```python
@dataclass
class Relationship:
    """
    长期关系：Correlation Engine 的最终输出。
    描述两个服务之间"存在某种关系"——不包含运行时细节。

    "Nova calls Neutron" 是 Relationship。
    "2026-01-01 12:00:00 那次调用" 是 Interaction (见第 8 节)。

    不可变记录：relationship_id / version / created_at。
    """
    relationship_id: str   # 不可变 ID（不是 edge_id，避免与 Topology 混淆）
    version: int = 1
    created_at: datetime = field(default_factory=datetime.utcnow)
    source: str
    target: str
    relationship_type: str = "calls"  # calls, depends_on, runs_on, peers
    confidence: float
    call_count: int
    inferences: List[Inference]   # 完整推断链
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
    def correlate(self, time_window: str, **kwargs) -> Tuple[List[Fact], List[Inference]]:
        """
        发现关联。

        Returns:
            (facts, inferences)
        """
        ...

    @property
    @abstractmethod
    def required_source(self) -> Type[EventSource]:
        """此 Provider 需要的事件源类型"""
        ...
```

### 7.5 RequestCorrelator（按 request_id 分组）

```python
class RequestCorrelator(CorrelationProvider):
    """按 global_request_id / request_id 分组发现调用链"""

    name = "request_correlator"

    def correlate(self, time_window: str, **kwargs) -> List[Inference]:
        events = self.source.get_events(time_window)
        # 按 request_id 分组
        # 同 request_id 出现在两个服务中 -> Evidence
        # 合并 -> Inference
        ...
```

### 7.6 ResourceCorrelator（type-agnostic）

```python
class ResourceCorrelator(CorrelationProvider):
    """
    按 ResourceIdentity 分组发现关联。
    不硬编码任何资源类型 - 匹配任何在两个服务中出现的 (type, id) 对。

    新增 Kubernetes：只需 Semantic Engine 提取 POD/PVC 等 entity，
    此 Provider 零改动。
    """
    name = "resource_correlator"
    RESOURCE_MATCH_WEIGHT = 0.45

    def correlate(self, time_window: str, **kwargs) -> List[Inference]:
        # 使用 EventSource.get_participating_services()
        # 获取 (entity, service, timestamp)
        # 相同 (type, id) 在不同 service -> Observation
        # -> Extraction -> Fact（通过 Knowledge 验证）
        # -> Evidence -> Inference
        ...
```

### 7.7 EvidenceMerger

```python
class EvidenceMerger:
    """
    多源证据融合：Observation -> Extraction -> Facts -> Evidence -> Inferences -> Relationship。

    保留完整追溯链：
    Relationship.inferences[i].evidence_list[j].extractions[k].observation
      -> 可直接追溯到具体日志行
    """

    EVIDENCE_BASE_WEIGHTS = {
        "request_match":  0.20,
        "resource_match": 0.45,
        "time_window":    0.10,
        "message_target": 0.20,
        "host_match":     0.15,
        "ai_inferred":    0.10,
        "registry_relation": 0.30,
    }

    WEIGHT_DECAY_MINUTES = 120

    def merge(self, inferences: List[Inference],
              facts: Optional[List[Fact]] = None) -> Dict[Tuple[str, str], Relationship]:
        """
        合并所有 Inference -> Relationship。
        relationship_id 为不可变 UUID（非 edge_id）。
        """
        ...
```

---

## 8. Interaction（运行时交互）

### 8.1 定位

```
v4: Relationship -> Topology（直接画图）
v5: Relationship -> Interaction -> Topology（中间加一层）
```

**Relationship** 是静态的、长期的：

```
Nova <-> Neutron: calls (置信度 0.90)
```

**Interaction** 是运行时的、单次的：

```
2026-01-01T00:00:00Z  Nova -> Neutron  calls
  duration: 42ms
  request_id: req-abc-123
  resource: INSTANCE abc-123
  evidence_ref: evidence-001
```

**为什么区分：**
- Topology 画的是 Relationship（服务间存在调用关系）
- AI 看的是 Interaction（具体的调用，有时延、状态、资源）
- Timeline 看的是 Interaction 序列（一个资源的时间线）

### 8.2 Interaction 模型

```python
# interaction-builder/models.py

@dataclass
class Interaction:
    """
    运行时交互：Relationship 的一次具体实例。

    Relationship: "Nova calls Neutron"
    Interaction: "2026-01-01T00:00:00Z Nova -> Neutron, duration 42ms"
    """
    interaction_id: str
    relationship_id: str        # 关联回静态 Relationship
    timestamp: datetime
    source: str
    target: str
    interaction_type: str       # "calls", "rpc", "api_call", "message"
    duration_ms: float = 0.0
    request_id: str = ""
    resource_identity: Optional[ResourceIdentity] = None
    outcome: str = ""           # "success", "failure", "timeout"
    evidence_ref: str = ""      # 关联回 Evidence
```

### 8.3 InteractionBuilder

```python
# interaction-builder/builder.py

class InteractionBuilder:
    """
    从 Relationship + NormalizedEvent 构造 Interaction。
    """

    def build(self, relationship: Relationship,
              source_events: List[NormalizedEvent]) -> List[Interaction]:
        """
        对每个 Relationship，找出对应的事件序列。
        根据事件创建 Interaction。
        """
        interactions = []
        for event in source_events:
            interaction = Interaction(
                interaction_id=uuid4().hex,
                relationship_id=relationship.relationship_id,
                timestamp=event.timestamp,
                source=relationship.source,
                target=relationship.target,
                interaction_type=relationship.relationship_type,
                request_id=event.request_id,
                outcome=event.event.outcome,
                evidence_ref=relationship.inferences[0].evidence_list[0].match_value,
            )
            interactions.append(interaction)
        return interactions
```

---

## 9. Topology Engine

### 9.1 精简后的职责

**Topology Engine 只做三件事：**

```
TopologyEngine
+-- 1. 接收 Interaction[]（来自 Correlation + InteractionBuilder）
+-- 2. Node 查找（从 Knowledge Layer 查找节点属性，不 new 新的）
+-- 3. Layout + Render（分层/力导向）
```

不再负责：
- 置信度计算（Correlation Engine）
- 证据管理（Correlation Engine models）
- 资源关系（Knowledge Layer Relationship Registry）
- 推理（AICorrelator）
- Node 创建（Knowledge Layer Entity Registry）
- 运行时细节（InteractionBuilder）

### 9.2 实现

```python
class TopologyEngine:
    """
    拓扑引擎：纯构图。
    不 new Node，从 Knowledge Layer 查找。
    Interaction -> lookup Entity -> Edge -> Layout
    """

    def __init__(self, knowledge_layer: KnowledgeLayer):
        self.knowledge_layer = knowledge_layer

    def build(self, relationships: List[Relationship],
              interactions: List[Interaction]) -> TopologyResult:
        # 1. 收集所有服务节点（从 Knowledge Layer 查找，不是 new 的）
        nodes = self._lookup_nodes(relationships)

        # 2. 转换 Interactions -> Edges
        edges = self._interactions_to_edges(interactions)

        # 3. 布局
        layout = self._compute_layout(nodes, edges)

        return TopologyResult(nodes=nodes, edges=edges, layout=layout)

    def _lookup_nodes(self, relationships: List[Relationship]) -> List[Node]:
        """
        从 Knowledge Layer 查找服务节点。
        只有 Knowledge Layer 知道节点的完整属性：
          - service_name, version, host, zone, labels
        如果 Knowledge Layer 未知，返回基础 Node（不含属性）。
        """
        service_names = set()
        for rel in relationships:
            service_names.add(rel.source)
            service_names.add(rel.target)

        nodes = []
        for svc in service_names:
            entity = self.knowledge_layer.find_entity("SERVICE", svc)
            if entity:
                nodes.append(Node(id=svc, name=svc,
                                  attributes=entity.attributes))
            else:
                nodes.append(Node(id=svc, name=svc))
        return nodes

    def _interactions_to_edges(self, interactions: List[Interaction]) -> List[Edge]:
        """Interaction -> Edge。聚合相同 (source, target)。"""
        # 聚合 call_count, avg_duration, last_seen
        ...

    def _compute_layout(self, nodes, edges):
        """分层布局 / 力导向布局"""
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
      base += 0.45                   # 通用资源匹配权重（不区分类型）

  request_match:
    if global_request_id matches:   base += 0.35
    if request_id matches:          base += 0.20

  time_window:
    if < 0.5s same host:            base += 0.10
    if < 0.1s same host:            base += 0.15

  message_match:
    if has target match:            base += 0.20
    if inbound+outbound:            base += 0.15

  registry_relation:
    if Knowledge Layer confirms:    base += 0.30

  max_confidence = min(base, 0.98)

  time_decay:
    if minutes_since_last_seen > 120:
      decay = 0.5 ^ (minutes / 120)
    else: decay = 1.0

  final = max_confidence * decay
```

### 10.2 示例

| 场景 | resource | request | registry | time | score | 说明 |
|------|----------|---------|----------|------|-------|------|
| Nova->Neutron: device_id=instance_uuid | 0.45 | 0.35 | - | 0.10 | **0.90** | 通用资源匹配 |
| Registry 确认 Nova->Neutron 关系 | - | - | 0.30 | - | **0.30** | 已知但无运行时证据 |
| Nova->Cinder: volume_id 匹配 | 0.45 | 0.35 | - | - | **0.80** | 存储关联 |
| OVS->Neutron: Registry 知 port->network | - | - | 0.30 | 0.10 | **0.40** | 弱关联 |
| Nova->Compute: 全维度匹配 | 0.45 | 0.55 | 0.30 | 0.10 | **0.98** | 封顶 |

### 10.3 O-X-F-E-I 追溯示例

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
          "extractions": [
            {
              "extraction_type": "alias_resolution",
              "observation": {
                "log_id": "nova-api-log-001",
                "service_name": "nova-api",
                "observed_field": "instance_uuid",
                "observed_value": "abc-123"
              },
              "resolved_type": "INSTANCE",
              "resolved_value": "abc-123"
            },
            {
              "extraction_type": "alias_resolution",
              "observation": {
                "log_id": "neutron-log-042",
                "service_name": "neutron-server",
                "observed_field": "device_id",
                "observed_value": "abc-123"
              },
              "resolved_type": "INSTANCE",
              "resolved_value": "abc-123"
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
# 拓扑查询（从 Topology Engine 输出）
GET /api/v1/topology/hybrid?time_window=1+HOUR
-> {
    "nodes": [{ "id": "Nova", "attributes": {...} }],
    "edges": [{
      "edge_id": "edge-xyz",
      "source": "Nova",
      "target": "Neutron",
      "confidence": 0.90,
      "relationship_id": "rel-abc-123",  # 关联回原始 Relationship
      "call_count": 42
    }]
  }

# 证据详情（Correlation Engine 直接输出）
GET /api/v1/evidence?source=Nova&target=Neutron&time_window=1+HOUR
-> {
    "relationships": [{
      "relationship_id": "rel-abc-123",
      "version": 1,
      "source": "Nova",
      "target": "Neutron",
      "confidence": 0.90,
      "inferences": [...],     # 完整 O-X-F-E-I 追溯链
    }]
  }

# 交互详情（Interaction 运行时细节）
GET /api/v1/interactions?source=Nova&target=Neutron&time_window=1+HOUR
-> {
    "interactions": [{
      "interaction_id": "int-001",
      "timestamp": "2026-01-01T00:00:00Z",
      "duration_ms": 42,
      "outcome": "success",
      "request_id": "req-abc-123"
    }]
  }

# 知识层查询
GET /api/v1/knowledge/relationships?type=INSTANCE&id=abc-123
GET /api/v1/knowledge/facts?type=INSTANCE&id=abc-123
GET /api/v1/knowledge/path?source=PORT:port-xyz&target=INSTANCE

# Correlation Engine 直接接口（AI Service 专用）
GET /api/v1/correlate/services?source=Nova&time_window=1+HOUR
```

### 11.2 现有端点兼容

`GET /api/v1/topology/hybrid` 保持现有返回格式不变，仅在 `metrics` 内新增字段。

---

## 12. 实施阶段

### Phase 1: Core Data Architecture（约 3 周）

**目标：** NormalizedEvent v5 定稿 -> EventCategory 六维 -> EventSource 接口 -> Resource/Request Correlator -> EvidenceMerger

| 模块 | 文件 | 改动 |
|------|------|------|
| **New** | `semantic-engine/normalize/operation.py` | 新建：extract_event_category() -> EventCategory |
| **New** | `semantic-engine/normalize/resource.py` | 新建：extract_entities(), assign_participants() |
| Modify | `semantic-engine/normalize/normalizer.py` | 集成新 extractors，输出四分区 NormalizedEvent |
| Modify | `shared_src/logoscope_storage/adapter.py` | 新增列 DDL (event_*, context_*, entities_json) |
| **New** | `correlation-engine/__init__.py` | 新建包 |
| **New** | `correlation-engine/models.py` | Observation, Extraction, Fact, Evidence, Inference, Relationship |
| **New** | `correlation-engine/base.py` | CorrelationProvider 基类 |
| **New** | `correlation-engine/event_source.py` | EventSource 抽象接口 + ClickHouse/Kafka/Replay 实现 |
| **New** | `correlation-engine/engine.py` | CorrelationEngine |
| **New** | `correlation-engine/merger.py` | EvidenceMerger |
| **New** | `correlation-engine/providers/resource_correlator.py` | 通用 ResourceIdentity 匹配 |
| **New** | `correlation-engine/providers/request_correlator.py` | request_id 分组 |
| **New** | `interaction-builder/builder.py` | InteractionBuilder |
| Modify | `topology-service/graph/topology_engine.py` | 重写：Knowledge Layer Node 查找 + Interaction->Edge |
| Modify | `topology-service/api/topology_routes.py` | 新增 evidence/interaction/correlate 端点 |
| Tests | 5 个新测试文件 | event/entity/correlator/merger/interaction |

### Phase 2: Knowledge Layer + Entity/Fact Builder（约 2 周）

| 模块 | 文件 | 改动 |
|------|------|------|
| **New** | `knowledge-layer/` | 新建服务 |
| **New** | `knowledge-layer/registry/entity_registry.py` | Entity Registry (Neo4j) |
| **New** | `knowledge-layer/registry/fact_registry.py` | Fact Registry (Neo4j + KV) |
| **New** | `knowledge-layer/registry/relationship_registry.py` | Relationship Registry (Neo4j) |
| **New** | `knowledge-layer/builders/entity_builder.py` | 消费 NormalizedEvent -> Entity |
| **New** | `knowledge-layer/builders/fact_builder.py` | 消费 NormalizedEvent -> Fact |
| Modify | `topology_engine.py` | Node 查找从 Knowledge Layer |

### Phase 3: Host + Infrastructure（约 1 周）

| 模块 | 文件 | 改动 |
|------|------|------|
| **New** | `correlation-engine/providers/host_correlator.py` | host + pid + thread |
| Modify | `semantic-engine/normalize/resource.py` | 展开 _EXTRACTION_MAP |

### Phase 4: AI + Multi-platform（约 2 周）

| 模块 | 文件 | 改动 |
|------|------|------|
| **New** | `correlation-engine/providers/ai_correlator.py` | AI 推理 |
| Modify | `ai-service/` | Interaction 推理接口 |
| Modify | `semantic-engine/normalize/resource.py` | 增加 POD, NODE, PVC 等 Kubernetes 实体提取 |
| Modify | `knowledge-layer/builders/` | 平台无关化：K8s/VMware adapter |

---

## 13. 测试策略

### 13.1 Semantic Engine 测试

```python
# tests/test_entity_extraction.py

def test_extract_instance_uuid():
    log = {"_raw_attributes": {"instance_uuid": "abc-123-def-456"}}
    entities = extract_entities(log)
    assert len(entities) == 1
    assert entities[0].type == "INSTANCE"
    assert entities[0].id == "abc-123-def-456"

def test_device_id_extraction():
    """device_id 归一为 instance_uuid 的 Extraction"""
    log = {"_raw_attributes": {"device_id": "abc-123-def-456"}}
    entities = extract_entities(log)
    assert len(entities) == 1
    assert entities[0].type == "INSTANCE"

def test_assign_participants_attach_volume():
    """attach_volume 有 actor 和 target 参与者"""
    category = EventCategory(resource="INSTANCE", action="ATTACH_VOLUME")
    entities = [
        ResourceIdentity("INSTANCE", "abc-123"),
        ResourceIdentity("VOLUME", "vol-456"),
    ]
    participants = assign_participants(category, entities)
    assert len(participants) == 2
    assert any(p.role == "actor" for p in participants)
    assert any(p.role == "target" for p in participants)

def test_normalized_event_four_sections():
    """NormalizedEvent 包含 event/context/entities/attributes 四分区"""
    log = {"_raw_attributes": {"event_type": "volume.attach.end",
                                "instance_uuid": "abc-123",
                                "volume_id": "vol-456"}}
    event = normalize_log(log)
    assert event.event.category == "infra"
    assert event.event.resource == "INSTANCE"
    assert event.event.action == "ATTACH_VOLUME"
    assert len(event.entities) == 2
    assert len(event.participants) == 2
```

### 13.2 Correlation Engine 测试

```python
def test_resource_correlator_any_type():
    """通用资源关联：不区分资源类型，匹配任何 (type, id) 对"""
    source = ReplayEventSource([
        NormalizedEvent(timestamp=..., service_name="A",
                        entities=[ResourceIdentity("INSTANCE", "abc")]),
        NormalizedEvent(timestamp=..., service_name="B",
                        entities=[ResourceIdentity("INSTANCE", "abc")]),
    ])
    correlator = ResourceCorrelator(source)
    results = correlator.correlate("1 HOUR")
    assert len(results) == 1
    assert results[0].source == "A"
    assert results[0].target == "B"

def test_extraction_preserved_in_chain():
    """Extraction 层在证据链中保留"""
    obs = Observation("log-1", "A", datetime(2026, 1, 1),
                      "device_id", "abc-123")
    ext = Extraction("alias_resolution", obs, "instance_uuid", "abc-123")
    fact = Fact("alias_resolution", "device_id", "abc-123",
                "instance_uuid", "abc-123", source="extraction")
    evidence = Evidence("A", "B", "resource_match", "INSTANCE:abc", 0.45,
                        extractions=[ext])
    inference = Inference("A", "B", [evidence], "calls")

    merger = EvidenceMerger()
    result = merger.merge([inference], facts=[fact])
    assert ("A", "B") in result
    rel = result[("A", "B")]
    assert rel.relationship_id != ""
    assert rel.version >= 1
    # Extraction 层在链中保留
    assert rel.inferences[0].evidence_list[0].extractions[0].extraction_type \
        == "alias_resolution"
```

### 13.3 Interaction + Topology 测试

```python
def test_interaction_from_relationship():
    """Relationship + Event -> Interaction"""
    rel = Relationship(
        relationship_id="rel-1", source="A", target="B",
        relationship_type="calls", confidence=0.90,
    )
    events = [NormalizedEvent(timestamp=datetime(2026, 1, 1, 12, 0, 0),
                              service_name="A", request_id="req-1")]
    builder = InteractionBuilder()
    interactions = builder.build(rel, events)
    assert len(interactions) == 1
    assert interactions[0].relationship_id == "rel-1"
    assert interactions[0].interaction_type == "calls"

def test_topology_looks_up_nodes_from_knowledge():
    """Topology Engine 从 Knowledge Layer 查找 Node"""
    kl = FakeKnowledgeLayer({
        "SERVICE": {
            "A": NodeAttributes(version="1.0", host="host-1"),
            "B": NodeAttributes(version="2.0", host="host-2"),
        }
    })
    engine = TopologyEngine(kl)
    rels = [Relationship(relationship_id="r1", source="A", target="B",
                         relationship_type="calls", confidence=0.9)]
    result = engine.build(rels, [])
    assert len(result.nodes) == 2
    assert result.nodes[0].attributes["host"] == "host-1"

def test_topology_edge_uses_relationship_id():
    """Topology Edge 引用原始 relationship_id"""
    kl = FakeKnowledgeLayer({})
    engine = TopologyEngine(kl)
    interactions = [
        Interaction("i1", "rel-1", datetime(2026, 1, 1),
                    "A", "B", "calls", outcome="success"),
        Interaction("i2", "rel-1", datetime(2026, 1, 2),
                    "A", "B", "calls", outcome="success"),
    ]
    result = engine.build([], interactions)
    assert result.edges[0].relationship_id == "rel-1"
```

### 13.4 EventSource 测试

```python
def test_replay_event_source():
    """ReplayEventSource 回放测试"""
    events = [NormalizedEvent(...), NormalizedEvent(...)]
    source = ReplayEventSource(events)
    result = list(source.stream_events("1 HOUR"))
    assert len(result) == 2

def test_clickhouse_source_uses_index_columns():
    """ClickHouseEventSource 使用索引列加速"""
    # 验证生成的 SQL 包含 PREWHERE instance_uuid != '' 等
    ...
```

---

## 14. 性能考量

| 场景 | 当前 | 优化后 | 改善 |
|------|------|--------|------|
| 单次拓扑查询（1h 窗口） | ~800ms | ~500ms | Bloom filter + 独立列 |
| EventSource 流式读取 | 无 | 实时 | KafkaEventSource |
| Resource 关联查询 | 无 | ~300ms | Bloom filter 索引 |
| Resource 关联查询（有 MV） | 无 | ~50ms | 预聚合 materialized view |
| Knowledge Layer Entity 查询 | 无 | ~5ms | Neo4j 索引 |
| Fact Registry 查询 | 无 | ~10ms | KV 索引 |
| Interaction 构建 | 无 | ~100ms/1k | 批量处理 |

---

## 15. 向后兼容

| 影响点 | 兼容策略 |
|--------|----------|
| ClickHouse 存量数据 | ALTER TABLE ADD COLUMN ... DEFAULT '' |
| 现有 API /hybrid | 保持输出格式不变，仅新增字段 |
| 现有测试 | 不改现有测试，新增模块测试 |
| 前端 | 新增字段不影响现有渲染逻辑 |
| Topology Service | Correlation Engine 可独立部署，通过 API 调用 |
| event_resource/event_verb -> event_category | 旧列保留，新列 ADD COLUMN event_category_json |
