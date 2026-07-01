# Resource Correlation Engine Design

> **将 Logoscope 的拓扑从单一 request_id 关联升级为多级资源关联模型（Resource-based Correlation Engine），解决 OpenStack 场景下调用链断裂和覆盖率不足的问题。**
> 引入 Knowledge Layer（Relationship Registry + Event Model），使 Correlation、Topology、AI、Rule Engine 共享统一的事实层和事件模型。
>
> **Key design decisions:**
> - NO `primary_resource` — all resources are equal, expressed as `resources[ResourceRef]` with roles
> - NO hardcoded resource types in Correlation Engine — uses generic `ResourceIdentity` matching
> - Knowledge Layer is platform-wide, not owned by Topology
> - Event Store (ClickHouse) stores `NormalizedEvent`, not raw logs
> - Correlation Engine outputs **Relationship**, not Edge — Topology converts Relationship → Edge
> - Providers consume **EventRepository** abstract interface, not ClickHouse directly
> - No `EdgeResult` — use `RelationshipResult` with immutable records (edge_id, version, created_at)

**Status:** Draft v3  
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

### 1.2 为什么要用 Resource Correlation

OpenStack 日志中实际存在比 request_id 更稳定的关联键——**资源 UUID**：

| 资源 UUID | 出现组件 | 覆盖场景 |
|-----------|----------|----------|
| `instance_uuid` | Nova API/Compute/Sched, Neutron, Cinder, Glance, Libvirt | VM 全生命周期 |
| `volume_id` | Cinder API/Volume, Nova Compute, os-brick | 存储操作 |
| `port_id` | Neutron, Nova Compute, OVS Agent | 网络操作 |
| `image_id` | Glance API, Nova Compute | 镜像操作 |

这些 UUID 在请求跨组件传递时**保持不变**，即使 request_id 被 RPC 重新生成或丢失。

### 1.3 从「日志关联」到「知识层」的跨越

仅靠日志字段关联是不够的。例如 OVS Agent 日志中出现 `port abc-123`，要推断它属于 `VM xyz-789`，需要知道 Neutron 层面的资源关系：

```
Port abc-123  →  device_id = instance xyz-789  →  Instance xyz-789  →  host compute-01
```

这种关系不来自日志，而来自 **OpenStack API**（Neutron list_ports、Nova list_servers）。引入 **Knowledge Layer (Relationship Registry)** 将这些已知资源关系存入 Neo4j，使整个平台（Correlation、Topology、AI、Rule Engine）共享同一事实层。

---

## 2. Architecture

### 2.1 整体架构

```text
Data Pipeline
══════════════════════════════════════════════════════════════════════════════════

[Fluent Bit] → [OTel Collector]
         │
         ▼
  ┌──────────────────────────────────────────────────┐
  │                Semantic Engine                     │
  │                                                  │
  │  ┌──────────────────────────────────────────┐   │
  │  │ Normalize Pipeline                       │   │
  │  │                                          │   │
  │  │ ① extract_operation()                   │   │  CREATE_VM → {resource:INSTANCE, verb:CREATE}
  │  │ ② extract_resource_fields()             │   │  instance_uuid, volume_id, ...
  │  │ ③ resolve_aliases()                     │   │  device_id → instance_uuid
  │  │ ④ assign_resource_roles()               │   │  resources[{INSTANCE, abc, role:actor}, ...]
  │  │ ⑤ build_normalized_event()              │   │  输出统一 Event 模型
  │  └──────────────────────────────────────────┘   │
  │         │                                        │
  │         ▼ NormalizedEvent                        │
  │  ┌──────────────────────────────────────────┐   │
  │  │ NormalizedEvent {                        │   │
  │  │   timestamp, service, host, severity,    │   │
  │  │   event: {resource, verb},               │   │  ← resource=INSTANCE, verb=CREATE
  │  │   resources: [{type, id, role}, ...],    │   │  ← 多资源，带角色
  │  │   request: {trace_id, req_id, glob_id}, │   │
  │  │   message, attributes                    │   │
  │  │ }                                        │   │
  │  └──────────────────────────────────────────┘   │
  └──────────────────────────────────────────────────┘
         │
         ▼ NormalizedEvent
  ┌──────────────────────────────────────────────────┐
  │            Event Store (ClickHouse)               │
  │                                                  │
  │  logs.logs  — 实际上存储的是标准化 Event          │
  │   ├─ event_resource / event_verb                  │
  │   ├─ resources_json (完整资源列表)                │
  │   ├─ instance_uuid / volume_id / ... (索引列)    │
  │   └─ attributes_json                             │
  └──────────────────────────────────────────────────┘
         │
         ▼
  ╔══════════════════════════════════════════════════╗
  ║             Knowledge Layer                       ║
  ║  (全平台共享的 Knowledge Platform)                ║
  ║                                                  ║
  ║  ┌──────────────────────────────────────────┐   ║
  ║  │ Resource Registry                        │   ║
  ║  │  Instance, Volume, Port, Host, Flavor    │   ║
  ║  │  (Neo4j + 属性完整)                      │   ║
  ║  ├──────────────────────────────────────────┤   ║
  ║  │ Relationship Registry                     │   ║
  ║  │  Instance ─→ Port ─→ Network              │   ║
  ║  │  Instance ─→ Host                         │   ║
  ║  │  Volume ─→ Attachment ─→ Instance         │   ║
  ║  │  (同步: 事件驱动为主, API Cold Sync 为辅)  │   ║
  ║  ├──────────────────────────────────────────┤   ║
  ║  │ Metadata Registry (future)                │   ║
  ║  │  NUMA, CPU Policy, Hugepage, PCI, SRIOV │   ║
  ║  └──────────────────────────────────────────┘   ║
  ║                                                  ║
  ║  Consumers (all equal):                          ║
  ║   Correlation · Topology · AI · Rule            ║
  ╚══════════════════════════════════════════════════╝
         │
         ▼
  ┌──────────────────────────────────────────────────┐
  │          Correlation Engine                       │
  │  (消费 EventStore + Knowledge Layer)             │
  │                                                  │
  │  ┌──────────────────────────────────────────┐   │
  │  │ Providers (pluggable)                    │   │
  │  │  • RequestCorrelator  — request_id       │   │
  │  │  • ResourceCorrelator — ResourceIdentity │   │
  │  │  • TimeCorrelator     — host+time_window │   │
  │  │  • HostCorrelator     — host+pid (P2)   │   │
  │  │  • AICorrelator       — AI 推理 (P4)    │   │
  │  │                                          │   │
  │  │  Data: EventRepository (抽象接口)        │   │
  │  │  (不是直接写 SQL，适配 ClickHouse/S3/...)│   │
  │  └──────────────────────────────────────────┘   │
  │                                                  │
  │  ↓ Observations → Facts → Evidence → Inference  │
  │    → RelationshipResult (含完整追溯链)            │
  │    → 关系不可变，带 version/created_at            │
  └──────────────────────────────────────────────────┘
         │
         ▼ RelationshipResult[]
  ┌──────────────────────────────────────────────────┐
  │           Topology Engine                         │
  │                                                  │
  │  RelationshipResult ──→ _to_edges() ──→ Edge[]  │
  │                                                  │
  │  纯构图: Node → Edge → Layout → Render            │
  │  不计算置信度, 不存储证据, 不定义关系              │
  └──────────────────────────────────────────────────┘
         │
         ▼
  API / Frontend / AI Service

  ┌──────────────────────────────────────────────────┐
  │           Rule Engine (future)                    │
  │  消费 NormalizedEvent 做规则匹配、告警            │
  └──────────────────────────────────────────────────┘
```

### 2.2 数据流

```
Raw Log
   ↓ Semantic Engine
NormalizedEvent (event.resource+verb, resources[] with roles, request, attributes)
   ↓ Event Store (ClickHouse + EventRepository 抽象)
Indexed columns (instance_uuid, volume_id, ...) + resources_json
   ↓ Correlation Engine (via EventRepository, not direct SQL)
Observations → Facts → Evidence → Inference → RelationshipResult (不可变, 带 version)
   ↓ Topology Engine
RelationshipResult → _to_edges() → Graph (nodes + edges)
   ↓
API / Frontend / AI
```

### 2.3 组件职责

| 层 | 职责 | 不做什么 |
|----|------|----------|
| **Semantic Engine** | 日志理解、标准化、输出 NormalizedEvent | 不计算拓扑、不做关联 |
| **Event Store (ClickHouse)** | 存储 NormalizedEvent、索引、预聚合 | 不做业务逻辑 |
| **EventRepository** | 抽象数据访问层，屏蔽 ClickHouse/S3/Kafka 差异 | 不涉及具体查询逻辑 |
| **Knowledge Layer** | 资源注册 + 关系 + 元数据，共享事实层 | 不分析日志、不计算置信度 |
| **Correlation Engine** | O→F→E→I→R 链，输出不可变 Relationship | 不画图、不存储、不知 Graph 概念 |
| **Topology Engine** | Relationship → Edge → Graph → Layout → Render | 不计算置信度、不推理 |
| **AI Service** | 推理补全不可证明的关系 | 不覆盖已有证据的关系 |
| **Rule Engine (future)** | 基于 NormalizedEvent 的规则匹配告警 | 不画图、不关联 |

### 2.4 为什么取消 primary_resource

`primary_resource` 试图回答"哪个资源最重要"，但在多资源事件（attach_volume、live_migration）中，这个选择是武断的。改为 `resources[ResourceRef]` + role：

- **attach_volume**: `resources = [{INSTANCE, abc, actor}, {VOLUME, vol-123, target}]`
- **live_migration**: `resources = [{INSTANCE, abc, actor}, {HOST, host-a, source}, {HOST, host-b, target}]`
- **create_port**: `resources = [{PORT, p-xyz, actor}, {NETWORK, n-456, target}]`

AI 和 Correlation Engine 看到完整的资源列表，自己决定用哪个。

### 2.5 为什么 Correlation Engine 不用硬编码资源类型

ResourceCorrelator 不关心 `INSTANCE` vs `VOLUME` vs `POD`。它只匹配 `(type, id)` 对：

```python
# 匹配任何在两个服务中出现相同 (type, id) 的资源
# OpenStack: (INSTANCE, abc-123) → Nova + Neutron 都出现 → 产生边
# Kubernetes: (POD, xyz-789) → kube-apiserver + kubelet 都出现 → 产生边
```

新增平台只需在 Semantic Engine 的 extractors 中注册新资源类型，Correlation Engine 零改动。

---

## 3. Normalized Event 模型

### 3.1 为什么需要统一 Event 模型

NormalizedEvent 是整个平台的**数据契约**。所有下游（Correlation、Topology、AI、Rule Engine）围绕同一模型工作，避免每个模块各自从 raw log 中解析。

ClickHouse 存储的不再是"日志"，而是**标准化事件**（Event）。

### 3.2 Event Schema

```python
@dataclass
class ResourceRef:
    """资源引用：type + id + role + version 信息"""
    type: str           # INSTANCE, VOLUME, PORT, IMAGE, POD, NODE, PVC, ...
    id: str             # 资源 UUID
    role: str = ""      # actor, target, source, destination, ...
    version: str = ""   # 资源版本 (例如 OpenStack microversion)
    confidence: float = 1.0  # 此引用的置信度 (1.0 = API 确认, <1.0 = 日志推断)

@dataclass
class EventType:
    """事件类型：domain + resource + verb + phase 四维模型

    Rule Engine 可以直接匹配 domain=compute, resource=INSTANCE, verb=CREATE, phase=end
    AI 可以问 "哪些 domain=compute 的操作最近失败了？"
    """
    domain: str = ""    # compute, network, volume, image, identity, ...
    resource: str       # INSTANCE, VOLUME, PORT, ...
    verb: str           # CREATE, DELETE, ATTACH, DETACH, REBOOT, ...
    phase: str = ""     # start, end, error (对应 OpenStack notification phases)

@dataclass
class NormalizedEvent:
    """Semantic Engine 输出的统一 Event 模型"""

    # 时间 & 标识
    timestamp: datetime
    service_name: str           # nova-api, nova-compute, neutron-server
    pod_name: str
    namespace: str
    host: str
    source_cluster: str

    # 日志元信息
    severity: str               # INFO, WARN, ERROR
    message: str
    pid: int = 0
    thread: str = ""

    # 事件类型（resource + verb）
    event: EventType

    # 资源列表（多资源 + 角色，替代 primary_resource）
    resources: List[ResourceRef] = field(default_factory=list)

    # 请求标识
    trace_id: str = ""
    span_id: str = ""
    request_id: str = ""
    global_request_id: str = ""

    # 具体资源列（独立索引列，保留查询性能）
    instance_uuid: str = ""
    volume_id: str = ""
    port_id: str = ""
    image_id: str = ""
    aggregate: str = ""

    # 完整保留
    attributes_json: str = ""
    labels_json: str = ""
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
    event=EventType(domain="volume", resource="INSTANCE", verb="ATTACH_VOLUME", phase="end"),
    resources=[
        ResourceRef(type="INSTANCE", id="abc-123", role="actor"),
        ResourceRef(type="VOLUME", id="vol-456", role="target"),
    ],
    instance_uuid="abc-123",
    volume_id="vol-456",
    request_id="req-abc-123",
)
```

### 3.4 Event 消费关系

```
NormalizedEvent
    │
    ├── ClickHouse          → 所有 flat 字段 + resources_json
    ├── Correlation Engine  → 取 resources[] 做 ResourceIdentity 匹配
    ├── Topology Engine     → 取 service_name + resources 作为图的属性
    ├── AI Service          → 取 message + resources + event 做推理
    └── Rule Engine         → 取 event + severity + service_name 做规则匹配
```

---

## 4. Semantic Engine 改造

### 4.1 新增 extract_operation()

从日志中识别标准化的事件类型（resource + verb 模型）。

```python
# semantic-engine/normalize/operation.py

_OPERATION_PATTERNS = {
    # (event_type_prefix, action_verb) → (domain, resource, verb, phase)
    ("compute.instance.create.start", None):  ("compute", "INSTANCE", "CREATE", "start"),
    ("compute.instance.create.end", None):    ("compute", "INSTANCE", "CREATE", "end"),
    ("compute.instance.create.error", None):  ("compute", "INSTANCE", "CREATE", "error"),
    ("compute.instance.delete.end", None):    ("compute", "INSTANCE", "DELETE", "end"),
    ("compute.instance.rebuild.end", None):   ("compute", "INSTANCE", "REBUILD", "end"),
    ("compute.instance.reboot.end", None):    ("compute", "INSTANCE", "REBOOT", "end"),
    ("compute.instance.power_off.end", None): ("compute", "INSTANCE", "POWER_OFF", "end"),
    ("compute.instance.power_on.end", None):  ("compute", "INSTANCE", "POWER_ON", "end"),
    ("compute.instance.live_migration.end", None): ("compute", "INSTANCE", "LIVE_MIGRATE", "end"),
    ("compute.instance.resize.end", None):    ("compute", "INSTANCE", "RESIZE", "end"),
    ("volume.attach.end", None):              ("volume", "INSTANCE", "ATTACH_VOLUME", "end"),
    ("volume.detach.end", None):              ("volume", "INSTANCE", "DETACH_VOLUME", "end"),
    ("volume.create.end", None):              ("volume", "VOLUME", "CREATE", "end"),
    ("volume.delete.end", None):              ("volume", "VOLUME", "DELETE", "end"),
    ("port.create.end", None):                ("network", "PORT", "CREATE", "end"),
    ("port.delete.end", None):                ("network", "PORT", "DELETE", "end"),
    ("image.create.end", None):               ("image", "IMAGE", "CREATE", "end"),
    ("image.delete.end", None):               ("image", "IMAGE", "DELETE", "end"),

    # Phase 2: message text fallback (domain unknown)
    (None, "attach"):                         ("", "INSTANCE", "ATTACH_VOLUME", ""),
    (None, "detach"):                         ("", "INSTANCE", "DETACH_VOLUME", ""),
    (None, "spawn"):                          ("", "INSTANCE", "SPAWN", ""),
    (None, "create_server"):                  ("", "INSTANCE", "CREATE", ""),
}

def extract_operation(log_data: Dict[str, Any]) -> EventType:
    """从日志中提取标准化 EventType。优先级: event_type > action > message text"""
    event_type = _candidate_text(log_data.get("event_type") or ...)
    action = _candidate_text(log_data.get("action") or ...)

    for (ev_prefix, act_verb), (domain, resource, verb, phase) in _OPERATION_PATTERNS.items():
        if ev_prefix and event_type.startswith(ev_prefix):
            return EventType(domain=domain, resource=resource, verb=verb, phase=phase)

    for (ev_prefix, act_verb), (domain, resource, verb, phase) in _OPERATION_PATTERNS.items():
        if act_verb and action and act_verb in action.lower():
            return EventType(domain=domain, resource=resource, verb=verb, phase=phase)

    return EventType(domain="", resource="UNKNOWN", verb="UNKNOWN", phase="")
### 4.2 新增 extract_resource_fields()

从 `_raw_attributes` 和 message 中提取所有已知的资源 UUID。输出为 `List[ResourceRef]`。

```python
# semantic-engine/normalize/resource.py

# Phase 1 Core Resources（直接提取，带 ClickHouse 索引）
_RESOURCE_FIELD_MAP = {
    "instance_uuid": ["instance_uuid", "instance_id", "server_id", "uuid"],
    "volume_id":     ["volume_id", "volumeId"],
    "port_id":       ["port_id", "portId"],
    "image_id":      ["image_id", "imageId", "image_uuid"],
}

# Phase 2 Alias Map（归一，在 Semantic Engine 完成）
_ALIAS_MAP = {
    "device_id":     "instance_uuid",
    "consumer_uuid": "instance_uuid",
    "domain_uuid":   "instance_uuid",
    "qemu_uuid":     "instance_uuid",
    "snapshot_id":   "volume_id",
    "attachment_id": "volume_id",
}

# 资源角色推断规则（替代旧的 primary_resource 规则）
_ROLE_RULES = {
    # (resource_type, verb) → {resource_type: role, ...}
    ("INSTANCE", "ATTACH_VOLUME"): {"INSTANCE": "actor", "VOLUME": "target"},
    ("INSTANCE", "DETACH_VOLUME"): {"INSTANCE": "actor", "VOLUME": "target"},
    ("INSTANCE", "CREATE"):        {"INSTANCE": "actor"},
    ("VOLUME", "CREATE"):          {"VOLUME": "actor"},
    ("PORT", "CREATE"):            {"PORT": "actor", "NETWORK": "target"},
    ("INSTANCE", "LIVE_MIGRATE"):  {"INSTANCE": "actor",
                                     "HOST.source": "source", "HOST.destination": "target"},
}


def extract_resource_fields(log_data: Dict[str, Any]) -> Dict[str, str]:
    """提取资源 UUID 字段，包含 alias 归一化"""
    message = _candidate_text(log_data.get("message"))
    raw_attrs = log_data.get("_raw_attributes", {})
    if not isinstance(raw_attrs, dict):
        raw_attrs = {}

    resources = {}
    for target_key, source_keys in _RESOURCE_FIELD_MAP.items():
        for source_key in source_keys:
            value = _find_value(raw_attrs, message, source_key)
            if value:
                resources[target_key] = value
                break

    for alias_key, target_key in _ALIAS_MAP.items():
        if target_key not in resources:
            value = _find_value(raw_attrs, message, alias_key)
            if value:
                resources[target_key] = value

    return resources


def assign_resource_roles(
    event_type: EventType,
    resource_fields: Dict[str, str],
) -> List[ResourceRef]:
    """
    为资源分配角色（替代 primary_resource）。
    无角色时保留所有资源，由下游自己决定用哪个。
    """
    role_map = _ROLE_RULES.get((event_type.resource, event_type.verb), {})

    refs = []
    for field_name, uuid in resource_fields.items():
        if not uuid:
            continue
        res_type = _resource_type_from_field(field_name)
        role = role_map.get(res_type, "")
        refs.append(ResourceRef(type=res_type, id=uuid, role=role))

    return refs


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

    event_type = extract_operation(log_data)
    resource_fields = extract_resource_fields(log_data)
    resource_refs = assign_resource_roles(event_type, resource_fields)

    return NormalizedEvent(
        timestamp=...,
        service_name=...,
        host=...,
        severity=...,
        message=...,
        event=event_type,
        resources=resource_refs,  # List[ResourceRef]
        trace_id=...,
        span_id=...,
        request_id=...,
        global_request_id=...,
        instance_uuid=resource_fields.get("instance_uuid", ""),
        volume_id=resource_fields.get("volume_id", ""),
        port_id=resource_fields.get("port_id", ""),
        image_id=resource_fields.get("image_id", ""),
        attributes_json=json.dumps(raw_attributes),
    )
```

---

## 5. Event Store (ClickHouse)

### 5.1 ALTER TABLE（增量迁移，幂等）

```sql
-- Phase 1 Core
ALTER TABLE logs.logs ADD COLUMN IF NOT EXISTS event_resource String DEFAULT '';
ALTER TABLE logs.logs ADD COLUMN IF NOT EXISTS event_verb String DEFAULT '';
ALTER TABLE logs.logs ADD COLUMN IF NOT EXISTS resources_json String DEFAULT '';
ALTER TABLE logs.logs ADD COLUMN IF NOT EXISTS instance_uuid String DEFAULT '';
ALTER TABLE logs.logs ADD COLUMN IF NOT EXISTS volume_id String DEFAULT '';
ALTER TABLE logs.logs ADD COLUMN IF NOT EXISTS port_id String DEFAULT '';
ALTER TABLE logs.logs ADD COLUMN IF NOT EXISTS image_id String DEFAULT '';

-- Phase 3 Infrastructure
ALTER TABLE logs.logs ADD COLUMN IF NOT EXISTS aggregate String DEFAULT '';

-- Indexes
ALTER TABLE logs.logs ADD INDEX IF NOT EXISTS idx_instance_uuid (instance_uuid) TYPE bloom_filter(0.025) GRANULARITY 4;
ALTER TABLE logs.logs ADD INDEX IF NOT EXISTS idx_volume_id (volume_id) TYPE bloom_filter(0.025) GRANULARITY 4;
ALTER TABLE logs.logs ADD INDEX IF NOT EXISTS idx_port_id (port_id) TYPE bloom_filter(0.025) GRANULARITY 4;
ALTER TABLE logs.logs ADD INDEX IF NOT EXISTS idx_image_id (image_id) TYPE bloom_filter(0.025) GRANULARITY 4;
ALTER TABLE logs.logs ADD INDEX IF NOT EXISTS idx_event_resource (event_resource) TYPE set(32) GRANULARITY 4;
```

### 5.2 Materialized View

```sql
-- 预聚合：按 resources[] 中每个 (type, id) 分组的服务序列
CREATE MATERIALIZED VIEW IF NOT EXISTS logs.resource_edges_mv
ENGINE = AggregatingMergeTree
PARTITION BY toDate(timestamp)
ORDER BY (resource_type, resource_id, timestamp)
AS SELECT
    resource_type,
    resource_id,
    service_name,
    timestamp
FROM logs.logs
WHERE instance_uuid != '' OR volume_id != '' OR port_id != '' OR image_id != ''
ORDER BY resource_type, resource_id, timestamp;
```

### 5.3 INSERT 示例

```python
# Semantic Engine 写入时
insert = {
    "timestamp": event.timestamp,
    "service_name": event.service_name,
    "event_resource": event.event.resource,
    "event_verb": event.event.verb,
    "resources_json": json.dumps([asdict(r) for r in event.resources]),
    "instance_uuid": event.instance_uuid,
    "volume_id": event.volume_id,
    "port_id": event.port_id,
    "image_id": event.image_id,
    # ... other fields preserved ...
}
```

---

## 6. Correlation Engine

### 6.1 架构

独立模块 `correlation-engine/`，不隶属于 Topology Service，AI Service 可直接调用。

```
correlation-engine/
├── __init__.py
├── engine.py              # CorrelationEngine 入口（注册 provider + 调度）
├── base.py                # CorrelationProvider 抽象基类
├── event_repository.py    # EventRepository 抽象接口（适配 ClickHouse/S3/Iceberg）
├── models.py              # Observation, Fact, Evidence, Inference, RelationshipResult
│                          # 分层模型：Observation → Fact → Evidence → Inference → Relationship
├── merger.py              # EvidenceMerger 多源证据融合
│
├── providers/
│   ├── request_correlator.py     # request_id 分组
│   ├── resource_correlator.py    # ResourceIdentity (type-agnostic)
│   ├── time_correlator.py        # host + time_window
│   ├── host_correlator.py        # host + pid + thread (Phase 2)
│   └── ai_correlator.py          # AI 推理 (Phase 4)
│
└── tests/
```

### 6.2 分层数据模型（Observation → Fact → Evidence → Inference → Relationship）

```python
# correlation-engine/models.py

@dataclass
class Observation:
    """
    原始观测：一条日志中出现的事实。
    这是最底层的数据，可追溯至具体日志行。
    """
    log_id: str
    service_name: str
    timestamp: datetime
    observed_field: str           # "instance_uuid", "port_id", "host"
    observed_value: str           # "abc-123"
    source_detail: str = ""       # "从 _raw_attributes 提取" / "从 message 正则提取"

@dataclass
class Evidence:
    """
    证据：同一条观测事实出现在两个服务中。
    例如：instance_uuid=abc 出现在 nova-api 和 nova-compute。
    """
    source_service: str
    target_service: str
    evidence_type: str            # "resource_match", "request_match", "host_match"
    match_value: str              # "INSTANCE:abc-123", "req-abc-123"
    weight: float
    observations: List[Observation] = field(default_factory=list)  # ← 可追溯至原始日志

@dataclass
class Fact:
    """
    事实：从 Observation 提炼的确定性信息。
    Observation = "device_id=abc" (日志原文)
    Fact = "device_id 是 instance_uuid，值为 abc" (经过知识层解析)
    Fact 可以来自日志、API、CMDB、Knowledge Graph、AI，全部统一。
    """
    fact_type: str            # "alias_resolution", "resource_relation", "api_known"
    subject_type: str         # "device_id", "port_id"
    subject_value: str        # "abc-123"
    object_type: str          # "instance_uuid", "instance_id"
    object_value: str         # "abc-123"
    source: str               # "alias_map", "knowledge_layer", "api"
    confidence: float = 1.0   # API 确认 = 1.0, 推断 = <1.0

@dataclass
class Inference:
    """
    推断：根据证据推断两个服务之间存在调用关系。
    同一对 (source, target) 可能有多个 Inference（来自不同证据类型）。
    """
    source: str
    target: str
    evidence_list: List[Evidence]   # 这条推断的所有证据
    inferred_relationship: str      # "calls", "depends_on", "runs_on"

@dataclass
class RelationshipResult:
    """
    最终关系：经过 EvidenceMerger 合并后的结果。
    Correlation Engine 不知道 Graph 概念，只输出 Relationship。
    Topology Engine 负责 Relationship → Edge 的转换。
    
    不可变记录：edge_id/version/created_at 用于追溯历史。
    """
    edge_id: str              # 不可变 ID，用于追溯历史
    version: int = 1          # 版本号，每次重新生成递增
    created_at: datetime = field(default_factory=datetime.utcnow)
    source: str
    target: str
    confidence: float
    call_count: int
    inferences: List[Inference]   # 完整推断链（可追溯至 Observation → Fact）
    data_sources: List[str]
```

### 6.3 CorrelationProvider 接口

```python
# correlation-engine/base.py

class CorrelationProvider(ABC):
    """关联提供者：从一种维度发现候选边。新增 provider = 新增文件。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """provider 名称"""
        ...

    @abstractmethod
    def correlate(self, time_window: str, **kwargs) -> Tuple[List[Fact], List[Inference]]:
        """
        在指定时间窗口内发现关联。

        Returns:
            (facts, inferences) — Facts 是提炼后的确定性事实，
                                  Inferences 是基于 Facts 的关联推断
        """
        ...
```

### 6.4 EventRepository 接口

Providers 不直接写 ClickHouse SQL，通过 `EventRepository` 抽象接口访问数据：

```python
# correlation-engine/event_repository.py

class EventRepository(ABC):
    """
    事件存储抽象接口。
    Provider 只调用此接口，不直接写 SQL。
    适配器模式：ClickHouse/S3/Iceberg/Kafka 各有自己的实现。
    """

    @abstractmethod
    def query_events(self, time_window: str, filters: Dict[str, str]) -> List[NormalizedEvent]:
        """
        查询 NormalizedEvent。
        Args:
            time_window: "1 HOUR", "30 MINUTE"
            filters: {"instance_uuid": "abc", "service_name": "nova-api"}
        Returns:
            List[NormalizedEvent]
        """
        ...

    @abstractmethod
    def query_resource_timeline(self, resource_type: str, resource_id: str,
                                 time_window: str) -> List[NormalizedEvent]:
        """
        查询一个资源的时间线（Phase 2 Resource Timeline）。
        按时间排序的事件序列。
        """
        ...

    @abstractmethod
    def query_resource_pairs(self, time_window: str) -> List[Tuple[str, str, str, str]]:
        """
        查询所有 (resource_type, resource_id, service_name, timestamp) 组合。
        供 ResourceCorrelator 使用。
        """
        ...


class ClickHouseEventRepository(EventRepository):
    """ClickHouse 实现，使用独立索引列加速"""
    ...

class LogEventRepository(EventRepository):
    """直接解析日志文件（本地调试用）"""
    ...
```

### 6.5 RequestCorrelator

```python
class RequestCorrelator(CorrelationProvider):
    """按 global_request_id / request_id 分组发现调用链（不变）"""

    name = "request_correlator"

    def correlate(self, time_window: str, **kwargs) -> List[Inference]:
        # 查询 → 分组 → 生成 Inference 包含 Evidence + Observations
        ...
```

### 6.6 ResourceCorrelator（type-agnostic）

```python
class ResourceCorrelator(CorrelationProvider):
    """
    按 ResourceIdentity 分组发现调用链。
    不硬编码任何资源类型 — 匹配任何在两个服务中出现的 (type, id) 对。
    
    新增 Kubernetes 支持：只需在 Semantic Engine 提取 POD/PVC 等资源，
    此 Provider 零改动。
    """

    name = "resource_correlator"

    # 通用证据权重（不区分资源类型，所有 UUID match 同等对待）
    # 可在配置中按 deployment 调整
    RESOURCE_MATCH_WEIGHT = 0.45

    def correlate(self, time_window: str, **kwargs) -> List[Inference]:
        """
        从 ClickHouse 查询 resource_type + resource_id 分组。
        resource_type 来自 event_resource + resources_json。
        使用 instance_uuid / volume_id 等索引列加速。
        """
        query = """
        SELECT
            event_resource,
            instance_uuid,
            volume_id,
            port_id,
            image_id,
            service_name,
            timestamp
        FROM logs.logs
        PREWHERE timestamp > now() - INTERVAL {time_window}
            AND (instance_uuid != '' OR volume_id != '' OR port_id != '' OR image_id != '')
        ORDER BY instance_uuid, volume_id, port_id, image_id, timestamp
        """
        rows = self.storage.execute_query(query)

        # 对每个 (type, id) 对，找出在不同服务中的出现
        # 出现 → 生成 Observation
        # 相同 (type, id) 在不同服务 → 生成 Evidence
        # 多个 Evidence 合并 → 生成 Inference
        ...
```

### 6.7 EvidenceMerger

```python
class EvidenceMerger:
    """
    多源证据融合：Observations → Facts → Evidence → Inferences → RelationshipResults。
    """

    EVIDENCE_BASE_WEIGHTS = {
        "request_match":  0.20,
        "resource_match": 0.45,   # 通用权重，不区分资源类型
        "time_window":    0.10,
        "message_target": 0.20,
        "host_match":     0.15,
        "ai_inferred":    0.10,
        "registry_relation": 0.30,  # Knowledge Layer 确认的关系
    }

    WEIGHT_DECAY_MINUTES = 120

    def merge(self, inferences: List[Inference], facts: Optional[List[Fact]] = None) -> Dict[Tuple[str, str], RelationshipResult]:
        """
        合并所有 Inference + Facts，输出 RelationshipResult。
        
        RelationshipResult 保留完整追溯链：
        RelationshipResult.inferences[i].evidence_list[j].observations[k]
          → Fact 层连接 Observation 和 Evidence
          → 可直接追溯到具体日志行
        
        算法:
            base = 0.3
            for inference in merged:
                for evidence in inference.evidence_list:
                    base += evidence.weight
            confidence = min(base, 0.98)
            时间衰减同前
        """
        ...
```

---

## 7. Knowledge Layer（Knowledge Platform）

### 7.1 定位

Knowledge Layer 不是 Topology 的附属，而是整个平台的 **共享事实层**。

```
Knowledge Layer
├── Resource Registry      # 资源实例 (Instance, Volume, Port, Host … 完整属性)
├── Relationship Registry  # 资源间关系 (Instance → Host, Port → Network …)
├── Metadata Registry      # 运行时元数据 (future: NUMA, CPU pinning, Hugepage, PCI)
└── Schema Registry        # 平台 schema (future: OpenStack API, Kubernetes CRD)
```

Phase 3 只实现 Resource Registry + Relationship Registry，其余预留目录结构。

```
                  ┌───────────────────────┐
                  │   Knowledge Layer      │
                  │  (Neo4j)              │
                  │                       │
                  │  Instance → Host      │
                  │  Port → Network       │
                  │  Volume → Instance    │
                  └───────────────────────┘
                        ↑     ↑     ↑
                        │     │     │
              ┌─────────┘     │     └──────────┐
              ▼               ▼                ▼
       Correlation       Topology            AI
       (资源关系佐证)    (基础设施拓扑)     (推理上下文)
```

**为什么独立：**
- Correlation Engine 用它来验证资源关联（"port abc 确实属于 instance xyz"）
- Topology Engine 用它来增强基础设施视图
- AI Service 用它作为推理上下文
- Rule Engine (future) 用它做依赖分析

### 7.2 数据模型（Neo4j）

```cypher
// 节点
(:Resource {type: "INSTANCE", id: "abc-123", name: "vm-1", host: "compute-01"})
(:Resource {type: "PORT",     id: "port-xyz", mac: "fa:16:3e:..."})
(:Resource {type: "NETWORK",  id: "net-456", name: "private"})
(:Resource {type: "VOLUME",   id: "vol-789", size: 50})
(:Resource {type: "HOST",     id: "compute-01", aggregate: "az-1"})

// 关系
(:INSTANCE)-[:ATTACHED_TO]->(:VOLUME)
(:INSTANCE)-[:HAS_PORT]->(:PORT)
(:PORT)-[:BELONGS_TO]->(:NETWORK)
(:INSTANCE)-[:RUNS_ON]->(:HOST)
(:HOST)-[:MEMBER_OF]->(:AGGREGATE)
```

### 7.3 同步策略（事件驱动为主）

```
主同步: OpenStack 通知（事件驱动）
    Nova/Neutron/Cinder Notification Bus
        ↓
    Notification Listener
        ↓
    Resolver (转换 notification 为 Cypher)
        ↓
    Neo4j MERGE

冷同步: API 定期轮询（降级方案）
    OpenStack API (list_servers, list_ports, list_volumes)
        ↓
    Paginated Sync
        ↓
    增量对比 (last_updated 时间戳)
        ↓
    Neo4j MERGE

优先级: 通知 > API。通知是最新的，API 用于补全遗漏。
```

```python
# knowledge-layer/registry/sync.py

class NotificationListener:
    """监听 OpenStack 通知总线，增量更新 Neo4j"""

    TOPIC_MAP = {
        "compute.instance.create.end":   ("INSTANCE", "CREATE"),
        "compute.instance.delete.end":   ("INSTANCE", "DELETE"),
        "network.port.create.end":       ("PORT", "CREATE"),
        "volume.attach.end":             ("VOLUME", "ATTACH"),
    }

    def handle_notification(self, notification: dict):
        event_type = notification.get("event_type")
        if resource_type := self.TOPIC_MAP.get(event_type):
            # 转换为 Neo4j MERGE 语句
            self.neo4j.execute(self._to_cypher(notification))

class ApiSync:
    """定时 API 同步（冷同步，兜底）"""
    SYNC_INTERVAL_MINUTES = 60

    def sync_all(self):
        # 逐页拉取 Nova servers, Neutron ports, Cinder volumes
        # 对比上次同步时间戳，增量更新
        ...
```

### 7.4 Knowledge Layer API

```text
# 查询一个资源的所有关联
GET /api/v1/knowledge/relationships?type=INSTANCE&id=abc-123
→ {
    "resource": {"type": "INSTANCE", "id": "abc-123"},
    "relationships": [
        {"relation": "HAS_PORT", "target": {"type": "PORT", "id": "port-xyz"}},
        {"relation": "RUNS_ON",  "target": {"type": "HOST", "id": "compute-01"}},
        {"relation": "ATTACHED_TO", "target": {"type": "VOLUME", "id": "vol-789"}},
    ]
}

# 路径查询（用于 AI 推理）
GET /api/v1/knowledge/path?source_type=PORT&source_id=port-xyz&target_type=INSTANCE
→ {
    "path": [
        {"type": "PORT", "id": "port-xyz"},
        {"relation": "device_id"},
        {"type": "INSTANCE", "id": "abc-123"},
    ]
}
```

---

## 8. Topology Engine

### 8.1 精简后的职责

**Topology Engine 只做一件事：画图。**

```
TopologyEngine (约 300-400 行)
├── 消费 CorrelationEngine.merge() 的 RelationshipResult[]
├── 可选查询 Knowledge Layer 增强节点属性
├── 构建 Nodes (去重、合并)
├── 构建 Edges (去重、聚合 confidence)
├── 布局计算 (分层 / 力导向)
└── 渲染 TopologyResult
```

不再负责：
- ❌ 置信度计算（已移至 Correlation Engine EvidenceMerger）
- ❌ 证据管理（已移至 Correlation Engine models）
- ❌ 资源关系（已移至 Knowledge Layer）
- ❌ 推理（已移至 AICorrelator）

### 8.2 实现

```python
class TopologyEngine:
    """
    拓扑引擎：纯构图。
    消费 RelationshipResult，构建 Node + Edge 结构。
    """

    def __init__(self, correlation_engine: CorrelationEngine,
                 knowledge_layer: Optional[KnowledgeLayer] = None):
        self.correlation_engine = correlation_engine
        self.knowledge_layer = knowledge_layer

    def build(self, time_window: str) -> TopologyResult:
        # 1. 获取关系（Correlation 不知道 Graph 概念）
        relationships = self.correlation_engine.correlate_all(time_window)

        # 2. 转换: Relationship → Edge（只转换格式，不重新计算）
        edges = self._to_edges(relationships)

        # 3. 构建节点
        nodes = self._build_nodes(edges)

        # 4. 可选：增强节点属性
        if self.knowledge_layer:
            self._enrich_nodes(nodes)

        return TopologyResult(nodes=nodes, edges=edges)

    def _to_edges(self, relationships: List[RelationshipResult]) -> List[Edge]:
        """Relationship → Edge 转换。纯格式转换，不计算置信度。"""
        return [
            Edge(
                source=r.source,
                target=r.target,
                confidence=r.confidence,
                call_count=r.call_count,
                data_sources=r.data_sources,
            )
            for r in relationships
        ]

    def _build_nodes(self, edges: List[Edge]) -> List[Node]:
        seen = set()
        nodes = []
        for edge in edges:
            for service in [edge.source, edge.target]:
                if service not in seen:
                    seen.add(service)
                    nodes.append(Node(id=service, name=service))
        return nodes
```

---

## 9. 置信度模型

### 9.1 详细计算公式

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

### 9.2 示例

| 场景 | resource | request | registry | time | score | 说明 |
|------|----------|---------|----------|------|-------|------|
| Nova→Neutron: device_id=instance_uuid | 0.45 | 0.35 | - | 0.10 | **0.90** | 通用资源匹配 |
| Registry 确认 Nova→Neutron 关系 | - | - | 0.30 | - | **0.30** | 已知但无运行时证据 |
| Nova→Cinder: volume_id 匹配 | 0.45 | 0.35 | - | - | **0.80** | 存储关联 |
| OVS→Neutron: Registry 知 port→network | - | - | 0.30 | 0.10 | **0.40** | 弱关联 |
| Nova→Compute: 全维度匹配 | 0.45 | 0.55 | 0.30 | 0.10 | **0.98** | 封顶 |

### 9.3 O→E→I→E 追溯示例

```json
{
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
          "observations": [
            {
              "log_id": "nova-api-log-001",
              "service_name": "nova-api",
              "observed_field": "instance_uuid",
              "observed_value": "abc-123"
            },
            {
              "log_id": "neutron-log-042",
              "service_name": "neutron-server",
              "observed_field": "device_id",
              "observed_value": "abc-123"
            }
          ]
        }
      ]
    }
  ]
}
```

---

## 10. API 变化

### 10.1 新端点

```text
# 拓扑查询（增强版）
GET /api/v1/topology/hybrid?time_window=1+HOUR
→ 现有格式保持兼容，新增 inferences 字段

# 证据详情（Correlation Engine 直接输出，输出 Relationship 而非 Edge）
GET /api/v1/evidence?source=Nova&target=Neutron&time_window=1+HOUR
→ {
    "relationships": [{
      "relationship_id": "rel-abc-123",
      "version": 1,
      "source": "Nova",
      "target": "Neutron",
      "confidence": 0.90,
      "inferences": [...],     # 完整 O→F→E→I→R 追溯链
      "call_count": 42
    }]
  }

# Topology Engine 消费后转为 Edge（纯转换，不重新计算）
GET /api/v1/topology/hybrid?time_window=1+HOUR
→ {
    "edges": [{
      "source": "Nova",
      "target": "Neutron",
      "confidence": 0.90,
      "relationship_id": "rel-abc-123",  # 关联回原始 Relationship
      "call_count": 42
    }]
  }

# 知识层查询
GET /api/v1/knowledge/relationships?type=INSTANCE&id=abc-123
GET /api/v1/knowledge/path?source_type=PORT&source_id=port-xyz&target_type=INSTANCE

# Correlation Engine 直接接口（AI Service 专用）
GET /api/v1/correlate/services?source=Nova&time_window=1+HOUR
```

### 10.2 现有端点兼容

`GET /api/v1/topology/hybrid` 保持现有返回格式不变，仅在 `metrics` 内新增字段。

---

## 11. 实施阶段

### Phase 1: Core（~2 周）

**目标：** NormalizedEvent 模型定稿 → Semantic Engine 提取 event+resources → ClickHouse 新增列 → Resource/Request Correlator → EvidenceMerger

| 模块 | 文件 | 改动 |
|------|------|------|
| **New** | `semantic-engine/normalize/operation.py` | 新建：extract_operation() → EventType |
| **New** | `semantic-engine/normalize/resource.py` | 新建：extract_resource_fields(), assign_resource_roles() |
| Modify | `semantic-engine/normalize/normalizer.py` | 集成新 extractors，输出 NormalizedEvent |
| Modify | `shared_src/logoscope_storage/adapter.py` | 新增列 DDL (event_resource, event_verb, resources_json, uuid 列) |
| **New** | `correlation-engine/__init__.py` | 新建包 |
| **New** | `correlation-engine/models.py` | Observation, Fact, Evidence, Inference, RelationshipResult |
| **New** | `correlation-engine/base.py` | CorrelationProvider 基类 |
| **New** | `correlation-engine/event_repository.py` | EventRepository 抽象接口 + ClickHouse 实现 |
| **New** | `correlation-engine/engine.py` | CorrelationEngine（provider 注册 + 调度） |
| **New** | `correlation-engine/merger.py` | EvidenceMerger（合并 Facts + Inferences → RelationshipResult） |
| **New** | `correlation-engine/providers/resource_correlator.py` | 通用 ResourceIdentity 匹配（type-agnostic） |
| **New** | `correlation-engine/providers/request_correlator.py` | 从 _get_openstack_topology 迁移 |
| Modify | `topology-service/graph/hybrid_topology.py` | 集成 CorrelationEngine，精简 |
| **New** | `topology-service/graph/topology_engine.py` | 薄层 TopologyEngine |
| Modify | `topology-service/api/topology_routes.py` | 新增 evidence/correlate 端点 |
| Tests | 4 个新测试文件 | resource/operation/correlator/merger |

### Phase 2: Alias + Infrastructure（~1 周）

| 模块 | 文件 | 改动 |
|------|------|------|
| Modify | `semantic-engine/normalize/resource.py` | 展开 _ALIAS_MAP, _INFRA_FIELDS |
| **New** | `correlation-engine/providers/host_correlator.py` | host + pid + thread |
| Modify | `shared_src/adapter.py` | infrastructure 列 |

### Phase 3: Knowledge Layer（~2 周）

| 模块 | 文件 | 改动 |
|------|------|------|
| **New** | `knowledge-layer/` | 新建：同步服务 |
| **New** | `knowledge-layer/registry/sync.py` | 通知监听 + API 轮询 |
| **New** | `knowledge-layer/registry/neo4j_client.py` | Neo4j 读写 |
| **New** | `knowledge-layer/registry/resolver.py` | 通知/API → Cypher |
| Modify | `topology_engine.py` | 可选集成 Knowledge Layer |
| Modify | `correlation-engine/merger.py` | 增加 registry_relation 证据权重 |

### Phase 4: AI + Kubernetes（~2 周）

| 模块 | 文件 | 改动 |
|------|------|------|
| **New** | `correlation-engine/providers/ai_correlator.py` | AI 推理 |
| Modify | `ai-service/` | 边推理接口 |
| Modify | `semantic-engine/normalize/resource.py` | 增加 POD, NODE, PVC 等 Kubernetes 资源提取 |

---

## 12. 测试策略

### 12.1 Semantic Engine 测试

```python
# tests/test_resource_extraction.py

def test_extract_instance_uuid():
    log = {"_raw_attributes": {"instance_uuid": "abc-123-def-456"}}
    result = extract_resource_fields(log)
    assert result["instance_uuid"] == "abc-123-def-456"

def test_device_id_alias():
    """device_id 归一为 instance_uuid"""
    log = {"_raw_attributes": {"device_id": "abc-123-def-456"}}
    result = extract_resource_fields(log)
    assert result["instance_uuid"] == "abc-123-def-456"

def test_assign_roles_attach_volume():
    """attach_volume 有 actor 和 target 角色"""
    event = EventType("INSTANCE", "ATTACH_VOLUME")
    fields = {"instance_uuid": "abc-123", "volume_id": "vol-456"}
    refs = assign_resource_roles(event, fields)
    assert len(refs) == 2
    assert any(r.type == "INSTANCE" and r.role == "actor" for r in refs)
    assert any(r.type == "VOLUME" and r.role == "target" for r in refs)

def test_normalized_event_includes_resources():
    """NormalizedEvent 包含完整资源列表"""
    log = {"_raw_attributes": {"event_type": "volume.attach.end",
                                "instance_uuid": "abc-123",
                                "volume_id": "vol-456"}}
    event = normalize_log(log)
    assert event.event.resource == "INSTANCE"
    assert event.event.verb == "ATTACH_VOLUME"
    assert len(event.resources) == 2
    assert event.instance_uuid == "abc-123"
```

### 12.2 Correlation Engine 测试

```python
def test_resource_correlator_any_type():
    """通用资源关联：不区分资源类型，匹配任何 (type, id) 对"""
    storage = FakeStorage(rows=[
        {"event_resource": "INSTANCE", "instance_uuid": "abc",
         "service_name": "A", "timestamp": "2026-01-01T00:00:00"},
        {"event_resource": "INSTANCE", "instance_uuid": "abc",
         "service_name": "B", "timestamp": "2026-01-01T00:00:01"},
    ])
    correlator = ResourceCorrelator(storage)
    results = correlator.correlate("1 HOUR")
    assert len(results) == 1
    assert results[0].source == "A"
    assert results[0].target == "B"

def test_merger_preserves_observation_chain():
    """合并后保留 O→F→E→I→R 追溯链"""
    obs_a = Observation("log-1", "A", datetime(2026, 1, 1),
                        "instance_uuid", "abc-123")
    obs_b = Observation("log-2", "B", datetime(2026, 1, 1),
                        "instance_uuid", "abc-123")
    fact = Fact("alias_resolution", "device_id", "abc-123", "instance_uuid", "abc-123",
                source="alias_map")
    evidence = Evidence("A", "B", "resource_match", "INSTANCE:abc", 0.45,
                        observations=[obs_a, obs_b])
    inference = Inference("A", "B", [evidence], "calls")

    merger = EvidenceMerger()
    result = merger.merge([inference], facts=[fact])
    assert ("A", "B") in result
    assert len(result[("A", "B")].inferences) == 1
    assert result[("A", "B")].inferences[0].evidence_list[0].observations[0].log_id == "log-1"
    assert result[("A", "B")].edge_id != ""  # 不可变 ID
    assert result[("A", "B")].version >= 1
```

### 12.3 Knowledge Layer 测试

```python
def test_registry_integrates_with_correlation():
    """Topology Engine 转换 Relationship → Edge"""
    engine = TopologyEngine(
        FakeCorrelationEngine([RelationshipResult("rel-1", source="A", target="B", confidence=0.90)]),
        FakeKnowledgeLayer({"A": [("PORT", "p-1")]}),
    )
    result = engine.build("1 HOUR")
    assert result.edges[0].confidence == 0.90  # 置信度来自 Correlation，Topology 只转换
    assert result.edges[0].relationship_id == "rel-1"  # 关联回原始 Relationship
```

---

## 13. 性能考量

| 场景 | 当前 | 优化后 | 改善 |
|------|------|--------|------|
| 单次拓扑查询（1h 窗口） | ~800ms | ~500ms | Bloom filter + 独立列 |
| Resource 关联查询（无 MV） | ❌ | ~300ms | Bloom filter 索引 |
| Resource 关联查询（有 MV） | ❌ | ~50ms | 预聚合 resource_edges_mv |
| Knowledge Layer 查询 | ❌ | ~5ms | Neo4j 索引 |
| Notification → Neo4j 延迟 | ❌ | <1s | 事件驱动 |

---

## 14. 向后兼容

| 影响点 | 兼容策略 |
|--------|----------|
| ClickHouse 存量数据 | ALTER TABLE ADD COLUMN ... DEFAULT '' |
| 现有 API /hybrid | 保持输出格式不变，仅新增 inferences 字段 |
| 现有测试 | 不改现有测试，新增模块测试 |
| 前端 | 新增字段不影响现有渲染逻辑 |
| Topology Service | Correlation Engine 可独立部署，通过 API 调用 |
