# Logoscope Data Architecture v1 — Event-driven Observability Platform

> **Event Sourcing + Projection + Workflow — 所有存储层都是 Projection，只有 Event 是 Source of Truth。**
>
> 定义从 Raw Log → NormalizedEvent → Multi-topic Event Bus → Projection Layer → Context Builder → Inference → Topology → Workflow → Action 的完整闭环。

**Status:** Draft v7
**Date:** 2026-07-01
**Authors:** croner01, Claude

---

## 1. Problem

### 1.1 当前架构的系统性瓶颈

v6 引入了 Event Bus 和 Property Graph，但仍有根本性问题：

**① Knowledge Layer 仍然是 mutable database，不是 Projection。**
Entity Builder 直接写入 Neo4j——如果 Knowledge Layer 损坏，无法从 Event Stream 恢复。违反 Event Sourcing 原则。

**② Graph Builder 仍然是 ETL，不是 Projection。**
全量 rebuild 在 10 亿 Event 量级不可行。增量 update 的正确性无法保证。

**③ 没有 Context Builder。**
AI 需要 IncidentContext（资源 + 邻居 + 时序 + 状态 + 告警），但现在 AI 要分别查 5 个 Registry。

**④ 没有 Workflow Engine。**
系统是单向分析管道——收到 Event → 分析 → 展示。不能 Event → 分析 → 行动 → Event，无法闭环。

**⑤ 单一 Topic 无法扩展。**
所有 Event 在一个 topic，不同 consumer 的 retention 和消费速率不同，互相干扰。

### 1.2 v7 核心理念

```
只有 Event 是 Source of Truth。
所有存储层都是 Projection（Materialized View）。
Projection 可以损坏、可以删除、可以重建——只要 Event 还在。
```

这意味着：

```
Entity Registry = Projection from entity-created/updated/deleted events
State          = Projection from state events (TTL-based, recomputable)
Property Graph = Projection from entity + state + interaction events
Timeline       = Projection from normalized events (time-ordered index)
DynamicRelationship = Projection from interaction events (recomputable)
```

### 1.3 v6 → v7 变更

| 维度 | v6 | v7 |
|------|----|----|
| **Knowledge Layer** | mutable database | Inventory Projection（Event Sourcing） |
| **State** | State Registry | State Projection（独立，TTL，可重建） |
| **Graph** | Graph Builder (ETL) | Graph Projection（versioned，可重建） |
| **Context** | AI 自己拼装 | Context Builder → IncidentContext |
| **Event Bus** | 单 topic | Multi-topic：normalized / entity / state / interaction / graph |
| **Projection** | 无版本 | 所有 Projection 带 projection_version |
| **Workflow** | 无 | Workflow Engine（架构预留） |
| **Fact Builder** | 三个职责 | Attribute / State / History 拆为独立 Projection |

---

## 2. Architecture

### 2.1 整体架构

```
                    Raw Logs / Metrics / Traces / Events
                               |
                               v
                       Semantic Engine
                               |
                       NormalizedEvent (event_id, 4 partitions)
                               |
                    +-----------v-----------+
                    |      Event Bus        |
                    |   (Multi-topic)       |
                    +---+-------+-------+---+
                        |       |       |
              +---------+       |       +---------+
              |                 |                 |
              v                 v                 v
        Entity Events     State Events      Interaction Events
        (topic: entity)   (topic: state)    (topic: interaction)
              |                 |                 |
              +--------+--------+--------+--------+
                       |                 |
                       v                 v
                  Projection Layer
     +-----------+----------+----------+-----------+
     |           |          |          |           |
     v           v          v          v           v
 Inventory   State     Graph      Timeline    DynamicRel
 Projection  Projection Projection  Projection  Projection
 (Neo4j)     (Redis)   (Neo4j)    (ClickHouse) (Cache)
     |           |          |          |           |
     +-----------+----------+----------+-----------+
                              |
                       Context Builder
                              |
                     IncidentContext
                              |
              +-------+-------+-------+
              |       |               |
              v       v               v
          Topology  Rule Engine    AI Inference
              |       |               |
              +-------+-------+-------+
                              |
                       Workflow Engine
                              |
                Action / API / SSH / kubectl
                              |
                     Generated Events
                              |
                     Event Bus (loop back)
```

### 2.2 Event Sourcing 原则

```
原则 1: 只有 Event 是不可变的 Source of Truth
原则 2: 所有 Projection 可以从 Event Stream 重建
原则 3: Projection 可以随时删除重建，不影响 Event
原则 4: 查询永远不直接查 Event Stream——查 Projection
原则 5: 写入永远通过 Event Bus——不直接修改 Projection
```

### 2.3 Multi-topic Event Bus

```
Topic                      | 分区 key         | Retention | 消费者
---------------------------|------------------|-----------|--------
normalized-events          | service_name     | 7d hot +  | Semantic → 原始 Event
                           |                  | 90d warm  |
entity-events              | entity_type:id   | 90d       | Inventory Projection
state-events               | entity_type:id   | 24h       | State Projection
interaction-events         | source:target    | 90d       | Graph + Correlation
graph-events               | entity_id        | 90d       | Graph Projection
alert-events               | severity         | 30d       | AI / Notification
workflow-events            | workflow_id      | 90d       | Workflow Engine
```

**为什么拆 topic：**
- 不同 Projection 的 retention 策略不同（State 24h，Inventory 90d）
- 不同 consumer 的消费速率不同（State 高频，Inventory 低频）
- Replay 时可以按 topic 单独回放，不影响其他 Projection

### 2.4 Projection 框架

每个 Projection 遵循统一接口：

```python
class Projection(ABC):
    """所有 Projection 的统一基类。"""

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    @abstractmethod
    def version(self) -> str:
        """projection_version — 用于并行运行不同版本的算法。"""
        ...

    @abstractmethod
    def apply(self, event: NormalizedEvent):
        """增量更新：收到 Event 时更新此 Projection。"""
        ...

    @abstractmethod
    def rebuild(self, event_source: EventSource):
        """全量重建：从 Event Stream 重建整个 Projection。"""
        ...

    @abstractmethod
    def status(self) -> ProjectionStatus:
        ...


@dataclass
class ProjectionStatus:
    projection_version: str
    event_count: int
    last_event_id: str
    is_rebuilding: bool = False
    rebuild_progress: float = 0.0
```

### 2.5 组件职责

| 层 | 职责 | 不做什么 |
|----|------|----------|
| **Semantic Engine** | Raw → NormalizedEvent，写入 Event Bus | 不建 Projection |
| **Event Bus** | Multi-topic Event 分发 | 不涉及业务逻辑 |
| **Inventory Projection** | 从 entity-events 构建 Entity Registry | 不处理 State |
| **State Projection** | 从 state-events 构建当前状态（TTL） | 不保存历史 |
| **Graph Projection** | 从 entity+state+interaction → Property Graph | 不做推理 |
| **Timeline Projection** | 从 normalized-events → 时序索引 | 不做聚合 |
| **Correlation Engine** | interaction-events → DynamicRel Projection | 不做 Inference |
| **Context Builder** | Projection → IncidentContext | 不存数据 |
| **Topology Engine** | Graph Projection → Layout → Render | 不查 Projection |
| **Inference Engine** | IncidentContext → 推理 | 不查原始数据 |
| **Workflow Engine** | Inference → Action → Event | 不做分析 |

---

## 3. Event Schema

### 3.1 核心类型

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

### 3.2 NormalizedEvent

```python
@dataclass
class NormalizedEvent:
    """唯一不可变的 Source of Truth。"""
    event_id: str              # UUID7
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
    """Multi-topic 事件总线。"""

    TOPICS = {
        "normalized-events":  {"retention_days": 7,   "partitions": 16},
        "entity-events":      {"retention_days": 90,  "partitions": 8},
        "state-events":       {"retention_days": 1,   "partitions": 8},
        "interaction-events": {"retention_days": 90,  "partitions": 16},
        "graph-events":       {"retention_days": 90,  "partitions": 8},
        "alert-events":       {"retention_days": 30,  "partitions": 4},
        "workflow-events":    {"retention_days": 90,  "partitions": 4},
    }

    @abstractmethod
    def publish(self, topic: str, event: Any):
        ...

    @abstractmethod
    def subscribe(self, topic: str, group: str,
                   callback: Callable[[Any], None]):
        ...
```

---

## 5. Semantic Engine

不变，同 v6。Raw → NormalizedEvent → Event Bus(normalized-events)。

---

## 6. Projection Layer

### 6.1 Inventory Projection

从 `entity-events` topic 构建。
Entity Created/Updated/Deleted Event → 版本化 Entity Registry。

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


class InventoryProjection(Projection):
    """
    从 entity-events topic 构建。
    可全量重建：清空 Neo4j → 从 Event Stream Replay entity-events。
    """
    name = "inventory"
    version = "v1"

    def apply(self, event: NormalizedEvent):
        # Entity Builder 消费 normalized-events
        # 产出 entity-created/updated/deleted → publish entity-events
        # InventoryProjection 消费 entity-events → 写入 Neo4j
        ...

    def rebuild(self, event_source: EventSource):
        self._clear_all()
        for event in event_source.stream("entity-events"):
            self.apply(event)


class EntityBuilder:
    """
    消费 normalized-events，产出 entity-events。
    这是 normalized-events → entity-events 的转换器。
    """

    def process(self, event: NormalizedEvent):
        for entity in event.entities:
            entity_event = {
                "event_type": "entity_seen",
                "entity_type": entity.type.value,
                "entity_id": entity.id,
                "timestamp": event.timestamp.isoformat(),
                "service": event.service_name,
            }
            event_bus.publish("entity-events", entity_event)
```

### 6.2 State Projection

从 `state-events` topic 构建。TTL 过期自动失效。

```python
@dataclass
class StateEntry:
    entity_key: str          # "INSTANCE:abc-123"
    key: str                 # "attached", "migration_state"
    value: str
    timestamp: datetime
    ttl_seconds: int = 60
    source: str = ""

    @property
    def is_current(self) -> bool:
        return (datetime.utcnow() - self.timestamp).total_seconds() < self.ttl_seconds


class StateProjection(Projection):
    """
    从 state-events topic 构建。
    存储在 Redis（或内存 KV），TTL 自动过期。
    可全量重建：清空 → Replay state-events（只保留最近 24h）。
    """
    name = "state"
    version = "v1"

    def apply(self, event: NormalizedEvent):
        # 从 event 中提取状态
        state_entries = self._extract_state(event)
        for entry in state_entries:
            self._store.setex(
                f"state:{entry.entity_key}:{entry.key}",
                entry.ttl_seconds,
                json.dumps(asdict(entry)),
            )

    def query(self, entity_type: ResourceType, entity_id: str,
              key: str) -> Optional[str]:
        entry = self._store.get(f"state:{entity_type.value}:{entity_id}:{key}")
        if entry:
            parsed = StateEntry(**json.loads(entry))
            return parsed.value if parsed.is_current else None
        return None
```

### 6.3 Graph Projection

从 `entity-events` + `state-events` + `interaction-events` 构建 Property Graph。

```python
class GraphProjection(Projection):
    """
    从多个 topic 构建 Property Graph。
    versioned — 可以 v1 和 v2 并行运行。

    v1: Neo4j + Cypher
    v2: (future) Native GraphQL / DGraph
    """
    name = "graph"
    version = "v1"

    def apply(self, event: NormalizedEvent):
        # entity-events → Node (MERGE)
        # state-events → Node State (SET)
        # interaction-events → Edge (MERGE)
        ...

    def get_subgraph(self, entity_id: str, depth: int = 2,
                     edge_types: Optional[List[str]] = None) -> "PropertyGraph":
        """查询邻居子图。Topology / AI / Rule 都调此接口。"""
        ...

    def rebuild(self, event_source: EventSource):
        self._clear_all()
        # 按顺序消费: entity-events → state-events → interaction-events
        for topic in ["entity-events", "state-events", "interaction-events"]:
            for event in event_source.stream(topic):
                self.apply(event)


@dataclass
class PropertyGraph:
    nodes: List['GraphNode']
    edges: List['GraphEdge']

    def get_subgraph(self, entity_id, depth, edge_types=None):
        ...
```

### 6.4 Timeline Projection

从 `normalized-events` 构建时间序索引。

```python
class TimelineProjection(Projection):
    """
    从 normalized-events 构建。
    ClickHouse Materialized View + 时间序索引。
    """
    name = "timeline"
    version = "v1"

    def get_timeline(self, entity_id: str,
                     time_range: str) -> List[NormalizedEvent]:
        """按 (entity_id, time) 查询事件序列。"""
        ...
```

### 6.5 DynamicRel Projection

从 `interaction-events` 聚合 Dynamic Relationship。

```python
class DynamicRelProjection(Projection):
    """
    从 interaction-events 聚合。
    可缓存、可重建、可版本化。
    """
    name = "dynamic_rel"
    version = "v1"

    def rebuild(self, event_source: EventSource):
        # SELECT source, target, count(*), first_seen, last_seen
        # FROM interaction-events GROUP BY source, target
        ...
```

---

## 7. Correlation Engine

### 7.1 Interaction（不可变，append-only）

```python
@dataclass
class InteractionEndpoint:
    entity: ResourceIdentity
    role: str = ""


@dataclass
class Interaction:
    """原子交互记录。不可变。"""
    interaction_id: str
    timestamp: datetime
    source_endpoint: InteractionEndpoint
    target_endpoint: InteractionEndpoint
    interaction_type: str
    duration_ms: float = 0.0
    request_id: str = ""
    outcome: str = ""


class InteractionExtractor:
    """
    从 NormalizedEvent 提取可能端点。
    不做端到端关联 —— 只提取"这个 Event 涉及哪些端点"。
    输出发布到 interaction-events topic。
    """

    def extract(self, event: NormalizedEvent) -> List[Interaction]:
        endpoints = self._extract_endpoints(event)
        # 发布到 interaction-events
        for ia in self._pair_endpoints(endpoints):
            event_bus.publish("interaction-events", ia)
```

### 7.2 Correlation Engine（轻量：只聚合端点对）

```python
class CorrelationEngine:
    """
    消费 interaction-events。
    聚合端点对 (A, B) → DynamicRelationship。
    Inference 已独立出去。
    """

    def process_interaction(self, ia: Interaction):
        # SELECT count, first_seen, last_seen
        # FROM interactions WHERE source=ia.source AND target=ia.target
        # UPDATE DynamicRelProjection
        ...

    def get_relationship(self, source: str, target: str
                          ) -> Optional[DynamicRelationship]:
        return self.dynamic_rel_projection.query(source, target)
```

### 7.3 Dynamic Relationship

```python
@dataclass
class DynamicRelationship:
    relationship_id: str
    version: int = 1
    source: str
    target: str
    relationship_type: str = "calls"
    confidence: float
    first_seen: datetime
    last_seen: datetime
    expire_after_minutes: int = 30
    status: str = "ACTIVE"  # ACTIVE, STALE, EXPIRED
    call_count: int = 0
```

---

## 8. Context Builder

### 8.1 定位

```
Graph Projection
State Projection
Timeline Projection
DynamicRel Projection
        |
        v
  Context Builder
        |
  IncidentContext
        |
    AI / Rule
```

Context Builder 是**薄层封装**，不引入新存储，不引入新 ETL。职责是：从多个 Projection 查询数据，组装为 `IncidentContext`。

### 8.2 IncidentContext

```python
@dataclass
class IncidentContext:
    """
    AI 和 Rule Engine 的统一输入。
    不需要知道 Evidence / Fact / Assertion 的存在。
    """
    # 核心资源
    resource_type: ResourceType
    resource_id: str
    resource_attributes: Dict[str, str]

    # 邻居
    neighbors: List[Dict]         # 邻近实体 + 关系类型

    # 当前状态
    current_state: Dict[str, str]  # key → value (attached=true)

    # 时序
    timeline: List[Dict]           # 关键事件序列

    # 动态关系
    relationships: List[Dict]      # 此资源参与的服务间关系

    # 告警
    recent_alerts: List[Dict]

    # 元信息
    context_id: str
    created_at: datetime


class ContextBuilder:
    """
    从 Projection 层查询数据，组装为 IncidentContext。
    不存数据，不建索引。
    """

    def build(self, resource_type: ResourceType, resource_id: str,
              time_window: str = "1 HOUR") -> IncidentContext:
        entity = self.inventory_projection.query(resource_type, resource_id)
        subgraph = self.graph_projection.get_subgraph(f"{resource_type.value}:{resource_id}")
        state = self.state_projection.query_all(resource_type, resource_id)
        timeline = self.timeline_projection.get_timeline(
            f"{resource_type.value}:{resource_id}", time_window)
        rels = self.dynamic_rel_projection.query_by_entity(
            resource_type, resource_id)

        return IncidentContext(
            resource_type=resource_type,
            resource_id=resource_id,
            resource_attributes=entity.attributes if entity else {},
            neighbors=[{"entity": n.id, "relation": e.type}
                       for n, e in subgraph.get_neighbors()],
            current_state=state,
            timeline=[{"event_id": t.event_id, "timestamp": t.timestamp.isoformat(),
                       "action": t.event.action}
                      for t in timeline[:50]],
            relationships=[{"source": r.source, "target": r.target,
                            "type": r.relationship_type, "confidence": r.confidence}
                           for r in rels],
            recent_alerts=self._get_alerts(resource_type, resource_id, time_window),
            context_id=uuid4().hex,
            created_at=datetime.utcnow(),
        )
```

### 8.3 API

```text
GET /api/v1/context/build?type=INSTANCE&id=abc-123&time_window=1+HOUR
→ IncidentContext (JSON)
```

---

## 9. Inference Engine

### 9.1 输入

```python
@dataclass
class InferenceInput:
    """
    推理引擎输入。
    AI/Rule/ML 统一接口。
    """
    context: IncidentContext
    query: str = ""


class InferenceEngine(ABC):
    """
    推理引擎。

    输入: IncidentContext（已经组装好）
    输出: 推理结果

    实现:
      LLMInferenceEngine — 大模型
      RuleInferenceEngine — 规则
      GraphInferenceEngine — 图模式
    """

    @abstractmethod
    def infer(self, input: InferenceInput) -> List['InferenceResult']:
        ...
```

### 9.2 AI 不再直接消费 Evidence

```
v6: AI 输入 = Subgraph + Evidence + Timeline
  → AI 仍然需要知道 Evidence/Fact 等平台内部概念

v7: AI 输入 = IncidentContext
  → AI 只需要知道: 这是什么资源？它现在什么状态？它的邻居是谁？
    最近发生了什么？哪些服务关联？
```

---

## 10. Topology Engine

```python
class TopologyEngine:
    """
    纯渲染引擎。
    输入：GraphProjection.get_subgraph()
    输出：Layout + Render
    """

    def render(self, entity_id: str, depth: int = 2) -> TopologyResult:
        subgraph = self.graph_projection.get_subgraph(entity_id, depth)
        nodes = [Node(...) for n in subgraph.nodes]
        edges = [Edge(...) for e in subgraph.edges]
        layout = self._compute_layout(nodes, edges)
        return TopologyResult(nodes=nodes, edges=edges, layout=layout)
```

---

## 11. Workflow Engine

### 11.1 定位

```
Event → Detect → Diagnose → Act → Event (闭环)

                ┌──────────────────────────────────────┐
                │          Workflow Engine              │
                │                                      │
                │  ┌──────────────────────────────┐    │
                │  │ Trigger: Alert Event          │    │
                │  │   │                           │    │
                │  │   v                           │    │
                │  │ Step 1: SSH → check process   │    │
                │  │ Step 2: kubectl → describe pod │    │
                │  │ Step 3: OpenStack API → check  │    │
                │  │ Step 4: Generate Result Event  │    │
                │  └──────────────────────────────┘    │
                └──────────────────────────────────────┘
                           │
                           v
                    Event Bus (workflow-events)
```

### 11.2 架构预留

Phase 4 实现 MVP：

```python
@dataclass
class Workflow:
    workflow_id: str
    name: str
    steps: List['WorkflowStep']


@dataclass
class WorkflowStep:
    step_type: str     # "ssh", "kubectl", "api", "http"
    target: str
    command: str
    timeout_seconds: int = 30
    retry_count: int = 0


class WorkflowEngine:
    """
    执行 Workflow。
    输入：InferenceResult → Workflow
    输出：WorkflowResult → Event Bus (workflow-events)
    """

    def execute(self, workflow: Workflow, context: IncidentContext) -> str:
        for step in workflow.steps:
            result = self._execute_step(step, context)
            self._publish_step_event(workflow.workflow_id, step, result)
        return "completed"
```

---

## 12. 置信度模型

（同 v6，不变）

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

## 13. API

```text
# Topology (消费 Graph Projection)
GET /api/v1/topology/hybrid?time_window=1+HOUR

# Context Builder
GET /api/v1/context/build?type=INSTANCE&id=abc-123&time_window=1+HOUR

# Interaction
GET /api/v1/interactions?source=Nova&target=Neutron

# Graph Projection
GET /api/v1/graph/subgraph?entity_id=abc-123&depth=2
GET /api/v1/graph/timeline?entity_id=abc-123&time_range=1h

# Inventory Projection
GET /api/v1/inventory/entities?type=INSTANCE&id=abc-123
GET /api/v1/inventory/entities/history?type=INSTANCE&id=abc-123

# State Projection
GET /api/v1/state/current?type=INSTANCE&id=abc-123

# Correlation
GET /api/v1/correlate/services?source=Nova

# Capability
GET /api/v1/capabilities/platforms
GET /api/v1/capabilities/extractors?resource_type=INSTANCE

# Projection Management
GET /api/v1/projections/status
POST /api/v1/projections/rebuild?name=inventory
```

---

## 14. 实施阶段

### Phase 0: Event Bus + Schema Registry（~1 周）

| 模块 | 内容 |
|------|------|
| Multi-topic Event Bus | 7 topics, Kafka |
| Schema Registry | EventSchema + EntitySchema + StateSchema + GraphSchema |
| Capability Registry | Platform + Extractor 注册 |
| event_id UUID7 | |

### Phase 1: Semantic Engine + NormalizedEvent → Event Bus（~1 周）

| 模块 | 内容 |
|------|------|
| Semantic Engine | Raw → NormalizedEvent |
| Event Bus 集成 | 写入 normalized-events |

### Phase 2: Inventory + State Projection（~2 周）

| 模块 | 内容 |
|------|------|
| Entity Builder | normalized-events → entity-events |
| Inventory Projection | entity-events → Neo4j (versioned) |
| State Projection | normalized-events → state-events → Redis (TTL) |
| Identity Resolution | Alias Registry |

### Phase 3: Interaction + Correlation（~2 周）

| 模块 | 内容 |
|------|------|
| Interaction Extractor | normalized-events → interaction-events |
| Correlation Engine | interaction-events → DynamicRel Projection |
| DynamicRel Projection | cacheable, rebuildable |

### Phase 4: Graph Projection + Context Builder（~2 周）

| 模块 | 内容 |
|------|------|
| Graph Projection | entity + state + interaction → Property Graph |
| Timeline Projection | normalized-events → ClickHouse MV |
| Context Builder | Multi-Projection → IncidentContext |

### Phase 5: Topology + Inference Engine（~1 周）

| 模块 | 内容 |
|------|------|
| Topology Engine | 纯渲染，消费 Graph Projection |
| Inference Engine | LLM + Rule 实现，消费 IncidentContext |

### Phase 6: Workflow Engine + Multi-platform（~2 周）

| 模块 | 内容 |
|------|------|
| Workflow Engine MVP | SSH + kubectl read |
| K8s Extractor | Capability 注册 |
| VMware Extractor | Capability 注册 |

---

## 15. 测试策略

```python
def test_event_sourcing_rebuild():
    """Projection 可以从 Event Stream 重建"""

    # 1. 启动语义引擎，处理一批日志
    events = [normalize_log(log) for log in test_logs]
    for e in events:
        bus.publish("normalized-events", e)

    # 2. Projection 消费 Event
    inventory.apply(events)
    assert inventory.query(ResourceType.INSTANCE, "abc") is not None

    # 3. 删除 Projection
    inventory._clear_all()
    assert inventory.query(ResourceType.INSTANCE, "abc") is None

    # 4. 从 Event Stream 重建
    inventory.rebuild(ReplayEventSource(events))
    assert inventory.query(ResourceType.INSTANCE, "abc") is not None
    # 结果完全一致


def test_multi_topic_independence():
    """不同 topic 的 Projection 独立重建"""
    # 只重建 inventory，不影响 state
    inventory.rebuild(event_source)
    assert inventory.query(...) is not None
    # state 未受影响
    assert state.query(...) is not None


def test_context_builder_no_storage():
    """Context Builder 不引入新存储"""
    builder = ContextBuilder(inventory, state, graph, timeline, rels)
    ctx = builder.build(ResourceType.INSTANCE, "abc-123")
    # 数据来自 Projection，Builder 只是组装
    assert ctx.resource_id == "abc-123"
    assert ctx.current_state is not None
    assert len(ctx.timeline) > 0


def test_incident_context_ai_ready():
    """IncidentContext 不暴露内部概念"""
    ctx = builder.build(ResourceType.INSTANCE, "abc-123")
    # AI 不需要知道 Evidence/Fact/Assertion
    assert not hasattr(ctx, "evidence")
    assert not hasattr(ctx, "fact")
    assert not hasattr(ctx, "assertion")


def test_projection_version_parallel():
    """两个不同版本的 Projection 可并行运行"""
    v1 = InventoryProjection(version="v1")
    v2 = InventoryProjection(version="v2")  # 不同算法
    for e in events:
        v1.apply(e)
        v2.apply(e)
    assert v1.query(...) is not None
    assert v2.query(...) is not None


def test_workflow_engine_loop():
    """Workflow Engine → Action → Event → Event Bus"""
    engine = WorkflowEngine()
    context = builder.build(ResourceType.HOST, "compute-01")
    result = engine.execute(
        Workflow(steps=[WorkflowStep("ssh", "compute-01", "systemctl status nova-compute")]),
        context,
    )
    assert result == "completed"
    # 检查 workflow-events topic 有新的 Event


def test_state_ttl_expiry():
    """State Projection TTL 自动过期"""
    state.apply(state_event(key="attached", value="true", ttl=60))
    assert state.query(ResourceType.VOLUME, "vol-1", "attached") == "true"
    # 模拟 70 秒后
    with freeze_time(now + timedelta(seconds=70)):
        assert state.query(ResourceType.VOLUME, "vol-1", "attached") is None


def test_topology_no_projection_access():
    """Topology Engine 不直接查 Projection"""
    graph = FakeGraphProjection()
    engine = TopologyEngine(graph)
    result = engine.render("entity-001")
    # Topology 只消费 GraphProjection.get_subgraph()
    # 不查 Inventory / State / Correlation
    assert len(result.nodes) > 0
```

---

## 16. 性能考量

| 场景 | 预期 | 瓶颈 | 扩展方式 |
|------|------|------|----------|
| Event Bus 吞吐 | 100K events/s | Kafka partition | 增加 partitions |
| Inventory Projection | 10K updates/s | Neo4j write | Batch write |
| State Projection | 50K updates/s | Redis | Cluster mode |
| Graph Projection | 5K updates/s | Neo4j write | Sharding |
| Context Builder | 100 req/s | Multi-projection read | Cache |

---

## 17. 向后兼容

| 影响点 | 策略 |
|--------|------|
| 现有 API /hybrid | 保持格式不变 |
| ClickHouse 存量 | ALTER TABLE |
| event_id 历史 | 存量="", 新 UUID7 |
| 单 topic 迁移到 multi-topic | Phase 0 并行，Phase 1 切换 |

---

## Appendix: v6 → v7 变更对照

| 维度 | v6 | v7 |
|------|----|----|
| **Knowledge Layer** | mutable database | **Inventory Projection**（Event Sourcing） |
| **State** | Knowledge State Registry | **State Projection**（独立 Redis，TTL） |
| **Graph** | Graph Builder (ETL) | **Graph Projection**（versioned，可重建） |
| **Fact Builder** | 三个职责（Attribute/State/Historical） | 拆为 **Attribute / State / History 三个 Projection** |
| **Context** | AI 自己拼装 | **Context Builder → IncidentContext** |
| **Evidence 链** | O→T→A→F→E→I | **不变**（Assertion 保留） |
| **Correlation** | 包含 Interaction + Inference | **只有 Interaction→Relationship**，Inference 独立 |
| **Inference 输入** | Subgraph + Evidence + Timeline | **IncidentContext**（不暴露平台内部概念） |
| **Event Bus** | 单 topic | **Multi-topic**（7 topics，不同 retention） |
| **Projection** | 无版本 | 所有 Projection 带 **projection_version** |
| **Workflow** | 无 | **Workflow Engine**（架构预留） |
| **闭环** | 单向分析管道 | **Event → Action → Event 闭环** |
