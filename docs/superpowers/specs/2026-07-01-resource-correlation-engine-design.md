# Resource Correlation Engine Design

> **将 Logoscope 的拓扑从单一 request_id 关联升级为多级资源关联模型（Resource-based Correlation Engine），解决 OpenStack 场景下调用链断裂和覆盖率不足的问题。**
> 同时引入 Relationship Registry（资源关系注册表），将日志关联与已知资源关系分离，为 AI 推理和 Kubernetes 扩展奠定基础。

**Status:** Draft v2  
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

### 1.3 从「日志关联」到「资源关系」的跨越

仅靠日志字段关联是不够的。例如 OVS Agent 日志中出现 `port abc-123`，要推断它属于 `VM xyz-789`，需要知道 Neutron 层面的资源关系：

```
Port abc-123  →  device_id = instance xyz-789  →  Instance xyz-789  →  host compute-01
```

这种关系不来自日志，而来自 **OpenStack API**（Neutron list_ports、Nova list_servers）。引入 **Relationship Registry** 将这些已知资源关系存入 Neo4j，使拓扑引擎能构建完整的 infrastructure 拓扑，而不仅限于服务间调用链。

---

## 2. Architecture

### 2.1 整体架构

```text
Data Pipeline
═════════════════════════════════════════════════════════════════════════════

[Fluent Bit] → [OTel Collector]
         │
         ▼
  ┌────────────────────────────────────────┐
  │          Semantic Engine                │
  │                                        │
  │  ┌────────────────────────────────┐   │
  │  │ Normalize Pipeline             │   │
  │  │                                │   │
  │  │ ① extract_request_fields()    │   │  request_id, trace_id, span_id
  │  │ ② extract_operation()         │   │  CREATE_VM, ATTACH_VOLUME, ...
  │  │ ③ extract_resource_fields()   │   │  instance_uuid, volume_id, ...
  │  │ ④ resolve_aliases()           │   │  device_id → instance_uuid
  │  │ ⑤ resolve_primary_resource()  │   │  按 operation 决定主资源
  │  │ ⑥ build_normalized_event()    │   │  输出统一 Event 模型
  │  └────────────────────────────────┘   │
  │         │                              │
  │         ▼ NormalizedEvent              │
  │  ┌────────────────────────────────┐   │
  │  │  NormalizedEvent {            │   │
  │  │    timestamp, service, host,   │   │
  │  │    severity, operation,        │   │
  │  │    resource (type + id),       │   │
  │  │    request (ids),              │   │
  │  │    message, attributes         │   │
  │  │  }                              │   │
  │  └────────────────────────────────┘   │
  └────────────────────────────────────────┘
         │
         ▼ NormalizedEvent
  ┌────────────────────────────────────────┐
  │             ClickHouse                  │
  │                                        │
  │  logs.logs (flat + indexed)            │
  │   ├─ operation, resource_type/id       │
  │   ├─ primary_resource_type/id          │
  │   ├─ instance_uuid / volume_id / ...   │
  │   └─ attributes_json (full blob)       │
  └────────────────────────────────────────┘
         │
         ▼
  ┌─────────────────────────────────────────────┐
  │          Correlation Engine                   │
  │  (独立模块，可被 Topology / AI 直接调用)       │
  │                                             │
  │  ┌─────────────────────────────────────┐   │
  │  │ Providers (pluggable)              │   │
  │  │  • RequestCorrelator               │   │  global_request_id / request_id
  │  │  • ResourceCorrelator              │   │  resource_type + resource_id
  │  │  • TimeCorrelator                  │   │  host + time_window
  │  │  • HostCorrelator                  │   │  host + pid + thread (P2)
  │  │  • AICorrelator                    │   │  AI 推理 (P3)
  │  │  • KubernetesPodCorrelator         │   │  future
  │  └─────────────────────────────────────┘   │
  │         │                                    │
  │         ▼ CandidateEdge[]                    │
  │  ┌─────────────────────────────────────┐   │
  │  │ EvidenceMerger                      │   │
  │  │  → 合并多源证据                      │   │
  │  │  → 加权置信度计算                    │   │
  │  │  → 时间衰减                         │   │
  │  │  → 输出完整证据链                    │   │
  │  └─────────────────────────────────────┘   │
  └─────────────────────────────────────────────┘
         │
         ▼ EdgeResult[] (含完整 evidence)
  ┌──────────────────────────────────────────────┐
  │         Topology Engine                       │
  │                                              │
  │  Build Nodes → Build Edges → Score Confidence│
  │  → Render Graph (合并来自 Correlation Engine │
  │     + Relationship Registry 的信息)            │
  └──────────────────────────────────────────────┘
         │
         ▼
  ┌──────────────────────────────────────────────┐
  │           Relationship Registry               │
  │  (Neo4j: 已知的资源关系，来自 OpenStack API)   │
  │                                              │
  │  Instance ──→ Port ──→ Network               │
  │  Instance ──→ Host                           │
  │  Volume   ──→ Attachment ──→ Instance        │
  └──────────────────────────────────────────────┘
         │
         ▼
  API / Frontend / AI Service
```

### 2.2 数据流

```
Raw Log
   ↓ Semantic Engine
Normalized Event (含 operation, resource, request, ...)
   ↓ ClickHouse
Structured rows with indexed resource columns
   ↓ Correlation Engine (time_window 窗口)
CandidateEdges[] with evidence
   ↓ EvidenceMerger
EdgeResult[] with confidence + evidence chain
   ↓ Topology Engine
Graph (nodes + edges) + Relationship Registry 增强
   ↓
Neo4j / API / Frontend / AI
```

### 2.3 组件职责

| 层 | 职责 | 不做什么 |
|----|------|----------|
| **Semantic Engine** | 日志理解、标准化、输出 Normalized Event | 不计算拓扑、不做关联 |
| **ClickHouse** | 存储、索引、预聚合 | 不做业务逻辑 |
| **Correlation Engine** | 多源证据融合、候选边生成、完整证据链输出 | 不画图、不存储、不关心资源关系 |
| **Relationship Registry** | 已知资源关系存储（Neo4j），API/API 定期拉取 | 不分析日志、不计算置信度 |
| **Topology Engine** | 构图、节点合并、边解释、AI 推理集成 | 不关心数据来源的具体协议 |
| **AI Service** | 推理补全不可证明的边 | 不覆盖已有证据的边 |
| **Rule Engine (future)** | 基于 Normalized Event 的规则匹配告警 | 不画图、不关联 |

### 2.4 为什么 Correlation Engine 要独立

Topology Service 负责「画图」，而 Correlation Engine 负责「找关系」：

- **AI Service** 需要直接问 "Nova 和 Neutron 有关联吗？有什么证据？" — 不需要经过 Topology
- **Rule Engine (future)** 需要知道 "这个服务依赖了哪些服务？" — 不需要拓扑图
- **Kubernetes 扩展时** 只需要加 `PodCorrelator`，Topology Engine 零改动
- **性能隔离**：Topology 请求高峰不影响实时的关联计算

---

## 3. Normalized Event 模型

### 3.1 为什么需要统一 Event 模型

目前 Semantic Engine 只关注资源字段的提取，没有统一的输出结构。随着平台演进（AI、Rule Engine、Topology），所有下游都需要消费一个结构化的 Event，而不是各自从 raw log 中重新解析。

### 3.2 Event Schema

```python
@dataclass
class NormalizedEvent:
    """Semantic Engine 输出的统一 Event 模型"""
    
    # --- 时间 & 标识 ---
    timestamp: datetime
    service_name: str           # nova-api, nova-compute, neutron-server
    pod_name: str               # Kubernetes pod name
    namespace: str              # Kubernetes namespace
    host: str                   # 宿主机名
    source_cluster: str         # 集群标识
    
    # --- 日志元信息 ---
    severity: str               # INFO, WARN, ERROR, CRITICAL
    message: str                # 原始日志文本
    pid: int                    # 进程 ID (可选)
    thread: str                 # 线程 ID (可选)
    
    # --- 请求标识 ---
    trace_id: str               # OpenTelemetry trace_id
    span_id: str                # OpenTelemetry span_id
    request_id: str             # oslo.log request_id
    global_request_id: str      # oslo.log global_request_id
    
    # --- 操作 & 资源 ---
    operation: str              # CREATE_VM, ATTACH_VOLUME, ...
    
    # 泛化资源键（为 Kubernetes / 其他平台预留）
    resource_type: str          # INSTANCE, VOLUME, PORT, IMAGE, POD, NODE, PVC
    resource_id: str            # 对应 UUID
    
    # 主资源（按 operation 决定哪个资源是"主角"）
    primary_resource_type: str
    primary_resource_id: str
    
    # --- 具体资源字段（带 ClickHouse 索引，高性能查询） ---
    # Phase 1 Core Resources
    instance_uuid: str
    volume_id: str
    port_id: str
    image_id: str
    
    # Phase 2 Alias Resources（归一后存入对应字段）
    # device_id → instance_uuid, consumer_uuid → instance_uuid, ...
    
    # Phase 3 Infrastructure
    aggregate: str              # 主机聚合
    
    # --- 完整保留 ---
    attributes_json: str        # 原始 raw_attributes 完整 JSON
    labels_json: str            # Kubernetes labels JSON
```

### 3.3 Event 消费关系

```
NormalizedEvent
    │
    ├── ClickHouse      → 所有 flat 字段（索引、查询）
    ├── Correlation     → resource, request, host 字段
    ├── Topology        → service_name, resource, operation
    ├── AI Service      → message, attributes, resource, operation
    └── Rule Engine     → severity, operation, service_name, message
```

---

## 4. Semantic Engine 改造

### 4.1 新增 extract_operation()

从日志中识别标准化的操作类型。

```python
# semantic-engine/normalize/operation.py

_OPERATION_PATTERNS = {
    # (event_type_prefix, action_verb) → normalized operation
    ("compute.instance.create", None): "CREATE_VM",
    ("compute.instance.delete", None): "DELETE_VM",
    ("compute.instance.rebuild", None): "REBUILD_VM",
    ("volume.attach", None):           "ATTACH_VOLUME",
    ("volume.detach", None):           "DETACH_VOLUME",
    ("volume.create", None):           "CREATE_VOLUME",
    ("volume.delete", None):           "DELETE_VOLUME",
    ("port.create", None):             "CREATE_PORT",
    ("port.delete", None):             "DELETE_PORT",
    ("image.create", None):            "CREATE_IMAGE",
    ("image.delete", None):            "DELETE_IMAGE",
    ("compute.instance.live_migration", None): "LIVE_MIGRATION",
    ("compute.instance.resize", None): "RESIZE",
    ("compute.instance.reboot", None): "REBOOT",
    ("compute.instance.power_off", None): "POWER_OFF",
    ("compute.instance.power_on", None):  "POWER_ON",

    # Phase 2: message text fallback
    (None, "attach"):                  "ATTACH_VOLUME",
    (None, "detach"):                  "DETACH_VOLUME",
    (None, "spawn"):                   "SPAWN_INSTANCE",
    (None, "create_server"):           "CREATE_VM",
}

def extract_operation(log_data: Dict[str, Any]) -> str:
    """从日志中提取标准化操作名。优先级: event_type > action > message text"""
    event_type = _candidate_text(
        log_data.get("event_type") or 
        (log_data.get("event") or {}).get("type") or
        (log_data.get("_raw_attributes") or {}).get("event_type")
    )
    action = _candidate_text(
        log_data.get("action") or
        (log_data.get("_raw_attributes") or {}).get("action")
    )

    # Phase 1: 精确匹配 event_type 前缀
    for (ev_prefix, act_verb), operation in _OPERATION_PATTERNS.items():
        if ev_prefix and event_type.startswith(ev_prefix):
            return operation

    # Phase 1: 匹配 action 字段
    for (ev_prefix, act_verb), operation in _OPERATION_PATTERNS.items():
        if act_verb and action and act_verb in action.lower():
            return operation

    return "UNKNOWN"
```

### 4.2 新增 extract_resource_fields()

从 `_raw_attributes` 和 message 中提取所有已知的资源 UUID。

```python
# semantic-engine/normalize/resource.py

# Phase 1 Core Resources（直接提取，带 ClickHouse 索引）
_RESOURCE_FIELD_MAP = {
    "instance_uuid": ["instance_uuid", "instance_id", "server_id", "uuid"],
    "volume_id":     ["volume_id", "volumeId"],
    "port_id":       ["port_id", "portId"],
    "image_id":      ["image_id", "imageId", "image_uuid"],
}

# Phase 2 Alias Map（归一到 Core Resource，在 Semantic Engine 完成）
_ALIAS_MAP = {
    "device_id":     "instance_uuid",    # Neutron port.device_id
    "consumer_uuid": "instance_uuid",    # Placement consumer
    "domain_uuid":   "instance_uuid",    # Libvirt domain
    "qemu_uuid":     "instance_uuid",    # QEMU
    "snapshot_id":   "volume_id",
    "attachment_id": "volume_id",
}

# Phase 3 Infrastructure
_INFRA_FIELDS = ["host", "node", "hypervisor_hostname", "aggregate",
                 "availability_zone", "cell", "rack"]


def extract_resource_fields(log_data: Dict[str, Any]) -> Dict[str, str]:
    """提取资源 UUID 字段，包含 alias 归一化"""
    message = _candidate_text(log_data.get("message"))
    raw_attrs = log_data.get("_raw_attributes", {})
    if not isinstance(raw_attrs, dict):
        raw_attrs = {}

    resources = {}
    
    # Phase 1: 从结构化字段提取 Core Resources
    for target_key, source_keys in _RESOURCE_FIELD_MAP.items():
        for source_key in source_keys:
            value = _find_value(raw_attrs, message, source_key)
            if value:
                resources[target_key] = value
                break

    # Phase 2: 从别名提取并归一（只在 Core 未找到时尝试 alias）
    for alias_key, target_key in _ALIAS_MAP.items():
        if target_key not in resources:
            value = _find_value(raw_attrs, message, alias_key)
            if value:
                resources[target_key] = value

    return resources


def _find_value(raw_attrs: Dict, message: str, key: str) -> str:
    """从 attributes 或 message 中查找字段值"""
    value = raw_attrs.get(key)
    if value and isinstance(value, str) and _is_uuid(value):
        return value.lower()
    if "_" in key:
        dotted = key.replace("_", ".")
        value = raw_attrs.get(dotted)
        if value and isinstance(value, str) and _is_uuid(value):
            return value.lower()
    for suffix in ("_uuid", "_id"):
        if key.endswith(suffix):
            base = key[:-len(suffix)]
            value = raw_attrs.get(base)
            if value and isinstance(value, str) and _is_uuid(value):
                return value.lower()
    # Phase 2: 从 message text 正则提取
    if message:
        patterns = [
            rf'{key}\s*[=:]\s*([a-f0-9-]{{36}})',
            rf'\b{key.replace("_", " ")}(?:\s+is)?\s+([a-f0-9-]{{36}})',
        ]
        for pattern in patterns:
            match = re.search(pattern, message, re.IGNORECASE)
            if match and _is_uuid(match.group(1)):
                return match.group(1).lower()
    return ""


def _is_uuid(value: str) -> bool:
    return bool(re.match(
        r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
        value.strip().lower()
    ))
```

### 4.3 新增 resolve_primary_resource()

基于 operation 和资源列表，决定哪个资源是"主资源"。

```python
_PRIMARY_RESOURCE_RULES = {
    "CREATE_VM":          "INSTANCE",
    "DELETE_VM":          "INSTANCE",
    "SPAWN_INSTANCE":     "INSTANCE",
    "ATTACH_VOLUME":      "INSTANCE",
    "DETACH_VOLUME":      "INSTANCE",
    "CREATE_VOLUME":      "VOLUME",
    "DELETE_VOLUME":      "VOLUME",
    "ATTACH_INTERFACE":   "INSTANCE",
    "DETACH_INTERFACE":   "INSTANCE",
    "CREATE_PORT":        "PORT",
    "DELETE_PORT":        "PORT",
    "CREATE_IMAGE":       "IMAGE",
    "DELETE_IMAGE":       "IMAGE",
    "LIVE_MIGRATION":     "INSTANCE",
    "RESIZE":             "INSTANCE",
    "REBOOT":             "INSTANCE",
}

_RESOURCE_TYPE_MAP = {
    "instance_uuid": "INSTANCE",
    "volume_id":     "VOLUME",
    "port_id":       "PORT",
    "image_id":      "IMAGE",
}


def resolve_primary_resource(
    operation: str,
    resources: Dict[str, str],
) -> Dict[str, str]:
    """根据 operation 决定主资源类型和 ID"""
    if not resources:
        return {"primary_resource_type": "", "primary_resource_id": ""}

    preferred_type = _PRIMARY_RESOURCE_RULES.get(operation)
    if preferred_type:
        for field_name, res_type in _RESOURCE_TYPE_MAP.items():
            if res_type == preferred_type and resources.get(field_name):
                return {"primary_resource_type": res_type,
                        "primary_resource_id": resources[field_name]}

    priority = ["instance_uuid", "volume_id", "port_id", "image_id"]
    for field_name in priority:
        if resources.get(field_name):
            res_type = _RESOURCE_TYPE_MAP.get(field_name, "UNKNOWN")
            return {"primary_resource_type": res_type,
                    "primary_resource_id": resources[field_name]}

    return {"primary_resource_type": "", "primary_resource_id": ""}
```

### 4.4 normalize_log() 集成

输出统一 NormalizedEvent：

```python
def normalize_log(log_data: Dict[str, Any]) -> NormalizedEvent:
    # ... existing request_id / trace_id extraction ...

    operation = extract_operation(log_data)
    resource_fields = extract_resource_fields(log_data)
    primary = resolve_primary_resource(operation, resource_fields)

    # 构建泛化 resource_type / resource_id
    resource_type = primary["primary_resource_type"] if primary["primary_resource_type"] else ""
    resource_id = primary["primary_resource_id"] if primary["primary_resource_id"] else ""

    return NormalizedEvent(
        timestamp=...,
        service_name=...,
        host=...,
        severity=...,
        message=...,
        trace_id=...,
        span_id=...,
        request_id=...,
        global_request_id=...,
        operation=operation,
        resource_type=resource_type,
        resource_id=resource_id,
        primary_resource_type=primary["primary_resource_type"],
        primary_resource_id=primary["primary_resource_id"],
        instance_uuid=resource_fields.get("instance_uuid", ""),
        volume_id=resource_fields.get("volume_id", ""),
        port_id=resource_fields.get("port_id", ""),
        image_id=resource_fields.get("image_id", ""),
        attributes_json=json.dumps(raw_attributes),
        # ... other fields ...
    )
```

---

## 5. ClickHouse Schema

### 5.1 ALTER TABLE（增量迁移，幂等）

```sql
-- Phase 1 Core
ALTER TABLE logs.logs ADD COLUMN IF NOT EXISTS operation String DEFAULT '';
ALTER TABLE logs.logs ADD COLUMN IF NOT EXISTS resource_type String DEFAULT '';
ALTER TABLE logs.logs ADD COLUMN IF NOT EXISTS resource_id String DEFAULT '';
ALTER TABLE logs.logs ADD COLUMN IF NOT EXISTS primary_resource_type String DEFAULT '';
ALTER TABLE logs.logs ADD COLUMN IF NOT EXISTS primary_resource_id String DEFAULT '';
ALTER TABLE logs.logs ADD COLUMN IF NOT EXISTS instance_uuid String DEFAULT '';
ALTER TABLE logs.logs ADD COLUMN IF NOT EXISTS volume_id String DEFAULT '';
ALTER TABLE logs.logs ADD COLUMN IF NOT EXISTS port_id String DEFAULT '';
ALTER TABLE logs.logs ADD COLUMN IF NOT EXISTS image_id String DEFAULT '';

-- Phase 3 Infrastructure
ALTER TABLE logs.logs ADD COLUMN IF NOT EXISTS aggregate String DEFAULT '';

-- Indexes（Bloom filter 适合高基数 UUID 字段）
ALTER TABLE logs.logs ADD INDEX IF NOT EXISTS idx_instance_uuid (instance_uuid) TYPE bloom_filter(0.025) GRANULARITY 4;
ALTER TABLE logs.logs ADD INDEX IF NOT EXISTS idx_volume_id (volume_id) TYPE bloom_filter(0.025) GRANULARITY 4;
ALTER TABLE logs.logs ADD INDEX IF NOT EXISTS idx_port_id (port_id) TYPE bloom_filter(0.025) GRANULARITY 4;
ALTER TABLE logs.logs ADD INDEX IF NOT EXISTS idx_image_id (image_id) TYPE bloom_filter(0.025) GRANULARITY 4;
ALTER TABLE logs.logs ADD INDEX IF NOT EXISTS idx_primary_resource_id (primary_resource_id) TYPE bloom_filter(0.025) GRANULARITY 4;
ALTER TABLE logs.logs ADD INDEX IF NOT EXISTS idx_resource_type (resource_type) TYPE set(32) GRANULARITY 4;
```

### 5.2 Materialized View

```sql
-- 预聚合：按 resource 分组的服务序列
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
WHERE resource_id != ''
ORDER BY resource_type, resource_id, timestamp;
```

---

## 6. Correlation Engine

### 6.1 架构

独立模块，位于 `correlation-engine/`（不在 `topology-service/correlation/` 内），可被 Topology Service 和 AI Service 直接调用：

```
correlation-engine/
├── __init__.py
├── engine.py              # CorrelationEngine 入口
├── base.py                # CorrelationProvider 抽象基类
├── models.py              # Evidence, CandidateEdge, EdgeResult
├── merger.py              # EvidenceMerger 多源证据融合
│
├── providers/
│   ├── __init__.py
│   ├── request_correlator.py     # global_request_id / request_id
│   ├── resource_correlator.py    # resource_type + resource_id
│   ├── time_correlator.py        # host + time_window
│   ├── host_correlator.py        # host + pid + thread (Phase 2)
│   └── ai_correlator.py          # AI 推理 (Phase 3)
│
└── tests/
    ├── test_engine.py
    ├── test_request_correlator.py
    └── test_resource_correlator.py
```

### 6.2 数据模型

```python
# correlation-engine/models.py

@dataclass
class Evidence:
    """
    一条边的单条证据。
    保存完整证据链，而非仅置信度分数。
    后续 AI、前端、审计可解释：为什么这条边存在？
    """
    source: str               # 证据来源: "request_id" / "resource_id" / "time_window" / ...
    provider: str             # 发现者: RequestCorrelator / ResourceCorrelator / ...
    value: str                # 具体值: "req-abc-123" / "INSTANCE:abc-123"
    weight: float             # 0.0 ~ 1.0
    detail: str = ""          # 可读描述: "instance_uuid abc-123 found in both services"

@dataclass
class CandidateEdge:
    """来自一个 provider 的候选边"""
    source: str               # service_name
    target: str               # service_name
    call_count: int
    evidence_list: List[Evidence]
    first_seen: datetime
    last_seen: datetime

@dataclass
class EdgeResult:
    """最终边（合并后的结果）"""
    source: str
    target: str
    confidence: float
    call_count: int
    evidence: List[Evidence]          # 完整证据链（可审计、可解释）
    data_sources: List[str]           # ["openstack", "inferred", "resource"]
```

### 6.3 CorrelationProvider 接口

```python
# correlation-engine/base.py

class CorrelationProvider(ABC):
    """
    关联提供者：从一种维度发现候选边。
    新增 provider = 新增文件（不修改 Topology Engine）
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """provider 名称，用于定位"""
        ...

    @abstractmethod
    def correlate(self, time_window: str, **kwargs) -> List[CandidateEdge]:
        """
        在指定时间窗口内发现候选边。
        
        Args:
            time_window: ClickHouse 兼容的时间窗口表达式 "1 HOUR"
            **kwargs: provider 特定参数
            
        Returns:
            候选边列表，每个包含其发现的证据链。
        """
        ...
```

### 6.4 RequestCorrelator

```python
class RequestCorrelator(CorrelationProvider):
    """按 global_request_id / request_id 分组发现调用链"""

    name = "request_correlator"

    def correlate(self, time_window: str, **kwargs) -> List[CandidateEdge]:
        # 查询已有 openstack_global_request_id 列
        query = """
        SELECT
            openstack_global_request_id,
            openstack_request_id,
            service_name,
            timestamp
        FROM logs.logs
        PREWHERE timestamp > now() - INTERVAL {time_window}
            AND openstack_global_request_id != ''
        ORDER BY openstack_global_request_id, timestamp
        """
        rows = self.storage.execute_query(query)
        # 分组 → 同一 global_request_id 的不同 service pair → 生成 CandidateEdge
        # 每条边附带 Evidence(source="request_id", provider="request_correlator",
        #                     value=global_request_id, weight=0.6)
        ...
```

### 6.5 ResourceCorrelator

```python
class ResourceCorrelator(CorrelationProvider):
    """按 resource_type + resource_id 分组发现调用链（核心新增）"""

    name = "resource_correlator"

    RESOURCE_EVIDENCE_WEIGHT = {
        "INSTANCE": 0.50,
        "VOLUME":   0.45,
        "PORT":     0.45,
        "IMAGE":    0.40,
    }

    def correlate(self, time_window: str, **kwargs) -> List[CandidateEdge]:
        # 从 ClickHouse 独立列查询（利用 bloom filter 索引）
        query = """
        SELECT
            primary_resource_type,
            primary_resource_id,
            service_name,
            timestamp
        FROM logs.logs
        PREWHERE timestamp > now() - INTERVAL {time_window}
            AND primary_resource_id != ''
        ORDER BY primary_resource_type, primary_resource_id, timestamp
        """
        rows = self.storage.execute_query(query)
        # 分组 → 相同 (primary_resource_type, primary_resource_id) 的不同 service pair
        # → 生成 CandidateEdge
        # 每条边附带 Evidence(source="resource_id", provider="resource_correlator",
        #                     value="INSTANCE:abc-123", weight=0.50,
        #                     detail="instance_uuid=abc-123 found in both Nova and Neutron")
        ...
```

### 6.6 EvidenceMerger

```python
class EvidenceMerger:
    """
    多源证据融合：将多个 provider 发现的候选边按照 (source, target)
    合并，计算综合置信度。输出完整证据链（非仅最终分数）。
    """

    EVIDENCE_BASE_WEIGHTS = {
        "request_id":    0.20,   # RequestCorrelator
        "resource_id":   0.50,   # ResourceCorrelator
        "time_window":   0.10,   # TimeCorrelator
        "message_target":0.20,   # 现有推断机制
        "host_match":    0.15,   # HostCorrelator (Phase 2)
        "ai_inferred":   0.10,   # AICorrelator (Phase 3)
    }

    WEIGHT_DECAY_MINUTES = 120

    def merge(self, candidates: List[CandidateEdge]) -> Dict[Tuple[str, str], EdgeResult]:
        """
        合并所有候选边的证据，输出最终边。
        
        算法:
            base = 0.3                           # 基础置信度
            for evidence on merged edge:
                base += evidence.weight          # 每项证据加权重
            confidence = min(base, 0.98)          # 封顶
            
            时间衰减:
                minutes_since = (now - last_seen).total_seconds() / 60
                if minutes_since > WEIGHT_DECAY_MINUTES:
                    decay = 0.5 ** (minutes_since / WEIGHT_DECAY_MINUTES)
                    confidence *= decay

        输出: EdgeResult 包含完整 evidence 列表（可审计、可解释）
        """
        ...
```

---

## 7. Relationship Registry

### 7.1 为什么需要

仅靠日志关联无法回答 "OVS Agent 日志中的 port abc 属于哪个 VM"——因为 port→VM 的关系存在于 Neutron 数据库，不在日志里。

**Relationship Registry** 填补这个空白：

```
Without Registry:
  OVS Agent 日志 [port abc-123 port_update] ──→ Neutron
  ↑ 只能通过 time_window 弱关联

With Registry:
  port abc-123 ──(device_id)──→ instance xyz-789 ──(host)──→ compute-01
  ↑ 明确已知关系，置信度 1.0
```

### 7.2 数据来源

| 来源 | 数据 | 更新策略 |
|------|------|----------|
| Neutron API | port → network, port → device_id | 定时轮询 + 事件通知 |
| Nova API | server → host, server → hypervisor | 定时轮询 |
| Cinder API | volume → attachment → instance | 定时轮询 |
| Glance API | image → server (通过 instance image_ref) | 定时轮询 |
| Placement API | resource_provider → aggregate | 定时轮询 |

### 7.3 数据库（Neo4j）

```cypher
// 节点类型
(:Resource {type: "INSTANCE", id: "abc-123", name: "vm-1", host: "compute-01"})
(:Resource {type: "PORT", id: "port-xyz", mac: "fa:16:3e:..."})
(:Resource {type: "NETWORK", id: "net-456", name: "private"})
(:Resource {type: "VOLUME", id: "vol-789", size: 50})
(:Resource {type: "HOST", id: "compute-01", aggregate: "az-1"})

// 关系
(:INSTANCE)-[:ATTACHED_TO]->(:VOLUME)
(:INSTANCE)-[:HAS_PORT]->(:PORT)
(:PORT)-[:BELONGS_TO]->(:NETWORK)
(:INSTANCE)-[:RUNS_ON]->(:HOST)
(:HOST)-[:MEMBER_OF]->(:AGGREGATE)
```

### 7.4 集成方式

```
OpenStack API (Neutron/Nova/Cinder)
    │ 定时轮询
    ▼
Relationship Registry Sync Service
    │
    ├── OpenStackClient  → 调用 API 获取资源关系
    ├── Resolver         → 转换 API 响应为 Neo4j 语句
    └── Neo4jClient      → 写入 Neo4j (MERGE + CREATE)
    
Topology Engine 查询:
    MATCH (i:INSTANCE {id: $instance_id})-[*1..2]->(n)
    RETURN n
    
    → 返回 instance 关联的所有资源（port, network, volume, host）
```

### 7.5 拓扑增强

Topology Engine 在构图时：

1. **从 Correlation Engine 获取服务级边**（Nova→Neutron，置信度 0.95）
2. **从 Relationship Registry 获取资源级关系**（Port→Network，确定性 1.0）
3. **合并：** 服务边 + 资源关系 → 完整的 infrastructure 拓扑

```
Topology 结果:
  Nova API ──0.95──→ Neutron Server
     │                   │
     │ resource          │ port
     ▼                   ▼
  Instance abc     Port xyz ──1.0──→ Network net-456
     │
     │ host
     ▼
  Host compute-01
```

这样拓扑不再只是服务间的调用链图，而是完整的 **基础设施依赖图**。

---

## 8. Topology Engine

### 8.1 职责范围

```
Topology Engine（精简后，约 500 行）
├── 调用 CorrelationEngine.correlate_all() 获取边证据
├── 查询 RelationshipRegistry 获取资源关系
├── 合并证据 → 计算置信度 → 构建节点/边
└── 返回可解释的 TopologyResult
```

### 8.2 实现

```python
class TopologyEngine:
    """
    拓扑引擎：消费 Correlation Engine 的边证据 + Relationship Registry 的资源关系，
    构建完整拓扑。
    """

    def __init__(self, correlation_engine: CorrelationEngine, registry: RelationshipRegistry):
        self.correlation_engine = correlation_engine
        self.registry = registry

    def build(self, time_window: str) -> TopologyResult:
        # 1. 从 Correlation Engine 获取服务级边（含证据链）
        evidence_map = self.correlation_engine.correlate_all(time_window)
        
        # 2. 从 Relationship Registry 获取资源关系
        resource_relations = self.registry.get_relations_for_edges(evidence_map)
        
        # 3. 合并边 + 资源关系 → 完整拓扑
        edges = self._merge_edges(evidence_map, resource_relations)
        nodes = self._build_nodes(edges, resource_relations)
        
        return TopologyResult(nodes=nodes, edges=edges)

    def _merge_edges(self, evidence_map, resource_relations) -> List[EdgeResult]:
        """
        合并策略:
        - 服务级边（来自 Correlation Engine）保留原置信度
        - 资源关系（来自 Registry）赋予置信度 1.0（确定性已知关系）
        - 相同 (source, target) 合并，取高置信度
        """
        ...
```

---

## 9. 置信度模型

### 9.1 详细计算公式

```
For each (source, target) pair:

  base = 0.0                        # 起始基础值

  resource_evidence:
    if has instance_uuid:   base += 0.50
    if has volume_id:       base += 0.45
    if has port_id:         base += 0.45
    if has image_id:        base += 0.40

  request_evidence:
    if has global_request_id: base += 0.35
    if has request_id:        base += 0.20
  
  time_evidence:
    if time_window < 0.5s:  base += 0.10
    if time_window < 0.1s:  base += 0.15 （仅当 host 相同）

  message_evidence:
    if has target_match:    base += 0.20
    if has inbound+outbound: base += 0.15

  relationship_evidence:              # ← 新增：Relationship Registry 证据
    if registry has direct relation:  base += 0.30 （确定性已知关系）

  max_confidence = min(base, 0.98)

  time_decay:
    minutes_since_last_seen = (now - last_seen) / 60
    if minutes_since_last_seen > 120:
      decay = 0.5 ^ (minutes_since_last_seen / 120)
    else:
      decay = 1.0

  final_confidence = max_confidence * decay
```

### 9.2 示例

| 场景 | resource | request | registry | time | score | 说明 |
|------|----------|---------|----------|------|-------|------|
| Nova→Neutron: device_id=instance_uuid | 0.50 | 0.35 | - | 0.10 | **0.95** | 最强日志证据 |
| Registry 已知 Nova→Neutron（Neutron API 直接确认） | - | - | 0.30 | - | **0.30** | 低但确定（Neutron API 确认过关系） |
| Nova→Cinder: volume_id 匹配 | 0.45 | 0.35 | - | - | **0.80** | 只有存储关联 |
| OVS→Neutron: Registry 知道 port→network | - | - | 0.30 | 0.10 | **0.40** | 无日志关联但 Registry 确认 |
| Nova→Libvirt: 只有同 host 时间窗 | - | - | - | 0.10 | **0.10** | 弱关联，加 AI 改善 |

### 9.3 如何保存 Evidence

Evidence 走 API 输出，不单独落库：

```json
{
  "source": "nova-api",
  "target": "neutron-server",
  "confidence": 0.95,
  "evidence": [
    {"source": "resource_id", "provider": "ResourceCorrelator",
     "value": "INSTANCE:abc-123", "weight": 0.50,
     "detail": "instance_uuid=abc-123 found in nova-api (t1) and neutron-server (t2)"},
    {"source": "request_id", "provider": "RequestCorrelator",
     "value": "req-abc-123", "weight": 0.35,
     "detail": "global_request_id=req-abc-123 spans both services"}
  ]
}
```

---

## 10. API 变化

### 10.1 新端点

```text
# 拓扑查询（增强版）
GET /api/v1/topology/hybrid?time_window=1+HOUR
→ 现有格式保持兼容，新增 evidence 数组

# Evidence 详情（AI 可调用）
GET /api/v1/evidence?source=Nova&target=Neutron&time_window=1+HOUR
→ {
    "edges": [{
      "source": "Nova",
      "target": "Neutron",
      "confidence": 0.95,
      "evidence": [ ... ],
      "call_count": 42
    }]
  }

# 资源关系查询（Relationship Registry）
GET /api/v1/relationships?resource_type=INSTANCE&resource_id=abc-123
→ {
    "resource": {"type": "INSTANCE", "id": "abc-123"},
    "relationships": [
      {"type": "HAS_PORT", "target": {"type": "PORT", "id": "port-xyz"}},
      {"type": "RUNS_ON",  "target": {"type": "HOST", "id": "compute-01"}},
      {"type": "ATTACHED_TO", "target": {"type": "VOLUME", "id": "vol-789"}}
    ]
  }

# Correlation Engine 直接接口（AI Service 专用）
GET /api/v1/correlate/services?source=Nova&time_window=1+HOUR
→ {
    "correlations": [
      {"target": "Neutron", "confidence": 0.95, "evidence": [...]},
      {"target": "Cinder",  "confidence": 0.80, "evidence": [...]}
    ]
  }
```

### 10.2 现有端点兼容

`GET /api/v1/topology/hybrid` 保持现有返回格式不变，仅在 `metrics` 内新增字段：

```json
{
  "edges": [{
    "source": "Nova",
    "target": "Neutron",
    "confidence": 0.95,
    "metrics": {
      "data_source": "openstack",
      "evidence": [
        {"source": "resource_id", "value": "INSTANCE:abc-123"},
        {"source": "global_request_id", "value": "req-abc-123"}
      ],
      "evidence_type": "observed"
    }
  }]
}
```

---

## 11. 实施阶段

### Phase 1: Core（~2 周）

**目标：** Normalized Event 模型定稿 → Semantic Engine 提取 operation + resource → ClickHouse 新增列 → Resource/Request Correlator 产出边 → EvidenceMerger 融合

| 模块 | 文件 | 改动 |
|------|------|------|
| **New** | `semantic-engine/normalize/operation.py` | 新建：extract_operation() |
| **New** | `semantic-engine/normalize/resource.py` | 新建：extract_resource_fields(), resolve_aliases(), resolve_primary_resource() |
| Modify | `semantic-engine/normalize/normalizer.py` | 集成新 extractors，输出 NormalizedEvent |
| Modify | `shared_src/logoscope_storage/adapter.py` | 新增列 DDL + INSERT |
| **New** | `correlation-engine/__init__.py` | 新建包 |
| **New** | `correlation-engine/models.py` | 新建：Evidence, CandidateEdge, EdgeResult |
| **New** | `correlation-engine/base.py` | 新建：CorrelationProvider 基类 |
| **New** | `correlation-engine/engine.py` | 新建：CorrelationEngine（provider 注册 + 调度） |
| **New** | `correlation-engine/merger.py` | 新建：EvidenceMerger |
| **New** | `correlation-engine/providers/resource_correlator.py` | 新建：ResourceCorrelator |
| **New** | `correlation-engine/providers/request_correlator.py` | 新建：从 _get_openstack_topology 迁移 |
| Modify | `topology-service/graph/hybrid_topology.py` | 集成 CorrelationEngine，精简代码 |
| **New** | `topology-service/graph/topology_engine.py` | 新建：TopologyEngine |
| Modify | `topology-service/api/topology_routes.py` | 新增 evidence 端点 |
| **New** | `topology-service/tests/test_resource_correlator.py` | 新建 |
| **New** | `topology-service/tests/test_evidence_merger.py` | 新建 |
| **New** | `semantic-engine/tests/test_resource_extraction.py` | 新建 |
| **New** | `semantic-engine/tests/test_operation_extraction.py` | 新建 |

### Phase 2: Alias + Infrastructure（~1 周）

**目标：** Alias 覆盖所有常见字段、Infrastructure 字段、HostCorrelator

| 模块 | 文件 | 改动 |
|------|------|------|
| Modify | `semantic-engine/normalize/resource.py` | 展开 _ALIAS_MAP 和 _INFRA_FIELDS |
| **New** | `correlation-engine/providers/host_correlator.py` | 新建：按 host + pid + thread 关联 |
| Modify | `shared_src/adapter.py` | 新增 infrastructure 列 DDL |

### Phase 3: Relationship Registry + 优化（~2 周）

**目标：** OpenStack API 定时同步到 Neo4j → 拓扑增强 → 物化视图 → 增量构建 → 缓存

| 模块 | 文件 | 改动 |
|------|------|------|
| **New** | `relationship-registry/` | 新建：OpenStack 定时同步服务 |
| **New** | `relationship-registry/sync.py` | 定时从 Neutron/Nova/Cinder API 拉取关系 |
| **New** | `relationship-registry/neo4j_client.py` | Neo4j 读写 |
| **New** | `relationship-registry/resolver.py` | API 响应 → Cypher 语句 |
| Modify | `topology-engine.py` | 集成 RelationshipRegistry |
| Modify | `shared_src/adapter.py` | resource_edges_mv 物化视图 |
| Modify | `topology-engine.py` | 增量构建 + Redis 缓存 |

### Phase 4: AI + Kubernetes（~2 周）

**目标：** AI 推理补全不可证明的边、Kubernetes Pod/Node/Service 关联

| 模块 | 文件 | 改动 |
|------|------|------|
| **New** | `correlation-engine/providers/ai_correlator.py` | 新建：AI 推理 |
| Modify | `ai-service/` | 边推理接口 |
| **New** | `correlation-engine/providers/k8s_pod_correlator.py` | 新建：Kubernetes Pod 关联 |
| Modify | `relationship-registry/sync.py` | 增加 Kubernetes API 同步 |

---

## 12. 测试策略

### 12.1 Semantic Engine 测试

```python
# tests/test_resource_extraction.py

def test_extract_instance_uuid_from_attributes():
    log = {"_raw_attributes": {"instance_uuid": "abc-123-def-456"}}
    result = extract_resource_fields(log)
    assert result["instance_uuid"] == "abc-123-def-456"

def test_device_id_alias_to_instance_uuid():
    """Phase 2: device_id 归一为 instance_uuid（在 Semantic Engine 完成）"""
    log = {"_raw_attributes": {"device_id": "abc-123-def-456"}}
    result = extract_resource_fields(log)
    assert result["instance_uuid"] == "abc-123-def-456"
    assert "device_id" not in result  # alias 不保留原始 key

def test_primary_resource_attach_volume():
    log = {"_raw_attributes": {"event_type": "volume.attach.end"}}
    operation = extract_operation(log)
    resources = {"instance_uuid": "abc-123", "volume_id": "vol-456"}
    primary = resolve_primary_resource(operation, resources)
    assert primary["primary_resource_type"] == "INSTANCE"
    assert primary["primary_resource_id"] == "abc-123"

def test_normalized_event_includes_operation():
    """NormalizedEvent 输出包含操作和资源字段"""
    log = {"_raw_attributes": {"event_type": "volume.attach.end",
                                "instance_uuid": "abc-123"}}
    event = normalize_log(log)
    assert event.operation == "ATTACH_VOLUME"
    assert event.primary_resource_type == "INSTANCE"
    assert event.instance_uuid == "abc-123"
```

### 12.2 Correlation Engine 测试

```python
def test_resource_correlator_creates_edges():
    """相同 INSTANCE:abc 的不同服务生成边"""
    storage = FakeStorage(rows=[
        {"primary_resource_type": "INSTANCE", "primary_resource_id": "abc",
         "service_name": "nova-api", "timestamp": "2026-01-01T00:00:00"},
        {"primary_resource_type": "INSTANCE", "primary_resource_id": "abc",
         "service_name": "nova-compute", "timestamp": "2026-01-01T00:00:01"},
    ])
    correlator = ResourceCorrelator(storage)
    edges = correlator.correlate("1 HOUR")
    assert len(edges) == 1
    assert edges[0].source == "nova-api"
    assert edges[0].target == "nova-compute"

def test_merger_combines_evidence():
    """同一条边来自 resource + request 两个来源"""
    edges = [
        CandidateEdge("A", "B", 1, [
            Evidence("resource_id", "rc", "INSTANCE:abc", 0.50)]),
        CandidateEdge("A", "B", 1, [
            Evidence("request_id", "rqc", "req-abc", 0.35)]),
    ]
    merger = EvidenceMerger()
    result = merger.merge(edges)
    assert ("A", "B") in result
    assert result[("A", "B")].confidence == pytest.approx(0.98, rel=1e-2)
    # 合并后的置信度为 0.3 + 0.50 + 0.35 = 1.15，封顶 0.98

def test_evidence_chain_preserved():
    """合并后保留完整证据链（非仅置信度）"""
    edges = [
        CandidateEdge("A", "B", 1, [
            Evidence("resource_id", "rc", "INSTANCE:abc", 0.50,
                     detail="found in both A and B")]),
    ]
    merger = EvidenceMerger()
    result = merger.merge(edges)
    assert len(result[("A", "B")].evidence) == 1
    assert result[("A", "B")].evidence[0].detail == "found in both A and B"
```

### 12.3 Relationship Registry 测试

```python
def test_registry_returns_port_to_instance_relation():
    """port abc's device_id is instance xyz"""
    registry = FakeRegistry()
    registry.seed("PORT", "port-abc", "device_id", "INSTANCE", "xyz-789")
    relations = registry.get_relations("PORT", "port-abc")
    assert ("INSTANCE", "xyz-789") in relations

def test_topology_integrates_registry_edges():
    """TopologyEngine 合并 Correlation + Registry 两种边"""
    engine = TopologyEngine(
        FakeCorrelationEngine([CandidateEdge("A", "B", ...)]),
        FakeRegistry(has_relation=True),
    )
    result = engine.build("1 HOUR")
    # Registry 的关系增加了额外的证据
    assert result.edges[0].confidence > 0.95
```

---

## 13. 性能考量

| 场景 | 当前 | 优化后 | 改善 |
|------|------|--------|------|
| 单次拓扑查询（1h 窗口） | ~800ms | ~600ms | Bloom filter + 独立列减少 message 扫描 |
| Resource 关联查询（无 MV） | ❌ | ~400ms | Bloom filter 索引 (idx_instance_uuid 等) |
| Resource 关联查询（有 MV） | ❌ | ~50ms | 预聚合 resource_edges_mv |
| 边增量构建 | ❌ | ~100ms | 只处理新数据 |
| 边缓存命中 | ❌ | ~2ms | Redis TTL=60s |
| Relationship Registry 查询 | ❌ | ~5ms | Neo4j 索引 |
| 多 provider 并发 | ❌ | N/A | 各 provider 独立查询，EvidenceMerger 合并 |

---

## 14. 向后兼容

| 影响点 | 兼容策略 |
|--------|----------|
| ClickHouse 存量数据 | ALTER TABLE ADD COLUMN ... DEFAULT ''，已有数据不补填 |
| 现有 API /hybrid | 保持输出格式不变，仅新增 evidence 字段 |
| 现有测试 | 不改现有测试，新增 correlation engine + registry 测试 |
| 前端 | 新增 evidence 字段不影响现有渲染逻辑 |
| 配置 | 新功能默认开启（`ENABLE_RESOURCE_CORRELATION=true`） |
| Topology Service | Correlation Engine 独立部署不影响旧拓扑路径 |
