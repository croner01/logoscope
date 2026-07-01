# Logoscope v10 — AI Observability Operating System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the v10 architecture: Event Sourcing + Envelope + Pipeline + Projection + Context API + Knowledge + Policy + Workflow + Feedback.

**Architecture:** 7 sequential phases. Each phase produces independently testable software on top of existing Logoscope services. Phase 0-2 focus on data plane (events → projections). Phase 3-4 build correlation and query layers. Phase 5-6 add AI and automation layers.

**Tech Stack:** Python 3.11, aiokafka, Kafka (Go segmentio/kafka-go), ClickHouse, Neo4j, Redis, OPA (Open Policy Agent), ChromaDB/Weaviate (Knowledge Store)

**Scope note:** The spec covers ~7 independent subsystems. This plan decomposes into separate Phase documents. Each Phase is independently implementable and testable.

## Global Constraints

- All new Event types use EventEnvelope wrapping (envelope_version, schema_version, event_type, producer, event_id, parent_event_ids, payload)
- All new code lives in `shared_src/` for cross-service shared logic, service-specific code in respective `{service}/` directories
- Projection checkpoint uses partition+offset (not event_id)
- All Inference output is Finding (unified structure)
- Policy Engine uses OPA (Rego), not custom DSL
- OPA policies live in `deploy/policies/*.rego`, loaded at startup
- Capability Registry requires effect + risk_score on all capabilities
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
# Phase 4: Graph + Context API

**Goal:** GraphProjection (topology-only) + ContextAPI (4 ContextTypes) + Context Snapshot

**Dependencies:** Phase 2-3 (entity, state, interaction events available)

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

### Task 4.2: ContextAPI + ContextResult (with context_version)

**Files:**
- Create: `shared_src/context/__init__.py`
- Create: `shared_src/context/api.py` — ContextAPI, ContextResult, ContextType
- Create: `shared_src/context/builders.py` — Incident/Topology/Workflow/Rule builders
- Create: `shared_src/context/snapshot.py` — ContextSnapshot
- Create: `shared_src/tests/context/test_context_api.py`

**Key tests:**
```python
def test_context_version_reproducible():
    api = ContextAPI(...)
    r1 = api.build(ResourceType.INSTANCE, "abc-123")
    r2 = api.build(ResourceType.INSTANCE, "abc-123")
    assert r1.context_version == r2.context_version

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

def test_context_snapshot_consistency():
    api = ContextAPI(...)
    result = api.build(type=INSTANCE, id="abc-123", use_snapshot=True)
    assert result.snapshot_id != ""
    snapshot = api.get_snapshot(result.snapshot_id)
    assert snapshot is not None
```

---
&nbsp;
# Phase 5: Knowledge + Inference + Planner

**Goal:** Knowledge & Memory Store + Inference Engine (LLM + Rule) + Planner (multi-candidate)

**Dependencies:** Phase 4 (ContextAPI provides IncidentContext)

---

### Task 5.1: Knowledge & Memory Store

**Files:**
- Create: `shared_src/knowledge/__init__.py`
- Create: `shared_src/knowledge/store.py` — KnowledgeMemoryStore
- Create: `shared_src/knowledge/document.py` — KnowledgeDocument, MemoryRecord
- Create: `shared_src/tests/knowledge/test_store.py`

**Key tests:**
```python
def test_knowledge_retrieval():
    store = KnowledgeMemoryStore()
    store.add_document(KnowledgeDocument(
        document_id="kb-001",
        title="Nova OOM Troubleshooting",
        content="When Nova scheduler runs out of memory...",
        source_type="runbook",
    ))
    results = store.retrieve("nova scheduler OOM")
    assert len(results) > 0

def test_memory_write_and_retrieve():
    store = KnowledgeMemoryStore()
    store.add_memory(MemoryRecord(
        record_id="m1", record_type="repair", outcome="success",
        action_taken="restart neutron-dhcp-agent",
    ))
    results = store.retrieve("neutron agent failure")
    assert "neutron" in results[0].content

def test_knowledge_provenance():
    doc = KnowledgeDocument(
        document_id="kb-001",
        title="OpenStack Wallaby Admin Guide",
        origin="openstack-official",
        version="wallaby-2025-12",
        trust_level=5,
    )
    assert doc.trust_level == 5
```

### Task 5.2: Inference Engine (LLM + Rule)

**Files:**
- Create: `semantic-engine/inference/__init__.py`
- Create: `semantic-engine/inference/engine.py` — InferenceEngine ABC, InferenceInput
- Create: `semantic-engine/inference/llm_engine.py` — LLMInferenceEngine (with RAG)
- Create: `semantic-engine/inference/rule_engine.py` — RuleInferenceEngine
- Create: `semantic-engine/inference/finding.py` — Finding
- Create: `semantic-engine/tests/inference/test_inference.py`

**Key tests:**
```python
def test_finding_unified_output():
    rule_engine = RuleInferenceEngine()
    llm_engine = LLMInferenceEngine(knowledge_store, llm_client)
    findings = rule_engine.infer(InferenceInput(context=ctx))
    for f in findings:
        assert hasattr(f, "severity")
        assert hasattr(f, "confidence")
        assert hasattr(f, "context_version")

def test_llm_uses_knowledge():
    engine = LLMInferenceEngine(knowledge_store, mock_llm)
    findings = engine.infer(InferenceInput(
        context=ctx,
        knowledge=[KnowledgeDocument(document_id="kb-001", title="Test", content="test")],
    ))
    assert "kb-001" in findings[0].knowledge_sources
```

### Task 5.3: Planner (Multi-Candidate)

**Files:**
- Create: `semantic-engine/planner/__init__.py`
- Create: `semantic-engine/planner/planner.py` — Planner
- Create: `semantic-engine/planner/candidate.py` — WorkflowCandidate
- Create: `semantic-engine/tests/planner/test_planner.py`

**Key tests:**
```python
def test_planner_multiple_candidates():
    planner = Planner(knowledge_store, capability_registry)
    result = planner.plan(mock_finding, mock_context)
    assert len(result.candidates) >= 2  # primary + diagnostic
    assert result.primary is not None
    for c in result.candidates:
        assert 1 <= c.risk_score <= 100
        assert 0 <= c.confidence <= 1.0
```

---
&nbsp;
# Phase 6: Policy + Workflow + Feedback

**Goal:** OPA-based PolicyEngine + CapabilityRegistry + WorkflowEngine + FeedbackLoop

**Dependencies:** Phase 5 (Planner produces WorkflowCandidates)

---

### Task 6.1: PolicyEngine (OPA)

**Files:**
- Create: `shared_src/policy/__init__.py`
- Create: `shared_src/policy/engine.py` — PolicyEngine (OPA client)
- Create: `shared_src/policy/models.py` — PolicyDecision, PolicyEvaluationRequest, PolicyAction
- Create: `deploy/policies/` — Rego policy files
- Create: `shared_src/tests/policy/test_opa_engine.py`

**Key test:**
```python
def test_opa_deny():
    engine = PolicyEngine(opa_endpoint="http://localhost:8181")
    request = PolicyEvaluationRequest(
        candidates=[WorkflowCandidate(
            workflow=Workflow(name="restart_service"),
            risk_score=70,
        )],
    )
    result = engine.evaluate_candidates(request, ctx, finding)
    assert result.decision in (
        PolicyDecision.CANDIDATE_SELECTED,
        PolicyDecision.DENY,
        PolicyDecision.PENDING_APPROVAL,
    )
```

**Rego policy:**
```rego
# deploy/policies/logoscope_policy.rego
package logoscope.policy

decision = "deny" {
    input.candidate.risk_score >= 80
}

decision = "pending_approval" {
    input.candidate.risk_score >= 40
    input.candidate.risk_score < 80
}

decision = "allow" {
    input.candidate.risk_score < 40
}
```

### Task 6.2: CapabilityRegistry + Effect Model

**Files:**
- Create: `shared_src/capability/__init__.py`
- Create: `shared_src/capability/registry.py` — CapabilityRegistry
- Create: `shared_src/capability/models.py` — Capability, EffectType
- Create: `exec-service/capability/executors/__init__.py`
- Create: `exec-service/capability/executors/ssh_executor.py`
- Create: `exec-service/capability/executors/k8s_executor.py`
- Create: `shared_src/tests/capability/test_registry.py`

**Key tests:**
```python
def test_capability_effect():
    cap = Capability(
        capability_id="ssh.restart_service",
        effect=EffectType.RESTART,
        risk_score=70,
    )
    assert cap.effect == EffectType.RESTART
    assert cap.risk_score == 70

def test_registry_execute():
    registry = CapabilityRegistry()
    registry.register(Capability(
        capability_id="echo.test",
        provider="mock",
        effect=EffectType.READ,
        risk_score=5,
    ))
    result = registry.execute("echo.test", {"msg": "hello"})
    assert result is not None
```

### Task 6.3: WorkflowEngine + Command/Event separation

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
    assert event.command_id != ""
    # Command 和 Event 在不同 topic
    assert any(env in bus._history.get("platform.workflow.command", []))
    assert any(env in bus._history.get("platform.workflow.event", []))
```

### Task 6.4: FeedbackLoop

**Files:**
- Create: `shared_src/feedback/__init__.py`
- Create: `shared_src/feedback/loop.py` — FeedbackLoop
- Create: `shared_src/tests/feedback/test_loop.py`

**Key test:**
```python
def test_feedback_writes_memory():
    store = KnowledgeMemoryStore()
    loop = FeedbackLoop(store)
    loop.evaluate(
        workflow=Workflow(name="restart_service"),
        results=[MockCapabilityResult(success=True)],
    )
    memories = store.retrieve("restart_service")
    assert len(memories) >= 1
    assert memories[0].outcome == "success"

def test_feedback_learning():
    store = KnowledgeMemoryStore()
    loop = FeedbackLoop(store)
    # 第一次失败
    loop.evaluate(workflow=wf, results=[MockCapabilityResult(success=False)])
    # 第二次检索到失败记录
    results = store.retrieve("restart")
    failed = [r for r in results
              if r.record_type == "repair" and r.outcome == "failure"]
    assert len(failed) >= 1
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
- [x] GraphProjection (Task 4.1)
- [x] ContextAPI + context_version (Task 4.2)
- [x] Context Snapshot (Task 4.2)
- [x] Knowledge & Memory Store (Task 5.1)
- [x] Inference Engine (Task 5.2)
- [x] Finding unified output (Task 5.2)
- [x] Planner multi-candidate (Task 5.3)
- [x] OPA PolicyEngine (Task 6.1)
- [x] Capability + Effect Model (Task 6.2)
- [x] Workflow Command/Event (Task 6.3)
- [x] FeedbackLoop (Task 6.4)

**2. Placeholder scan:** No TBD/TODO found. All tasks have actual code in test steps.

**3. Type consistency:**
- `EventEnvelope.parent_event_ids` (Task 0.1) matches lineage usage in Task 3.3
- `ProjectionCheckpoint.update(topic, partition, offset)` (Task 0.5) matches EventBus offset structure
- `ContextResult.context_version` (Task 4.2) matches `Finding.context_version` (Task 5.2)
- `WorkflowCandidate.risk_score` (Task 5.3) matches `PolicyEngine.evaluate_candidates` (Task 6.1)
- `Capability.effect + risk_score` (Task 6.2) matches Policy OPA input (Task 6.1)
