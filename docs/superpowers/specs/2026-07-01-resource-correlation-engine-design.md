# Resource Correlation Engine Design

> **将 Logoscope 的拓扑从单一 request_id 关联升级为多级资源关联模型（Resource-based Correlation Engine），解决 OpenStack 场景下调用链断裂和覆盖率不足的问题。**

**Status:** Draft  
**Date:** 2026-07-01  
**Authors:** croner01, Claude  

---

## 1. Problem

### 1.1 当前拓扑的局限性

Logoscope 现有拓扑计算有 6 种边类型，但核心问题在于它们各自独立、互不感知：

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

这些 UUID 在请求跨组件传递时**保持不变**，即使 request_id 被 RPC 重新生成或丢失。单一 resource UUID 就能覆盖整条调用链。

---

## 2. Design

### 2.1 整体架构

```text
Data Pipeline
═══════════════════════════════════════════════════════════

[Fluent Bit] → [OTel Collector]
         │
         ▼
  ┌─────────────────────────────────────┐
  │         Semantic Engine              │
  │                                     │
  │  ┌──────────────────────────────┐   │
  │  │ Normalize Pipeline           │   │
  │  │                              │   │
  │  │ ① extract_request_fields()  │   │  request_id, trace_id, span_id
  │  │ ② extract_operation()       │   │  CREATE_VM, ATTACH_VOLUME, ...
  │  │ ③ extract_resource_fields()  │   │  instance_uuid, volume_id, ...
  │  │ ④ resolve_aliases()          │   │  device_id → INSTANCE:abc-123
  │  │ ⑤ resolve_primary_resource() │   │  按 operation 决定主资源
  │  └──────────────────────────────┘   │
  └─────────────────────────────────────┘
         │
         ▼
  ┌─────────────────────────────────────┐
  │           ClickHouse                │
  │                                     │
  │  logs.logs                          │
  │   ├─ request_id / global_request_id │  ← 已有
  │   ├─ resource_type / resource_id    │  ← 新增 (泛化资源键)
  │   ├─ primary_resource_type / id     │  ← 新增 (主资源)
  │   ├─ operation                      │  ← 新增 (标准化操作名)
  │   ├─ instance_uuid / volume_id / .. │  ← 新增 (原始资源，带索引)
  │   └─ attributes_json                │  ← 已有 (完整 raw_attributes)
  └─────────────────────────────────────┘
         │
         ▼
  ┌─────────────────────────────────────┐
  │        Correlation Engine           │  ← 从 Topology Service 拆出
  │                                     │
  │  ┌──────────────────────────────┐   │
  │  │ ① RequestCorrelator         │   │  global_request_id / request_id 分组
  │  │ ② ResourceCorrelator        │   │  resource_type + resource_id 分组
  │  │ ③ TimeCorrelator            │   │  host + time_window 关联
  │  │ ④ HostCorrelator            │   │  host + pid + thread (Phase 2)
  │  │ ⑤ AICorrelator              │   │  AI 推理补全 (Phase 3)
  │  └──────────────────────────────┘   │
  │         │                            │
  │         ▼  CandidateEdge[]           │
  │  EvidenceMerger                     │
  │   → 合并证据 → 加权置信度           │
  └─────────────────────────────────────┘
         │
         ▼
  ┌─────────────────────────────────────┐
  │         Topology Engine             │
  │                                     │
  │  Build Nodes → Build Edges          │
  │  → Score Confidence → Explain       │
  └─────────────────────────────────────┘
         │
         ▼
  Neo4j / API / Frontend / AI Service
```

### 2.2 职责边界

| 层 | 职责 | 不做什么 |
|----|------|----------|
| **Semantic Engine** | 日志理解、标准化、提取 | 不计算拓扑、不做关联 |
| **ClickHouse** | 存储、索引、预聚合 | 不做业务逻辑 |
| **Correlation Engine** | 多源证据融合、候选边生成 | 不画图、不存储 |
| **Topology Engine** | 构图、置信度计算、边解释 | 不关心数据来源的具体协议 |
| **AI Service** | 推理补全不可证明的边 | 不覆盖已有证据的边 |

---

## 3. Semantic Engine 改造

### 3.1 新增 extract_operation()

从日志中识别标准化的操作类型。Phase 1 只从结构化字段提取，Phase 2 扩展到 message text。

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
    (None, "attach"):                  "ATTACH_VOLUME",       # Phase 2: message text fallback
    (None, "detach"):                  "DETACH_VOLUME",
    (None, "spawn"):                   "SPAWN_INSTANCE",
    (None, "create_server"):           "CREATE_VM",           # Phase 2
}

def extract_operation(log_data: Dict[str, Any]) -> str:
    """
    从日志中提取标准化操作名。
    优先级: event_type > action > message text
    """
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

### 3.2 新增 extract_resource_fields()

从 `_raw_attributes` 和 message 中提取所有已知的资源 UUID。

```python
# semantic-engine/normalize/resource.py

# Phase 1 Core Resources（直接提取）
_RESOURCE_FIELD_MAP = {
    "instance_uuid": ["instance_uuid", "instance_id", "server_id", "uuid"],
    "volume_id":     ["volume_id", "volumeId"],
    "port_id":       ["port_id", "portId"],
    "image_id":      ["image_id", "imageId", "image_uuid"],
}

# Phase 2 Alias Map（通过 alias 关系归一到 Core Resource）
_ALIAS_MAP = {
    # Neutron port.device_id → instance_uuid（attr 中不叫 instance_uuid 但与它相同）
    "device_id":     "instance_uuid",
    # Placement consumer_uuid → instance_uuid
    "consumer_uuid": "instance_uuid",
    # Libvirt domain uuid → instance_uuid
    "domain_uuid":   "instance_uuid",
    # QEMU uuid → instance_uuid
    "qemu_uuid":     "instance_uuid",
    # Cinder snapshot → volume_id
    "snapshot_id":   "volume_id",
    "attachment_id": "volume_id",
}

# Phase 3 Infrastructure
_INFRA_FIELDS = ["host", "node", "hypervisor_hostname", "aggregate",
                 "availability_zone", "cell", "rack"]


def extract_resource_fields(log_data: Dict[str, Any]) -> Dict[str, str]:
    """
    从日志中提取资源 UUID 字段。
    
    Returns:
        {
            "instance_uuid": "abc-...",
            "volume_id": "vol-...",
            "port_id": "...",
            ...
        }
    """
    message = _candidate_text(log_data.get("message"))
    raw_attrs = log_data.get("_raw_attributes", {})
    context = log_data.get("context", {})
    if not isinstance(raw_attrs, dict):
        raw_attrs = {}

    resources = {}
    
    # 从结构化字段提取（Phase 1 Core）
    for target_key, source_keys in _RESOURCE_FIELD_MAP.items():
        for source_key in source_keys:
            value = _find_value(raw_attrs, message, source_key)
            if value:
                resources[target_key] = value
                break

    # 从别名提取并归一（Phase 2）
    for alias_key, target_key in _ALIAS_MAP.items():
        if target_key not in resources:  # 只在 Core 未找到时尝试 alias
            value = _find_value(raw_attrs, message, alias_key)
            if value:
                resources[target_key] = value

    return resources


def _find_value(raw_attrs: Dict, message: str, key: str) -> str:
    """从 attributes 或 message 中查找字段值"""
    # 优先从结构化 attributes 提取
    value = raw_attrs.get(key)
    if value and isinstance(value, str) and _is_uuid(value):
        return value.lower()
    # 尝试点式嵌套 key（如 "instance.uuid" → key.replace("_", ".")）
    if "_" in key:
        dotted = key.replace("_", ".")
        value = raw_attrs.get(dotted)
        if value and isinstance(value, str) and _is_uuid(value):
            return value.lower()
    # 尝试不带后缀的 base name（如 "instance_uuid" → "instance"）
    for suffix in ("_uuid", "_id"):
        if key.endswith(suffix):
            base = key[: -len(suffix)]
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
    """检查是否为标准 UUID 格式"""
    return bool(re.match(
        r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
        value.strip().lower()
    ))
```

### 3.3 新增 resolve_primary_resource()

基于 operation 和提取到的资源列表，决定哪个资源是"主资源"。

```python
# semantic-engine/normalize/resource.py

_PRIMARY_RESOURCE_RULES = {
    # operation → primary_resource_type
    "CREATE_VM":          "INSTANCE",
    "DELETE_VM":          "INSTANCE",
    "REBUILD_VM":         "INSTANCE",
    "SPAWN_INSTANCE":     "INSTANCE",
    "ATTACH_VOLUME":      "INSTANCE",      # 虽然涉及 volume，但对 VM 的操作
    "DETACH_VOLUME":      "INSTANCE",
    "CREATE_VOLUME":      "VOLUME",
    "DELETE_VOLUME":      "VOLUME",
    "ATTACH_INTERFACE":   "INSTANCE",
    "DETACH_INTERFACE":   "INSTANCE",
    "CREATE_PORT":        "PORT",
    "DELETE_PORT":        "PORT",
    "CREATE_IMAGE":       "IMAGE",
    "DELETE_IMAGE":       "IMAGE",
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
    """
    根据 operation 决定主资源类型和 ID。
    
    Returns:
        {
            "primary_resource_type": "INSTANCE",
            "primary_resource_id": "abc-...",
        }
    """
    if not resources:
        return {"primary_resource_type": "", "primary_resource_id": ""}

    # 按 operation 规则
    preferred_type = _PRIMARY_RESOURCE_RULES.get(operation)
    if preferred_type:
        for field_name, res_type in _RESOURCE_TYPE_MAP.items():
            if res_type == preferred_type and resources.get(field_name):
                return {
                    "primary_resource_type": res_type,
                    "primary_resource_id": resources[field_name],
                }

    # fallback: 按优先级取第一个存在的资源
    priority = ["instance_uuid", "volume_id", "port_id", "image_id"]
    for field_name in priority:
        if resources.get(field_name):
            res_type = _RESOURCE_TYPE_MAP.get(field_name, "UNKNOWN")
            return {
                "primary_resource_type": res_type,
                "primary_resource_id": resources[field_name],
            }

    return {"primary_resource_type": "", "primary_resource_id": ""}
```

### 3.4 normalize_log() 集成

在 `normalize.py` 的 `normalize_log()` 中增加：

```python
def normalize_log(log_data: Dict[str, Any]) -> Dict[str, Any]:
    # ... existing code ...

    # ⭐ 新增: 提取操作
    operation = extract_operation(log_data)

    # ⭐ 新增: 提取资源 UUID
    resource_fields = extract_resource_fields(log_data)

    # ⭐ 新增: 决定主资源
    primary = resolve_primary_resource(operation, resource_fields)

    normalized = {
        # ... existing fields ...
        "openstack_request_id": openstack_ids.get(...),
        "openstack_global_request_id": openstack_ids.get(...),
        
        # ⭐ 新增字段
        "operation": operation,
        "resource_type": "",                                          # 泛化：多个资源时留空
        "resource_id": "",                                            # 泛化：由 primary 字段决定
        "primary_resource_type": primary["primary_resource_type"],
        "primary_resource_id": primary["primary_resource_id"],
        "instance_uuid": resource_fields.get("instance_uuid", ""),
        "volume_id": resource_fields.get("volume_id", ""),
        "port_id": resource_fields.get("port_id", ""),
        "image_id": resource_fields.get("image_id", ""),
        
        "_raw_attributes": raw_attributes,
    }

    return normalized
```

---

## 4. ClickHouse Schema

### 4.1 ALTER TABLE（增量迁移，幂等）

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

-- Phase 2 Alias
-- Phase 2 不新增列，全部归一到 Core 列

-- Phase 3 Infrastructure
ALTER TABLE logs.logs ADD COLUMN IF NOT EXISTS aggregate String DEFAULT '';

-- Indexes（Bloom filter 适合高基数 UUID 字段）
ALTER TABLE logs.logs ADD INDEX IF NOT EXISTS idx_instance_uuid (instance_uuid) TYPE bloom_filter(0.025) GRANULARITY 4;
ALTER TABLE logs.logs ADD INDEX IF NOT EXISTS idx_volume_id (volume_id) TYPE bloom_filter(0.025) GRANULARITY 4;
ALTER TABLE logs.logs ADD INDEX IF NOT EXISTS idx_port_id (port_id) TYPE bloom_filter(0.025) GRANULARITY 4;
ALTER TABLE logs.logs ADD INDEX IF NOT EXISTS idx_image_id (image_id) TYPE bloom_filter(0.025) GRANULARITY 4;
ALTER TABLE logs.logs ADD INDEX IF NOT EXISTS idx_primary_resource_id (primary_resource_id) TYPE bloom_filter(0.025) GRANULARITY 4;
```

### 4.2 Materialized View（可选优化）

```sql
-- 预聚合：按 resource 分组的服务序列
CREATE MATERIALIZED VIEW IF NOT EXISTS logs.resource_edges_mv
ENGINE = AggregatingMergeTree
PARTITION BY toDate(timestamp)
ORDER BY (resource_type, resource_id, timestamp)
AS SELECT
    multiIf(
        instance_uuid != '', 'INSTANCE',
        volume_id != '', 'VOLUME',
        port_id != '', 'PORT',
        image_id != '', 'IMAGE',
        ''
    ) AS resource_type,
    multiIf(
        instance_uuid != '', instance_uuid,
        volume_id != '', volume_id,
        port_id != '', port_id,
        image_id != '', image_id,
        ''
    ) AS resource_id,
    service_name,
    timestamp
FROM logs.logs
WHERE instance_uuid != '' OR volume_id != '' OR port_id != '' OR image_id != ''
ORDER BY resource_type, resource_id, timestamp;
```

---

## 5. Correlation Engine

### 5.1 架构

```
Correlation Engine (新模块: topology-service/correlation/)
├── engine.py              # CorrelationEngine 入口
├── base.py                # CorrelationProvider 抽象基类
├── evidence.py            # Evidence, CandidateEdge 数据模型
├── merger.py              # EvidenceMerger 多源证据融合
│
├── providers/
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

### 5.2 数据模型

```python
# correlation/evidence.py

@dataclass
class Evidence:
    """一条边的一条证据"""
    source: str           # "request_id" / "resource_id" / "time_window" / ...
    provider: str         # CorrelationProvider 名称
    value: str            # 具体值，如 "req-abc-123" / "INSTANCE:abc-123"
    weight: float         # 0.0 ~ 1.0，该证据对置信度的贡献权重

@dataclass
class CandidateEdge:
    """潜在边"""
    source: str           # service_name
    target: str
    call_count: int
    evidence: List[Evidence]
    first_seen: datetime
    last_seen: datetime

@dataclass
class EdgeResult:
    """最终边"""
    source: str
    target: str
    confidence: float
    call_count: int
    evidence: List[Evidence]       # 完整证据链（给 AI 和前端解释用）
    evidence_sources: List[str]    # ["resource_id", "request_id", ...]
    evidence_types: List[str]      # ["observed", "inferred", ...]
    data_sources: List[str]        # ["openstack", "inferred", ...]
```

### 5.3 CorrelationProvider 接口

```python
# correlation/base.py

class CorrelationProvider(ABC):
    """关联提供者：从一种维度发现候选边"""

    @property
    @abstractmethod
    def name(self) -> str:
        """provider 名称"""
        ...

    @abstractmethod
    def correlate(self, time_window: str, **kwargs) -> List[CandidateEdge]:
        """
        在指定时间窗口内发现候选边。
        
        Returns:
            候选边列表，每个包含其发现的证据。
        """
        ...
```

### 5.4 RequestCorrelator

```python
# correlation/providers/request_correlator.py

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
        PREWHERE
            timestamp > now() - INTERVAL {time_window}
            AND openstack_global_request_id != ''
        ORDER BY openstack_global_request_id, timestamp
        LIMIT {limit}
        """

        rows = self.storage.execute_query(query)
        # 分组 → 去重 → 生成 CandidateEdge
        # 每条边赋予 Evidence(source="request_id", provider="request_correlator",
        #                       value=global_request_id, weight=0.6)
        ...
```

### 5.5 ResourceCorrelator

```python
# correlation/providers/resource_correlator.py

class ResourceCorrelator(CorrelationProvider):
    """按 resource_type + resource_id 分组发现调用链"""

    name = "resource_correlator"

    # 每种资源类型的证据权重
    RESOURCE_EVIDENCE_WEIGHT = {
        "INSTANCE": 0.50,      # instance_uuid 是 OpenStack 最稳定的资源标识
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
            timestamp,
            instance_uuid,
            volume_id,
            port_id,
            image_id
        FROM logs.logs
        PREWHERE
            timestamp > now() - INTERVAL {time_window}
            AND primary_resource_id != ''
        ORDER BY primary_resource_type, primary_resource_id, timestamp
        LIMIT {limit}
        """

        rows = self.storage.execute_query(query)
        # 分组 → 去重 → 生成 CandidateEdge
        # 每条边赋予 Evidence(source="resource_id", provider="resource_correlator",
        #                       value="INSTANCE:abc-123", weight=0.50)
        ...
```

### 5.6 EvidenceMerger

```python
# correlation/merger.py

class EvidenceMerger:
    """
    多源证据融合：将多个 provider 发现的候选边按照 (source, target)
    合并，计算综合置信度。
    """

    # 各证据来源的基础权重（加到置信度上）
    EVIDENCE_WEIGHTS = {
        "trace_id":      0.50,   # Traces 由 TopologyEngine 单独处理
        "request_id":    0.20,   # 由 RequestCorrelator 提供
        "resource_id":   0.50,   # 由 ResourceCorrelator 提供
        "time_window":   0.10,   # 由 TimeCorrelator 提供（降噪后）
        "message_target":0.20,   # 由现有推断机制提供
        "host_match":    0.15,   # 由 HostCorrelator 提供（Phase 2）
        "ai_inferred":   0.10,   # 由 AICorrelator 提供（Phase 3，标注为 inferred）
    }

    # 衰减：超过此时间（分钟）的证据权重乘 0.5
    WEIGHT_DECAY_MINUTES = 120

    def merge(self, candidates: List[CandidateEdge]) -> Dict[Tuple[str, str], EdgeResult]:
        """
        合并所有候选边的证据，输出最终边。
        
        算法:
            base_confidence = 0.3  # 基础置信度
            for each evidence on the edge:
                base_confidence += evidence.weight  # 每项证据加权重
            confidence = min(base_confidence, 0.98)  # 封顶（不超过 traces 的 1.0）
            
            时间衰减:
                minutes_since_last_seen = (now - last_seen).total_seconds() / 60
                if minutes_since_last_seen > WEIGHT_DECAY_MINUTES:
                    decay = 0.5 ** (minutes_since_last_seen / WEIGHT_DECAY_MINUTES)
                    confidence *= decay
        """
        ...
```

---

## 6. Topology Engine 改造

现有 `hybrid_topology.py` ~2200+ 行，拆分后变为：

```python
# topology-service/graph/
# ├── hybrid_topology.py          ← 删减：仅保留公共入口
# ├── topology_engine.py          ← 新增：构图核心
# └── correlation/
#     ├── engine.py               ← 新增：关联引擎
#     ├── evidence.py             ← 新增：数据模型
#     ├── merger.py               ← 新增：证据融合
#     └── providers/...           ← 新增：各关联维度
```

TopologyEngine 职责：

```python
class TopologyEngine:
    """
    拓扑引擎：接收 CorrelationEngine 产出的证据，
    构建节点、生成边、计算置信度、提供可解释性。
    """

    def __init__(self, correlation_engine: CorrelationEngine):
        self.correlation_engine = correlation_engine

    def build(self, time_window: str) -> TopologyResult:
        # 1. 调用 CorrelationEngine 获取证据
        evidence_map = self.correlation_engine.correlate_all(time_window)
        
        # 2. 构建节点（去重、合并）
        nodes = self._build_nodes(evidence_map)
        
        # 3. 构建边（置信度计算、数据源标注）
        edges = self._build_edges(evidence_map)
        
        # 4. 返回可解释的拓扑
        return TopologyResult(nodes=nodes, edges=edges)

    def _build_edges(self, evidence_map) -> List[EdgeResult]:
        ...
```

---

## 7. 置信度模型

### 7.1 详细计算公式

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
    if time_window < 0.1s:  base += 0.15  (仅当 host 相同)

  message_evidence:
    if has target_match:    base += 0.20
    if has inbound+outbound: base += 0.15

  max_confidence = min(base, 0.98)

  time_decay:
    minutes_since_last_seen = (now - last_seen) / 60
    if minutes_since_last_seen > 120:
      decay = 0.5 ^ (minutes_since_last_seen / 120)
    else:
      decay = 1.0

  final_confidence = max_confidence * decay
```

### 7.2 示例

| 场景 | resource | request | time | score | 说明 |
|------|----------|---------|------|-------|------|
| Nova→Neutron: device_id matches instance_uuid | 0.50 | 0.35 | 0.10 | **0.95** | 最强证据链 |
| Nova→Cinder: volume_id 匹配 | 0.45 | 0.35 | - | **0.80** | 缺少时间窗 |
| Nova→Libvirt: 只有同 host 时间窗 | - | - | 0.10 | **0.10** | 弱关联，加 AI 改善 |
| Nova→Compute: 全维度匹配 | 0.50 | 0.55 | 0.10 | **0.98** | 封顶 |

---

## 8. API 变化

### 8.1 新端点

```text
GET /api/v1/topology/evidence?source=Nova&target=Neutron&time_window=1+HOUR
→ {
    "edges": [{
      "source": "Nova",
      "target": "Neutron",
      "confidence": 0.95,
      "evidence": [
        {"source": "resource_id", "value": "INSTANCE:abc-123", "weight": 0.50},
        {"source": "request_id", "value": "req-abc-123", "weight": 0.35}
      ]
    }]
  }
```

### 8.2 现有端点兼容

`GET /api/v1/topology/hybrid` 保持现有返回格式不变，新增字段：

```json
{
  "edges": [{
    "source": "Nova",
    "target": "Neutron",
    "confidence": 0.95,
    "metrics": {
      "data_source": "openstack",
      "data_sources": ["openstack", "inferred", "resource"],
      "evidence_type": "observed",
      "evidence": [
        {"source": "resource_id", "value": "INSTANCE:abc-123"},
        {"source": "global_request_id", "value": "req-abc-123"}
      ]
    }
  }]
}
```

---

## 9. 实施阶段

### Phase 1: Core（约 2 周）

**目标：** 提取 instance_uuid/volume_id/port_id/image_id → 写入 ClickHouse → ResourceCorrelator 产出边 → 证据融合

**涉及改动：**

| 模块 | 文件 | 改动 |
|------|------|------|
| Semantic Engine | `normalize/resource.py` | 新建：extract_resource_fields(), resolve_aliases(), resolve_primary_resource() |
| Semantic Engine | `normalize/operation.py` | 新建：extract_operation() |
| Semantic Engine | `normalize/normalizer.py` | 修改：integrate new extractors |
| Shared Storage | `logoscope_storage/adapter.py` | 修改：新增列 DDL + INSERT |
| Correlation | `correlation/engine.py` | 新建：CorrelationEngine |
| Correlation | `correlation/base.py` | 新建：CorrelationProvider 基类 |
| Correlation | `correlation/evidence.py` | 新建：数据模型 |
| Correlation | `correlation/merger.py` | 新建：EvidenceMerger |
| Correlation | `correlation/providers/resource_correlator.py` | 新建：ResourceCorrelator |
| Correlation | `correlation/providers/request_correlator.py` | 新建：从现有 _get_openstack_topology 迁移 |
| Topology | `graph/hybrid_topology.py` | 修改：集成 CorrelationEngine |
| Topology | `graph/topology_engine.py` | 新建：TopologyEngine |
| API | `api/topology_routes.py` | 修改：排除新端点 |
| Tests | `tests/test_resource_correlator.py` | 新建 |
| Tests | `tests/test_evidence_merger.py` | 新建 |
| Tests | `tests/test_resource_extraction.py` | 新建（Semantic Engine 侧） |

### Phase 2: Alias + Infrastructure（约 1 周）

**目标：** Alias 映射全覆盖、Infrastructure 字段提取、HostCorrelator

**涉及改动：**

| 模块 | 文件 | 改动 |
|------|------|------|
| Semantic Engine | `normalize/resource.py` | 修改：展开 _ALIAS_MAP 和 _INFRA_FIELDS |
| Correlation | `correlation/providers/host_correlator.py` | 新建：按 host + pid + thread 关联 |
| ClickHouse | `adapter.py` | 修改：新增 infrastructure 列 DDL |

### Phase 3: 优化 + AI（约 2 周）

**目标：** 物化视图、增量构建、边缓存、AI 推理补全

**涉及改动：**

| 模块 | 文件 | 改动 |
|------|------|------|
| ClickHouse | `adapter.py` | 修改：resource_edges_mv 物化视图 |
| Correlation | `correlation/providers/ai_correlator.py` | 新建：AI 推理 |
| Topology | `topology_engine.py` | 修改：增量构建 + 缓存 |
| AI Service | `ai-service/` | 修改：边推理接口 |

---

## 10. 测试策略

### 10.1 Semantic Engine 测试

```python
# tests/test_resource_extraction.py

def test_extract_instance_uuid_from_attributes():
    """从 _raw_attributes 提取 instance_uuid"""
    log = {"_raw_attributes": {"instance_uuid": "abc-123-def-456"}}
    result = extract_resource_fields(log)
    assert result["instance_uuid"] == "abc-123-def-456"

def test_extract_instance_uuid_from_message():
    """从 message text 提取 instance_uuid（Phase 2）"""
    log = {"message": "Instance abc-123-def-456 spawned successfully"}
    result = extract_resource_fields(log)
    assert result["instance_uuid"] == "abc-123-def-456"

def test_device_id_alias_to_instance_uuid():
    """Phase 2: device_id 归一为 instance_uuid"""
    log = {"_raw_attributes": {"device_id": "abc-123-def-456"}}
    result = extract_resource_fields(log)
    assert result["instance_uuid"] == "abc-123-def-456"

def test_primary_resource_attach_volume():
    """attach_volume 操作的主资源是 INSTANCE"""
    log = {
        "_raw_attributes": {"event_type": "volume.attach.end"},
        "message": "...",
    }
    operation = extract_operation(log)
    resources = {"instance_uuid": "abc-123", "volume_id": "vol-456"}
    primary = resolve_primary_resource(operation, resources)
    assert primary["primary_resource_type"] == "INSTANCE"
    assert primary["primary_resource_id"] == "abc-123"

def test_primary_resource_create_volume():
    """create_volume 操作的主资源是 VOLUME"""
    log = {
        "_raw_attributes": {"event_type": "volume.create.end"},
        "message": "...",
    }
    operation = extract_operation(log)
    resources = {"volume_id": "vol-456", "instance_uuid": ""}
    primary = resolve_primary_resource(operation, resources)
    assert primary["primary_resource_type"] == "VOLUME"
```

### 10.2 Correlation Engine 测试

```python
# tests/test_resource_correlator.py

def test_resource_correlator_creates_edges():
    """相同 instance_uuid 的不同服务生成边"""
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

# tests/test_evidence_merger.py

def test_merger_combines_evidence():
    """同一条边来自 resource + request 两个来源"""
    edges = [
        CandidateEdge("A", "B", 1, [Evidence("resource_id", "rc", "INSTANCE:abc", 0.50)]),
        CandidateEdge("A", "B", 1, [Evidence("request_id", "rqc", "req-abc", 0.35)]),
    ]
    merger = EvidenceMerger()
    result = merger.merge(edges)
    assert ("A", "B") in result
    assert result[("A", "B")].confidence == pytest.approx(0.85, rel=1e-2)
    assert len(result[("A", "B")].evidence) == 2
```

---

## 11. 性能考量

| 场景 | 当前 | 优化后 | 改善 |
|------|------|--------|------|
| 单次拓扑查询（1h 窗口） | ~800ms | ~600ms | Bloom filter + 独立列减少 message 扫描 |
| Resource 关联查询（无物化视图） | ❌ | ~400ms | Bloom filter 索引 |
| Resource 关联查询（有物化视图） | ❌ | ~50ms | 预聚合 |
| 边增量构建 | ❌ | ~100ms | 只处理新数据 |
| 边缓存命中 | ❌ | ~2ms | Redis TTL=60s |

---

## 12. 向后兼容

| 影响点 | 兼容策略 |
|--------|----------|
| ClickHouse 存量数据 | ALTER TABLE ADD COLUMN ... DEFAULT ''，已有数据不补填 |
| 现有 API | 保持 /hybrid 端点输出格式不变，仅新增字段 |
| 现有测试 | 不改现有测试，新增 correlation engine 测试 |
| 前端 | 新增 evidence 字段不影响现有渲染逻辑 |
| 配置 | 新功能默认开启（`ENABLE_RESOURCE_CORRELATION=true`） |
