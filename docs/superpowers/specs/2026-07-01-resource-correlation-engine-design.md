# Logoscope Data Architecture v1 — AI Observability Operating System

> **Event Sourcing + CQRS + Goal-Driven + Decision State Machine + Episode Learning**
>
> Goal 描述目标状态（不是 Workflow）。WorldView 是组合式查询（不是 God Object）。Capability 用结构化表达式声明条件。Episode 记录完整决策路径。Orchestrator 编排生命周期。

**Status:** Draft v15
**Date:** 2026-07-01
**Authors:** croner01, Claude

---

## 1. Problem

### 1.1 v14 的系统性问题

v14 引入了 Decision State Machine、WorldView、Episode Memory 和 Blast Radius Analyzer，但仍有根本性问题：

**① WorldView 正在变成 God Object。**
`WorldView` 同时承担 Topology、State、History、Impact Estimate、Alarm 查询——10+ 方法挤在一个类里。Planner、RiskEngine、BlastRadiusAnalyzer 全部依赖它。后续会膨胀到 50+ 方法。

**② Goal 的语义是 Workflow，不是目标状态。**
`GoalNode.action = "restart"`、`GoalNode.ordering = "sequential"`——这些是 Workflow 的概念。Goal 应该描述"什么状态应该达到"（RabbitMQ healthy），不是"怎么做"（restart、verify）。

**③ Capability Precondition 是字符串，无法程序化检查。**
`preconditions: List[str] = ["host.alive", "service.exists"]` —"alive"和"exists"没有标准定义。Planner 无法自动求值。

**④ Episode 缺少决策理由。**
Episode 记录 observation / hypothesis / workflow / outcome，但不记录"为什么没选 Candidate A"或"为什么选 Candidate B"。这是 LLM 训练最宝贵的数据。

**⑤ DecisionManager 职责过重。**
既做 orchestrator（串 Planner → Execution → Risk → Policy → Execution），又做 lifecycle management。可以拆分为更专注的组件。

### 1.2 v15 核心理念

```
WorldView 是 Facade——内部由 TopologyQuery / StateQuery / HistoryQuery 实现。
Goal 描述目标状态（desired_state），不含 action/ordering/completion 等 Workflow 概念。
Capability 用 Expression 结构化表达式声明条件——可被 Planner 自动求值。
Episode 增加 DecisionStep——记录候选方案评分和拒绝理由。
DecisionManager 拆为 DecisionOrchestrator + DecisionStateMachine。
Utility 权重通过 OPA Rego or Config 配置。
Blast Radius 综合考虑 Capability Metadata + Dependency Graph + Current State。
```

### 1.3 v14 → v15 变更

| 维度 | v14 | v15 |
|------|-----|-----|
| **WorldView** | 单体类（10+ 方法） | **Facade** + TopologyQuery/StateQuery/HistoryQuery |
| **Goal** | `action` + `ordering` + `completion`（Workflow 化） | **`desired_state` + `target`**（目标状态） |
| **Precondition** | `List[str]`（字符串） | **`List[Expression]`**（field + operator + value） |
| **EpisodeStep** | observation / hypothesis / goal / workflow / execution / outcome | **+ `DecisionStep`**（candidate_scores + reject_reasons） |
| **DecisionManager** | lifecycle + orchestration 混合 | **DecisionOrchestrator** + **DecisionStateMachine**（分离） |
| **Utility 权重** | 硬编码（0.5/0.3/0.1/0.05） | **OPA Rego 可配置** |
| **Blast Radius 输入** | 仅 Dependency Graph | **+ Capability Metadata + Current State** |
| **ExperienceGraph Key** | `(capability_id, env_fingerprint)` | **`(failure_pattern, capability_id, env_fingerprint)`** |
| **API** | — | **+`/worldview/topology`、`/worldview/state`、`/expressions/evaluate`** |
| **测试** | 50+ | **55+** |

---

## 2. Architecture

### 2.1 整体架构（更新）

```
Raw Event → Projection Layer
                │
                ▼
            WorldView（Facade）
              ├── TopologyQuery（DAG + Dependents + Impact Set）
              ├── StateQuery（Current State + Timeline）
              └── HistoryQuery（Recent Events + Alarms）
                │
                ▼
            Inference Registry → Pipeline → Finding（无 recommended_action）
                │
                ▼
            GoalInferrer → Goal Tree（目标状态节点，不含 action/ordering）
                │
                │  GoalNode: RabbitMQ.healthy
                │    ├── GoalNode: NovaAPI.responding
                │    └── GoalNode: Neutron.connected
                │
                ▼
            IntentGenerator（Finding + GoalNode → PlanIntent）
                │
                ▼
            ExecutionPlanner（Intent → WorkflowCandidate）
                │
                ▼
            Blast Radius Analyzer（Capability Metadata + Dependency + State）
                │
                ▼
            RiskEngine（三层 + Constraint + Expression 检查）
                │
                ▼
            PolicyEngine（Utility 权重可配置 + OPA）
                │
                ▼ ═══════ Decision State Machine ═══════
                │
            DecisionOrchestrator（编排 Planner→Evaluate→Policy→Execute）
            DecisionStateMachine（纯生命周期管理）
                │
                ▼
            Workflow Engine → Execution Event
                │
                ▼
            Episode（含 DecisionStep）
                │
                ├── ExperienceGraphProjection（+ Failure Pattern 维度）
                └── CapabilityStatsProjector
```

### 2.2 原则

```
新增原则 37: WorldView 是 Facade——组合多个 Query 接口，不负责任何查询实现。
新增原则 38: Goal 描述目标状态（desired_state）。Workflow 描述如何达到目标状态。两者不同。
新增原则 39: Capability 条件用结构化 Expression 声明——可被程序自动求值。
新增原则 40: 所有决策理由必须记录在 Episode 中——包括"为什么不选 A"。
新增原则 41: Decision Orchestration 和 State Management 是两种不同职责，由不同组件承担。
```

---

## 3. Event Schema

同 v9。

## 4. Event Bus

同 v9。Topic 列表同 v14。

## 5. Event Pipeline

同 v9。

## 6. Semantic Engine

同 v9。

## 7. Projection Layer

同 v13（Snapshot 含版本元数据）。

## 8. Correlation Engine

同 v9。

---

## 9. WorldView（v15：组合式 Query 接口）

### 9.1 Query 接口层（v15 新增）

```python
class TopologyQuery:
    """拓扑查询——依赖关系、Imapt Set。"""

    def __init__(self, graph_projection: GraphProjection):
        self.graph = graph_projection

    def get_dependents(self, rid: ResourceIdentity) -> List[ResourceIdentity]:
        """谁依赖此资源。"""
        return self.graph.get_downstream(rid.resource_type, rid.resource_id)

    def get_dependencies(self, rid: ResourceIdentity) -> List[ResourceIdentity]:
        """此资源依赖谁。"""
        return self.graph.get_upstream(rid.resource_type, rid.resource_id)

    def get_impact_set(self, rid: ResourceIdentity, depth: int = 3
                       ) -> List[List[ResourceIdentity]]:
        """按 BFS 层返回影响集合。"""
        return self.graph.bfs_downstream(rid, depth)

    def query_path(self, from_rid: ResourceIdentity,
                   to_rid: ResourceIdentity) -> List[ResourceIdentity]:
        """查询两个资源间的通路。"""
        return self.graph.find_path(from_rid, to_rid)


class StateQuery:
    """状态查询——当前状态、状态演化链。"""

    def __init__(self, state_projection: StateProjection,
                 timeline_projection: TimelineProjection):
        self.state = state_projection
        self.timeline = timeline_projection

    def get_state(self, rid: ResourceIdentity) -> Optional[str]:
        """当前状态（ACTIVE / ERROR / SHUTOFF / ...）。"""
        return self.state.query(rid.resource_type, rid.resource_id)

    def get_states(self, rids: List[ResourceIdentity]) -> Dict[str, str]:
        """批量查询。"""
        return {str(r): self.state.query(r.resource_type, r.resource_id)
                for r in rids}

    def get_timeline(self, rid: ResourceIdentity, window: str = "1 HOUR"
                     ) -> List[StateTransition]:
        """状态演化链。"""
        return self.timeline.get_timeline(rid, window)

    def has_state_changed(self, rid: ResourceIdentity,
                          window: str = "5 MINUTE") -> bool:
        """指定窗口内状态是否变化过。"""
        timeline = self.get_timeline(rid, window)
        return len(timeline) > 0

    def resolve_field(self, field_path: str, target: ResourceIdentity
                      ) -> Any:
        """按字段路径解析当前值——供 Expression 求值使用。

        "host.status" → worldview.state.get_state(host_rid)
        "vm.vcpus" → worldview.inventory.get_attribute(vm_rid, "vcpus")
        """
        parts = field_path.split(".")
        if parts[0] == "resource":
            return self.state.get_state(target)
        elif parts[0] == "host":
            host_id = self._resolve_host(target)
            return self.state.get_state(ResourceIdentity(ResourceType.HOST, host_id))
        elif parts[0] == "service":
            return self.state.get_state(target)
        ...


class HistoryQuery:
    """历史事件查询。"""

    def __init__(self, event_store: RawEventStore):
        self.event_store = event_store

    def get_recent_events(self, rid: ResourceIdentity,
                          count: int = 50) -> List[EventEnvelope]:
        return self.event_store.query(resource=rid, limit=count)

    def get_alarms(self, rid: ResourceIdentity) -> List[Dict]:
        ...

    def get_events_by_type(self, event_type: str,
                           window: str = "1 HOUR") -> List[EventEnvelope]:
        ...
```

### 9.2 WorldView Facade（v15：组合而非实现）

```python
class WorldView:
    """
    世界视图——AI 组件的统一查询入口。

    v15: Facade 模式——组合多个 Query 接口，不负责任何查询的实现逻辑。
         避免 God Object——新增查询能力 = 新增 Query 类，不膨胀 WorldView 自身。
    """

    def __init__(self, topology: TopologyQuery,
                 state: StateQuery,
                 history: HistoryQuery):
        self.topology = topology
        self.state = state
        self.history = history
```

### 9.3 ContextAPI + CanonicalContextHasher

同 v14。ContextAPI 内部使用 WorldView 查询状态。

---

## 10. Inference Engine

同 v13（InferenceRegistry + Pipeline + Finding 不含 recommended_action）。

---

## 11. Knowledge & Memory（v15：Expression 类型）

### 11.1 Expression（v15 新增——结构化条件表达式）

```python
@dataclass
class Expression:
    """
    结构化条件表达式——可被 Planner 和 Policy 自动求值。

    v15: 替代字符串 precondition/postcondition。
         不再有"host.alive"这种模糊字符串——而是明确的字段路径 + 操作符 + 值。
    """
    field: str                          # "resource.status", "host.host_status",
                                        # "service.exists", "ssh.accessible"
    operator: str                       # "==", "!=", "in", "not_in",
                                        # "exists", "not_exists", "contains"
    value: Any = None                   # 比较值（for "==", "!=", "in", ...）
                                        # None for "exists", "not_exists"

    def evaluate(self, worldview: WorldView, target: ResourceIdentity) -> bool:
        """使用 WorldView 求值。"""
        actual = worldview.state.resolve_field(self.field, target)
        if self.operator == "==":
            return actual == self.value
        elif self.operator == "!=":
            return actual != self.value
        elif self.operator == "in":
            return actual in self.value
        elif self.operator == "not_in":
            return actual not in self.value
        elif self.operator == "exists":
            return actual is not None
        elif self.operator == "not_exists":
            return actual is None
        elif self.operator == "contains":
            return self.value in actual if isinstance(actual, (list, str)) else False
        return False

    def __str__(self) -> str:
        return f"{self.field} {self.operator} {self.value}"


# 预定义常用 Expression——方便 Capability 声明
def expr_status_eq(status: str) -> Expression:
    return Expression(field="resource.status", operator="==", value=status)

def expr_host_alive() -> Expression:
    return Expression(field="host.host_status", operator="==", value="alive")

def expr_service_exists() -> Expression:
    return Expression(field="service.exists", operator="==", value=True)

def expr_not_pinned() -> Expression:
    return Expression(field="vm.pinned_to_host", operator="!=", value=True)
```

### 11.2 Constraint Knowledge（v15：使用 Expression）

```python
@dataclass
class Constraint(KnowledgeObject):
    """约束——使用 Expression 表达条件。"""
    source_type: str = "constraint"
    applies_to: str = ""                # "restart_service", "migrate_vm", "*"
    condition: Expression = None        # 适用条件（用 Expression 表达）
    restriction: str = ""               # 限制内容
    severity: str = "error"             # "error", "warning"
    policy_hint: str = ""
```

---

## 12. Planner（v15：Goal = 目标状态，不是 Workflow）

### 12.1 GoalNode（v15：只描述目标状态）

```python
@dataclass
class GoalNode:
    """
    目标状态节点——只描述"什么状态应该达到"。

    v15: 不再有 action/ordering/completion 等 Workflow 概念。
         action → Workflow Composer 负责。
         ordering → ExecutionPlanner 的优化问题。
         completion → 验证环节的任务。
    """
    goal_id: str
    desired_state: str                  # "RabbitMQ.healthy", "NovaAPI.responding",
                                        # "Neutron.connected"
    target: ResourceIdentity            # 哪个资源要达到这个状态
    children: List['GoalNode'] = field(default_factory=list)
    # 不含: action, ordering, completion_criteria, status


@dataclass
class Goal:
    """顶层目标——只描述目标状态，不描述执行步骤。"""
    primary: str                        # "restore_messaging"
    tree: GoalNode
    priority: int = 50
    reason: str = ""
```

### 12.2 Goal 示例

```python
# v15: Goal 描述目标状态
goal = Goal(
    primary="restore_messaging",
    tree=GoalNode(
        goal_id="root",
        desired_state="Cluster.healthy",
        target=ResourceIdentity(ResourceType.CLUSTER, "rabbitmq-prod"),
        children=[
            GoalNode(
                goal_id="mq-ready",
                desired_state="RabbitMQ.healthy",
                target=ResourceIdentity(ResourceType.SERVICE, "rabbitmq-server"),
            ),
            GoalNode(
                goal_id="nova-ready",
                desired_state="NovaAPI.responding",
                target=ResourceIdentity(ResourceType.SERVICE, "nova-api"),
                children=[
                    GoalNode(
                        goal_id="nova-mq",
                        desired_state="NovaAPI.rabbitmq_connected",
                        target=ResourceIdentity(ResourceType.SERVICE, "nova-api"),
                    ),
                ],
            ),
        ],
    ),
)

# v14 对比（Workflow 化）:
# GoalNode(action="restart_rabbitmq", ordering="sequential", ...)  ← 这是 Workflow
# v15: GoalNode(desired_state="RabbitMQ.healthy", ...)              ← 这是 Goal
```

### 12.3 IntentGenerator（v15：匹配 GoalNode.desired_state）

```python
class IntentGenerator(ABC):
    @abstractmethod
    def can_handle(self, finding: Finding, goal_node: GoalNode,
                   worldview: WorldView) -> bool:
        """此 Generator 能否帮助达到 goal_node 描述的目标状态。"""
        ...

    @abstractmethod
    def generate(self, finding: Finding, goal_node: GoalNode,
                 worldview: WorldView) -> Optional[PlanIntent]:
        ...


class RestartIntentGenerator(IntentGenerator):
    """重启——适用于"service healthy"类目标状态。"""
    def can_handle(self, finding, goal_node, worldview) -> bool:
        desired = goal_node.desired_state
        # 检查约束
        constraints = self.knowledge_store.get_constraints("restart_service")
        if any(c.severity == "error" for c in constraints):
            return False
        return any(keyword in desired
                   for keyword in ["healthy", "responding", "connected"])

    def generate(self, finding, goal_node, worldview) -> Optional[PlanIntent]:
        state = worldview.state.get_state(goal_node.target)
        if state == "ERROR":
            return PlanIntent(action="restart_service",
                              target=goal_node.target, ...)
        return None


class DiagnosticIntentGenerator(IntentGenerator):
    """诊断——适用于"unknown"类目标状态或缺少证据的场景。"""
    def can_handle(self, finding, goal_node, worldview) -> bool:
        return ("unknown" in goal_node.desired_state or
                "diagnose" in goal_node.desired_state or
                finding.confidence < 0.5)

    def generate(self, finding, goal_node, worldview) -> Optional[PlanIntent]:
        return PlanIntent(action="collect_diagnostic",
                          target=goal_node.target, ...)


class FailoverIntentGenerator(IntentGenerator):
    """故障转移——适用于"可用性"类目标状态。"""
    def can_handle(self, finding, goal_node, worldview) -> bool:
        desired = goal_node.desired_state
        return ("available" in desired or "failover" in desired) and \
               self._has_standby(goal_node.target, worldview)

    def generate(self, finding, goal_node, worldview) -> Optional[PlanIntent]:
        return PlanIntent(action="failover",
                          target=goal_node.target, ...)
```

### 12.4 Planner（v15：Goal Tree 构建 + Intent 生成）

```python
class GoalInferrer:
    """Finding → Goal Tree（目标状态树）。"""

    def infer(self, finding: Finding, context: Context,
              worldview: WorldView) -> Goal:
        # 示例：RabbitMQ 心跳丢失 → 目标状态是恢复消息层
        if finding.category == "RabbitMQHeartbeatLost":
            primary_target = finding.affected_entities[0]
            return Goal(
                primary="restore_messaging",
                tree=GoalNode(
                    goal_id="root",
                    desired_state="Cluster.healthy",
                    target=primary_target,
                    children=[
                        GoalNode(goal_id="svc",
                                 desired_state="RabbitMQ.healthy",
                                 target=primary_target),
                        GoalNode(goal_id="nova",
                                 desired_state="NovaAPI.responding",
                                 target=self._find_nova_api(finding, worldview)),
                    ],
                ),
                priority=90,
                reason="RabbitMQ heartbeat lost → restore messaging cluster",
            )
        # 默认：收集证据
        return Goal(
            primary="collect_evidence",
            tree=GoalNode(goal_id="root",
                          desired_state="evidence_collected",
                          target=finding.affected_entities[0]),
            priority=50,
        )


class Planner:
    """
    Finding → Goal Tree → Intent（通过 IntentGenerator + WorldView）。

    v15: Goal 描述目标状态。每个 GoalNode 独立匹配 IntentGenerator。
    """

    def plan(self, finding: Finding, context: Context,
             goal: Goal = None) -> PlannerResult:
        if not goal:
            goal = self.goal_inferrer.infer(finding, context, self.worldview)

        # 遍历 Goal Tree，为每个节点生成 Intent
        intents = []
        self._plan_goal_node(goal.tree, finding, intents)

        return PlannerResult(
            finding_id=finding.id,
            goal=goal,
            intents=intents,
        )

    def _plan_goal_node(self, node: GoalNode, finding: Finding,
                        intents: List[PlanIntent]):
        """递归处理 Goal 树中的每个节点。"""
        for gen in self.generators:
            if gen.can_handle(finding, node, self.worldview):
                intent = gen.generate(finding, node, self.worldview)
                if intent:
                    intents.append(intent)
        for child in node.children:
            self._plan_goal_node(child, finding, intents)
```

---

## 13. Policy + Risk + Utility + Blast Radius（v15：可配置 Utility + 完整 Blast Radius）

### 13.1 Blast Radius Analyzer（v15：综合 3 个输入）

```python
@dataclass
class ImpactModel:
    """影响模型——从 Capability Metadata 提取。"""
    severity: str                # "temporary", "permanent", "degradation"
    duration: str                # "30s", "5min", "permanent"
    scope: str                   # "service", "instance", "data", "network"


class BlastRadiusAnalyzer:
    """
    影响范围分析器——综合 Capability + Dependency + State。

    v15:
      - Capability.side_effects → ImpactModel（影响类型 + 持续时间）
      - Dependency Graph → 谁受影响
      - Current State → 影响程度调整
    """

    def analyze(self, intent: PlanIntent, target: ResourceIdentity,
                capability: Capability, worldview: WorldView) -> BlastRadiusReport:
        # 1. Capability Metadata
        impact = self._get_impact_model(capability)

        # 2. Dependency Graph
        impact_sets = worldview.topology.get_impact_set(target, depth=5)
        directly = impact_sets[0] if impact_sets else []

        # 3. Current State 调整
        current_state = worldview.state.get_state(target)
        risk_level = self._assess_risk_level(impact, current_state, directly)

        return BlastRadiusReport(
            primary_target=target,
            directly_affected=directly,
            indirectly_affected=self._flatten(impact_sets[1:]) if len(impact_sets) > 1 else [],
            estimated_vm_count=worldview.topology.estimate_vm_count(target),
            estimated_service_count=len(directly),
            risk_level=risk_level,
            reasoning=self._build_reasoning(impact, current_state, risk_level),
        )

    def _get_impact_model(self, capability: Capability) -> ImpactModel:
        if any("delete" in e for e in capability.effects):
            return ImpactModel("permanent", "permanent", "data")
        if any("restart" in e for e in capability.effects):
            return ImpactModel("temporary", "30s", "service")
        if any("migrate" in e for e in capability.effects):
            return ImpactModel("temporary", "5s", "instance")
        return ImpactModel("degradation", "unknown", "service")

    def _assess_risk_level(self, impact: ImpactModel, current_state: str,
                            dependents: list) -> str:
        if impact.severity == "permanent":
            return "critical"
        if impact.severity == "temporary" and len(dependents) > 10:
            return "high"
        if current_state == "ERROR" and len(dependents) > 5:
            return "high"
        return "medium" if len(dependents) > 2 else "low"
```

### 13.2 RiskEngine（v15：Expression 检查）

```python
class RiskEngine:
    def compute(self, intent: PlanIntent, candidate: WorkflowCandidate,
                context: Context, worldview: WorldView) -> RiskProfile:
        business_risk = self._business_risk(intent.action)
        execution_risk = self._execution_risk(candidate)
        operational_risk = self._operational_risk(context)

        # Blast Radius
        blast = self.blast_analyzer.analyze(intent, intent.target,
                                             self._get_capability(candidate),
                                             worldview)
        if blast.risk_level in ("critical", "high"):
            operational_risk += 30 if blast.risk_level == "critical" else 15

        # Constraint 检查（使用 Expression）
        constraints = self.knowledge_store.get_constraints(intent.action, context)
        for c in constraints:
            if c.condition:
                if c.condition.evaluate(worldview, intent.target):
                    if c.severity == "error":
                        operational_risk += 50
                        candidate.blocked_reason = c.restriction

        return RiskProfile(
            business_risk=business_risk,
            execution_risk=execution_risk,
            operational_risk=operational_risk,
        )
```

### 13.3 PolicyEngine（v15：可配置 Utility 权重）

```python
@dataclass
class UtilityWeights:
    """Utility 权重配置——可被 OPA 或 Config 覆盖。"""
    success: float = 0.5
    risk: float = 0.3
    cost: float = 0.1
    blast: float = 0.05


class PolicyEngine:
    def __init__(self, opa_endpoint, policy_dir, decision_store=None,
                 weights: Optional[UtilityWeights] = None):
        self.weights = weights or UtilityWeights()
        ...

    def _rank(self, candidates: List[WorkflowCandidate],
              intent: PlanIntent, worldview: WorldView) -> List[WorkflowCandidate]:

        blast = self.blast_analyzer.analyze(intent, intent.target, ...)

        def utility(c: WorkflowCandidate) -> float:
            risk = c.risk_profile.final_risk if c.risk_profile else 50
            return (
                c.estimated_success_rate * 100 * self.weights.success
                - risk * self.weights.risk
                - c.estimated_duration_minutes * self.weights.cost
                - blast.estimated_vm_count * self.weights.blast
            )

        return sorted(candidates, key=utility, reverse=True)
```

### 13.4 OPA Rego（v15：可配置 Utility weights）

```rego
package logoscope.policy

# Utility 权重通过 data.logoscope.utility_weights 注入
# 不同环境可有不同权重：
#   生产:  {"success": 0.4, "risk": 0.4, "cost": 0.1, "blast": 0.1}
#   测试:  {"success": 0.6, "risk": 0.1, "cost": 0.2, "blast": 0.1}

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
```

### 13.5 ExecutionPlanner

同 v14，集成 Blast Radius Analyzer + Expression 检查。

---

## 14. Topology Engine

同 v9。

---

## 15. Workflow Engine + Capability（v15：Expression Pre/Post + ImpactModel）

### 15.1 Capability（v15：Expression + ImpactModel）

```python
@dataclass
class Expression:
    field: str
    operator: str           # "==", "!=", "in", "exists", "contains"
    value: Any = None


@dataclass
class Capability:
    capability_id: str
    name: str
    provider: str
    effects: List[str] = field(default_factory=list)
    base_risk: int = 50
    risk_reason: str = ""

    # v15: 结构化表达式（不再是字符串）
    preconditions: List[Expression] = field(default_factory=list)
    postconditions: List[Expression] = field(default_factory=list)
    side_effects: List[str] = field(default_factory=list)
    impact_model: Optional['ImpactModel'] = None  # 影响模型（Blast Radius 用）
    rollback_capability: str = ""

    estimated_duration_ms: int = 30000
    estimated_cost: float = 1.0
    parameters: Dict[str, ParameterDef] = field(default_factory=dict)
    timeout_seconds: int = 30
    retry_count: int = 0
```

### 15.2 注册示例

```python
registry.register(Capability(
    capability_id="ssh.restart_service",
    provider="ssh-executor",
    effects=["service.restart", "process.modify"],
    base_risk=50,
    preconditions=[
        Expression("host.host_status", "==", "alive"),
        Expression("service.exists", "==", True),
        Expression("ssh.accessible", "==", True),
    ],
    postconditions=[
        Expression("resource.status", "==", "running"),
        Expression("service.active", "==", True),
    ],
    side_effects=["service.restart -> 30s_connection_drop"],
    impact_model=ImpactModel("temporary", "30s", "service"),
    rollback_capability="ssh.restart_service",
))

registry.register(Capability(
    capability_id="openstack.migrate_vm",
    provider="openstack-api",
    effects=["vm.migrate", "network.modify"],
    base_risk=60,
    preconditions=[
        Expression("resource.status", "==", "ACTIVE"),
        Expression("host.host_status", "==", "alive"),
        Expression("vm.pinned_to_host", "!=", True),
    ],
    postconditions=[
        Expression("resource.status", "==", "ACTIVE"),
    ],
    side_effects=["vm.migrate -> 5s_network_interruption"],
    impact_model=ImpactModel("temporary", "5s", "instance"),
    rollback_capability="openstack.migrate_vm",
))
```

### 15.3 Planner 使用 Expression

```python
class WorkflowComposer:
    def _check_preconditions(self, capability: Capability,
                              target: ResourceIdentity, worldview: WorldView) -> bool:
        """使用 Expression 自动检查前置条件。"""
        for expr in capability.preconditions:
            if not expr.evaluate(worldview, target):
                logger.info(f"Precondition failed: {expr} for {target}")
                return False
        return True
```

---

## 16. Feedback + Episode（v15：DecisionStep）

### 16.1 EpisodeStep（v15：增加 DecisionStep）

```python
@dataclass
class EpisodeStep:
    order: int
    step_type: str                    # "observation", "hypothesis", "goal_choice",
                                      # "decision", "intent", "workflow",
                                      # "execution", "outcome", "user_feedback",
                                      # "reflection"（P3）
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class DecisionStep(EpisodeStep):
    """决策步骤——记录"为什么选这个"。

    v15 新增。这是 LLM 训练最宝贵的数据。
    """
    step_type: str = "decision"
    candidates_scores: Dict[str, float] = field(default_factory=dict)
                                        # {"candidate_a": 85.0, "candidate_b": 72.3}
    selected_candidate_id: str = ""
    reject_reasons: List[str] = field(default_factory=list)
                                        # ["candidate_a: Policy denied (risk >= 80)",
                                        #  "candidate_b: Lower utility score"]
    selected_reason: str = ""
```

### 16.2 Episode 生命周期

```python
# 完整 Episode 示例
episode = Episode(
    episode_id="ep-001",
    finding_id="f-001",
    decision_id="d-001",
    context_hash="ctx_abc123",
)

# Step 1: 观察到什么
episode.add_step("observation", {
    "category": "RabbitMQHeartbeatLost",
    "confidence": 0.91,
    "evidence": ["heartbeat timeout", "AMQP disconnected"],
})

# Step 2: 假设
episode.add_step("hypothesis", {
    "hypothesis": "RabbitMQ network partition",
    "confidence": 0.91,
})

# Step 3: Goal
episode.add_step("goal_choice", {
    "primary": "restore_messaging",
    "desired_state": "RabbitMQ.healthy",
})

# Step 4: 决策（v15 新增）
episode.add_step("decision", {
    "candidates_scores": {
        "restart_service": 85.0,
        "collect_diagnostic": 72.3,
        "failover": 45.0,
    },
    "selected_candidate_id": "restart_service",
    "reject_reasons": [
        "collect_diagnostic: Lower utility (will not achieve goal)",
        "failover: No standby available",
    ],
    "selected_reason": "Highest utility: 85.0 (success=0.95, risk=30, duration=15s)",
})

# Step 5: 执行
episode.add_step("execution", {
    "workflow_id": "wf-001",
    "outcome": "success",
    "duration_ms": 12000,
})

# Step 6: 结果
episode.add_step("outcome", {
    "final_outcome": "success",
    "actual_state": "RabbitMQ.healthy",
})

episode.final_outcome = "success"
episode.total_duration_ms = 12000
```

### 16.3 PolicyEngine 记录 DecisionStep（v15）

```python
class PolicyEngine:
    def evaluate_candidates(self, ...) -> PolicyEvaluationResult:
        # ... 评估逻辑 ...
        decision_step = DecisionStep(
            candidates_scores={
                c.workflow.name: self._compute_utility(c)
                for c in candidates
            },
            selected_candidate_id=selected.workflow.name if selected else "",
            reject_reasons=[f"{c.workflow.name}: {reason}"
                           for c, reason in rejected],
            selected_reason=selected_reason,
        )
        # DecisionStep 将被写入 Episode
        result.decision_step = decision_step
        return result
```

---

## 17. Decision State Machine + Orchestrator（v15：职责分离）

### 17.1 DecisionStateMachine（纯生命周期管理）

```python
class DecisionStatus(Enum):
    CREATED = "created"
    PLANNING = "planning"
    PLANNED = "planned"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    CANCELLED = "cancelled"


class DecisionStateMachine:
    """
    纯生命周期管理。

    v15: 只负责状态转换 + Event 发布。
        不负责编排（编排由 DecisionOrchestrator 负责）。
    """

    TRANSITIONS = { ... }  # 同 v14

    def transition(self, decision: 'DecisionRecord',
                   to: DecisionStatus) -> 'DecisionRecord':
        current = decision.status
        allowed = self.TRANSITIONS.get(current, [])
        if to not in allowed:
            raise InvalidTransitionError(
                f"Cannot transition from {current.value} to {to.value}")
        decision.status = to
        decision.status_history.append((to, datetime.utcnow()))
        if to in (DecisionStatus.SUCCEEDED, DecisionStatus.FAILED,
                  DecisionStatus.ROLLED_BACK, DecisionStatus.CANCELLED):
            decision.completed_at = datetime.utcnow()
        self.bus.publish("platform.decision.state", self._build_event(decision, to))
        return decision
```

### 17.2 DecisionOrchestrator（编排——v15 新增）

```python
class DecisionOrchestrator:
    """
    Decision 编排器——v15 从 DecisionManager 拆分。

    职责：串联规划→评估→策略→执行的完整流程。
    不负责：生命周期状态管理（DecisionStateMachine 负责）。
    """

    def __init__(self, planner: Planner,
                 exec_planner: ExecutionPlanner,
                 risk_engine: RiskEngine,
                 blast_analyzer: BlastRadiusAnalyzer,
                 policy_engine: PolicyEngine,
                 state_machine: DecisionStateMachine,
                 workflow_engine: 'WorkflowEngine',
                 episode_store: EpisodeStore):
        ...

    def execute(self, finding: Finding, context: Context,
                goal: Goal = None) -> DecisionResult:
        """完整决策执行流程。"""
        decision = DecisionRecord(
            decision_id=uuid4().hex,
            finding_id=finding.id,
            context_hash=context.context_hash,
        )

        try:
            # === Phase 1: PLAN ===
            self.state_machine.transition(decision, DecisionStatus.PLANNING)
            plan_result = self.planner.plan(finding, context, goal)
            decision.goal = plan_result.goal
            decision.intents = plan_result.intents
            self.state_machine.transition(decision, DecisionStatus.PLANNED)

            # === Phase 2: EVALUATE ===
            candidates = self.exec_planner.plan(plan_result, context)
            for intent, candidate in zip(plan_result.intents, candidates):
                candidate.risk_profile = self.risk_engine.compute(
                    intent, candidate, context, self.planner.worldview)
            decision.candidates = candidates

            # === Phase 3: POLICY ===
            result = self.policy_engine.evaluate_candidates(
                plan_result, candidates, context, finding, decision)
            decision.selected_candidate = result.selected_candidate
            decision.policy_rules_matched = result.matched_rules

            if result.decision == PolicyDecision.CANDIDATE_SELECTED:
                self.state_machine.transition(decision, DecisionStatus.APPROVED)
            elif result.decision == PolicyDecision.PENDING_APPROVAL:
                self.state_machine.transition(decision, DecisionStatus.PENDING_APPROVAL)
                return DecisionResult(decision=decision, status="pending_approval")
            else:
                self.state_machine.transition(decision, DecisionStatus.REJECTED)
                return DecisionResult(decision=decision, status="rejected")

            # === Phase 4: EXECUTE ===
            self.state_machine.transition(decision, DecisionStatus.EXECUTING)
            execution_result = self.workflow_engine.execute(
                result.selected_candidate.workflow)
            self.state_machine.transition(decision, DecisionStatus.VERIFYING)
            # Verify ...
            outcome = "success" if execution_result.success else "failed"
            if outcome == "success":
                self.state_machine.transition(decision, DecisionStatus.SUCCEEDED)
            else:
                self.state_machine.transition(decision, DecisionStatus.FAILED)

            # === Phase 5: LEARN ===
            self._record_episode(decision, plan_result, execution_result)

            return DecisionResult(decision=decision, status=outcome)

        except Exception as e:
            self.state_machine.transition(decision, DecisionStatus.FAILED)
            raise

    def _record_episode(self, decision, plan_result, execution_result):
        """记录 Episode（含 DecisionStep）。"""
        episode = Episode(episode_id=uuid4().hex,
                          finding_id=decision.finding_id,
                          decision_id=decision.decision_id,
                          context_hash=decision.context_hash)
        # ... 记录各步骤 ...
        self.feedback_loop.record_episode(episode)
```

---

## 18. Episode + ExperienceGraph（v15：增加 Failure Pattern 维度）

### 18.1 ExperienceGraphProjection（v15：key 增加 failure_pattern）

```python
@dataclass
class ExperienceStats:
    """统计经验——按 (failure_pattern, capability_id, env_fingerprint) 索引。

    v15: 增加 failure_pattern 维度。
         不同故障场景的相同操作有不同成功率。
    """
    failure_pattern: str = ""         # "RabbitMQHeartbeatLost", "NovaOOM"
    capability_id: str = ""
    env_fingerprint: str = ""
    total_executions: int = 0
    success_count: int = 0
    failure_count: int = 0
    avg_duration_ms: float = 0.0

    @property
    def key(self) -> str:
        return f"{self.failure_pattern}|{self.capability_id}|{self.env_fingerprint}"

    @property
    def success_rate(self) -> float:
        return self.success_count / self.total_executions if self.total_executions else 0.0


class ExperienceGraphProjection(Projection):
    """
    Episode → 统计投影。

    v15: key = failure_pattern|capability_id|env_fingerprint。
         "RabbitMQHeartbeatLost 时 restart 成功率"
         不再被 "NovaOOM 时 restart 成功率" 干扰。
    """

    name = "experience_graph"

    def apply(self, envelope: EventEnvelope):
        if envelope.event_type == "feedback.learning":
            event = deserialize_learning_event(envelope)
            key = ExperienceStats(
                failure_pattern=event.failure_pattern,
                capability_id=event.capability_id,
                env_fingerprint=event.env_fingerprint,
            ).key
            stats = self._stats.get(key)
            if not stats:
                stats = ExperienceStats(
                    failure_pattern=event.failure_pattern,
                    capability_id=event.capability_id,
                    env_fingerprint=event.env_fingerprint,
                )
                self._stats[key] = stats
            stats.total_executions += 1
            if event.outcome == "success":
                stats.success_count += 1
```

---

## 19. 置信度模型

同 v7。

---

## 20. API

```text
# WorldView（v15：拆为 3 个 Query 端点）
GET /api/v1/worldview/topology/dependents?type=SERVICE&id=rabbitmq
GET /api/v1/worldview/state/current?type=INSTANCE&id=abc-123
GET /api/v1/worldview/history/events?type=SERVICE&id=nova-api&count=50

# Expressions（v15 新增）
POST /api/v1/expressions/evaluate
  → { field, operator, value, target → result: true/false }

# Goal（v15：目标状态）
POST /api/v1/goals/infer
  → { primary, tree: [{ desired_state, target, children }] }

# Decision Orchestrator（v15 拆分）
POST /api/v1/decisions/execute  # Orchestrator.execute() — 全流程
POST /api/v1/decisions/state    # StateMachine.transition()
GET  /api/v1/decisions/{id}     # DecisionRecord

# Episode（v15：DecisionStep）
GET /api/v1/episodes/by-decision/{decision_id}
  → { steps: [..., {step_type: "decision", candidates_scores, reject_reasons}] }

# Capability（v15：Expression）
GET /api/v1/capabilities/executors
  → [{ preconditions: [{field, operator, value}], ... }]

# Blast Radius（v15：ImpactModel）
POST /api/v1/blast-radius/analyze
  → { impact_model: {severity, duration, scope}, ... }

# Utility（v15：权重可配置）
POST /api/v1/policies/utility
GET  /api/v1/policies/weights  # 当前权重
PUT  /api/v1/policies/weights  # 更新权重（Config 注入）

# Experience（v15：failure_pattern 维度）
GET /api/v1/experience/success-rate?pattern=RabbitMQHeartbeatLost&capability=ssh.restart_service&env=prod

# 其余同 v14
```

---

## 21. 实施阶段

| Phase | 新增/变更内容 |
|-------|---------------|
| **0-3** | 同 v14（Foundation → Projection → Interaction → Correlation） |
| **4** | **WorldView（TopologyQuery / StateQuery / HistoryQuery Facade）** + ContextAPI + CanonicalHasher |
| **5** | **Goal Tree（desired_state，不含 Workflow 概念）** + **Expression 类型** + Planner + Constraint |
| **6** | **Capability（Expression Pre/Post + ImpactModel）** + ExecutionPlanner + **Blast Radius Analyzer** + RiskEngine + **Policy（可配置 Utility）** + **DecisionStateMachine + DecisionOrchestrator（分离）** |
| **7** | **Episode（DecisionStep）** + Feedback + **ExperienceGraphProjection（+ Failure Pattern 维度）** |

---

## 22. 测试策略

```python
# === WorldView 拆分 ===
def test_worldview_is_facade():
    """WorldView 是 Facade，不包含查询实现"""
    wv = WorldView(topology=TopologyQuery(mock_graph),
                    state=StateQuery(mock_state, mock_timeline),
                    history=HistoryQuery(mock_events))
    assert hasattr(wv, "topology") and hasattr(wv, "state") and hasattr(wv, "history")
    # WorldView 自身没有任何方法（除了 __init__）

def test_topology_query_independent():
    """TopologyQuery 可独立使用"""
    tq = TopologyQuery(graph_projection)
    deps = tq.get_dependents(ResourceIdentity(ResourceType.SERVICE, "rabbitmq"))
    assert len(deps) >= 3

def test_state_query_resolve_field():
    """StateQuery.resolve_field 供 Expression 使用"""
    sq = StateQuery(mock_state, mock_timeline)
    value = sq.resolve_field("resource.status",
                              ResourceIdentity(ResourceType.INSTANCE, "vm-1"))
    assert value in ("ACTIVE", "ERROR", "SHUTOFF")

# === Goal = 目标状态 ===
def test_goal_desired_state():
    """GoalNode 只描述目标状态，不含 action/ordering"""
    node = GoalNode(goal_id="g1", desired_state="RabbitMQ.healthy",
                     target=ResourceIdentity(ResourceType.SERVICE, "rabbitmq"))
    assert not hasattr(node, "action")       # v14 移除
    assert not hasattr(node, "ordering")     # v14 移除
    assert node.desired_state == "RabbitMQ.healthy"

def test_intent_generator_matches_desired_state():
    """IntentGenerator 按目标状态匹配"""
    gen = RestartIntentGenerator(...)
    node = GoalNode(goal_id="g1", desired_state="RabbitMQ.healthy", target=...)
    assert gen.can_handle(mock_finding, node, worldview)
    node2 = GoalNode(goal_id="g2", desired_state="evidence_collected", target=...)
    assert not gen.can_handle(mock_finding, node2, worldview)

def test_goal_inferrer_produces_state_tree():
    """GoalInferrer 产出目标状态树，不是 Workflow 树"""
    goal = GoalInferrer().infer(finding, context, worldview)
    assert goal.tree.desired_state is not None
    assert all("healthy" in node.desired_state or "responding" in node.desired_state
               for node in goal.tree.children)

# === Expression ===
def test_expression_evaluate():
    """Expression 使用 WorldView 自动求值"""
    worldview = WorldView(topology=..., state=StateQuery(mock_state, ...), ...)
    expr = Expression("resource.status", "==", "ACTIVE")
    mock_state.resolve.return_value = "ACTIVE"
    assert expr.evaluate(worldview, target) == True

    mock_state.resolve.return_value = "ERROR"
    assert expr.evaluate(worldview, target) == False

def test_expression_exists_operator():
    expr = Expression("ssh.accessible", "exists")
    worldview.state.resolve.return_value = True
    assert expr.evaluate(worldview, target) == True

def test_capability_preconditions_expression():
    """Capability 使用 Expression，不是字符串"""
    cap = Capability(capability_id="ssh.restart_service",
                      preconditions=[
                          Expression("host.host_status", "==", "alive"),
                          Expression("service.exists", "==", True),
                      ])
    assert all(isinstance(p, Expression) for p in cap.preconditions)
    assert cap.preconditions[0].field == "host.host_status"

# === DecisionStep ===
def test_decision_step_record_reason():
    """DecisionStep 记录候选方案评分和拒绝理由"""
    step = DecisionStep(
        candidates_scores={"restart": 85.0, "diagnose": 72.3},
        selected_candidate_id="restart",
        reject_reasons=["diagnose: Lower utility"],
        selected_reason="Highest utility: 85.0",
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

# === DecisionOrchestrator（分离） ===
def test_state_machine_pure_lifecycle():
    """DecisionStateMachine 只做生命周期"""
    sm = DecisionStateMachine(bus)
    d = DecisionRecord(decision_id="d1")
    d.status = DecisionStatus.CREATED
    sm.transition(d, DecisionStatus.PLANNING)
    assert d.status == DecisionStatus.PLANNING

def test_orchestrator_uses_state_machine():
    """DecisionOrchestrator 编排流程，StateMachine 管理状态"""
    orchestrator = DecisionOrchestrator(planner, exec_planner, risk, blast,
                                          policy, state_machine, workflow, episodes)
    result = orchestrator.execute(finding, context)
    assert result.decision.status in (
        DecisionStatus.SUCCEEDED,
        DecisionStatus.FAILED,
        DecisionStatus.REJECTED,
    )

# === Utility 权重可配置 ===
def test_utility_configurable_weights():
    """Utility 权重可通过配置调整"""
    engine = PolicyEngine(..., weights=UtilityWeights(success=0.4, risk=0.4))
    assert engine.weights.success == 0.4
    assert engine.weights.risk == 0.4

def test_utility_different_weights_different_ranking():
    """不同权重产生不同排序"""
    engine_a = PolicyEngine(..., weights=UtilityWeights(success=0.6, risk=0.1))
    engine_b = PolicyEngine(..., weights=UtilityWeights(success=0.1, risk=0.6))
    candidates = [
        WorkflowCandidate(workflow=wf1, estimated_success_rate=0.95, ...),
        WorkflowCandidate(workflow=wf2, estimated_success_rate=0.85, ...),
    ]
    ranked_a = engine_a._rank(candidates, intent, worldview)
    ranked_b = engine_b._rank(candidates, intent, worldview)
    # 不同权重下排序可能不同

# === Blast Radius 含 ImpactModel ===
def test_blast_radius_uses_impact_model():
    """Blast Radius 使用 Capability.impact_model"""
    cap = Capability(capability_id="ssh.restart_service",
                      impact_model=ImpactModel("temporary", "30s", "service"))
    analyzer = BlastRadiusAnalyzer(...)
    report = analyzer.analyze(intent, target, cap, worldview)
    assert report.risk_level in ("low", "medium", "high", "critical")
    # temporary 30s 的风险 < permanent

# === ExperienceGraph 含 failure_pattern ===
def test_experience_stats_with_failure_pattern():
    """ExperienceStats 按 (failure_pattern, capability, env) 索引"""
    stats = ExperienceStats(failure_pattern="RabbitMQHeartbeatLost",
                             capability_id="ssh.restart_service",
                             env_fingerprint="prod:rabbitmq")
    assert "RabbitMQHeartbeatLost|ssh.restart_service|prod:rabbitmq" == stats.key

def test_different_pattern_separate_stats():
    """不同 failure_pattern 的统计不混合"""
    p1 = ExperienceStats(failure_pattern="RabbitMQHeartbeatLost", ...)
    p2 = ExperienceStats(failure_pattern="NovaOOM", ...)
    assert p1.key != p2.key

# === End-to-end ===
def test_end_to_end_v15():
    """v15 完整链路"""
    # 1. Finding（不含 recommended_action）
    finding = Finding(category="RabbitMQHeartbeatLost",
                      hypothesis="RabbitMQ network partition",
                      confidence=0.91)

    # 2. Orchestrator 执行全流程
    orchestrator = DecisionOrchestrator(...)
    result = orchestrator.execute(finding, context)

    # 3. Decision 有生命周期
    assert result.decision.status in (
        DecisionStatus.SUCCEEDED, DecisionStatus.FAILED,
        DecisionStatus.REJECTED, DecisionStatus.PENDING_APPROVAL,
    )
    assert len(result.decision.status_history) > 1

    # 4. Episode 包含 DecisionStep
    episode = episode_store.get_by_decision(result.decision.decision_id)
    assert episode is not None
    assert any(s.step_type == "decision" for s in episode.steps)
    decision_step = next(s for s in episode.steps if s.step_type == "decision")
    assert "candidates_scores" in decision_step.data
    assert "reject_reasons" in decision_step.data
```

---

## 23. 向后兼容

| 影响点 | 策略 |
|--------|------|
| `WorldView` 拆为 Facade | 旧版代码 `worldview.get_resource_state(x)` → `worldview.state.get_state(x)`；兼容过渡期保持旧方法作为委托 |
| `GoalNode` 移除 action/ordering | v14 的 `GoalNode` 字段 deprecate 但保留；v16 移除 |
| `Capability.preconditions` 字符串 → Expression | 旧字符串自动转为 `Expression(field=str, operator="==", value=True)` |
| `DecisionManager` → `DecisionOrchestrator` | 旧 `DecisionManager` 保留为 Orchestrator 的 alias |
| `ExperienceStats.key` 增加 failure_pattern | 旧 key 自动补 `failure_pattern=""` |
| `EpisodeStep.step_type` 增加 "decision" | 不影响已有 step_type |

---

## 24. v14 → v15 变更对照

| 维度 | v14 | v15 |
|------|-----|-----|
| **WorldView** | 单体类（10+ 方法） | **Facade + TopologyQuery + StateQuery + HistoryQuery** |
| **GoalNode** | `action` + `ordering` + `completion` | **`desired_state` + `target`**（目标状态） |
| **GoalInferrer** | 产出 action 树 | **产出 desired_state 树** |
| **Capability Precondition** | `List[str]`（字符串） | **`List[Expression]`**（field + operator + value） |
| **Capability Metadata** | effects + base_risk | **+ `Expression` pre/post + `ImpactModel`** |
| **Episode** | 6 种 step_type | **+ `decision` 类型（DecisionStep）** |
| **DecisionManager** | lifecycle + orchestration | **DecisionOrchestrator + DecisionStateMachine** |
| **Utility 权重** | 硬编码（0.5/0.3/0.1/0.05） | **UtilityWeights 类 + OPA Rego 可配置** |
| **Blast Radius 输入** | 仅 Dependency Graph | **+ Capability.ImpactModel + Current State** |
| **ExperienceGraph Key** | `(capability_id, env_fingerprint)` | **`(failure_pattern, capability_id, env_fingerprint)`** |
| **Expression** | 不存在 | **新增 `Expression` 数据类 + `evaluate()` 方法** |
| **API 新增** | — | **`/worldview/topology`、`/worldview/state`、`/expressions/evaluate`、`/policies/weights`** |
| **测试** | 50+ | **55+** |
