# Logoscope Data Architecture v1 — AI Observability Operating System

> **Event Sourcing + CQRS + Unified Context + Knowledge Feedback — 只有 Raw Event 是 Source of Truth。**
> Event 携带全血缘链条。Inference 依赖 Context + Knowledge。Workflow 被 Policy 约束。Action 产生 Feedback 闭环学习。
>
> 定义完整的 **Observe → Understand → Decide → Act → Learn** 闭环。

**Status:** Draft v9
**Date:** 2026-07-01
**Authors:** croner01, Claude

---

## 1. Problem

### 1.1 当前架构的系统性瓶颈

v8 引入了 Schema Registry、Event Pipeline、Context API、Capability 和 Planner，但仍有根本性问题：

**① 没有 Event Lineage。**
一条 Finding 由哪些原始日志推导而来？AI 的结论不可追溯、不可解释。运维人员无法回答"为什么 AI 认为 Nova Scheduler 是根因"。

**② 没有 Knowledge。**
Inference Engine 的输入只有 IncidentContext（当前状态 + 拓扑），没有 Runbook、SOP、OpenStack 文档、Kubernetes 文档、历史 Incident。LLM 只能"猜"，不能基于实际知识推理。

**③ 没有 Memory。**
系统不记住过去的修复经验。相同问题再次出现时，AI 重新分析、重新诊断，不能利用历史成功经验加速定位。

**④ 没有 Policy。**
Planner 生成的 Workflow 直接执行。AI 可能在业务高峰期重启生产服务、同时操作 100 台机器、或对黑名单主机操作。没有治理和安全控制。

**⑤ 没有 Feedback Loop。**
Workflow 执行后没有反馈。系统不学。修复成功了？下次是否应该用同样策略？系统一无所知。这意味着 AI 永远不会变得更聪明。

### 1.2 v9 核心理念

```
所有 Event 携带 parent_event_ids，构成完整血缘链（Lineage）。
所有 Inference 不仅依赖 Context，也依赖 Knowledge（RAG）。
系统从每次 Workflow 执行中学习，形成 Memory。
所有 Action 必须通过 Policy 治理。
系统形成 Observe → Understand → Decide → Act → Learn 闭环。
```

### 1.3 v8 → v9 变更

| 维度 | v8 | v9 |
|------|----|----|
| **Event Lineage** | 无血缘追踪 | **parent_event_ids**，每个 Event 携带上游引用 |
| **Inference 输入** | IncidentContext | **IncidentContext + Knowledge** |
| **Knowledge** | 无 | **Knowledge & Memory Store**（Runbook + SOP + 历史 Incident） |
| **Memory** | 无 | **Memory Store**（Feedback Loop 写入，Inference 读取） |
| **Policy Engine** | 无 | **Policy Engine**（Planner → Policy → Workflow） |
| **Feedback Loop** | 无 | **Workflow → Evaluation → Feedback → Knowledge & Memory** |
| **Projection** | 单算法 | **Versioned / 多算法并行 + 流量切换** |
| **闭环** | Observe → Decide → Act | **Observe → Understand → Decide → Act → Learn** |

---

## 2. Architecture

### 2.1 整体架构

```
                    Raw Logs / Metrics / Traces / Events
                               |
                               v
                     ┌─────────────────────┐
                     │   Raw Event Store    │  ← Source of Truth
                     │  EventEnvelope{      │
                     │    event_id=raw-001, │
                     │    parent_ids=[]     │
                     │  }                   │
                     └──────────┬───────────┘
                                |
                   Event Bus (platform.raw)
                                |
                    ┌───────────┴──────────┐
                    │    Event Pipeline     │
                    │  Aggregate → Dedup    │
                    │  → Sample → Enrich    │
                    └───────────┬──────────┘
                                |
                         Semantic Engine
                     (parent_ids += raw_id)
                                |
                    Event Envelope {
                      event_id=norm-001,
                      parent_ids=[raw-001]
                    }
                   Event Bus (platform.normalized)
                                |
           ┌────────────────────┼────────────────────┐
           v                    v                    v
      EntityProjector      StateProjector     InteractionProjector
      (parent_ids +=       (parent_ids +=     (parent_ids +=
       norm-001)             norm-001)          norm-001)
           v                    v                    v
     ┌─────┴────────────────────┴────────────────────┴─────┐
     │                    Projection Layer                  │
     │  Inventory / State / Graph / Timeline / DynamicRel   │
     └────────────────────┬────────────────────────────────┘
                          |
                    Context API
                          |
                ┌─────────┴────────────┐
                |                       |
                v                       v
         Inference Engine      Knowledge & Memory Store
         (Context + Knowledge   ├── Static Knowledge
          → Finding)            │   (Runbooks, SOPs, Docs)
         Finding{               ├── Memory
           parent_ids=[         │   (Past Incidents,
             norm-001,          │    Root Causes,
             entity-001,        │    Repairs, Feedback)
             interaction-001    │
           ]                    │
         }                      |
                |               |
                └───────┬───────┘
                        |
                     Planner
                     (Finding → Workflow)
                        |
                    Policy Engine
                     ├── ALLOW
                     ├── DENY
                     └── PENDING_APPROVAL
                        |
                    Workflow Engine
                        |
                   Capability Registry
                        |
             SSH / k8s / API / VMware
                        |
                   Action Result
                        |
          ┌────────────┴──────────────┐
          |                           |
     Generated Event           Feedback Loop
     Event Bus (loop)          ├── Evaluation
                               │   (Success/Failure/
                               │    Partial)
                               ├── Feedback Signal
                               └── → Knowledge & Memory Store
                                   (Next inference is smarter)
```

### 2.2 Event Sourcing 原则

```
原则 1-10: 同 v8。
新增原则 11: 所有 Event 携带 parent_event_ids，构成有向无环图（DAG）。
新增原则 12: Inference Engine 消费 Context + Knowledge，二者缺一不可。
新增原则 13: 所有 Workflow 执行前必须经过 Policy Engine 评估。
新增原则 14: 每次 Workflow 执行后产生 Feedback，写入 Memory。
新增原则 15: 系统从 Feedback 中学习，下次 Inference 更准确。
```

### 2.3 EventEnvelope + Schema Registry

#### 2.3.1 EventEnvelope（v9 新增 parent_event_ids）

```python
@dataclass
class EventEnvelope:
    """
    所有 Event 的通用信封。
    parent_event_ids 构成完整血缘链（Lineage）。

    血缘链示例：
      RawEvent(raw-001, parent_ids=[])
        → NormalizedEvent(norm-001, parent_ids=[raw-001])
          → EntityEvent(entity-001, parent_ids=[norm-001])
          → Finding(finding-001, parent_ids=[norm-001, entity-001, state-001])
            → WorkflowEvent(wf-001, parent_ids=[finding-001])
              → ActionEvent(action-001, parent_ids=[wf-001])
    """
    envelope_version: str = "v1"
    schema_version: int = 1
    event_type: str = ""
    producer: str = ""
    event_id: str = ""
    parent_event_ids: List[str] = field(default_factory=list)  # ← v9 新增
    timestamp: datetime = field(default_factory=datetime.utcnow)
    payload: bytes = b""
    metadata: Dict[str, str] = field(default_factory=dict)
```

**Lineage 构建规则：**

```
每个组件消费一个 EventEnvelope，产生一个新 EventEnvelope 时：
  1. 新 Event 获得新的 event_id（UUID7）
  2. 新 Event 的 parent_event_ids = [输入 Event 的 event_id] + 输入 Event 的 parent_event_ids
     或：
  3. 新 Event 的 parent_event_ids = [所有输入 Event 的 event_id] + 合并的祖父代
```

**实现示例：**

```python
class LineageAwareProjector(Projector):
    """
    感知血缘关系的 Projector。
    自动维护 parent_event_ids。
    """

    def project(self, input_envelope: EventEnvelope) -> List[EventEnvelope]:
        # 处理 payload
        result_payload = self._transform(input_envelope)

        # 产出 Event——自动继承血缘
        output = EventEnvelope(
            event_id=generate_uuid7(),
            event_type=output_type,
            producer=self.name,
            # 血缘：当前 event_id + 祖传 parent_ids
            parent_event_ids=[input_envelope.event_id] + input_envelope.parent_event_ids,
            payload=serialize(result_payload),
        )
        return [output]


class MergingLineageProjector(Projector):
    """
    消费多个输入 Event 的 Projector（如 Context Builder）。
    合并多个血缘链。
    """

    def project(self, envelopes: List[EventEnvelope]) -> EventEnvelope:
        all_parents = []
        for env in envelopes:
            all_parents.append(env.event_id)
            all_parents.extend(env.parent_event_ids)

        output = EventEnvelope(
            event_id=generate_uuid7(),
            parent_event_ids=all_parents,  # 所有输入的完整血缘
            ...
        )
        return output
```

#### 2.3.2 Schema Registry（同 v8）

（Schema 注册 + 迁移链，不变）

### 2.4 Topic 结构

同 v8。domain-first 命名：`platform.raw`, `platform.normalized`, `platform.entity` 等。

### 2.5 Projection 框架（v9 支持 Versioned Projection）

```python
class Projection(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...
    @property
    @abstractmethod
    def epoch(self) -> str: ...
    @property
    def upstream_topics(self) -> List[str]: return []
    @abstractmethod
    def apply(self, envelope: EventEnvelope): ...
    @abstractmethod
    def rebuild(self, event_source, checkpoint): ...
    @abstractmethod
    def checkpoint(self) -> 'ProjectionCheckpoint': ...
    @abstractmethod
    def status(self) -> ProjectionStatus: ...


class VersionedProjectionRegistry:
    """
    多算法版本并行运行 + 流量切换。

    Datadog / Chronosphere 模式：
      - 多个算法实现同时运行
      - 流量按比例分发（shadow / canary / full）
      - 验证后切换，零停机

    示例：
      registry = VersionedProjectionRegistry("graph")
      registry.add_version(HashMapGraphProjection(epoch="20260701"), traffic=0.9)
      registry.add_version(ListGraphProjection(epoch="20260701"), traffic=0.1)
      # 监控指标 → 验证正确 → 切换
      registry.set_traffic_split({"hashmap": 0.0, "list": 1.0})
    """

    def __init__(self, name: str):
        self.name = name
        self._versions: Dict[str, Projection] = {}
        self._traffic: Dict[str, float] = {}

    def add_version(self, projection: Projection,
                    traffic: float = 0.0):
        key = type(projection).__name__
        self._versions[key] = projection
        self._traffic[key] = traffic

    def route(self, envelope: EventEnvelope) -> List[Projection]:
        """根据流量比例分发 Event。"""
        targets = []
        for key, ratio in self._traffic.items():
            if ratio > 0:
                targets.append(self._versions[key])
        return targets

    def set_traffic_split(self, split: Dict[str, float]):
        """调整流量比例（总和 = 1.0）。"""
        total = sum(split.values())
        assert abs(total - 1.0) < 0.001, f"Traffic split must sum to 1.0 (got {total})"
        self._traffic.update(split)

    def promote(self, key: str):
        """全量切换到指定版本。"""
        self.set_traffic_split({key: 1.0})
        self._publish_platform_event("projection_promoted",
            self.name, key)

    def compare_results(self, event: EventEnvelope) -> Dict:
        """比较所有版本的处理结果（shadow 验证用）。"""
        results = {}
        for key, proj in self._versions.items():
            before = self._snapshot_state(proj)
            proj.apply(event)
            after = self._snapshot_state(proj)
            results[key] = {"before": before, "after": after}
        return results
```

### 2.6 Component Responsibilities

| 层 | 职责 | 不做什么 |
|----|------|----------|
| **Raw Event Store** | 原样保留原始日志 | 不解析、不处理 |
| **Event Pipeline** | Aggregate / Dedup / Sample / Enrich / Route | 不做 Normalize |
| **Semantic Engine** | Raw → NormalizedEvent（维护血缘） | 不聚合、不去重 |
| **Projectors** | 上层 Event → 下层 Event（维护血缘） | 不构建存储 |
| **Projections** | 消费 Event → 持久化存储 | 不做推理 |
| **Context API** | 统一查询入口 | 不持久化 |
| **Knowledge & Memory Store** | 存储 Runbook / SOP / 历史 Incident / Memory | 不做推理 |
| **Inference Engine** | Context + Knowledge → Finding（维护血缘） | 不生成 Workflow |
| **Policy Engine** | 评估 Workflow 是否符合策略 | 不做推理 |
| **Planner** | Finding → Workflow | 不执行 |
| **Topology** | 纯渲染 | 不查 Projection |
| **Workflow Engine** | Workflow → Capability → Action | 不做决策 |
| **Feedback Loop** | Action Result → Evaluation → Memory | 不执行 Workflow |
| **Capability Registry** | 执行方式抽象 | 不做编排 |

---

## 3. Event Schema

### 3.1 EventEnvelope（v9 完整版）

```python
@dataclass
class EventEnvelope:
    """
    所有 Event 的通用信封。

    parent_event_ids 使所有 Event 构成有向无环图（DAG）：
      RawEvent(parent_ids=[])
        → NormalizedEvent(parent_ids=[raw-001])
          → Finding(parent_ids=[norm-001, entity-001, state-001])

    通过 parent_ids 可以追溯任何 Event 的完整来源。
    """
    envelope_version: str = "v1"
    schema_version: int = 1
    event_type: str = ""
    producer: str = ""
    event_id: str = ""
    parent_event_ids: List[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    payload: bytes = b""
    metadata: Dict[str, str] = field(default_factory=dict)
```

### 3.2 RawEvent

```python
# schema_version = 1, event_type = "raw.log"
@dataclass
class RawEvent:
    raw_id: str
    timestamp: datetime
    source: str                     # "fluentbit", "otel-collector"
    data_type: str                  # "log", "metric", "trace"
    raw_payload: str                # 原始日志内容
    content_type: str = "text/plain"
    host: str = ""
    cluster: str = ""
    namespace: str = ""
    pod_name: str = ""
    container_name: str = ""
    service_name: str = ""
    labels_json: str = ""
```

### 3.3 NormalizedEvent

同 v8。

### 3.4 Finding（v9 补充 lineage）

```python
# event_type = "inference.finding", schema_version = 1
@dataclass
class Finding:
    """
    推理结果。
    parent_event_ids（通过 EventEnvelope）追溯回原始 RawEvent。
    """
    id: str
    severity: str                     # "critical", "warning", "info"
    confidence: float                 # 0.0 ~ 1.0
    category: str                     # "anomaly", "dependency", "performance",
                                      # "security", "capacity", "change"
    reason: str                       # 人类可读的描述
    supporting_events: List[str]      # 关键事件 ID 列表（lineage 基础上的补充）
    affected_entities: List[ResourceIdentity]
    recommended_action: str
    engine_type: str                  # "rule", "llm", "ml", "graph"
    knowledge_sources: List[str] = field(default_factory=list)  # 引用的知识来源
                                                               # ← v9 新增
    created_at: datetime = field(default_factory=datetime.utcnow)
```

### 3.5 Schema Evolution

同 v8。所有 Schema 变更通过 SchemaRegistry 管理，迁移链自动运行。

---

## 4. Event Bus

同 v8。10 topics，domain-first 命名。

---

## 5. Event Pipeline

同 v8。

---

## 6. Semantic Engine

同 v8。

---

## 7. Projection Layer

### 7.1 Projection 框架

同 v8，增加 VersionedProjectionRegistry（见 2.5）。

### 7.2 Inventory / State / Graph / Timeline / DynamicRel

同 v8，仅更新为在 `apply()` 方法中维护 parent_event_ids。

---

## 8. Correlation Engine

同 v8。

---

## 9. Unified Context API

同 v8。

---

## 10. Inference Engine

### 10.1 输入（v9 增加 Knowledge）

```python
@dataclass
class InferenceInput:
    """
    推理引擎输入。
    v9 增加 knowledge：使 LLM 不仅依赖上下文，也依赖实际知识。
    """
    context: IncidentContext
    knowledge: List['KnowledgeDocument']  # ← v9 新增
    query: str = ""


@dataclass
class KnowledgeDocument:
    """知识文档——来自 Knowledge & Memory Store。"""
    document_id: str
    title: str
    content: str                      # 文档正文 / Runbook / SOP
    source_type: str                  # "runbook", "sop", "incident", "docs", "rfc"
    relevance_score: float = 0.0     # 检索时的相关度
    source_url: str = ""              # 原始来源链接
    created_at: datetime = field(default_factory=datetime.utcnow)
```

### 10.2 Inference Engine

```python
class InferenceEngine(ABC):
    """
    推理引擎。
    输入：Context + Knowledge
    输出：Finding（带血缘链）
    """

    @abstractmethod
    def infer(self, input: InferenceInput) -> List[Finding]:
        ...


class LLMInferenceEngine(InferenceEngine):
    """
    LLM 推理。消费 Context + Knowledge。

    知识使用策略（RAG）：
      1. 从 Knowledge & Memory Store 检索相关文档
      2. 将文档作为 Context 注入 LLM Prompt
      3. LLM 基于 Context + Knowledge 进行推理
      4. 输出 Finding 时记录引用源（knowledge_sources）
    """

    def __init__(self, knowledge_store: 'KnowledgeMemoryStore',
                 llm_client: Any):
        self.knowledge_store = knowledge_store
        self.llm = llm_client

    def infer(self, input: InferenceInput) -> List[Finding]:
        # 1. 从 Knowledge Store 检索（如果 input.knowledge 为空）
        if not input.knowledge:
            input.knowledge = self.knowledge_store.retrieve(
                query=self._build_query(input.context),
                max_results=5,
            )

        # 2. 构建 Prompt：Context + Knowledge
        prompt = self._build_prompt(input.context, input.knowledge)

        # 3. LLM 推理
        llm_result = self.llm.complete(prompt)

        # 4. 产出 Finding（带 knowledge_sources）
        finding = Finding(
            id=generate_uuid7(),
            severity=llm_result.severity,
            confidence=llm_result.confidence,
            category=llm_result.category,
            reason=llm_result.reason,
            supporting_events=self._extract_supporting_events(input.context),
            affected_entities=[input.context.resource_id],
            recommended_action=llm_result.action,
            engine_type="llm",
            knowledge_sources=[d.document_id for d in input.knowledge],
        )

        # 5. 发布 Finding（EventEnvelope 自动维护 parent_event_ids）
        envelope = EventEnvelope(
            event_id=finding.id,
            event_type="inference.finding",
            producer="llm-inference-engine",
            parent_event_ids=self._collect_parent_ids(input),
            payload=serialize(asdict(finding)),
        )
        self.bus.publish("platform.inference", envelope)

        return [finding]


class RuleInferenceEngine(InferenceEngine):
    """
    规则引擎。也访问 Knowledge Store（例如从 Runbook 加载规则）。
    """

    def infer(self, input: InferenceInput) -> List[Finding]:
        findings = []
        for rule in self.rules:
            if rule.matches(input.context):
                finding = rule.to_finding(input.context)
                finding.knowledge_sources = [rule.source_document]
                findings.append(finding)
        return findings
```

---

## 11. Knowledge & Memory Store

### 11.1 定位

```
v8:   Inference(context) → Finding
v9:   Inference(context + knowledge) → Finding   ← RAG 增强
      MemoryStore(write: Feedback Loop, read: Inference)
```

**Knowledge & Memory Store 是系统的长期记忆。** 它回答两个问题：

1. **"这个系统应该怎么运维？"** → 静态知识（Runbook / SOP / 文档）
2. **"以前遇到过类似问题吗？"** → 动态记忆（历史 Incident / 修复记录）

### 11.2 数据分类

```
Knowledge & Memory Store
├── Static Knowledge（只读，加载后不变）
│   ├── Runbook（Playbook / SOP）
│   ├── OpenStack 文档
│   ├── Kubernetes 文档
│   ├── Linux Kernel 文档
│   ├── RFC / 行业规范
│   └── Capability 文档
└── Memory（读写，Feedback Loop 写入）
    ├── Past Incident（过去的事故记录）
    ├── Root Cause（根因分析）
    ├── Successful Repair（成功修复方案）
    ├── Failed Attempt（失败的尝试）
    └── User Feedback（用户评价）
```

### 11.3 接口设计

```python
class KnowledgeMemoryStore:
    """
    知识 + 记忆的统一存储和检索接口。

    实现：
      - Static Knowledge: 向量数据库（Pinecone / Weaviate / Milvus）+ 文档解析器
      - Memory: 向量数据库 + 关系数据库（记录结构化和非结构化数据）

    检索策略：
      1. 对 query 进行向量化
      2. 在 Static Knowledge + Memory 中检索 top-K 相似文档
      3. 按 source_type 过滤（可选）
      4. 返回带相关度评分的文档列表
    """

    def retrieve(self, query: str,
                 max_results: int = 5,
                 source_types: Optional[List[str]] = None,
                 min_relevance: float = 0.5) -> List[KnowledgeDocument]:
        """
        检索相关知识。

        Args:
            query: 自然语言查询（"Nova scheduler OOM 原因"）
            max_results: 最大返回数量
            source_types: 过滤来源类型
            min_relevance: 最小相关度

        Returns:
            按相关度降序排列的文档列表
        """
        ...

    def add_memory(self, memory: 'MemoryRecord'):
        """
        添加一条记忆（由 Feedback Loop 调用）。

        记忆类型：
          - incident_record: 事故记录
          - repair_record: 修复记录（成功/失败）
          - user_feedback: 用户反馈
          - root_cause: 根因分析结果
        """
        ...

    def get_similar_incidents(self, context: IncidentContext) -> List['MemoryRecord']:
        """查找与当前 Incident 相似的过去事故。"""
        ...


@dataclass
class MemoryRecord:
    """
    记忆记录——系统从每次 Workflow 执行中学习。
    由 Feedback Loop 写入，Inference Engine 读取。
    """
    record_id: str
    record_type: str                    # "incident", "repair", "feedback", "root_cause"
    timestamp: datetime
    resource_type: ResourceType
    resource_id: str
    context_snapshot_id: str           # 关联的 Context Snapshot
    finding_id: str                    # 关联的 Finding
    workflow_id: str                   # 关联的 Workflow
    action_taken: str                  # 执行的操作
    outcome: str                       # "success", "failure", "partial"
    outcome_detail: str                # 结果描述
    user_rating: int = 0               # 用户评分（1-5），来自 Feedback
    embedding: List[float] = field(default_factory=list)  # 向量化索引
    created_at: datetime = field(default_factory=datetime.utcnow)
```

### 11.4 初始化

```python
# 系统启动时加载 Static Knowledge
store = KnowledgeMemoryStore()

# 注册知识来源
store.register_source("runbook", "/docs/runbooks/")
store.register_source("openstack-docs", "/docs/openstack/")
store.register_source("kubernetes-docs", "/docs/kubernetes/")

# 全量索引
store.index_all()

# Feedback Loop 运行时写入 Memory
store.add_memory(MemoryRecord(
    record_type="repair",
    outcome="success",
    action_taken="systemctl restart neutron-dhcp-agent",
    finding_id=finding.id,
    workflow_id=wf_id,
))
```

---

## 12. Planner

### 12.1 定位

同 v8。Finding → Workflow（v1 简单映射，v2+ LLM/Ivy/DSL）。

### 12.2 v9 变化：Planner 输出到 Policy Engine

```python
class Planner:
    """
    Finding → Workflow（或 Workflow 候选）。

    v9 变化：Planner 不直接执行，输出到 Policy Engine 评估。
    """

    def plan(self, finding: Finding, context: IncidentContext) -> Workflow:
        # 同 v8：Finding.recommended_action → Workflow
        ...

    def plan_with_policy(self, finding: Finding,
                          context: IncidentContext) -> PolicyEvaluationRequest:
        """产出 Workflow 并交给 Policy Engine。"""
        wf = self.plan(finding, context)
        return PolicyEvaluationRequest(
            workflow=wf,
            context=context,
            finding=finding,
        )
```

---

## 13. Policy Engine

### 13.1 定位

**v9 新增。** Planner 和 Workflow Engine 之间的治理层。

```
Planner → Policy Engine → Workflow Engine（批准后）
              ↓
          Deny / Pending Approval / Allow
```

### 13.2 Policy 模型

```python
class PolicyAction(Enum):
    ALLOW = "allow"               # 允许执行
    DENY = "deny"                 # 拒绝
    PENDING_APPROVAL = "pending"  # 需人工审批


@dataclass
class Policy:
    """
    安全策略。
    每条策略定义一个条件 + 条件满足时的动作。
    """
    policy_id: str
    name: str
    description: str
    condition: Callable[['PolicyContext'], bool]  # 是否匹配此策略
    action: PolicyAction                          # 匹配后的动作
    priority: int = 100                           # 优先级（小=高）
    approval_required: bool = False               # 是否需审批


@dataclass
class PolicyContext:
    """策略评估上下文。"""
    workflow: 'Workflow'
    finding: 'Finding'
    resource_type: ResourceType
    resource_id: str
    resource_state: Dict[str, str]
    current_time: datetime
    user: str = "system"


@dataclass
class PolicyEvaluationResult:
    """策略评估结果。"""
    policy: Policy
    matched: bool
    action: PolicyAction
    reason: str = ""
```

### 13.3 Policy Engine

```python
class PolicyEngine:
    """
    Policy Engine——安全治理层。

    Planner → PolicyEngine.evaluate() → 结果
      如果 ALLOW → Workflow Engine
      如果 DENY → 拒绝 + 原因
      如果 PENDING_APPROVAL → 等待审批
    """

    def __init__(self):
        self._policies: List[Policy] = []

    def register(self, policy: Policy):
        self._policies.append(policy)

    def evaluate(self, request: 'PolicyEvaluationRequest') -> 'PolicyDecision':
        """
        评估 Policy。所有 Policy 按优先级排序后依次评估。

        执行规则：
          - 高优先级（priority 小）优先
          - 对同 ActionType 的策略，最严格的策略生效
          - DENY > PENDING_APPROVAL > ALLOW
        """
        ctx = PolicyContext(
            workflow=request.workflow,
            finding=request.finding,
            resource_type=request.context.resource_type,
            resource_id=request.context.resource_id,
            resource_state=request.context.current_state,
            current_time=datetime.utcnow(),
        )

        results = []
        for policy in sorted(self._policies, key=lambda p: p.priority):
            if policy.condition(ctx):
                results.append(PolicyEvaluationResult(
                    policy=policy,
                    matched=True,
                    action=policy.action,
                    reason=f"Policy '{policy.name}' matched: {policy.description}",
                ))

        # 取最严格的结果
        final_action = PolicyAction.ALLOW
        final_reason = "All policies passed"
        for r in results:
            if r.action == PolicyAction.DENY:
                final_action = PolicyAction.DENY
                final_reason = r.reason
                break
            elif r.action == PolicyAction.PENDING_APPROVAL and final_action == PolicyAction.ALLOW:
                final_action = PolicyAction.PENDING_APPROVAL
                final_reason = r.reason

        return PolicyDecision(
            decision=final_action,
            reason=final_reason,
            matched_policies=[r.policy.policy_id for r in results],
            evaluated_policies=len(self._policies),
        )

    def approve(self, decision_id: str) -> bool:
        """人工审批通过一个 PENDING_APPROVAL 的决策。"""
        ...
```

### 13.4 内置策略示例

```python
engine = PolicyEngine()

# 策略 1：禁止在工作时间重启服务
engine.register(Policy(
    policy_id="no_restart_biz_hours",
    name="业务高峰期禁止重启",
    description="9:00-18:00 不允许执行重启类操作",
    condition=lambda ctx: (
        "restart" in ctx.workflow.name.lower()
        and 9 <= ctx.current_time.hour <= 18
    ),
    action=PolicyAction.DENY,
    priority=10,
))

# 策略 2：生产环境操作需审批
engine.register(Policy(
    policy_id="prod_needs_approval",
    name="生产环境操作需要审批",
    description="涉及 prod 集群的操作需要人工审批",
    condition=lambda ctx: (
        "prod" in ctx.resource_id.lower()
        or "prod" in str(ctx.resource_state.get("cluster", "")).lower()
    ),
    action=PolicyAction.PENDING_APPROVAL,
    priority=20,
))

# 策略 3：禁止同时操作超过 10 台机器
engine.register(Policy(
    policy_id="no_bulk_operation",
    name="批量操作限制",
    description="单次 Workflow 涉及超过 10 台机器时拒绝",
    condition=lambda ctx: (
        len(ctx.workflow.steps) > 10
    ),
    action=PolicyAction.DENY,
    priority=5,
))
```

### 13.5 流程图

```
Policy Evaluation:
  Planner → PolicyEngine.evaluate(Workflow)
    → 对所有 Policy 按优先级排序
    → 依次评估 condition
    → 取最严格结果
    ├── ALLOW
    │   → Workflow Engine 执行
    ├── DENY
    │   → 拒绝 + 返回 reason
    │   → 发布 platform.system 事件: policy_denied
    └── PENDING_APPROVAL
        → 等待人工审批
        → 发布 platform.system 事件: policy_pending_approval
        → Approve → Workflow Engine
        → Reject → 拒绝
```

---

## 14. Topology Engine

同 v8。

---

## 15. Workflow Engine

### 15.1 定位

同 v8。Command/Event 分离 + Capability 抽象。

### 15.2 v9 变化：集成 Feedback Loop

```python
class WorkflowEngine:
    """
    执行 Workflow。
    v9：执行完成后触发 Feedback Loop。
    """

    def execute(self, workflow: Workflow,
                context: WorkflowContext,
                finding: Optional[Finding] = None) -> WorkflowEvent:
        # 1. 发布 Command
        cmd = WorkflowCommand(...)
        self.bus.publish("platform.workflow.command", ...)

        # 2. 通过 Capability 执行
        results = []
        for step in workflow.steps:
            result = self.registry.execute(step.capability, step.params)
            results.append(result)

        # 3. 发布 Event
        outcome = "success" if all(r.success for r in results) else "failure"
        event = WorkflowEvent(
            event_id=uuid4().hex,
            event_type=f"{workflow.name}.completed",
            command_id=cmd.command_id,
            workflow_id=workflow.workflow_id,
            outcome=outcome,
            details={"steps": len(results)},
        )
        self.bus.publish("platform.workflow.event", ...)

        # 4. ← v9 新增：触发 Feedback Loop
        self.feedback_loop.evaluate(
            workflow=workflow,
            finding=finding,
            results=results,
            event=event,
        )

        return event
```

---

## 16. Feedback Loop

### 16.1 定位

**v9 核心变更。** 使系统具备学习能力的闭环。

```
Workflow Execution
    ↓
Evaluation ──→ Success? → Memory Store（记录成功经验）
    ↓                          ↓
  Failure?               下次类似 Incident 时：
    ↓                     Inference 检索到历史记忆
  Partial?               定位速度更快、更准确
    ↓
Feedback Signal → Knowledge & Memory Store
```

### 16.2 设计

```python
@dataclass
class EvaluationResult:
    """Workflow 执行评估结果。"""
    workflow_id: str
    finding_id: str
    outcome: str                          # "success", "failure", "partial"
    action_taken: str
    result_summary: str
    user_rating: int = 0                  # 用户评分（可选）
    user_comment: str = ""                # 用户反馈（可选）
    execution_duration_ms: int = 0
    error_detail: str = ""


class FeedbackLoop:
    """
    Feedback Loop——闭环学习。

    职责：
      1. 评估 Workflow 执行结果
      2. 将评估结果写入 Knowledge & Memory Store
      3. 更新模型权重（future：RLHF / Fine-tuning）
    """

    def __init__(self, memory_store: KnowledgeMemoryStore):
        self.memory_store = memory_store

    def evaluate(self, workflow: Workflow,
                 finding: Optional[Finding],
                 results: List['CapabilityResult'],
                 event: WorkflowEvent) -> EvaluationResult:
        """评估 Workflow 执行结果。"""
        # 1. 确定 outcome
        success_count = sum(1 for r in results if r.success)
        outcome = "success" if success_count == len(results) else (
            "failure" if success_count == 0 else "partial"
        )

        # 2. 构建记忆记录
        memory = MemoryRecord(
            record_type="repair",
            timestamp=datetime.utcnow(),
            resource_type=ResourceType.UNKNOWN,
            resource_id=workflow.target if hasattr(workflow, 'target') else "",
            finding_id=finding.id if finding else "",
            workflow_id=workflow.workflow_id,
            action_taken=workflow.name,
            outcome=outcome,
            outcome_detail=f"{success_count}/{len(results)} steps succeeded",
        )

        # 3. 写入 Knowledge & Memory Store
        self.memory_store.add_memory(memory)

        # 4. 发布 Platform Event
        self._publish_platform_event("feedback_recorded",
            workflow.workflow_id, outcome)

        return EvaluationResult(
            workflow_id=workflow.workflow_id,
            finding_id=finding.id if finding else "",
            outcome=outcome,
            action_taken=workflow.name,
            result_summary=memory.outcome_detail,
        )

    def user_feedback(self, workflow_id: str, rating: int,
                      comment: str = ""):
        """用户主动反馈。"""
        # 更新已有记忆的用户评价
        self.memory_store.update_feedback(workflow_id, rating, comment)

        # 发布 Platform Event
        self._publish_platform_event("user_feedback",
            workflow_id, f"rating={rating}")
```

### 16.3 学习循环

```
第一次 Incident:
  Raw Log → Context → Inference → Finding → Planner → Workflow → SSH → Restart
                                                                        ↓
                                                                  Feedback：Success
                                                                        ↓
                                                                  Memory 记录

第二次相同 Incident（1 个月后）:
  Raw Log → Context → Inference → Knowledge & Memory Store 检索
                                        ↓
                                  "上次成功方案：重启 neutron-dhcp-agent"
                                        ↓
                                  Finding 更准确、更快 → 直接定位
```

---

## 17. Platform Events

同 v8。新增以下 Platform Event 类型：

```python
# v9 新增 Platform Event 类型：
#   knowledge_retrieved       — Knowledge Store 检索完成
#   policy_evaluated          — Policy 评估完成（ALLOW / DENY / PENDING）
#   policy_denied             — Workflow 被策略拒绝
#   policy_pending_approval   — Workflow 待审批
#   feedback_recorded         — Feedback 写入 Memory
#   memory_added              — Memory 新增记录
#   projection_promoted       — Versioned Projection 流量切换
#   projection_compared       — 多版本对比结果
```

---

## 18. 置信度模型

（同 v7，不变）

---

## 19. API

```text
# Context API
GET /api/v1/context/build?type=INSTANCE&id=abc-123&context=incident&time_window=1+HOUR
GET /api/v1/context/build?type=INSTANCE&id=abc-123&context=topology&depth=2
GET /api/v1/context/build?type=INSTANCE&id=abc-123&context=workflow
GET /api/v1/context/build?type=INSTANCE&id=abc-123&context=rule
GET /api/v1/context/snapshot/{snapshot_id}

# Event Lineage（← v9 新增）
GET /api/v1/lineage/trace/{event_id}          # 返回完整血缘链（DAG）
GET /api/v1/lineage/from/{event_id}/to/{event_type}  # 从某 Event 回溯到指定类型

# Knowledge & Memory Store（← v9 新增）
GET    /api/v1/knowledge/retrieve?query=Nova+scheduler+OOM
POST   /api/v1/knowledge/sources/register
GET    /api/v1/knowledge/sources
POST   /api/v1/memory/feedback    # 用户提交反馈

# Policy Engine（← v9 新增）
GET  /api/v1/policies
POST /api/v1/policies/register
POST /api/v1/policies/evaluate   # 测试 Workflow 会否被拒绝

# Topology
GET /api/v1/topology/hybrid?time_window=1+HOUR

# Inventory / State / Interaction / Correlation（同 v8）
...

# Projection Management
GET   /api/v1/projections/status
POST  /api/v1/projections/rebuild?name=inventory
GET   /api/v1/projections/dependencies
POST  /api/v1/projections/rebuild-chain?name=graph
GET   /api/v1/projections/{name}/lag
POST  /api/v1/projections/{name}/traffic-split  # ← v9 新增（Versioned Projection）

# Schema Registry
GET  /api/v1/schemas?event_type=normalized.event
POST /api/v1/schemas/register
POST /api/v1/schemas/migrate?from=1&to=2

# Platform Events
GET /api/v1/platform/events?category=projection&limit=100
GET /api/v1/platform/events?category=policy     # ← v9 新增

# Capability
GET /api/v1/capabilities/executors
POST /api/v1/capabilities/discover  # ← v9 新增（自动发现）
```

---

## 20. 实施阶段

### Phase 0: Foundation（~2 周）

| 模块 | 内容 |
|------|------|
| Raw Event Store | 本地 WAL + Kafka `platform.raw`，EventEnvelope |
| Schema Registry | Schema 注册 + Migration 链 |
| EventEnvelope | **含 parent_event_ids** |
| Event Bus | 10 topics，domain 命名 |

### Phase 1: Event Pipeline + Semantic Engine（~1.5 周）

| 模块 | 内容 |
|------|------|
| Event Pipeline | Aggregate / Dedup / Sample / Enrich / Route |
| Semantic Engine | 只 normalize，维护血缘 |
| Schema Registry 集成 | NormalizedEvent Envelope 输出 |

### Phase 2: Projection Framework + Inventory/State（~2 周）

| 模块 | 内容 |
|------|------|
| Projection Checkpoint | Partition + Offset，lag |
| Projection Base | CheckpointedProjection |
| **Versioned Projection** | **多算法并行 + 流量切换** |
| EntityProjector | normalized → entity，维护血缘 |
| Inventory / State / Timeline | |

### Phase 3: Interaction + Correlation（~2 周）

| 模块 | 内容 |
|------|------|
| InteractionProjector | normalized → interaction，维护血缘 |
| Correlation Engine | 聚合端点对 |
| DynamicRel Projection | ClickHouse，多窗口 |
| **Lineage API** | **血缘追踪查询** |

### Phase 4: Graph + Context API（~2 周）

| 模块 | 内容 |
|------|------|
| Graph Projection | entity + interaction → Neo4j，不存状态 |
| Context API | 4 种 ContextType |
| Context Snapshot | |

### Phase 5: Knowledge + Inference + Planner（~2 周）

| 模块 | 内容 |
|------|------|
| **Knowledge & Memory Store** | **静态知识索引 + Memory 接口** |
| Inference Engine | LLM + Rule，消费 Context + Knowledge |
| Finding | 统一输出，**含 knowledge_sources + 血缘** |
| Planner | Finding → Workflow |

### Phase 6: Policy + Workflow + Feedback（~2 周）

| 模块 | 内容 |
|------|------|
| **Policy Engine** | Policy 注册 + 评估（DENY/ALLOW/APPROVAL） |
| Workflow Engine | Command/Event 分离 + Capability |
| Capability Registry | SSH + kubectl 注册 |
| **Feedback Loop** | **Evaluation + Memory 写入** |

---

## 21. 测试策略

```python
def test_event_lineage():
    """Event 携带完整 parent_event_ids，构成血缘链"""
    raw_env = EventEnvelope(event_id="raw-001", event_type="raw.log")
    norm_env = EventEnvelope(
        event_id="norm-001",
        event_type="normalized.event",
        parent_event_ids=["raw-001"],
    )
    finding_env = EventEnvelope(
        event_id="finding-001",
        event_type="inference.finding",
        parent_event_ids=["norm-001", "entity-001"],
    )
    # 血缘链完整
    assert "raw-001" in finding_env.parent_event_ids
    assert "norm-001" in finding_env.parent_event_ids


def test_lineage_dag():
    """血缘链构成有向无环图——可从 Finding 追溯回 RawEvent"""
    # 模拟 Context Builder 产生 Finding
    projector = MergingLineageProjector()
    inputs = [
        EventEnvelope(event_id="norm-001", parent_ids=["raw-001"]),
        EventEnvelope(event_id="entity-001", parent_ids=["norm-001"]),
        EventEnvelope(event_id="state-001", parent_ids=["norm-001"]),
    ]
    result = projector.project(inputs)
    # 所有上游都在 parent_ids 中
    assert "raw-001" in result.parent_event_ids
    assert "norm-001" in result.parent_event_ids
    assert "entity-001" in result.parent_event_ids


def test_knowledge_store_retrieval():
    """Knowledge & Memory Store 检索相关知识"""
    store = KnowledgeMemoryStore()
    store.add_memory(MemoryRecord(
        record_id="m1", record_type="repair",
        outcome="success",
        action_taken="restart neutron-dhcp-agent",
        finding_id="f1", workflow_id="w1",
    ))
    results = store.retrieve("neutron agent no response", max_results=5)
    assert len(results) >= 1
    assert "neutron" in results[0].content.lower()


def test_knowledge_enhanced_inference():
    """Inference 输入包含 Knowledge"""
    inference = LLMInferenceEngine(knowledge_store, llm)
    input = InferenceInput(
        context=mock_incident_context,
        knowledge=[
            KnowledgeDocument(
                document_id="kb-001",
                title="Nova OOM Troubleshooting",
                content="When Nova scheduler runs out of memory...",
                source_type="runbook",
            ),
        ],
    )
    findings = inference.infer(input)
    assert len(findings) > 0
    # Finding 记录引用源
    assert "kb-001" in findings[0].knowledge_sources


def test_policy_deny():
    """Policy Engine 拒绝违反策略的操作"""
    engine = PolicyEngine()
    engine.register(Policy(
        policy_id="no_restart_biz_hours",
        name="业务高峰期禁止重启",
        condition=lambda ctx: "restart" in ctx.workflow.name.lower()
                            and 9 <= ctx.current_time.hour <= 18,
        action=PolicyAction.DENY,
    ))

    wf = Workflow(name="restart_service", steps=[])
    ctx = PolicyContext(workflow=wf, current_time=datetime(2026, 7, 1, 14, 0))
    decision = engine.evaluate(PolicyEvaluationRequest(workflow=wf, context=mock_ctx))

    assert decision.decision == PolicyAction.DENY


def test_policy_allow():
    """Policy Engine 允许合规操作"""
    engine = PolicyEngine()
    engine.register(Policy(
        policy_id="no_restart_biz_hours",
        condition=lambda ctx: "restart" in ctx.workflow.name.lower()
                            and 9 <= ctx.current_time.hour <= 18,
        action=PolicyAction.DENY,
    ))

    wf = Workflow(name="describe_pod", steps=[])
    decision = engine.evaluate(PolicyEvaluationRequest(workflow=wf, context=mock_ctx))
    assert decision.decision == PolicyAction.ALLOW


def test_policy_pending_approval():
    """生产环境操作需审批"""
    engine = PolicyEngine()
    engine.register(Policy(
        policy_id="prod_needs_approval",
        condition=lambda ctx: "prod" in ctx.resource_id,
        action=PolicyAction.PENDING_APPROVAL,
    ))
    wf = Workflow(name="restart_service", steps=[])
    mock_ctx.resource_id = "prod-db-01"
    decision = engine.evaluate(PolicyEvaluationRequest(workflow=wf, context=mock_ctx))
    assert decision.decision == PolicyAction.PENDING_APPROVAL


def test_feedback_loop_writes_memory():
    """Feedback Loop 将执行结果写入 Memory"""
    store = KnowledgeMemoryStore()
    loop = FeedbackLoop(store)
    loop.evaluate(
        workflow=Workflow(name="restart_service", steps=[]),
        finding=None,
        results=[MockCapabilityResult(success=True)],
        event=MockWorkflowEvent(outcome="success"),
    )
    memories = store.retrieve("restart_service", max_results=10)
    assert len(memories) >= 1
    assert memories[0].outcome == "success"


def test_feedback_loop_learning():
    """系统从 Feedback 中学习——第二次查询更准确"""
    store = KnowledgeMemoryStore()
    loop = FeedbackLoop(store)

    # 第一次：修复失败
    loop.evaluate(
        workflow=Workflow(name="restart_neutron", steps=[]),
        results=[MockCapabilityResult(success=False)],
    )

    # 第二次：检索到失败记录
    results = store.retrieve("neutron down", max_results=5)
    failed_repairs = [r for r in results
                      if r.record_type == "repair" and r.outcome == "failure"]
    assert len(failed_repairs) >= 1


def test_versioned_projection_traffic_split():
    """Versioned Projection 支持流量分比例切换"""
    registry = VersionedProjectionRegistry("graph")
    v1 = MockProjection("hashmap")
    v2 = MockProjection("list")
    registry.add_version(v1, traffic=0.9)
    registry.add_version(v2, traffic=0.1)

    # 10% 流量到 v2
    calls_v1 = sum(1 for _ in range(1000)
                   if v1 in registry.route(MockEvent()))
    calls_v2 = 1000 - calls_v1
    assert 50 < calls_v2 < 150  # 期望 ~100

    # 全量切换到 v2
    registry.promote("list")
    assert registry._traffic["list"] == 1.0


def test_inference_from_knowledge():
    """Inference 使用 Knowledge Store 的 RAG 结果"""
    store = KnowledgeMemoryStore()
    store.add_memory(MemoryRecord(
        record_id="m1", record_type="repair",
        outcome="success",
        action_taken="restart nova-compute",
    ))

    inference = LLMInferenceEngine(store, mock_llm)
    findings = inference.infer(InferenceInput(
        context=mock_incident_context,
        knowledge=store.retrieve("nova compute not responding"),
    ))
    assert len(findings) > 0
    assert "nova" in findings[0].reason.lower()


def test_event_envelope_with_lineage():
    """EventEnvelope 必须携带 parent_event_ids"""
    env = EventEnvelope(
        envelope_version="v1",
        schema_version=1,
        event_type="test.event",
        producer="test",
        event_id="test-001",
        parent_event_ids=["parent-001", "parent-002"],
    )
    assert len(env.parent_event_ids) == 2


def test_knowledge_memory_store_types():
    """Knowledge & Memory Store 区分 Static/Memory"""
    store = KnowledgeMemoryStore()
    # Static Knowledge
    store.register_source("runbook", "/path/to/runbooks/")
    # Memory
    store.add_memory(MemoryRecord(
        record_id="m1", record_type="incident",
        outcome="success",
    ))
    # 可以分别查询
    static = store.retrieve("test", source_types=["runbook"])
    memory = store.retrieve("test", source_types=["incident"])
    assert isinstance(static, list)
    assert isinstance(memory, list)


def test_full_observe_learn_act_loop():
    """完整的 Observe → Understand → Decide → Act → Learn 闭环"""
    # 1. Observe：Raw Event → Normalized Event
    raw = RawEvent(raw_payload="nova-api: ERROR Connection refused")
    raw_env = schema_registry.serialize("raw.log", asdict(raw), "ingest")
    norm = SemanticEngine(schema_registry, bus).process(raw_env)

    # 2. Understand：Context → Knowledge → Finding
    context = context_api.build(ResourceType.INSTANCE, "abc-123")
    knowledge = knowledge_store.retrieve("nova connection error")
    findings = inference_engine.infer(InferenceInput(context, knowledge))
    assert len(findings) > 0
    assert len(findings[0].knowledge_sources) >= 1  # 引用了知识

    # 3. Decide：Finding → Planner → Policy
    wf = planner.plan(findings[0], context)
    decision = policy_engine.evaluate(PolicyEvaluationRequest(wf, context, findings[0]))
    if decision.decision == PolicyAction.ALLOW:
        # 4. Act：Workflow → Capability → Action
        event = workflow_engine.execute(wf, workflow_context, findings[0])
        assert event.outcome in ("success", "failure")

        # 5. Learn：Feedback → Memory
        feedback = feedback_loop.evaluate(wf, findings[0], results, event)
        assert feedback.outcome in ("success", "failure")
        # Memory 已更新，下次查询更准确
        related = knowledge_store.retrieve("nova connection error")
        assert len(related) >= 1
```

---

## 22. 性能考量

（同 v8，新增 Knowledge Store 和 Policy 的基准线）

| 场景 | 预期 | 瓶颈 | 扩展方式 |
|------|------|------|----------|
| Knowledge Store 检索 | < 200ms（P99） | 向量搜索 | HNSW 索引，GPU |
| Policy Evaluation | < 10ms | 条件运算 | 静态编译条件 |
| Feedback Loop 写入 | < 50ms | Memory Store 写入 | 异步写入 |
| Lineage 查询 | < 100ms | Graph DB 遍历 | 索引 parent_event_ids |

---

## 23. 向后兼容

| 影响点 | 策略 |
|--------|------|
| EventEnvelope parent_ids | 存量 parent_event_ids=[]，不影响功能 |
| Knowledge Store | 新部署后静态知识首次加载需时间 |
| Policy Engine | 默认空策略（ALLOW all），不影响现有 Workflow |
| Feedback Loop | 默认空操作，不影响现有 Workflow 执行 |
| Versioned Projection | 默认单版本，不影响现有 Projection |
| Lineage API | 新端点，存量 event 无血缘 |

---

## 24. v8 → v9 变更对照

| 维度 | v8 | v9 |
|------|----|----|
| **EventEnvelope** | event_id, payload, ... | **+parent_event_ids**（构建完整血缘链） |
| **Lineage** | 不可追溯 | **每个 Event 可追溯全部来源**（DAG） |
| **Inference 输入** | IncidentContext | **IncidentContext + Knowledge（RAG）** |
| **Knowledge** | 无 | **Knowledge & Memory Store**（Static + Memory） |
| **Memory** | 无 | **MemoryRecord**（Feedback Loop → Inference） |
| **Inference → Planner** | 直接 | **Inference → Planner → Policy Engine → Workflow** |
| **Policy Engine** | 无 | **Policy 模型（ALLOW/DENY/PENDING_APPROVAL）+ 评估引擎** |
| **Feedback Loop** | 无 | **Evaluation → Memory 写入 → 后续 Inference 受益** |
| **Projection 算法切换** | 手动替换 | **VersionedProjectionRegistry**（多版本并行 + 流量切换） |
| **Finding** | 无知识来源引用 | **+knowledge_sources**（引用了哪些知识文档） |
| **闭环** | Observe → Decide → Act | **Observe → Understand → Decide → Act → Learn** |
| **Platform Events** | 8 种 | **+8 种**（policy, knowledge, feedback, projection） |
| **API** | ~20 端点 | **+8 端点**（lineage, knowledge, policy, feedback） |
| **实施阶段** | 6 phases | **7 phases**（Phase 5: Knowledge + Inference） |
| **测试** | 28 个 | **30+ 个**（lineage, knowledge store, policy, feedback, versioned） |
