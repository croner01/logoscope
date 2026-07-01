# Logoscope v15 — AI Observability Operating System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the v15 architecture: Event Sourcing + CQRS + WorldView Facade (TopologyQuery/StateQuery/HistoryQuery) + Canonical Context Hash (RFC 8785 + Merkle DAG) + Goal Tree (desired_state, no Workflow concepts) + Expression (structured preconditions) + Inference Pipeline + Knowledge Object Model + Planner (GoalInferrer → IntentGenerator → ExecutionPlanner) + Blast Radius Analyzer (ImpactModel + Dependency + State) + RiskEngine (3-tier + Constraint/Expression) + OPA Policy (effects-based, configurable Utility weights) + DecisionOrchestrator + DecisionStateMachine (separated) + Episode (DecisionStep: candidate_scores + reject_reasons) + ExperienceGraphProjection (failure_pattern dimension).

**Architecture:** 8 sequential phases. Each phase produces independently testable software on top of existing Logoscope services. Phase 0-3 build the data plane (events → projections → correlation). Phase 4 adds WorldView Facade + Context API. Phase 5 adds Goal Tree + Inference. Phase 6 adds Decision Orchestration + Blast Radius + Policy. Phase 7 adds Episode Learning + Experience Graph.

**Tech Stack:** Python 3.11, aiokafka, Kafka (Go segmentio/kafka-go), ClickHouse, Neo4j, Redis, OPA (Open Policy Agent), ChromaDB/Weaviate (Knowledge Store)

**Scope note:** The spec covers ~8 independent subsystems. This plan decomposes into separate Phase documents. Each Phase is independently implementable and testable.

## Global Constraints

- All new Event types use EventEnvelope wrapping (envelope_version, schema_version, event_type, producer, event_id, parent_event_ids, payload)
- All new code lives in `shared_src/` for cross-service shared logic, service-specific code in respective `{service}/` directories
- Projection checkpoint uses partition+offset (not event_id)
- **v12+:** StateProjectionRef — Context references Projection via `projection_epoch`, no data copy in ContextResult
- **v12+:** Content-based hashing (context_hash = sha256 of canonical content, no timestamp component). Use `CanonicalContextHasher` (RFC 8785 deterministic JSON serialization + Merkle DAG)
- **v12+:** Snapshot belongs to Projection Layer (not Context API), `use_snapshot=False` by default
- **v12+:** PlanIntent — Generator outputs *what* (action + target), not *how* (steps). WorkflowComposer converts Intent → Workflow
- **v12+:** Policy Engine sorts candidates (Planner outputs unsorted), RiskEngine is independent component
- **v12+:** Feedback is eventized (EvaluationEvent → LearningEvent published to bus)
- **v13+:** All Inference uses `InferencePipeline` — Preprocess → Prompt → Engine → PostProcess → Validate → Normalize
- **v13+:** Knowledge Object Model: SOP / Runbook / FailurePattern / Incident / RCA as typed documents
- **v14+:** Decision State Machine has 12 states: CREATED → PLANNING → PLANNED → PENDING_APPROVAL → APPROVED/REJECTED → EXECUTING → VERIFYING → SUCCEEDED/FAILED/ROLLED_BACK/CANCELLED
- **v14+:** Episode is Event Sourcing fact (append-only, full trajectory). ExperienceGraph is Projection (statistical aggregation, read model)
- **v15:** WorldView is Facade — combines TopologyQuery + StateQuery + HistoryQuery. New query capability = new Query class, not WorldView method
- **v15:** Goal describes desired_state (target state). Workflow describes *how* to achieve it. GoalNode has no action/ordering/completion
- **v15:** Capability preconditions/postconditions use `Expression(field, operator, value)` — not strings. Programmatically evaluable via WorldView
- **v15:** All decision reasons recorded in Episode DecisionStep — including *why not* Candidate A
- **v15:** Decision Orchestration (DecisionOrchestrator) and State Management (DecisionStateMachine) are separate components
- **v15:** Utility weights configurable via `UtilityWeights` dataclass + OPA Rego `data.logoscope.utility_weights`
- **v15:** ExperienceGraph key = `(failure_pattern, capability_id, env_fingerprint)` — keeps different fault scenarios separate
- All Inference output is Finding (unified structure, no `recommended_action` in v15)
- Policy Engine uses OPA (Rego), not custom DSL
- OPA policies live in `deploy/policies/*.rego`, loaded at startup
- Capability Registry requires effects (List[str] tag) + base_risk on all capabilities
- Capability final_risk is computed dynamically: base_risk + env + time + blast_radius
- OPA input uses effects tag + risk only, NOT capability names
- DecisionRecord records Finding→Planner→Policy→Workflow audit chain
- All tests use pytest with TDD (write failing test first)
- Kafka topics follow `platform.{type}` naming convention
- Module `__init__.py` exports: TDD — write failing test first, then implement

---
&nbsp;
# Phase 0: Foundation — Raw Event Store + Schema Registry + Event Bus

**Goal:** EventEnvelope + RawEvent Store + Schema Registry + Event Bus (10 topics, domain naming, parent_event_ids). All existing services remain unchanged; this adds the new Event layer alongside.

**Dependencies:** None (first phase)

**Deliverable:** `shared_src/event/` package with working EventEnvelope, SchemaRegistry, RawEventStore, and Bus abstraction. Existing ingest pipeline continues to work unchanged.

---

### Task 0.1: EventEnvelope dataclass + Serialization

**Files:**
- Create: `shared_src/event/envelope.py`
- Create: `shared_src/event/__init__.py`
- Test: `shared_src/tests/event/test_envelope.py`

**Interfaces:**
- Produces: `EventEnvelope` dataclass, `serialize_envelope()`, `deserialize_envelope()`

- [ ] **Step 1: Create test directory**

```bash
mkdir -p shared_src/tests/event shared_src/event
```

- [ ] **Step 2: Write the failing test**

```python
# shared_src/tests/event/test_envelope.py
import pytest
import json
from datetime import datetime
from shared_src.event.envelope import EventEnvelope, serialize_envelope, deserialize_envelope


class TestEventEnvelope:
    def test_create_envelope(self):
        """EventEnvelope 创建并携带所有字段"""
        env = EventEnvelope(
            envelope_version="v1",
            schema_version=1,
            event_type="raw.log",
            producer="ingest-service",
            event_id="test-001",
            parent_event_ids=[],
            timestamp=datetime(2026, 7, 1, 12, 0, 0),
            payload=b'{"key": "value"}',
            metadata={"cluster": "prod"},
        )
        assert env.envelope_version == "v1"
        assert env.schema_version == 1
        assert env.event_type == "raw.log"
        assert env.producer == "ingest-service"
        assert env.event_id == "test-001"
        assert env.parent_event_ids == []
        assert env.metadata["cluster"] == "prod"

    def test_serialize_deserialize_roundtrip(self):
        """序列化后再反序列化，字段不变"""
        env = EventEnvelope(
            schema_version=1,
            event_type="raw.log",
            producer="test",
            event_id="test-001",
            payload=json.dumps({"key": "value"}).encode(),
        )
        data = serialize_envelope(env)
        restored = deserialize_envelope(data)
        assert restored.schema_version == env.schema_version
        assert restored.event_type == env.event_type
        assert restored.event_id == env.event_id
        assert json.loads(restored.payload) == {"key": "value"}

    def test_parent_event_ids_lineage(self):
        """parent_event_ids 构建血缘链"""
        raw = EventEnvelope(event_id="raw-001", parent_event_ids=[])
        norm = EventEnvelope(
            event_id="norm-001",
            parent_event_ids=[raw.event_id],
        )
        finding = EventEnvelope(
            event_id="finding-001",
            parent_event_ids=[norm.event_id] + norm.parent_event_ids,
        )
        assert "raw-001" in finding.parent_event_ids
        assert "norm-001" in finding.parent_event_ids
        # 血缘链长度正确：direct parent + grandparent
        assert len(finding.parent_event_ids) == 2

    def test_producer_tracking(self):
        """producer 字段追踪谁产生了这个 Event"""
        env = EventEnvelope(
            event_id="e1",
            event_type="normalized.event",
            producer="semantic-engine",
        )
        assert env.producer == "semantic-engine"
```

- [ ] **Step 3: Run test to verify it fails**

```bash
cd /root/logoscope && python -m pytest shared_src/tests/event/test_envelope.py -v
```
Expected: FAIL with ModuleNotFoundError or ImportError

- [ ] **Step 4: Write minimal implementation**

```python
# shared_src/event/envelope.py
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Any
import json


@dataclass
class EventEnvelope:
    """所有 Event 的通用信封。parent_event_ids 构成血缘链。"""
    envelope_version: str = "v1"
    schema_version: int = 1
    event_type: str = ""
    producer: str = ""
    event_id: str = ""
    parent_event_ids: List[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    payload: bytes = b""
    metadata: Dict[str, str] = field(default_factory=dict)


# --- Serialization ---

ENVELOPE_JSON_KEY = "_envelope"


def serialize_envelope(env: EventEnvelope) -> bytes:
    """序列化 EventEnvelope 为 JSON bytes。"""
    payload = env.payload
    # 检查 payload 是否为有效 JSON，是则直接存储
    # 否则作为原始 bytes 存储
    return json.dumps({
        "envelope_version": env.envelope_version,
        "schema_version": env.schema_version,
        "event_type": env.event_type,
        "producer": env.producer,
        "event_id": env.event_id,
        "parent_event_ids": env.parent_event_ids,
        "timestamp": env.timestamp.isoformat(),
        "payload": payload.decode("utf-8", errors="replace"),
        "metadata": env.metadata,
    }).encode("utf-8")


def deserialize_envelope(data: bytes) -> EventEnvelope:
    """从 JSON bytes 反序列化为 EventEnvelope。"""
    obj = json.loads(data.decode("utf-8"))
    payload = obj.get("payload", "")
    return EventEnvelope(
        envelope_version=obj.get("envelope_version", "v1"),
        schema_version=obj.get("schema_version", 1),
        event_type=obj.get("event_type", ""),
        producer=obj.get("producer", ""),
        event_id=obj.get("event_id", ""),
        parent_event_ids=obj.get("parent_event_ids", []),
        timestamp=datetime.fromisoformat(obj.get("timestamp", datetime.utcnow().isoformat())),
        payload=payload.encode("utf-8") if isinstance(payload, str) else payload,
        metadata=obj.get("metadata", {}),
    )
```

```python
# shared_src/event/__init__.py
from .envelope import EventEnvelope, serialize_envelope, deserialize_envelope

__all__ = ["EventEnvelope", "serialize_envelope", "deserialize_envelope"]
```

- [ ] **Step 5: Run test to verify it passes**

```bash
cd /root/logoscope && python -m pytest shared_src/tests/event/test_envelope.py -v
```
Expected: 4 PASSED

- [ ] **Step 6: Commit**

```bash
git add shared_src/event/ shared_src/tests/event/test_envelope.py
git commit -m "feat(event): add EventEnvelope dataclass with serialization

EventEnvelope 是所有 Event 的统一容器：
- parent_event_ids 构建血缘链
- serialize_envelope / deserialize_envelope JSON 序列化
- producer 字段追踪 Event 来源

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 0.2: SchemaRegistry + Schema Evolution

**Files:**
- Create: `shared_src/event/schema_registry.py`
- Create: `shared_src/tests/event/test_schema_registry.py`

**Interfaces:**
- Produces: `SchemaRegistry`, `Schema`, `SchemaMigration`, `SchemaMigrationError`
- Consumes: `EventEnvelope` (from Task 0.1)

- [ ] **Step 1: Write the failing test**

```python
# shared_src/tests/event/test_schema_registry.py
import pytest
import json
from shared_src.event.schema_registry import SchemaRegistry, Schema, SchemaMigrationError
from shared_src.event.envelope import EventEnvelope


class TestSchemaRegistry:
    def test_register_and_latest_version(self):
        registry = SchemaRegistry()
        registry.register("normalized.event", 1, Schema(
            event_type="normalized.event", version=1,
            fields={"event_id": str, "message": str},
        ))
        registry.register("normalized.event", 2, Schema(
            event_type="normalized.event", version=2,
            fields={"event_id": str, "message": str, "tenant_id": str},
        ))
        assert registry.latest_version("normalized.event") == 2

    def test_migration_v1_to_v2(self):
        registry = SchemaRegistry()
        registry.register("normalized.event", 1, Schema(
            event_type="normalized.event", version=1,
            fields={"event_id": str, "message": str},
        ))
        registry.register("normalized.event", 2, Schema(
            event_type="normalized.event", version=2,
            fields={"event_id": str, "message": str, "tenant_id": str},
        ))
        registry.register_migration(
            "normalized.event", 1, 2,
            migrate_fn=lambda p: {**p, "tenant_id": ""},
        )
        env = EventEnvelope(
            schema_version=1,
            event_type="normalized.event",
            payload=json.dumps({"event_id": "e1", "message": "test"}).encode(),
        )
        payload = registry.deserialize(env)
        assert "tenant_id" in payload
        assert payload["tenant_id"] == ""

    def test_deserialize_latest_version_no_migration(self):
        """最新版本不需要迁移"""
        registry = SchemaRegistry()
        registry.register("raw.log", 1, Schema(
            event_type="raw.log", version=1,
            fields={"raw_id": str, "raw_payload": str},
        ))
        env = EventEnvelope(
            schema_version=1,
            event_type="raw.log",
            payload=json.dumps({"raw_id": "r1", "raw_payload": "test"}).encode(),
        )
        payload = registry.deserialize(env)
        assert payload["raw_id"] == "r1"

    def test_missing_migration_raises_error(self):
        registry = SchemaRegistry()
        registry.register("test.event", 1, Schema(...))
        registry.register("test.event", 3, Schema(...))  # 跳过 v2
        env = EventEnvelope(
            schema_version=1,
            event_type="test.event",
            payload=json.dumps({"key": "val"}).encode(),
        )
        with pytest.raises(SchemaMigrationError):
            registry.deserialize(env)

    def test_validate_payload(self):
        registry = SchemaRegistry()
        registry.register("test.event", 1, Schema(
            event_type="test.event", version=1,
            fields={"id": str, "count": int},
        ))
        assert registry.validate("test.event", 1, {"id": "x", "count": 1})
        assert not registry.validate("test.event", 1, {"id": "x"})  # 缺少 count
        assert not registry.validate("test.event", 1, {"id": 123, "count": 1})  # id 类型错误
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /root/logoscope && python -m pytest shared_src/tests/event/test_schema_registry.py -v
```
Expected: ModuleNotFoundError for schema_registry

- [ ] **Step 3: Write minimal implementation**

```python
# shared_src/event/schema_registry.py
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Callable, Any, Type
import json
from .envelope import EventEnvelope


@dataclass
class Schema:
    event_type: str
    version: int
    fields: Dict[str, Type]
    created_at: Optional[str] = None


@dataclass
class SchemaMigration:
    from_version: int
    to_version: int
    migrate_fn: Callable[[Dict], Dict]


class SchemaMigrationError(Exception):
    pass


class SchemaRegistry:
    def __init__(self):
        self._schemas: Dict[str, Dict[int, Schema]] = {}
        self._migrations: Dict[str, Dict[int, SchemaMigration]] = {}

    def register(self, event_type: str, version: int, schema: Schema):
        if event_type not in self._schemas:
            self._schemas[event_type] = {}
        self._schemas[event_type][version] = schema

    def register_migration(self, event_type: str,
                           from_version: int, to_version: int,
                           migrate_fn: Callable[[Dict], Dict]):
        key = event_type
        if key not in self._migrations:
            self._migrations[key] = {}
        self._migrations[key][from_version] = SchemaMigration(
            from_version=from_version,
            to_version=to_version,
            migrate_fn=migrate_fn,
        )

    def latest_version(self, event_type: str) -> int:
        versions = self._schemas.get(event_type, {})
        if not versions:
            return 1
        return max(versions.keys())

    def deserialize(self, envelope: EventEnvelope) -> Dict:
        payload = json.loads(envelope.payload.decode("utf-8"))
        current_version = envelope.schema_version
        latest = self.latest_version(envelope.event_type)

        while current_version < latest:
            migrations = self._migrations.get(envelope.event_type, {})
            migration = migrations.get(current_version)
            if not migration:
                raise SchemaMigrationError(
                    f"No migration from v{current_version} for {envelope.event_type}"
                )
            payload = migration.migrate_fn(payload)
            current_version = migration.to_version

        return payload

    def validate(self, event_type: str, version: int,
                 payload: Dict) -> bool:
        schema = self._schemas.get(event_type, {}).get(version)
        if not schema:
            return False
        for field_name, field_type in schema.fields.items():
            if field_name not in payload:
                return False
            if not isinstance(payload[field_name], field_type):
                return False
        return True

    def serialize(self, event_type: str, payload: Dict,
                  producer: str, **metadata) -> EventEnvelope:
        import uuid
        from datetime import datetime
        version = self.latest_version(event_type)
        return EventEnvelope(
            schema_version=version,
            event_type=event_type,
            producer=producer,
            event_id=uuid.uuid4().hex,
            timestamp=datetime.utcnow(),
            payload=json.dumps(payload).encode("utf-8"),
            metadata=metadata,
        )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /root/logoscope && python -m pytest shared_src/tests/event/test_schema_registry.py -v
```
Expected: 5 PASSED

- [ ] **Step 5: Update __init__.py and commit**

```python
# shared_src/event/__init__.py
from .envelope import EventEnvelope, serialize_envelope, deserialize_envelope
from .schema_registry import SchemaRegistry, Schema, SchemaMigration, SchemaMigrationError

__all__ = [
    "EventEnvelope", "serialize_envelope", "deserialize_envelope",
    "SchemaRegistry", "Schema", "SchemaMigration", "SchemaMigrationError",
]
```

```bash
git add shared_src/event/schema_registry.py shared_src/tests/event/test_schema_registry.py shared_src/event/__init__.py
git commit -m "feat(event): add SchemaRegistry with schema evolution and migration

- Schema 注册和版本管理
- 自动迁移链（v1→v2→v3）
- payload 校验
- 序列化时自动使用最新版本

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 0.3: RawEvent Store + Event Bus abstraction

**Files:**
- Create: `shared_src/event/raw_event_store.py`
- Create: `shared_src/event/bus.py`
- Create: `shared_src/tests/event/test_raw_event_store.py`
- Create: `shared_src/tests/event/test_bus.py`

**Interfaces:**
- Produces: `RawEventStore`, `EventBus` ABC, `InMemoryEventBus`
- Consumes: `EventEnvelope`, `SchemaRegistry`

- [ ] **Step 1: Write failing tests for RawEventStore**

```python
# shared_src/tests/event/test_raw_event_store.py
import pytest
from datetime import datetime
from shared_src.event.envelope import EventEnvelope
from shared_src.event.raw_event_store import RawEventStore


class TestRawEventStore:
    def test_append_and_read(self):
        store = RawEventStore()
        env = EventEnvelope(
            event_id="e1",
            event_type="raw.log",
            payload=b"test log line",
        )
        store.append(env)
        retrieved = store.read("e1")
        assert retrieved is not None
        assert retrieved.event_id == "e1"
        assert retrieved.payload == b"test log line"

    def test_read_nonexistent(self):
        store = RawEventStore()
        assert store.read("nonexistent") is None

    def test_immutable_append_only(self):
        """写入后的 Event 不能修改"""
        store = RawEventStore()
        env = EventEnvelope(event_id="e1", payload=b"original")
        store.append(env)
        env.payload = b"modified"
        retrieved = store.read("e1")
        assert retrieved.payload == b"original"

    def test_replay_all(self):
        store = RawEventStore()
        for i in range(5):
            store.append(EventEnvelope(event_id=f"e{i}", payload=str(i).encode()))
        events = list(store.replay())
        assert len(events) == 5
        assert events[0].event_id == "e0"
```

- [ ] **Step 2: Write failing tests for EventBus**

```python
# shared_src/tests/event/test_bus.py
import pytest
from shared_src.event.envelope import EventEnvelope
from shared_src.event.bus import InMemoryEventBus


class TestEventBus:
    def test_publish_and_subscribe(self):
        bus = InMemoryEventBus()
        received = []

        def callback(env):
            received.append(env)

        bus.subscribe("platform.raw", "test-group", callback)
        env = EventEnvelope(event_id="e1", event_type="raw.log")
        bus.publish("platform.raw", env)
        assert len(received) == 1
        assert received[0].event_id == "e1"

    def test_topic_isolation(self):
        """不同 topic 不互相干扰"""
        bus = InMemoryEventBus()
        received = []
        bus.subscribe("platform.raw", "g1", lambda e: received.append(e))
        bus.publish("platform.normalized", EventEnvelope(event_id="e1"))
        assert len(received) == 0

    def test_multiple_subscribers(self):
        bus = InMemoryEventBus()
        r1, r2 = [], []
        bus.subscribe("platform.raw", "g1", lambda e: r1.append(e))
        bus.subscribe("platform.raw", "g2", lambda e: r2.append(e))
        bus.publish("platform.raw", EventEnvelope(event_id="e1"))
        assert len(r1) == 1
        assert len(r2) == 1
```

- [ ] **Step 3: Run tests** — expect failures

```bash
cd /root/logoscope && python -m pytest shared_src/tests/event/ -v
```

- [ ] **Step 4: Write implementation**

```python
# shared_src/event/raw_event_store.py
from typing import Dict, List, Optional, Generator
from .envelope import EventEnvelope


class RawEventStore:
    """
    原始事件存储。
    In-memory 实现（生产用 Kafka + WAL）。
    不可变 append-only。
    """

    def __init__(self):
        self._store: Dict[str, EventEnvelope] = {}
        self._order: List[str] = []

    def append(self, envelope: EventEnvelope):
        import copy
        self._store[envelope.event_id] = copy.deepcopy(envelope)
        self._order.append(envelope.event_id)

    def read(self, event_id: str) -> Optional[EventEnvelope]:
        import copy
        env = self._store.get(event_id)
        return copy.deepcopy(env) if env else None

    def replay(self) -> Generator[EventEnvelope, None, None]:
        import copy
        for eid in self._order:
            yield copy.deepcopy(self._store[eid])
```

```python
# shared_src/event/bus.py
from abc import ABC, abstractmethod
from typing import Callable, Dict, List, Optional
from .envelope import EventEnvelope


class EventBus(ABC):
    """Multi-topic 事件总线抽象。"""

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
        ...


class InMemoryEventBus(EventBus):
    """测试用内存实现。"""

    def __init__(self):
        self._subscribers: Dict[str, List[Callable]] = {}
        self._history: Dict[str, List[EventEnvelope]] = {}

    def publish(self, topic: str, envelope: EventEnvelope):
        if topic not in self._history:
            self._history[topic] = []
        self._history[topic].append(envelope)
        for cb in self._subscribers.get(topic, []):
            cb(envelope)

    def subscribe(self, topic: str, group: str,
                   callback: Callable[[EventEnvelope], None]):
        if topic not in self._subscribers:
            self._subscribers[topic] = []
        self._subscribers[topic].append(callback)

    def latest_offsets(self, topic: str) -> Dict[int, int]:
        return {0: len(self._history.get(topic, []))}
```

- [ ] **Step 5: Run tests — verify pass**

```bash
cd /root/logoscope && python -m pytest shared_src/tests/event/ -v
```
Expected: all tests pass

- [ ] **Step 6: Update __init__.py and commit**

```python
# shared_src/event/__init__.py
from .envelope import EventEnvelope, serialize_envelope, deserialize_envelope
from .schema_registry import SchemaRegistry, Schema, SchemaMigration, SchemaMigrationError
from .raw_event_store import RawEventStore
from .bus import EventBus, InMemoryEventBus

__all__ = [
    "EventEnvelope", "serialize_envelope", "deserialize_envelope",
    "SchemaRegistry", "Schema", "SchemaMigration", "SchemaMigrationError",
    "RawEventStore", "EventBus", "InMemoryEventBus",
]
```

```bash
git add shared_src/event/raw_event_store.py shared_src/event/bus.py shared_src/tests/event/ shared_src/event/__init__.py
git commit -m "feat(event): add RawEventStore + EventBus abstraction

- RawEventStore: immutable append-only storage with replay
- EventBus ABC + InMemoryEventBus for testing
- 10 topics with domain-style naming
- Topic isolation and multi-subscriber support

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 0.4: Update Ingest Service Go — Wire RawEvent + EventEnvelope

**Files:**
- Modify: `ingest-service/internal/ingest/queue.go`
- Modify: `ingest-service/internal/ingest/http.go`
- Modify: `ingest-service/internal/ingest/config.go`
- Test: `ingest-service/internal/ingest/queue_test.go`

**Interfaces:**
- Consumes: `EventEnvelope` serialization format (JSON)
- Produces: Kafka messages in `platform.raw` topic with EventEnvelope format

- [ ] **Step 1: Write test for EventEnvelope output**

```go
// ingest-service/internal/ingest/queue_test.go (add to existing)
func TestQueueWritesEventEnvelope(t *testing.T) {
    // 验证写入 Kafka 的消息是 EventEnvelope JSON 格式
    // 包含 event_id, event_type, producer, parent_event_ids
}
```

- [ ] **Step 2: Update config — add new topic name**

```go
// ingest-service/internal/ingest/config.go
// 新增 topic 配置
KafkaTopicRaw  = "platform.raw"     // 替代旧的 logs.raw
```

- [ ] **Step 3: Update queue.go — wrap messages in EventEnvelope**

```go
// ingest-service/internal/ingest/queue.go
// 在 WriteToQueue 中，将消息包装为 EventEnvelope JSON
// event_type = "{data_type}.raw" (e.g. "log.raw")
// producer = "ingest-service"
// event_id = UUID7
// parent_event_ids = []
```

- [ ] **Step 4: Run tests**

```bash
cd /root/logoscope/ingest-service && go test ./internal/ingest/ -v -run TestQueue
```

- [ ] **Step 5: Commit**

```bash
git add ingest-service/internal/ingest/
git commit -m "feat(ingest): wrap raw messages in EventEnvelope format

- Kafka topic 改为 platform.raw
- 消息包装为 EventEnvelope JSON（event_id, event_type, producer）
- parent_event_ids 初始为空

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 0.5: ProjectionCheckpoint dataclass + manager

**Files:**
- Create: `shared_src/projection/__init__.py`
- Create: `shared_src/projection/checkpoint.py`
- Create: `shared_src/tests/projection/test_checkpoint.py`

**Interfaces:**
- Produces: `ProjectionCheckpoint`, `PartitionOffset`

- [ ] **Step 1: Write failing test**

```python
# shared_src/tests/projection/test_checkpoint.py
import pytest
from shared_src.projection.checkpoint import ProjectionCheckpoint


class TestProjectionCheckpoint:
    def test_update_offset(self):
        cp = ProjectionCheckpoint(projection="inventory", epoch="20260701")
        cp.update("platform.entity", 0, 100)
        assert cp.records["platform.entity"][0] == 100

    def test_offset_monotonic(self):
        """offset 严格递增，不回退"""
        cp = ProjectionCheckpoint(projection="inventory", epoch="20260701")
        cp.update("platform.entity", 0, 100)
        cp.update("platform.entity", 0, 90)  # 小于当前——应忽略
        assert cp.records["platform.entity"][0] == 100

    def test_lag_calculation(self):
        cp = ProjectionCheckpoint(projection="inventory", epoch="20260701")
        cp.update("platform.entity", 0, 100)
        assert cp.get_lag("platform.entity", 0, 150) == 50

    def test_multiple_topics(self):
        cp = ProjectionCheckpoint(projection="graph", epoch="20260701")
        cp.update("platform.entity", 0, 200)
        cp.update("platform.interaction", 1, 300)
        assert cp.records["platform.entity"][0] == 200
        assert cp.records["platform.interaction"][1] == 300
```

- [ ] **Step 2: Run test — expect failure**

```bash
mkdir -p shared_src/tests/projection shared_src/projection
cd /root/logoscope && python -m pytest shared_src/tests/projection/test_checkpoint.py -v
```

- [ ] **Step 3: Implementation**

```python
# shared_src/projection/checkpoint.py
from dataclasses import dataclass, field
from typing import Dict, Optional
from datetime import datetime


@dataclass
class ProjectionCheckpoint:
    """Projection 的消费进度——基于 partition + offset。"""
    projection: str
    epoch: str
    records: Dict[str, Dict[int, int]] = field(default_factory=dict)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def update(self, topic: str, partition: int, offset: int):
        if topic not in self.records:
            self.records[topic] = {}
        current = self.records[topic].get(partition, -1)
        if offset > current:
            self.records[topic][partition] = offset
            self.updated_at = datetime.utcnow()

    def get_lag(self, topic: str, partition: int,
                latest_offset: int) -> int:
        current = self.records.get(topic, {}).get(partition, 0)
        return latest_offset - current

    def total_lag(self, topic_latest: Dict[str, Dict[int, int]]) -> int:
        total = 0
        for topic, partitions in topic_latest.items():
            for partition, latest in partitions.items():
                total += self.get_lag(topic, partition, latest)
        return total

    def __repr__(self) -> str:
        total = sum(
            len(parts)
            for parts in self.records.values()
        )
        return f"Checkpoint({self.projection}, epoch={self.epoch}, {total} partitions)"
```

```python
# shared_src/projection/__init__.py
from .checkpoint import ProjectionCheckpoint

__all__ = ["ProjectionCheckpoint"]
```

- [ ] **Step 4: Run tests — verify pass**

```bash
cd /root/logoscope && python -m pytest shared_src/tests/projection/ -v
```
Expected: 4 PASSED

- [ ] **Step 5: Commit**

```bash
git add shared_src/projection/ shared_src/tests/projection/
git commit -m "feat(projection): add ProjectionCheckpoint with offset management

- 基于 partition+offset 的消费进度跟踪
- offset 严格递增，不回退
- 支持多 topic 多 partition
- lag 计算

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 0.6: Projection base class + ProjectionStatus

**Files:**
- Modify: `shared_src/projection/__init__.py`
- Create: `shared_src/projection/base.py`
- Create: `shared_src/tests/projection/test_base.py`

**Interfaces:**
- Produces: `Projection` (ABC), `ProjectionStatus`
- Consumes: `EventEnvelope`, `ProjectionCheckpoint`

- [ ] **Step 1: Write failing test**

```python
# shared_src/tests/projection/test_base.py
import pytest
from datetime import datetime
from shared_src.projection.base import Projection, ProjectionStatus
from shared_src.projection.checkpoint import ProjectionCheckpoint
from shared_src.event.envelope import EventEnvelope


class MockProjection(Projection):
    name = "mock"
    epoch = "20260701"

    @property
    def upstream_topics(self):
        return ["platform.test"]

    def apply(self, envelope: EventEnvelope):
        self._applied += 1

    def rebuild(self, event_source):
        for env in event_source:
            self.apply(env)

    def checkpoint(self):
        return ProjectionCheckpoint(projection=self.name, epoch=self.epoch)

    def status(self):
        cp = self.checkpoint()
        return ProjectionStatus(
            projection_epoch=self.epoch,
            event_count=self._applied,
            checkpoint=cp,
        )


class TestProjectionBase:
    def test_projection_interface(self):
        proj = MockProjection()
        assert proj.name == "mock"
        assert proj.epoch == "20260701"
        assert proj.upstream_topics == ["platform.test"]

    def test_apply_and_status(self):
        proj = MockProjection()
        proj.apply(EventEnvelope(event_id="e1"))
        assert proj.status().event_count == 1

    def test_rebuild(self):
        proj = MockProjection()
        events = [EventEnvelope(event_id=f"e{i}") for i in range(3)]
        proj.rebuild(events)
        assert proj.status().event_count == 3
```

- [ ] **Step 2: Implementation**

```python
# shared_src/projection/base.py
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any
from datetime import datetime
from ..event.envelope import EventEnvelope
from .checkpoint import ProjectionCheckpoint


@dataclass
class ProjectionStatus:
    projection_epoch: str
    event_count: int
    checkpoint: ProjectionCheckpoint
    is_rebuilding: bool = False
    rebuild_progress: float = 0.0
    lag: int = 0


class Projection(ABC):
    """所有 Projection 的统一基类。"""

    name: str = ""
    epoch: str = ""

    @property
    def upstream_topics(self) -> List[str]:
        return []

    @abstractmethod
    def apply(self, envelope: EventEnvelope):
        ...

    @abstractmethod
    def rebuild(self, event_source: Any):
        ...

    @abstractmethod
    def checkpoint(self) -> ProjectionCheckpoint:
        ...

    @abstractmethod
    def status(self) -> ProjectionStatus:
        ...
```

- [ ] **Step 3: Run tests — verify pass**

```bash
cd /root/logoscope && python -m pytest shared_src/tests/projection/ -v
```

- [ ] **Step 4: Commit**

```bash
git add shared_src/projection/base.py shared_src/projection/__init__.py shared_src/tests/projection/test_base.py
git commit -m "feat(projection): add Projection ABC with status tracking

- Projection 抽象基类（name, epoch, apply, rebuild, checkpoint, status）
- ProjectionStatus dataclass
- MockProjection 测试实现

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Phase 0 Gate Checklist

Before proceeding to Phase 1, verify:

- [ ] `shared_src/event/` package with EventEnvelope, SchemaRegistry, RawEventStore, EventBus
- [ ] All 15+ Phase 0 tests pass
- [ ] Ingest Service writes platform.raw topic with EventEnvelope format
- [ ] `shared_src/projection/` package with Projection ABC and Checkpoint
- [ ] `git log` shows 6 commits for Phase 0

---
&nbsp;
# Phase 1: Event Pipeline + Semantic Engine

**Goal:** Event Pipeline (Aggregate/Dedup/Sample/Enrich/Route) + Semantic Engine refactored to consume EventEnvelope from platform.raw.

**Dependencies:** Phase 0 (EventEnvelope, SchemaRegistry, EventBus)

---

### Task 1.1: Event Pipeline Processor chain

**Files:**
- Create: `shared_src/pipeline/__init__.py`
- Create: `shared_src/pipeline/processors.py`
- Create: `shared_src/pipeline/config.py`
- Create: `shared_src/tests/pipeline/test_processors.py`

**Interfaces:**
- Produces: `PipelineProcessor` (ABC), `EventPipeline`, `AggregateProcessor`, `DedupProcessor`, `SampleProcessor`, `EnrichProcessor`, `RouteProcessor`

- [ ] **Step 1-6: (TDD cycle)** Write tests for each processor, implement, pass, commit.

Key test cases:
```python
def test_aggregate_traceback():
    """AggregateProcessor 聚合 Python Traceback"""
    pipeline = EventPipeline([AggregateProcessor(window_seconds=5)])
    line1 = RawEvent(raw_payload="Traceback (most recent call last):")
    line2 = RawEvent(raw_payload="  File \"main.py\", line 10, in foo")
    line3 = RawEvent(raw_payload="Exception: OOM")
    assert len(pipeline.execute(line1)) == 0   # buffer
    assert len(pipeline.execute(line2)) == 0   # buffer
    assert len(pipeline.execute(line3)) == 1   # 完整 traceback

def test_dedup_exponential_backoff():
    pipeline = EventPipeline([DedupProcessor(initial_window_ms=5000)])
    err = RawEvent(raw_payload="ERROR: Connection refused")
    assert len(pipeline.execute(err)) == 1     # 第一个
    assert len(pipeline.execute(err)) == 0     # 窗口内聚合
    assert len(pipeline.execute(err)) == 0     # 继续聚合

def test_sample_info_one_percent():
    pipeline = EventPipeline([SampleProcessor({"INFO": 1.0})])  # 100% 采样便于测试
    events = [RawEvent(raw_payload=f"INFO msg {i}") for i in range(100)]
    results = [e for ev in events for e in pipeline.execute(ev)]
    assert len(results) == 100  # 100% 采样率

def test_enrich_host_to_az():
    pipeline = EventPipeline([EnrichProcessor(host_map={"compute-01": "az-1"})])
    raw = RawEvent(host="compute-01")
    result = pipeline.execute(raw)
    assert "az-1" in result[0].labels_json

def test_route_openstack():
    pipeline = EventPipeline([RouteProcessor()])
    raw = RawEvent(raw_payload="nova-api: ERROR")
    result = pipeline.execute(raw)
    assert result[0].metadata.get("platform") == "openstack"
```

### Task 1.2: Refactor Semantic Engine to consume EventEnvelope

**Files:**
- Modify: `semantic-engine/msgqueue/worker.py` — consume from `platform.raw`, produce to `platform.normalized` with EventEnvelope
- Modify: `semantic-engine/normalize/normalizer.py` — ensure it produces `NormalizedEvent`-compatible dict
- Create: `semantic-engine/engine.py` — SemanticEngine class
- Create: `semantic-engine/tests/test_engine.py`

**Key test:**
```python
def test_semantic_engine_produces_envelope():
    engine = SemanticEngine(schema_registry, bus)
    raw_env = EventEnvelope(
        event_type="raw.log",
        payload=json.dumps({
            "raw_id": "r1",
            "raw_payload": "nova-api: ERROR Connection refused",
            "service_name": "nova-api",
            "timestamp": "2026-07-01T12:00:00",
        }).encode(),
    )
    output = engine.process(raw_env)
    assert output.event_type == "normalized.event"
    assert output.producer == "semantic-engine"
    assert output.parent_event_ids == [raw_env.event_id]
```

### Task 1.3: VersionedProjectionRegistry

**Files:**
- Create: `shared_src/projection/versioned.py`
- Create: `shared_src/tests/projection/test_versioned.py`

**Key tests:**
```python
def test_traffic_split():
    registry = VersionedProjectionRegistry("graph")
    v1 = MockProjection("hashmap")
    v2 = MockProjection("list")
    registry.add_version(v1, traffic=0.9)
    registry.add_version(v2, traffic=0.1)

    calls_v1 = sum(1 for _ in range(1000) if v1 in registry.route(MockEvent()))
    assert 800 < calls_v1 < 1000

def test_promote():
    registry = VersionedProjectionRegistry("graph")
    registry.add_version(v1, traffic=0.0)
    registry.add_version(v2, traffic=1.0)
    registry.promote("list")
    assert registry._traffic["list"] == 1.0

def test_compare_results():
    registry = VersionedProjectionRegistry("graph")
    registry.add_version(v1)
    registry.add_version(v2)
    results = registry.compare_results(MockEvent())
    assert "hashmap" in results
    assert "list" in results
```

---
&nbsp;
# Phase 2: Entity + State Projection

**Goal:** EntityProjector + StateProjector + InventoryProjection + StateProjection + Timeline MV

**Dependencies:** Phase 1 (Event Pipeline, Semantic Engine)

---

### Task 2.1: EntityProjector

**Files:**
- Create: `semantic-engine/projectors/__init__.py`
- Create: `semantic-engine/projectors/entity_projector.py`
- Create: `semantic-engine/tests/projectors/test_entity_projector.py`

**Key test:**
```python
def test_entity_projector_produces_entity_event():
    projector = EntityProjector(schema_registry, bus)
    norm_event = NormalizedEvent(
        event_id="n1",
        entities=[ResourceIdentity(ResourceType.INSTANCE, "abc-123")],
    )
    projector.process(wrap_in_envelope(norm_event))
    # 验证 bus 收到 platform.entity 消息
    assert any(
        env.event_type == "entity.seen"
        for env in bus._history.get("platform.entity", [])
    )
```

### Task 2.2: InventoryProjection

**Files:**
- Create: `semantic-engine/projections/__init__.py`
- Create: `semantic-engine/projections/inventory_projection.py`
- Create: `semantic-engine/tests/projections/test_inventory_projection.py`

**Key test:**
```python
def test_inventory_rebuild():
    projection = InventoryProjection(epoch="20260701")
    events = [
        entity_event("INSTANCE", "abc-123", "nova-api"),
        entity_event("INSTANCE", "abc-456", "neutron-server"),
    ]
    for e in events:
        projection.apply(e)
    assert projection.query(ResourceType.INSTANCE, "abc-123") is not None
    projection._clear_all()
    assert projection.query(ResourceType.INSTANCE, "abc-123") is None
    projection.rebuild(events, checkpoint)
    assert projection.query(ResourceType.INSTANCE, "abc-123") is not None
```

### Task 2.3: StateProjector + StateProjection

**Files:**
- Create: `semantic-engine/projectors/state_projector.py`
- Create: `semantic-engine/projections/state_projection.py`
- Create: `semantic-engine/tests/projectors/test_state_projector.py`
- Create: `semantic-engine/tests/projections/test_state_projection.py`

**Key test:**
```python
def test_state_ttl_expiry():
    projection = StateProjection()
    env = state_event("INSTANCE:abc-123", "status", "ACTIVE", ttl=60)
    projection.apply(env)
    assert projection.query(ResourceType.INSTANCE, "abc-123", "status") == "ACTIVE"
    with freeze_time(now + timedelta(seconds=70)):
        assert projection.query(ResourceType.INSTANCE, "abc-123", "status") is None
```

### Task 2.4: Timeline Projection (ClickHouse MV)

**Files:**
- Create: `deploy/clickhouse-ddl/010_timeline_mv.sql`
- Modify: `shared_src/projection/timeline.py` — query wrapper

```sql
-- deploy/clickhouse-ddl/010_timeline_mv.sql
CREATE MATERIALIZED VIEW IF NOT EXISTS logs.timeline_mv
ENGINE = MergeTree()
ORDER BY (entity_id, toStartOfMinute(timestamp))
POPULATE AS
SELECT
    entity_id,
    timestamp,
    event_id,
    service_name,
    event_category,
    severity,
    message
FROM logs.events
WHERE entity_id != ''
```

---
&nbsp;
# Phase 3: Interaction + Correlation

**Goal:** InteractionProjector + CorrelationEngine + DynamicRelProjection (ClickHouse)

**Dependencies:** Phase 2 (Entity/State Projection produces platform.entity/state)

---

### Task 3.1: InteractionProjector

**Files:**
- Create: `semantic-engine/projectors/interaction_projector.py`
- Create: `semantic-engine/tests/projectors/test_interaction_projector.py`

**Key test:**
```python
def test_interaction_from_endpoints():
    projector = InteractionProjector(...)
    event = NormalizedEvent(
        participants=[
            InteractionEndpoint(ResourceIdentity(ResourceType.INSTANCE, "vm-1"), "source"),
            InteractionEndpoint(ResourceIdentity(ResourceType.HOST, "compute-01"), "target"),
        ],
    )
    projector.process(wrap(event))
    # 验证 platform.interaction 收到消息
```

### Task 3.2: CorrelationEngine

**Files:**
- Modify: `semantic-engine/correlate/correlator.py`
- Create: `semantic-engine/correlate/dynamic_rel_projection.py`
- Create: `semantic-engine/tests/correlate/test_correlation_engine.py`

**Key test:**
```python
def test_dynamic_rel_time_window():
    rel_proj = DynamicRelProjection(...)
    for i in range(100):
        rel_proj.apply(interaction_envelope)
    trend = rel_proj.query_trend("A", "B",
                windows=["1 HOUR", "6 HOUR", "24 HOUR"])
    assert len(trend) == 3
```

### Task 3.3: Lineage API

**Files:**
- Create: `semantic-engine/api/lineage.py`
- Test: `semantic-engine/tests/test_lineage_api.py`

```python
def test_lineage_trace():
    client.get(f"/api/v1/lineage/trace/finding-001")
    # 返回完整血缘链 DAG
    assert response.status_code == 200
    assert "raw-001" in str(response.json())
```

---
&nbsp;
# Phase 4: Graph + WorldView Facade + ContextAPI

**Goal:** GraphProjection (topology-only, no state) + WorldView Facade (TopologyQuery / StateQuery / HistoryQuery) + ContextAPI (CanonicalContextHasher RFC 8785 + Merkle DAG) + Snapshot belongs to Projection Layer

**Dependencies:** Phase 2-3 (entity, state, interaction events available)

**v12-v15 changes applied:**
- WorldView is **Facade** — combines TopologyQuery + StateQuery + HistoryQuery, no God Object
- **StateProjectionRef** — Context references Projection via `projection_epoch`, no data copy in ContextResult
- **CanonicalContextHasher** — RFC 8785 deterministic JSON + Merkle DAG for content-based hash
- **Snapshot** belongs to Projection Layer, `use_snapshot=False` by default

---

### Task 4.1: GraphProjection (topology-only, no state)

**Files:**
- Create: `semantic-engine/projections/graph_projection.py`
- Create: `semantic-engine/tests/projections/test_graph_projection.py`

**Key test:**
```python
def test_graph_no_state():
    graph = GraphProjection()
    graph.apply_entity(entity_envelope)        # "INSTANCE:abc-123"
    graph.apply_interaction(interaction_env)    # INSTANCE → HOST edge
    subgraph = graph.get_subgraph("INSTANCE:abc-123")
    assert all(n.entity_id for n in subgraph.nodes)
    assert not any("status" in n.attributes for n in subgraph.nodes)
```

### Task 4.2: TopologyQuery — DAG-based Topology Reader

**Files:**
- Create: `shared_src/worldview/__init__.py`
- Create: `shared_src/worldview/topology_query.py` — TopologyQuery
- Create: `shared_src/tests/worldview/test_topology_query.py`

**Interfaces:**
- Produces: `TopologyQuery` — depends on GraphProjection
- Consumes: `GraphProjection` from Task 4.1

**Key tests:**
```python
def test_get_dependents():
    tq = TopologyQuery(graph_projection)
    deps = tq.get_dependents(ResourceIdentity(ResourceType.SERVICE, "rabbitmq"))
    assert len(deps) >= 3

def test_get_impact_set_bfs_layers():
    tq = TopologyQuery(graph_projection)
    layers = tq.get_impact_set(ResourceIdentity(ResourceType.SERVICE, "rabbitmq"), depth=3)
    assert len(layers) <= 3  # BFS layers
    assert all(isinstance(r, ResourceIdentity) for layer in layers for r in layer)

def test_query_path():
    tq = TopologyQuery(graph_projection)
    path = tq.query_path(ResourceIdentity(ResourceType.INSTANCE, "vm-1"),
                          ResourceIdentity(ResourceType.HOST, "compute-01"))
    assert len(path) >= 1
```

### Task 4.3: StateQuery — State + Timeline Reader

**Files:**
- Create: `shared_src/worldview/state_query.py` — StateQuery
- Create: `shared_src/tests/worldview/test_state_query.py`

**Interfaces:**
- Produces: `StateQuery` — depends on `StateProjection` + `TimelineProjection`
- Consumes: `StateProjection` (Task 2.3), `TimelineProjection` (Task 2.4)

**Key tests:**
```python
def test_get_state():
    sq = StateQuery(state_projection, timeline_projection)
    state = sq.get_state(ResourceIdentity(ResourceType.INSTANCE, "vm-1"))
    assert state in ("ACTIVE", "ERROR", "SHUTOFF", "BUILD", None)

def test_resolve_field():
    """resolve_field 供 Expression.evaluate() 使用"""
    sq = StateQuery(mock_state, mock_timeline)
    value = sq.resolve_field("resource.status",
                              ResourceIdentity(ResourceType.INSTANCE, "vm-1"))
    assert value in ("ACTIVE", "ERROR", "SHUTOFF")

def test_get_timeline():
    sq = StateQuery(mock_state, mock_timeline)
    timeline = sq.get_timeline(ResourceIdentity(ResourceType.INSTANCE, "vm-1"), "1 HOUR")
    assert all(hasattr(t, "from_state") for t in timeline)
```

### Task 4.4: HistoryQuery — Recent Events Reader

**Files:**
- Create: `shared_src/worldview/history_query.py` — HistoryQuery
- Create: `shared_src/tests/worldview/test_history_query.py`

**Interfaces:**
- Produces: `HistoryQuery` — depends on RawEventStore
- Consumes: `RawEventStore` (Task 0.3)

**Key tests:**
```python
def test_get_recent_events():
    hq = HistoryQuery(event_store)
    events = hq.get_recent_events(ResourceIdentity(ResourceType.SERVICE, "nova-api"), count=10)
    assert len(events) <= 10

def test_get_alarms():
    hq = HistoryQuery(event_store)
    alarms = hq.get_alarms(ResourceIdentity(ResourceType.SERVICE, "rabbitmq"))
    assert isinstance(alarms, list)
```

### Task 4.5: WorldView Facade — Unified Entry Point

**Files:**
- Create: `shared_src/worldview/facade.py` — WorldView Facade
- Modify: `shared_src/worldview/__init__.py`

**Interfaces:**
- Produces: `WorldView(topology, state, history)` — pure Facade, no query logic

**Key tests:**
```python
def test_worldview_is_pure_facade():
    """WorldView 是 Facade，不含查询实现"""
    wv = WorldView(topology=mock_topology, state=mock_state, history=mock_history)
    assert hasattr(wv, "topology") and hasattr(wv, "state") and hasattr(wv, "history")
    # WorldView 自身没有方法（除 __init__ 外）

def test_worldview_delegates():
    """Facade 委派给具体 Query 类"""
    wv = WorldView(topology=mock_topology, state=mock_state, history=mock_history)
    wv.topology.get_dependents(rid)
    wv.state.get_state(rid)
    wv.history.get_recent_events(rid)
```

### Task 4.6: CanonicalContextHasher (RFC 8785 + Merkle DAG)

**Files:**
- Create: `shared_src/context/hasher.py` — CanonicalContextHasher
- Create: `shared_src/tests/context/test_hasher.py`

**Interfaces:**
- Produces: `CanonicalContextHasher.hash(content) -> str` — deterministic sha256
- Produces: `CanonicalContextHasher.merkle_hash(parts) -> str` — Merkle DAG of content parts
- Consumes: Context content dicts, no timestamp component in hash input

**Key tests:**
```python
def test_canonical_hash_deterministic():
    """相同输入始终产出相同 hash"""
    hasher = CanonicalContextHasher()
    h1 = hasher.hash({"resource": "INSTANCE:abc-123", "state": "ACTIVE"})
    h2 = hasher.hash({"resource": "INSTANCE:abc-123", "state": "ACTIVE"})
    assert h1 == h2

def test_canonical_hash_no_timestamp():
    """hash 不含时间戳组件"""
    hasher = CanonicalContextHasher()
    h1 = hasher.hash({"resource": "INSTANCE:abc-123"})
    h2 = hasher.hash({"resource": "INSTANCE:abc-123", "ts": "ignored"})
    # 只使用明确声明的 content 字段

def test_merkle_hash():
    """Merkle DAG 模式——部分内容变更不全局失效"""
    hasher = CanonicalContextHasher()
    root = hasher.merkle_hash({
        "topology": topo_hash,
        "state": state_hash,
        "knowledge": knowledge_hash,
    })
    assert root.startswith("ctx_")

def test_rfc8785_sort_keys():
    """RFC 8785 确定性的 JSON 序列化——字典 key 排序"""
    hasher = CanonicalContextHasher()
    h1 = hasher.hash({"z": 1, "a": 2})
    h2 = hasher.hash({"a": 2, "z": 1})
    assert h1 == h2  # 排序后相同
```

### Task 4.7: ContextAPI + ContextResult (with StateProjectionRef)

**Files:**
- Create: `shared_src/context/__init__.py`
- Create: `shared_src/context/api.py` — ContextAPI, ContextResult, ContextType
- Create: `shared_src/context/builders.py` — Incident/Topology/Workflow/Rule builders
- Create: `shared_src/context/snapshot.py` — ContextSnapshot (在 Projection Layer)
- Create: `shared_src/tests/context/test_context_api.py`

**Interfaces:**
- Produces: `ContextAPI`, `ContextResult(context_hash, projection_epoch, knowledge_refs)`
- Consumes: `WorldView Facade` (Task 4.5), `CanonicalContextHasher` (Task 4.6)

**Key tests:**
```python
def test_context_hash_stable():
    """相同输入始终产出相同 context_hash"""
    api = ContextAPI(canonical_hasher)
    r1 = api.build(ResourceType.INSTANCE, "abc-123")
    r2 = api.build(ResourceType.INSTANCE, "abc-123")
    assert r1.context_hash == r2.context_hash

def test_context_result_has_projection_epoch():
    """ContextResult 引用 Projection epoch，不包含 data copy"""
    api = ContextAPI(...)
    result = api.build(ResourceType.INSTANCE, "abc-123")
    assert result.projection_epoch != ""
    # 不包含 projection 数据本身
    assert not hasattr(result, "projection_data")

def test_context_result_knowledge_refs():
    """knowledge_refs 记录 [(doc_id, version), ...]"""
    api = ContextAPI(knowledge_store)
    result = api.build(ResourceType.INSTANCE, "abc-123")
    for ref in result.knowledge_refs:
        assert len(ref) == 2  # (doc_id, version)

def test_context_api_uses_worldview():
    """ContextAPI 内部使用 WorldView 查询状态"""
    api = ContextAPI(...)
    result = api.build(ResourceType.INSTANCE, "abc-123")
    mock_topology.get_dependents.assert_called()

def test_context_api_multiple_types():
    api = ContextAPI(...)
    incident = api.build(type=INSTANCE, id="abc-123", context_type=INCIDENT)
    topo = api.build(type=INSTANCE, id="abc-123", context_type=TOPOLOGY)
    assert isinstance(incident.context, IncidentContext)
    assert isinstance(topo.context, TopologyContext)

def test_context_api_hides_storage():
    """Context API 隐藏所有存储实现"""
    result = api.build(type=INSTANCE, id="abc-123")
    assert not hasattr(result.context, "neo4j_query")
    assert not hasattr(result.context, "clickhouse_sql")

def test_context_snapshot_not_default():
    """Snapshot 默认不生成（use_snapshot=False）"""
    api = ContextAPI(...)
    result = api.build(type=INSTANCE, id="abc-123")
    assert result.snapshot_id == ""

def test_context_snapshot_when_requested():
    api = ContextAPI(...)
    result = api.build(type=INSTANCE, id="abc-123", use_snapshot=True)
    assert result.snapshot_id != ""
```

### Task 4.8: WorldView + ContextAPI Endpoints

**Files:**
- Create: `query-service/api/worldview.py` — WorldView HTTP routes (Facade endpoints)
- Create: `query-service/tests/api/test_worldview_api.py`

**Key tests:**
```python
def test_topology_endpoint():
    client.get("/api/v1/worldview/topology/dependents?type=SERVICE&id=rabbitmq")
    assert response.status_code == 200

def test_state_endpoint():
    client.get("/api/v1/worldview/state/current?type=INSTANCE&id=vm-1")
    assert response.status_code == 200

def test_history_endpoint():
    client.get("/api/v1/worldview/history/events?type=SERVICE&id=nova-api&count=50")
    assert response.status_code == 200

def test_expressions_evaluate():
    """Expression 求值端点（v15 新增）"""
    client.post("/api/v1/expressions/evaluate", json={
        "field": "resource.status", "operator": "==", "value": "ACTIVE",
        "target": {"type": "INSTANCE", "id": "vm-1"},
    })
    assert response.status_code == 200
```

---
&nbsp;
# Phase 5: Goal Tree + Expression + Knowledge + Inference

**Goal:** Goal Tree (desired_state, no Workflow concepts) + Expression type (structured precondition) + Knowledge Object Model (SOP/Runbook/FailurePattern/Incident/RCA) + InferenceRegistry + InferencePipeline (Preprocess→Prompt→Engine→PostProcess→Validate→Normalize) + Planner (GoalInferrer → IntentGenerator)

**Dependencies:** Phase 4 (WorldView provides query for GoalInferrer + Expression evaluation)

**v12-v15 changes applied:**
- **Goal** describes desired_state (not Workflow) — no action/ordering/completion
- **Expression** — structured `(field, operator, value)`, not strings
- **IntentGenerator** — Generator outputs *what* (PlanIntent), WorkflowComposer converts Intent → Workflow
- **GoalInferrer** — Finding → Goal Tree (target state tree)
- **InferenceRegistry** — maps event_type → InferencePipeline
- **InferencePipeline** — Preprocess → Prompt → Engine → PostProcess → Validate → Normalize
- **Knowledge Object Model** — typed documents: SOP / Runbook / FailurePattern / Incident / RCA
- **Planner** — uses Goal Tree, generates intents (not directly candidates)

---

### Task 5.1: Expression dataclass + evaluate()

**Files:**
- Create: `shared_src/expression/__init__.py`
- Create: `shared_src/expression/models.py` — Expression dataclass
- Create: `shared_src/tests/expression/test_expression.py`

**Interfaces:**
- Produces: `Expression(field, operator, value).evaluate(worldview, target) -> bool`
- Consumes: `WorldView` (Task 4.5)

**Key tests:**
```python
def test_expression_evaluate_eq():
    """Expression == 操作符"""
    worldview = WorldView(topology=..., state=StateQuery(mock_state, ...), ...)
    expr = Expression("resource.status", "==", "ACTIVE")
    mock_state.resolve_field.return_value = "ACTIVE"
    assert expr.evaluate(worldview, target) == True
    mock_state.resolve_field.return_value = "ERROR"
    assert expr.evaluate(worldview, target) == False

def test_expression_exists():
    expr = Expression("ssh.accessible", "exists")
    worldview.state.resolve_field.return_value = True
    assert expr.evaluate(worldview, target) == True
    worldview.state.resolve_field.return_value = None
    assert expr.evaluate(worldview, target) == False

def test_expression_contains():
    expr = Expression("service.tags", "contains", "production")
    worldview.state.resolve_field.return_value = ["production", "rabbitmq"]
    assert expr.evaluate(worldview, target) == True

def test_predefined_expressions():
    """预定义 Expression 工厂函数"""
    assert expr_status_eq("ACTIVE").field == "resource.status"
    assert expr_host_alive().value == "alive"
    assert expr_service_exists().operator == "=="
```

### Task 5.2: Knowledge Object Model + Knowledge & Memory Store

**Files:**
- Create: `shared_src/knowledge/__init__.py`
- Create: `shared_src/knowledge/models.py` — KnowledgeDocument, SOP, Runbook, FailurePattern, Incident, RCA
- Create: `shared_src/knowledge/store.py` — KnowledgeMemoryStore
- Create: `shared_src/knowledge/memory.py` — MemoryRecord
- Create: `shared_src/tests/knowledge/test_models.py`
- Create: `shared_src/tests/knowledge/test_store.py`

**Key tests:**
```python
def test_knowledge_object_types():
    """不同类型知识对象有独立字段"""
    sop = SOP(document_id="sop-001", title="Restart RabbitMQ",
              steps=["Check status", "Restart service", "Verify"])
    runbook = Runbook(document_id="rb-001", title="RabbitMQ Recovery",
                       category="messaging", severity="P1")
    fp = FailurePattern(document_id="fp-001", title="RabbitMQ heartbeat lost",
                         symptoms=["heartbeat timeout"], root_cause="network partition")
    assert sop.document_type == "sop"
    assert runbook.document_type == "runbook"
    assert fp.document_type == "failure_pattern"

def test_knowledge_retrieval():
    store = KnowledgeMemoryStore()
    store.add_document(SOP(
        document_id="sop-001",
        title="Nova OOM Troubleshooting",
        steps=["Check memory usage", "Migrate VMs"],
    ))
    results = store.retrieve("nova OOM")
    assert len(results) > 0

def test_memory_write_and_retrieve():
    store = KnowledgeMemoryStore()
    store.add_memory(MemoryRecord(
        record_id="m1", record_type="repair", outcome="success",
        action_taken="restart neutron-dhcp-agent",
    ))
    results = store.retrieve("neutron agent failure")
    assert results[0].action_taken == "restart neutron-dhcp-agent"

def test_knowledge_provenance():
    doc = SOP(document_id="kb-001", title="OpenStack Wallaby Admin Guide",
              origin="openstack-official", version="wallaby-2025-12", trust_level=5)
    assert doc.trust_level == 5
```

### Task 5.3: InferenceRegistry + InferencePipeline

**Files:**
- Create: `semantic-engine/inference/__init__.py`
- Create: `semantic-engine/inference/registry.py` — InferenceRegistry (maps event_type → Pipeline)
- Create: `semantic-engine/inference/pipeline.py` — InferencePipeline (5-stage chain)
- Create: `semantic-engine/inference/models.py` — InferenceInput, InferenceOutput, InferenceContext
- Create: `semantic-engine/inference/finding.py` — Finding (no recommended_action in v15)
- Create: `semantic-engine/tests/inference/test_registry.py`
- Create: `semantic-engine/tests/inference/test_pipeline.py`

**Interfaces:**
- Produces: `InferenceRegistry.register(event_type, pipeline)`, `pipeline.run(input) -> Finding[]`
- Consumes: `WorldView` (Task 4.5), `KnowledgeMemoryStore` (Task 5.2)

**Key tests:**
```python
def test_inference_registry():
    """Registry 按 event_type 分配 Pipeline"""
    registry = InferenceRegistry()
    pipeline = InferencePipeline(preprocessor, engine, validator)
    registry.register("normalized.event", pipeline)
    assert registry.get("normalized.event") == pipeline

def test_inference_pipeline_stages():
    """Pipeline 5 阶段完整执行"""
    pipeline = InferencePipeline(
        preprocessor=MockPreprocessor(),
        llm_engine=MockLLMEngine(),
        postprocessor=MockPostprocessor(),
        validator=MockValidator(),
        normalizer=MockNormalizer(),
    )
    result = pipeline.run(InferenceInput(context=ctx, knowledge=knowledge))
    assert len(result.findings) > 0

def test_finding_no_recommended_action():
    """v15: Finding 不含 recommended_action（由 Planner 从 Goal 推导）"""
    finding = Finding(category="RabbitMQHeartbeatLost", confidence=0.91)
    assert not hasattr(finding, "recommended_action")

def test_finding_has_knowledge_refs():
    """Finding 记录知识引用"""
    finding = Finding(category="RabbitMQHeartbeatLost",
                       knowledge_refs=[("kb-001", "v3"), ("kb-007", "v5")])
    assert len(finding.knowledge_refs) == 2

def test_finding_has_context_hash():
    """Finding 引用 context_hash"""
    finding = Finding(category="RabbitMQHeartbeatLost", context_hash="ctx_abc123")
    assert finding.context_hash == "ctx_abc123"
```

### Task 5.4: GoalNode + Goal (desired_state only)

**Files:**
- Create: `shared_src/goal/__init__.py`
- Create: `shared_src/goal/models.py` — GoalNode, Goal
- Create: `shared_src/tests/goal/test_goal_models.py`

**Interfaces:**
- Produces: `GoalNode(goal_id, desired_state, target, children)` — no action/ordering/completion
- Produces: `Goal(primary, tree, priority, reason)`

**Key tests:**
```python
def test_goal_desired_state():
    """GoalNode 只描述目标状态"""
    node = GoalNode(goal_id="g1", desired_state="RabbitMQ.healthy",
                     target=ResourceIdentity(ResourceType.SERVICE, "rabbitmq"))
    assert not hasattr(node, "action")       # 不含 Workflow 概念
    assert not hasattr(node, "ordering")
    assert not hasattr(node, "completion_criteria")
    assert not hasattr(node, "status")
    assert node.desired_state == "RabbitMQ.healthy"

def test_goal_nested_tree():
    """Goal 支持目标状态树"""
    goal = Goal(
        primary="restore_messaging",
        tree=GoalNode(goal_id="root", desired_state="Cluster.healthy", target=cluster,
                       children=[
                           GoalNode(goal_id="mq", desired_state="RabbitMQ.healthy", target=svc),
                       ]),
        priority=90,
    )
    assert len(goal.tree.children) == 1
    assert goal.tree.children[0].desired_state == "RabbitMQ.healthy"

def test_goal_no_workflow_fields():
    """Goal 不含 Workflow 字段"""
    goal = Goal(primary="restore_messaging",
                 tree=GoalNode(goal_id="root", desired_state="healthy", target=t))
    assert not hasattr(goal, "steps")
    assert not hasattr(goal, "ordering")
```

### Task 5.5: Constraint Knowledge (Expression-based)

**Files:**
- Create: `semantic-engine/knowledge/constraint.py` — Constraint
- Create: `semantic-engine/tests/knowledge/test_constraint.py`

**Key tests:**
```python
def test_constraint_uses_expression():
    """Constraint 使用 Expression 表达条件"""
    constraint = Constraint(
        constraint_id="c-001",
        applies_to="restart_service",
        condition=Expression("time.hour", "in", [9, 10, 11, 12, 13, 14, 15, 16, 17]),
        restriction="No restart during business hours",
        severity="warning",
    )
    assert isinstance(constraint.condition, Expression)
    assert constraint.severity == "warning"
```

### Task 5.6: GoalInferrer + IntentGenerator

**Files:**
- Create: `semantic-engine/planner/__init__.py`
- Create: `semantic-engine/planner/goal_inferrer.py` — GoalInferrer (Finding → Goal Tree)
- Create: `semantic-engine/planner/intent_generator.py` — IntentGenerator ABC + subclasses
- Create: `semantic-engine/planner/models.py` — PlanIntent
- Create: `semantic-engine/tests/planner/test_goal_inferrer.py`
- Create: `semantic-engine/tests/planner/test_intent_generator.py`

**Interfaces:**
- Consumes: `WorldView` (Task 4.5), `Finding` (Task 5.3), `Goal` models (Task 5.4)
- Produces: `GoalInferrer.infer(finding, context, worldview) -> Goal`
- Produces: `IntentGenerator.can_handle(finding, goal_node, worldview) -> bool`
- Produces: `IntentGenerator.generate(finding, goal_node, worldview) -> Optional[PlanIntent]`

**Key tests:**
```python
def test_goal_inferrer_produces_state_tree():
    """GoalInferrer 产出目标状态树"""
    goal = GoalInferrer().infer(finding, context, worldview)
    assert goal.tree.desired_state is not None
    for child in goal.tree.children:
        assert "healthy" in child.desired_state or "responding" in child.desired_state

def test_intent_generator_matches_desired_state():
    """IntentGenerator 按目标状态匹配"""
    gen = RestartIntentGenerator(...)
    node = GoalNode(goal_id="g1", desired_state="RabbitMQ.healthy", target=...)
    assert gen.can_handle(mock_finding, node, worldview)
    node2 = GoalNode(goal_id="g2", desired_state="evidence_collected", target=...)
    assert not gen.can_handle(mock_finding, node2, worldview)

def test_intent_generator_outputs_plan_intent():
    """Generator 输出 PlanIntent（what, not how）"""
    gen = RestartIntentGenerator(...)
    intent = gen.generate(mock_finding, goal_node, worldview)
    assert intent is not None
    assert intent.action == "restart_service"  # what
    assert intent.target is not None            # on what
    assert not hasattr(intent, "steps")         # not how (WorkflowComposer 负责)

def test_diagnostic_generator_low_confidence():
    """低置信度 Finding 触发诊断 Intent"""
    gen = DiagnosticIntentGenerator(...)
    finding = Finding(category="unknown", confidence=0.3)
    assert gen.can_handle(finding, diagnosis_node, worldview)
```

### Task 5.7: Planner (GoalInferrer + IntentGenerator)

**Files:**
- Create: `semantic-engine/planner/planner.py` — Planner (orchestrates GoalInferrer + IntentGenerators)
- Create: `semantic-engine/planner/result.py` — PlannerResult
- Create: `semantic-engine/tests/planner/test_planner.py`

**Key tests:**
```python
def test_planner_generates_intents():
    """Planner 从 Finding 生成 Intents（通过 Goal 树）"""
    planner = Planner(goal_inferrer, generators, worldview)
    result = planner.plan(mock_finding, mock_context)
    assert len(result.intents) >= 1
    assert result.goal is not None

def test_planner_traverses_goal_tree():
    """Planner 递归遍历 Goal Tree 的每个节点"""
    planner = Planner(goal_inferrer, generators, worldview)
    result = planner.plan(mock_finding, mock_context)
    # 每个 GoalNode 都应该有对应的 Intent（如果匹配 Generator）
    for intent in result.intents:
        assert intent.action in ("restart_service", "collect_diagnostic", "failover")
```

---
&nbsp;
# Phase 6: Capability + Blast Radius + Risk + Policy + Decision Orchestration

**Goal:** Capability (Expression Pre/Post + ImpactModel) + ExecutionPlanner + Blast Radius Analyzer (ImpactModel + Dependency + State) + RiskEngine (3-tier + Constraint/Expression) + PolicyEngine (configurable Utility weights) + DecisionStateMachine + DecisionOrchestrator (separated) + WorkflowEngine

**Dependencies:** Phase 5 (Planner produces intents, Goal Tree, Expression)

**v12-v15 changes applied:**
- **Capability** — Expression preconditions/postconditions + ImpactModel (not strings)
- **ExecutionPlanner** — Intent → WorkflowCandidate (Composer moved out of Planner in v13)
- **Blast Radius Analyzer** — inputs: Capability ImpactModel + Dependency Graph + Current State (v15)
- **RiskEngine** — independent component (v12), uses Expression-based Constraint check (v15)
- **PolicyEngine** — sorts candidates (v12), configurable Utility weights (v15)
- **UtilityWeights** — dataclass with OPA Rego `data.logoscope.utility_weights` injection
- **DecisionStateMachine** — pure lifecycle (12 states), separated from orchestration (v15)
- **DecisionOrchestrator** — pure orchestration (v15: PLAN → EVALUATE → POLICY → EXECUTE → LEARN)

---

### Task 6.1: Capability (v15: Expression Pre/Post + ImpactModel)

**Files:**
- Create: `shared_src/capability/__init__.py`
- Create: `shared_src/capability/models.py` — Capability (Expression pre/post, ImpactModel, effects tag)
- Create: `shared_src/capability/registry.py` — CapabilityRegistry
- Create: `shared_src/expression/impact_model.py` — ImpactModel
- Create: `exec-service/capability/executors/__init__.py`
- Create: `exec-service/capability/executors/ssh_executor.py`
- Create: `exec-service/capability/executors/k8s_executor.py`
- Create: `shared_src/tests/capability/test_registry.py`
- Create: `shared_src/tests/expression/test_impact_model.py`

**Key tests:**
```python
def test_capability_expression_preconditions():
    """Capability 使用 Expression，不是字符串"""
    cap = Capability(
        capability_id="ssh.restart_service",
        provider="ssh-executor",
        effects=["service.restart", "process.modify"],
        base_risk=50,
        preconditions=[
            Expression("host.host_status", "==", "alive"),
            Expression("service.exists", "==", True),
        ],
        postconditions=[
            Expression("resource.status", "==", "running"),
        ],
        impact_model=ImpactModel("temporary", "30s", "service"),
        rollback_capability="ssh.restart_service",
    )
    assert all(isinstance(p, Expression) for p in cap.preconditions)
    assert cap.impact_model.severity == "temporary"
    assert cap.preconditions[0].field == "host.host_status"

def test_capability_effect_tags():
    """Capability effects 是 List[str]"""
    cap = Capability(capability_id="openstack.delete_volume",
                      effects=["storage.delete", "data.loss"], base_risk=80)
    assert "storage.delete" in cap.effects

def test_registry_execute():
    registry = CapabilityRegistry()
    registry.register(Capability(
        capability_id="echo.test", provider="mock",
        effects=["read.process"], base_risk=5,
    ))
    result = registry.execute("echo.test", {"msg": "hello"})
    assert result is not None
```

### Task 6.2: ExecutionPlanner (Intent → WorkflowCandidate)

**Files:**
- Create: `semantic-engine/execution/__init__.py`
- Create: `semantic-engine/execution/planner.py` — ExecutionPlanner (Intent → WorkflowCandidate)
- Create: `semantic-engine/execution/workflow_composer.py` — WorkflowComposer (PlanIntent → Workflow)
- Create: `semantic-engine/execution/models.py` — WorkflowCandidate
- Create: `semantic-engine/tests/execution/test_execution_planner.py`
- Create: `semantic-engine/tests/execution/test_workflow_composer.py`

**Interfaces:**
- Produces: `ExecutionPlanner.plan(plan_result, context) -> List[WorkflowCandidate]`
- Consumes: `CapabilityRegistry` (Task 6.1), `WorldView` (Phase 4)

**Key tests:**
```python
def test_execution_planner_converts_intent():
    """ExecutionPlanner 将 PlanIntent 转为 WorkflowCandidate"""
    ep = ExecutionPlanner(capability_registry, world_view, knowledge_store)
    intent = PlanIntent(action="restart_service", target=...)
    candidates = ep.plan(intent, mock_context)
    assert len(candidates) >= 1
    assert all(isinstance(c, WorkflowCandidate) for c in candidates)

def test_workflow_composer_preconditions():
    """WorkflowComposer 使用 Expression 检查前置条件"""
    composer = WorkflowComposer(capability_registry)
    cap = Capability(capability_id="ssh.restart_service",
                      preconditions=[
                          Expression("host.host_status", "==", "alive"),
                          Expression("service.exists", "==", True),
                      ])
    mock_worldview.state.resolve.return_value = "alive"
    assert composer._check_preconditions(cap, target, mock_worldview) == True
    mock_worldview.state.resolve.return_value = "dead"
    assert composer._check_preconditions(cap, target, mock_worldview) == False

def test_candidate_estimated_success_rate():
    """Candidate 使用 estimated_success_rate（不同于 Finding.confidence）"""
    candidate = WorkflowCandidate(workflow=wf, estimated_success_rate=0.9, base_risk=50)
    assert 0.0 <= candidate.estimated_success_rate <= 1.0
    assert candidate.base_risk == 50
    assert candidate.final_risk >= candidate.base_risk  # dynamic adjustment
```

### Task 6.3: Blast Radius Analyzer (impact + dependency + state)

**Files:**
- Create: `shared_src/blast_radius/__init__.py`
- Create: `shared_src/blast_radius/analyzer.py` — BlastRadiusAnalyzer
- Create: `shared_src/blast_radius/models.py` — BlastRadiusReport
- Create: `shared_src/tests/blast_radius/test_analyzer.py`

**Key tests:**
```python
def test_blast_radius_uses_impact_model():
    """Blast Radius 使用 Capability.impact_model"""
    cap = Capability(capability_id="ssh.restart_service",
                      impact_model=ImpactModel("temporary", "30s", "service"))
    analyzer = BlastRadiusAnalyzer(topology_query, state_query)
    report = analyzer.analyze(intent, target, cap, worldview)
    assert report.risk_level in ("low", "medium", "high", "critical")
    # temporary 30s 的风险 < permanent data loss

def test_blast_radius_dependency_graph():
    """Blast Radius 使用 Dependency Graph"""
    analyzer = BlastRadiusAnalyzer(topology_query, state_query)
    report = analyzer.analyze(intent, target, cap, worldview)
    assert len(report.directly_affected) >= 0
    assert report.estimated_vm_count >= 0
    assert report.estimated_service_count >= 0

def test_blast_radius_current_state_adjustment():
    """Current State 调整影响范围"""
    analyzer = BlastRadiusAnalyzer(topology_query, state_query)
    worldview.state.get_state.return_value = "ERROR"
    report = analyzer.analyze(intent, target, cap, worldview)
    # ERROR 状态下调高风险
    assert report.risk_level in ("high", "critical")

def test_blast_radius_permanent_risk():
    """permanent 操作评为 critial"""
    cap = Capability(capability_id="openstack.delete_volume",
                      impact_model=ImpactModel("permanent", "permanent", "data"))
    analyzer = BlastRadiusAnalyzer(...)
    report = analyzer.analyze(intent, target, cap, worldview)
    assert report.risk_level == "critical"
```

### Task 6.4: RiskEngine (3-tier + Constraint Expression check)

**Files:**
- Create: `shared_src/risk/__init__.py`
- Create: `shared_src/risk/engine.py` — RiskEngine
- Create: `shared_src/risk/models.py` — RiskProfile
- Create: `shared_src/tests/risk/test_engine.py`

**Interfaces:**
- Produces: `RiskEngine.compute(intent, candidate, context, worldview) -> RiskProfile`
- Consumes: `BlastRadiusAnalyzer` (Task 6.3), `KnowledgeMemoryStore` (Task 5.2), `WorldView` (Phase 4)

**Key tests:**
```python
def test_risk_engine_three_tiers():
    """RiskEngine 计算三层风险"""
    engine = RiskEngine(blast_analyzer, knowledge_store)
    profile = engine.compute(intent, candidate, context, worldview)
    assert hasattr(profile, "business_risk")
    assert hasattr(profile, "execution_risk")
    assert hasattr(profile, "operational_risk")
    assert profile.final_risk >= 0

def test_risk_engine_expression_constraint():
    """Constraint 检查使用 Expression"""
    engine = RiskEngine(blast_analyzer, knowledge_store)
    constraint = Constraint(
        applies_to="restart_service",
        condition=Expression("resource.status", "==", "running"),
        restriction="Cannot restart a running service without approval",
        severity="error",
    )
    knowledge_store.get_constraints.return_value = [constraint]
    worldview.state.resolve_field.return_value = "running"
    profile = engine.compute(intent, candidate, context, worldview)
    assert profile.final_risk >= 50  # error severity 加分

def test_risk_engine_blast_radius_integration():
    """Blast Radius 影响 Operational Risk"""
    engine = RiskEngine(blast_analyzer, knowledge_store)
    blast_analyzer.analyze.return_value = BlastRadiusReport(risk_level="critical")
    profile = engine.compute(intent, candidate, context, worldview)
    assert profile.operational_risk >= 30
```

### Task 6.5: PolicyEngine + UtilityWeights (OPA configurable)

**Files:**
- Create: `shared_src/policy/__init__.py`
- Create: `shared_src/policy/engine.py` — PolicyEngine (OPA, effects-based, sorts candidates)
- Create: `shared_src/policy/models.py` — PolicyEvaluationResult, PolicyDecision, UtilityWeights
- Create: `shared_src/policy/decision_record.py` — DecisionRecord, DecisionRecordStore
- Create: `deploy/policies/high_risk.rego` — effects-based rules
- Create: `deploy/policies/utility.rego` — configurable Utility weights
- Create: `shared_src/tests/policy/test_policy_engine.py`
- Create: `shared_src/tests/policy/test_decision_record.py`
- Create: `shared_src/tests/policy/test_utility_weights.py`

**Key tests:**
```python
def test_utility_configurable_weights():
    """Utility 权重可通过配置调整"""
    engine = PolicyEngine(..., weights=UtilityWeights(success=0.4, risk=0.4))
    assert engine.weights.success == 0.4

def test_utility_different_weights_different_ranking():
    """不同权重产生不同候选排序"""
    engine_a = PolicyEngine(..., weights=UtilityWeights(success=0.6, risk=0.1))
    engine_b = PolicyEngine(..., weights=UtilityWeights(success=0.1, risk=0.6))
    candidates = [
        WorkflowCandidate(workflow=wf1, estimated_success_rate=0.95, final_risk=70),
        WorkflowCandidate(workflow=wf2, estimated_success_rate=0.85, final_risk=20),
    ]
    ranked_a = engine_a._rank(candidates, intent, worldview)
    ranked_b = engine_b._rank(candidates, intent, worldview)
    # 高风险偏好时，低风险候选应排名更高
    assert ranked_b[0].final_risk <= ranked_a[0].final_risk

def test_opa_uses_effects_not_names():
    """OPA 输入包含 effects tag，不包含 capability name"""
    engine = PolicyEngine(...)
    request = PolicyEvaluationRequest(
        candidates=[WorkflowCandidate(
            workflow=Workflow(steps=[WorkflowStep(capability="openstack.delete_volume")]),
            estimated_success_rate=0.9, final_risk=90,
        )],
    )
    opa_input = engine._build_opa_input(request, request.candidates[0])
    assert "capability" not in str(opa_input)
    assert "effects" in str(opa_input)

def test_opa_utility_rego():
    """OPA 使用可配置权重（data.logoscope.utility_weights）"""
    engine = PolicyEngine(opa_endpoint="http://localhost:8181",
                           weights=UtilityWeights(success=0.4, risk=0.4))
    # weights 传递给 OPA Rego
    assert engine.weights.risk == 0.4

def test_policy_selects_best_candidate():
    """PolicyEngine 从多个候选中选择最佳"""
    engine = PolicyEngine(...)
    candidates = [
        WorkflowCandidate(workflow=wf1, estimated_success_rate=0.95, final_risk=85),
        WorkflowCandidate(workflow=wf2, estimated_success_rate=0.60, final_risk=20),
    ]
    decision = engine.evaluate_candidates(plan_result, candidates, context, finding)
    assert decision.decision in (PolicyDecision.CANDIDATE_SELECTED, PolicyDecision.DENY)

def test_decision_record():
    """DecisionRecord 记录完整决策路径"""
    record = DecisionRecord(
        decision_id="dec-001", finding_id="f-001", context_hash="ctx_abc123",
        planner_candidates=[candidate_a, candidate_b],
        selected_candidate=candidate_a,
        policy_rules_matched=["no_restart_biz_hours"],
        rejected_candidates=["restart_service: denied"],
        execution_id="exec-001", approver="auto",
    )
    assert record.finding_id == "f-001"
```

**Rego policy (effects-based + utility weights):**
```rego
# deploy/policies/utility.rego
package logoscope.policy

default utility = 0

utility = score {
    w := data.logoscope.utility_weights
    score := (input.candidate.estimated_success_rate * 100 * w.success)
           - (input.candidate.risk.final_risk * w.risk)
           - (input.candidate.estimated_duration_minutes * w.cost)
           - (input.candidate.blast.vm_count * w.blast)
}

decision = "deny" { input.candidate.risk.final_risk >= 80 }
decision = "pending_approval" {
    input.candidate.risk.final_risk >= 40
    input.candidate.risk.final_risk < 80
}
decision = "allow" { input.candidate.risk.final_risk < 40 }

deny_delete {
    contains(input.candidate.steps[_].effects[_], "delete")
}
```

### Task 6.6: DecisionStateMachine (pure lifecycle, 12 states)

**Files:**
- Create: `shared_src/decision/__init__.py`
- Create: `shared_src/decision/state_machine.py` — DecisionStateMachine, DecisionStatus
- Create: `shared_src/tests/decision/test_state_machine.py`

**Interfaces:**
- Produces: `DecisionStateMachine.transition(decision, to) -> DecisionRecord`
- Consumes: EventBus (for state transition events)

**Key tests:**
```python
def test_state_machine_transition():
    sm = DecisionStateMachine(bus)
    d = DecisionRecord(decision_id="d1")
    d.status = DecisionStatus.CREATED
    sm.transition(d, DecisionStatus.PLANNING)
    assert d.status == DecisionStatus.PLANNING
    assert len(d.status_history) == 1

def test_state_machine_invalid_transition():
    sm = DecisionStateMachine(bus)
    d = DecisionRecord(decision_id="d1")
    d.status = DecisionStatus.CREATED
    with pytest.raises(InvalidTransitionError):
        sm.transition(d, DecisionStatus.SUCCEEDED)  # CREATED → SUCCEEDED 非法

def test_state_machine_terminal_states():
    """终止状态设置 completed_at"""
    sm = DecisionStateMachine(bus)
    d = DecisionRecord(decision_id="d1")
    d.status = DecisionStatus.EXECUTING
    sm.transition(d, DecisionStatus.SUCCEEDED)
    assert d.completed_at is not None

def test_state_machine_publishes_event():
    sm = DecisionStateMachine(bus)
    d = DecisionRecord(decision_id="d1")
    d.status = DecisionStatus.CREATED
    sm.transition(d, DecisionStatus.PLANNING)
    assert any("platform.decision.state" in str(e) for e in bus._history)

def test_state_machine_pure_lifecycle():
    """DecisionStateMachine 只做状态管理，不做编排"""
    sm = DecisionStateMachine(bus)
    assert hasattr(sm, "transition")
    assert not hasattr(sm, "execute")  # 编排由 Orchestrator 负责
```

### Task 6.7: DecisionOrchestrator (PLAN → EVALUATE → POLICY → EXECUTE → LEARN)

**Files:**
- Create: `shared_src/decision/orchestrator.py` — DecisionOrchestrator
- Create: `shared_src/tests/decision/test_orchestrator.py`

**Interfaces:**
- Produces: `DecisionOrchestrator.execute(finding, context, goal) -> DecisionResult`
- Consumes: Planner (Task 5.7), ExecutionPlanner (Task 6.2), RiskEngine (Task 6.4), BlastRadiusAnalyzer (Task 6.3), PolicyEngine (Task 6.5), DecisionStateMachine (Task 6.6), WorkflowEngine (Task 6.8), EpisodeStore (Phase 7)

**Key tests:**
```python
def test_orchestrator_complete_flow():
    """Orchestrator 执行完整 5 阶段流程"""
    orchestrator = DecisionOrchestrator(
        planner, exec_planner, risk_engine, blast_analyzer,
        policy_engine, state_machine, workflow_engine, episode_store,
    )
    result = orchestrator.execute(mock_finding, mock_context)
    assert result.decision.status in (
        DecisionStatus.SUCCEEDED, DecisionStatus.FAILED,
        DecisionStatus.REJECTED, DecisionStatus.PENDING_APPROVAL,
    )

def test_orchestrator_records_episode():
    """Orchestrator 在 FINAL 阶段记录 Episode"""
    orchestrator = DecisionOrchestrator(...)
    result = orchestrator.execute(mock_finding, mock_context)
    episode = episode_store.get_by_decision(result.decision.decision_id)
    assert episode is not None

def test_orchestrator_integration():
    """端到端集成：Finding → Decision → Episode"""
    orchestrator = DecisionOrchestrator(...)
    result = orchestrator.execute(finding, context)
    assert result.decision.finding_id == finding.id
    episode = episode_store.get_by_decision(result.decision.decision_id)
    assert episode.finding_id == finding.id
```

### Task 6.8: WorkflowEngine + Command/Event separation

**Files:**
- Create: `shared_src/workflow/__init__.py`
- Create: `shared_src/workflow/engine.py` — WorkflowEngine
- Create: `shared_src/workflow/models.py` — Workflow, WorkflowStep, WorkflowCommand, WorkflowEvent
- Create: `shared_src/tests/workflow/test_engine.py`

**Key test:**
```python
def test_workflow_command_event():
    engine = WorkflowEngine(bus, registry)
    wf = Workflow(steps=[WorkflowStep(capability="echo.test", params={"msg": "hello"})])
    event = engine.execute(wf, WorkflowContext(...))
    assert event.outcome in ("success", "failure")
    assert any(env in bus._history.get("platform.workflow.command", []))
    assert any(env in bus._history.get("platform.workflow.event", []))
```

---
&nbsp;
# Phase 7: Episode + Feedback + Experience Graph

**Goal:** Episode (with DecisionStep: candidate_scores + reject_reasons) + Feedback (eventized: EvaluationEvent → LearningEvent) + ExperienceGraphProjection (key with failure_pattern dimension) + CapabilityStatsProjector

**Dependencies:** Phase 6 (DecisionOrchestrator produces execution results for Episodes)

**v12-v15 changes applied:**
- **Episode** — Event Sourcing fact (append-only + full trajectory). Includes DecisionStep
- **DecisionStep** — records candidate_scores, reject_reasons, selected_reason
- **Feedback** eventized — EvaluationEvent → LearningEvent published to bus (v12)
- **ExperienceGraphProjection** — key = (failure_pattern, capability_id, env_fingerprint) (v15)
- **CapabilityStatsProjector** — per-capability execution statistics

---

### Task 7.1: Episode models + EpisodeStore

**Files:**
- Create: `shared_src/episode/__init__.py`
- Create: `shared_src/episode/models.py` — Episode, EpisodeStep, DecisionStep
- Create: `shared_src/episode/store.py` — EpisodeStore
- Create: `shared_src/tests/episode/test_episode_models.py`
- Create: `shared_src/tests/episode/test_episode_store.py`

**Key tests:**
```python
def test_episode_creation():
    """Episode 创建时自动设置时间戳"""
    episode = Episode(episode_id="ep-001", finding_id="f-001",
                       decision_id="d-001", context_hash="ctx_abc123")
    assert episode.finding_id == "f-001"
    assert episode.decision_id == "d-001"

def test_episode_append_only():
    """Episode 是 append-only——step 不可修改"""
    episode = Episode(episode_id="ep-1", finding_id="f-1")
    episode.add_step("observation", {"event": "rabbitmq heartbeat lost"})
    assert len(episode.steps) == 1
    with pytest.raises(Exception):
        episode.steps[0] = EpisodeStep(order=0, step_type="observation", data={})

def test_decision_step_records_reason():
    """DecisionStep 记录候选方案评分和拒绝理由（v15 新增）"""
    step = DecisionStep(
        candidates_scores={"restart": 85.0, "diagnose": 72.3},
        selected_candidate_id="restart",
        reject_reasons=["diagnose: Lower utility"],
        selected_reason="Highest utility: 85.0 (success=0.95, risk=30)",
    )
    assert step.candidates_scores["restart"] == 85.0
    assert len(step.reject_reasons) == 1
    assert step.selected_candidate_id == "restart"

def test_episode_contains_decision_step():
    """Episode 包含 DecisionStep"""
    episode = Episode(episode_id="ep-1", finding_id="f-1")
    episode.add_step("decision", {
        "candidates_scores": {"restart": 85.0},
        "selected_candidate_id": "restart",
        "reject_reasons": [],
    })
    assert episode.steps[-1].step_type == "decision"
    assert "candidates_scores" in episode.steps[-1].data

def test_episode_store_persist_and_retrieve():
    store = EpisodeStore()
    episode = Episode(episode_id="ep-1", finding_id="f-1")
    store.save(episode)
    retrieved = store.get("ep-1")
    assert retrieved is not None
    assert retrieved.episode_id == "ep-1"

def test_episode_store_by_decision():
    store = EpisodeStore()
    episode = Episode(episode_id="ep-1", finding_id="f-1", decision_id="d-1")
    store.save(episode)
    result = store.get_by_decision("d-1")
    assert result.episode_id == "ep-1"
```

### Task 7.2: EvaluationEvent + LearningEvent (eventized feedback)

**Files:**
- Create: `shared_src/feedback/__init__.py`
- Create: `shared_src/feedback/events.py` — EvaluationEvent, LearningEvent
- Create: `shared_src/tests/feedback/test_events.py`

**Key tests:**
```python
def test_evaluation_event():
    """EvaluationEvent 记录执行结果"""
    event = EvaluationEvent(
        event_id="ev-001", episode_id="ep-001",
        capability_id="ssh.restart_service",
        outcome="success", duration_ms=5000,
        error_message="",
    )
    assert event.outcome == "success"
    assert event.capability_id == "ssh.restart_service"

def test_learning_event():
    """LearningEvent 用于 ExperienceGraph 投影"""
    event = LearningEvent(
        event_id="le-001", episode_id="ep-001",
        failure_pattern="RabbitMQHeartbeatLost",
        capability_id="ssh.restart_service",
        env_fingerprint="prod:rabbitmq",
        outcome="success",
    )
    assert event.failure_pattern == "RabbitMQHeartbeatLost"
```

### Task 7.3: FeedbackLoop (episode → evaluation → learning events)

**Files:**
- Create: `shared_src/feedback/loop.py` — FeedbackLoop
- Create: `shared_src/tests/feedback/test_loop.py`

**Key tests:**
```python
def test_feedback_loop_eventized():
    """FeedbackLoop 发布 EvaluationEvent 和 LearningEvent"""
    loop = FeedbackLoop(knowledge_store, bus)
    loop.record_execution(episode, execution_result)
    assert any(e.event_type == "feedback.evaluation"
               for e in bus._history.get("platform.system", []))

def test_feedback_writes_memory():
    store = KnowledgeMemoryStore()
    loop = FeedbackLoop(store, bus)
    loop.record_execution(episode, execution_result)
    memories = store.retrieve("restart")
    assert any(m.outcome == "success" for m in memories)

def test_feedback_updates_stats():
    """Feedback 更新 CapabilityStatistics"""
    loop = FeedbackLoop(knowledge_store, bus, capability_stats_store)
    result = ExecutionResult(steps=[ExecutionStep(capability_id="ssh.restart_service",
                                                    success=True, duration_ms=5000)])
    loop.record_execution(episode, result)
    stats = capability_stats_store.get("ssh.restart_service")
    assert stats.total_executions >= 1
```

### Task 7.4: ExperienceGraphProjection (with failure_pattern dimension)

**Files:**
- Create: `semantic-engine/projections/experience_graph.py` — ExperienceGraphProjection
- Create: `shared_src/projection/experience_stats.py` — ExperienceStats
- Create: `semantic-engine/tests/projections/test_experience_graph.py`

**Key tests:**
```python
def test_experience_stats_failure_pattern_key():
    """ExperienceStats 按 (failure_pattern, capability, env) 索引"""
    stats = ExperienceStats(
        failure_pattern="RabbitMQHeartbeatLost",
        capability_id="ssh.restart_service",
        env_fingerprint="prod:rabbitmq",
    )
    assert stats.key == "RabbitMQHeartbeatLost|ssh.restart_service|prod:rabbitmq"

def test_different_failure_pattern_separate_stats():
    """不同 failure_pattern 的统计不混合"""
    p1 = ExperienceStats(failure_pattern="RabbitMQHeartbeatLost", ...)
    p2 = ExperienceStats(failure_pattern="NovaOOM", ...)
    assert p1.key != p2.key

def test_experience_graph_projector():
    """ExperienceGraphProjection 从 LearningEvent 构建统计"""
    projector = ExperienceGraphProjection(epoch="20260701")
    event = LearningEvent(event_id="le-1", failure_pattern="RabbitMQHeartbeatLost",
                           capability_id="ssh.restart_service",
                           env_fingerprint="prod", outcome="success")
    envelope = EventEnvelope(event_type="feedback.learning", payload=json.dumps(event.__dict__).encode())
    projector.apply(envelope)
    stats = projector.get_stats("RabbitMQHeartbeatLost", "ssh.restart_service", "prod")
    assert stats is not None
    assert stats.total_executions == 1
    assert stats.success_count == 1

def test_experience_success_rate():
    """ExperienceStats.success_rate 动态计算"""
    stats = ExperienceStats(total_executions=10, success_count=8)
    assert stats.success_rate == 0.8
```

### Task 7.5: CapabilityStatsProjector

**Files:**
- Create: `semantic-engine/projections/capability_stats.py` — CapabilityStatsProjector
- Create: `shared_src/capability/stats.py` — CapabilityStatistics
- Create: `semantic-engine/tests/projections/test_capability_stats.py`

**Key tests:**
```python
def test_capability_statistics():
    """CapabilityStatistics 记录历史执行统计"""
    stats = CapabilityStatistics(
        capability_id="ssh.restart_service",
        total_executions=10, success_count=8, failure_count=2,
        avg_recovery_time_ms=120000,
    )
    assert stats.success_rate == 0.8

def test_capability_stats_projector():
    """CapabilityStatsProjector 从 EvaluationEvent 构建"""
    projector = CapabilityStatsProjector()
    event = EvaluationEvent(capability_id="ssh.restart_service",
                             outcome="success", duration_ms=5000)
    envelope = EventEnvelope(event_type="feedback.evaluation", ...)
    projector.apply(envelope)
    stats = projector.get_stats("ssh.restart_service")
    assert stats.total_executions >= 1

def test_execution_planner_uses_experience_stats():
    """ExecutionPlanner 利用 ExperienceStatus 调整 estimated_success_rate"""
    planner = ExecutionPlanner(capability_registry, world_view, knowledge_store)
    planner.experience_graph.get_stats.return_value = ExperienceStats(
        failure_pattern="RabbitMQHeartbeatLost", capability_id="ssh.restart_service",
        env_fingerprint="prod", total_executions=100, success_count=70,
    )
    candidate = planner.plan(mock_intent, mock_context)
    assert candidate.estimated_success_rate < 0.85  # 历史 70% 降低估值
```

### Task 7.6: Phase 7 API Endpoints

**Files:**
- Create: `query-service/api/episodes.py` — Episode API routes
- Create: `query-service/api/experience.py` — Experience API routes
- Create: `query-service/tests/api/test_episodes_api.py`
- Create: `query-service/tests/api/test_experience_api.py`

**Key tests:**
```python
def test_episode_by_decision():
    client.get("/api/v1/episodes/by-decision/d-001")
    assert response.status_code == 200
    assert "decision" in [s["step_type"] for s in response.json()["steps"]]

def test_experience_success_rate():
    client.get("/api/v1/experience/success-rate?pattern=RabbitMQHeartbeatLost&capability=ssh.restart_service&env=prod")
    assert response.status_code == 200
    assert "success_rate" in response.json()
```

---

## Self-Review Checklist

**1. Spec coverage:**
- [x] EventEnvelope (Task 0.1)
- [x] SchemaRegistry + Schema Evolution (Task 0.2)
- [x] RawEvent Store (Task 0.3)
- [x] Event Bus 10 topics (Task 0.3)
- [x] Event Pipeline Processors (Task 1.1)
- [x] Semantic Engine EventEnvelope (Task 1.2)
- [x] Versioned Projection (Task 1.3)
- [x] Projection Checkpoint (Task 0.5)
- [x] EntityProjector (Task 2.1)
- [x] InventoryProjection (Task 2.2)
- [x] State Projection (Task 2.3)
- [x] Timeline MV (Task 2.4)
- [x] InteractionProjector (Task 3.1)
- [x] CorrelationEngine + DynamicRel (Task 3.2)
- [x] Lineage API (Task 3.3)
- [x] GraphProjection (Task 4.1 — topology-only, no state)
- [x] TopologyQuery (Task 4.2)
- [x] StateQuery (Task 4.3)
- [x] HistoryQuery (Task 4.4)
- [x] WorldView Facade (Task 4.5 — pure Facade, no God Object)
- [x] CanonicalContextHasher RFC 8785 + Merkle DAG (Task 4.6)
- [x] ContextAPI + StateProjectionRef + knowledge_refs (Task 4.7)
- [x] Context Snapshot (belongs to Projection Layer, use_snapshot=False) (Task 4.7)
- [x] Expression dataclass + evaluate() (Task 5.1)
- [x] Knowledge Object Model (SOP/Runbook/FailurePattern/Incident/RCA) (Task 5.2)
- [x] Knowledge & Memory Store (Task 5.2)
- [x] InferenceRegistry + InferencePipeline (Task 5.3)
- [x] Finding — no recommended_action, has context_hash + knowledge_refs (Task 5.3)
- [x] Goal = desired_state, no action/ordering/completion (Task 5.4)
- [x] Constraint Knowledge (Expression-based) (Task 5.5)
- [x] GoalInferrer (Finding → Goal Tree) (Task 5.6)
- [x] IntentGenerator (outputs PlanIntent: what, not how) (Task 5.6)
- [x] Planner (GoalInferrer + IntentGenerator) (Task 5.7)
- [x] Capability Expression Pre/Post + ImpactModel (Task 6.1)
- [x] ExecutionPlanner (Intent → WorkflowCandidate) (Task 6.2)
- [x] WorkflowComposer (PlanIntent → Workflow + Expression precondition check) (Task 6.2)
- [x] Blast Radius Analyzer (ImpactModel + Dependency + State) (Task 6.3)
- [x] RiskEngine (3-tier + Constraint Expression check) (Task 6.4)
- [x] PolicyEngine (effects-based OPA, sorts candidates) (Task 6.5)
- [x] UtilityWeights (configurable weights + OPA Rego utility_rego) (Task 6.5)
- [x] DecisionRecord (Task 6.5)
- [x] DecisionStateMachine (12 states, pure lifecycle) (Task 6.6)
- [x] DecisionOrchestrator (PLAN→EVALUATE→POLICY→EXECUTE→LEARN) (Task 6.7)
- [x] WorkflowEngine + Command/Event (Task 6.8)
- [x] Episode with DecisionStep (candidate_scores + reject_reasons) (Task 7.1)
- [x] EvaluationEvent + LearningEvent (eventized feedback) (Task 7.2)
- [x] FeedbackLoop (Task 7.3)
- [x] ExperienceGraphProjection (+ failure_pattern dimension) (Task 7.4)
- [x] CapabilityStatsProjector (Task 7.5)
- [x] Phase 7 API endpoints (Task 7.6)

**2. Placeholder scan:** No TBD/TODO found. All tasks have actual code in test steps.

**3. Type consistency (v15):**
- `EventEnvelope.parent_event_ids` (Task 0.1) matches lineage usage in Task 3.3
- `ProjectionCheckpoint.update(topic, partition, offset)` (Task 0.5) matches EventBus offset structure
- `CanonicalContextHasher.hash(content)` (Task 4.6) — RFC 8785 + Merkle DAG, no timestamp component
- `ContextResult.projection_epoch` (Task 4.7) — references Projection epoch, no data copy (StateProjectionRef)
- `ContextResult.knowledge_refs: List[Tuple[str,str]]` (Task 4.7) matches `Finding.knowledge_refs` (Task 5.3)
- `Expression(field, operator, value)` (Task 5.1) — matches `Capability.preconditions` (Task 6.1) and `Constraint.condition` (Task 5.5)
- `GoalNode.desired_state` (Task 5.4) — no action/ordering/completion (v15 changed from v14)
- `IntentGenerator.generate()` returns `PlanIntent(action, target)` — what, not how (Task 5.6)
- `ExecutionPlanner.plan(intent)` → `WorkflowCandidate` — how (Task 6.2)
- `Capability.effects: List[str]` (Task 6.1) — OPA input uses effects tag, not capability name
- `Capability.preconditions: List[Expression]` (Task 6.1) — structured expressions, not strings
- `Capability.impact_model: ImpactModel` (Task 6.1) — used by Blast Radius Analyzer (Task 6.3)
- `ImpactModel(severity, duration, scope)` (Task 6.1/6.3) — v15 new: temporary/permanent/degradation
- `UtilityWeights(success, risk, cost, blast)` (Task 6.5) — configurable, different from v14 hardcoded
- `DecisionRecord.context_hash` (Task 6.5) matches `Finding.context_hash` (Task 5.3)
- `DecisionStateMachine.transition()` (Task 6.6) — pure lifecycle, no execute()
- `DecisionOrchestrator.execute()` (Task 6.7) — pure orchestration, no transition() logic
- `DecisionStep.candidates_scores + reject_reasons` (Task 7.1) — v15 new step type
- `ExperienceStats.key = failure_pattern|capability_id|env_fingerprint` (Task 7.4) — v15 change from v14
- `EvaluationEvent → LearningEvent` eventized feedback (Task 7.2/7.4)
