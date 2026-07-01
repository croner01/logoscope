# Logoscope Data Architecture v1 — AI Observability Operating System

> **Event Sourcing + CQRS + Unified Context + Knowledge Feedback — 只有 Raw Event 是 Source of Truth。**
> Event 携带全血缘链条。Context 带版本可重现。Inference 依赖 Context + Knowledge。Workflow 被 OPA Policy 约束。Capability 声明 Effect 和 Risk。Action 产生 Feedback 闭环学习。
>
> 定义完整的 **Observe → Understand → Decide → Act → Learn** 闭环。

**Status:** Draft v10
**Date:** 2026-07-01
**Authors:** croner01, Claude

---

## 1. Problem

### 1.1 当前架构的系统性瓶颈

v9 引入了 Event Lineage、Knowledge & Memory Store、Policy Engine、Feedback Loop 和 Versioned Projection，但仍有根本性问题：

**① Context 没有版本。**
Finding 依赖 Context，Context 依赖特定 Epoch 的 Projection。Projection 重建后，Finding 可能变化。没有 `context_version` 就无法验证 Finding 是否可以重现。

**② Policy 用自定义 DSL。**
v9 的 `Policy.condition: Callable` 需要自行维护 DSL 解析器，没有测试框架、没有版本管理、无法与行业标准集成。

**③ Planner 只出一个方案。**
一个 Finding 只有一个 Workflow。没有备选方案的比较（风险、成本、成功率），Policy Engine 只能在"执行/拒绝"之间二选一。

**④ Capability 没有副作用声明。**
`Capability.execute()` 没有声明它做了什么（读/写/重启/删除）、风险多高。Policy Engine 只能靠匹配 Workflow name 来判断风险，脆弱且不安全。

### 1.2 v10 核心理念

```
Context 与 Finding 都携带 context_version，支持重现验证。
Policy 使用 OPA (Open Policy Agent) 标准 Rego 语言。
Planner 每次产出多个 Candidate，Policy 在候选方案之间选择。
Capability 声明 effect 和 risk_score，Policy 直接基于副作用决策。
```

### 1.3 v9 → v10 变更

| 维度 | v9 | v10 |
|------|----|-----|
| **Context** | 无版本 | **context_version**（sha256 of all inputs） |
| **Finding** | 无 context 引用 | **+context_version**（可追踪到哪个 Context） |
| **Policy Engine** | 自定义 DSL（Callable condition） | **OPA (Open Policy Agent)** + Rego |
| **Planner** | 单个 Workflow | **List[WorkflowCandidate]**（多候选 + confidence + risk） |
| **Capability** | execute() 无副作用声明 | **+effect + risk_score**（声明读写重启等 effect） |
| **配套变更** | — | Policy 利用 Capability effect 做决策；Planner 返回多个方案；Context 可重现 |

---

## 2. Architecture

### 2.1 整体架构

同 v9，增加 context_version 标注：

```
Context API 产出:
  ContextResult {
    context,
    context_version="v20260701.abc123def",
    snapshot_id="...",
  }

Inference Engine 产出:
  Finding {
    context_version="v20260701.abc123def",  # 可追溯
    knowledge_sources=[...],
  }
```

### 2.2 Event Sourcing 原则

```
原则 1-15: 同 v9。
新增原则 16: 每个 Context 有 version（sha256），Finding 引用 context_version。
新增原则 17: Policy 用 OPA/Rego 编写，与 Kubernetes/GitOps 生态兼容。
新增原则 18: Planner 产出多个候选方案，每个带 confidence + risk。
新增原则 19: 所有 Capability 声明 effect 和 risk_score，Policy 据此决策。
```

### 2.3 EventEnvelope

同 v9。`parent_event_ids` 构建血缘链。

### 2.4 Topic 结构

同 v9。domain-first 命名。

### 2.5 Projection 框架（含 Versioned Projection）

同 v9。

### 2.6 Component Responsibilities

（更新 Policy Engine 和 Planner 一行）

| 层 | 职责 | 不做什么 |
|----|------|----------|
| **Inference Engine** | Context + Knowledge → Finding（维护血缘） | 不生成 Workflow |
| **Planner** | Finding → **List[WorkflowCandidate]**（多方案） | 不执行、不决策 |
| **Policy Engine** | **OPA 评估** → **ALLOW/DENY/APPROVAL** | 不做推理 |
| **Workflow Engine** | Workflow → Capability → Action | 不做决策 |
| **Capability Registry** | 执行方式抽象，**声明 effect/risk** | 不做编排 |

---

## 3. Event Schema

### 3.1 EventEnvelope

同 v9。

### 3.2 RawEvent / NormalizedEvent / Finding

同 v9。

### 3.3 Schema Evolution

同 v9。

---

## 4. Event Bus

同 v9。

---

## 5. Event Pipeline

同 v9。

---

## 6. Semantic Engine

同 v9。

---

## 7. Projection Layer

同 v9。

---

## 8. Correlation Engine

同 v9。

---

## 9. Unified Context API

### 9.1 ContextResult（v10 增加 context_version）

```python
@dataclass
class ContextResult:
    """统一查询结果。
    v10 增加 context_version——确保 Finding 可重现。"""
    context_type: ContextType
    resource_type: ResourceType
    resource_id: str
    context: Any                    # IncidentContext / TopologyContext / ...
    context_version: str = ""       # ← v10: sha256(resource_type + resource_id +
                                    #                context_type + time_window +
                                    #                projection_epochs +
                                    #                knowledge_version + timestamp)
    snapshot_id: str = ""
    created_at: datetime = field(default_factory=datetime.utcnow)
```

### 9.2 ContextAPI（v10 生成 context_version）

```python
class ContextAPI:
    """
    统一查询入口。
    v10：每次 build 生成 context_version，Finding 引用此版本。
    """

    def build(self, resource_type, resource_id,
              context_type=ContextType.INCIDENT,
              time_window="1 HOUR",
              use_snapshot=True) -> ContextResult:
        builder = self._get_builder(context_type)
        ctx = builder.build(resource_type, resource_id, time_window)

        # ← v10: 计算 context_version
        context_version = self._compute_version(
            resource_type, resource_id, context_type, time_window,
            projection_epochs=self._get_current_epochs(),
            knowledge_version=self._get_knowledge_version(),
        )

        snapshot_id = ""
        if use_snapshot and hasattr(builder, 'create_snapshot'):
            snapshot = builder.create_snapshot(ctx)
            snapshot_id = snapshot.snapshot_id

        return ContextResult(
            context_type=context_type,
            resource_type=resource_type,
            resource_id=resource_id,
            context=ctx,
            context_version=context_version,
            snapshot_id=snapshot_id,
        )

    def _compute_version(self, *args, **kwargs) -> str:
        """所有输入的 sha256——相同输入产出相同 version。"""
        raw = f"{args}|{json.dumps(kwargs, sort_keys=True)}"
        return f"v{datetime.utcnow().strftime('%Y%m%d')}.{sha256(raw.encode()).hexdigest()[:12]}"
```

---

## 10. Inference Engine

### 10.1 Finding（v10 增加 context_version）

```python
@dataclass
class Finding:
    """
    推理结果。
    v10 增加 context_version——引用生成此 Finding 时的 Context 版本。
    """
    id: str
    severity: str
    confidence: float
    category: str
    reason: str
    supporting_events: List[str]
    affected_entities: List[ResourceIdentity]
    recommended_action: str
    engine_type: str
    knowledge_sources: List[str] = field(default_factory=list)
    context_version: str = ""          # ← v10: 关联到 ContextResult.context_version
    context_snapshot_id: str = ""      # ← v10: 关联到 ContextSnapshot
    created_at: datetime = field(default_factory=datetime.utcnow)
```

### 10.2 Inference Engine

```python
class LLMInferenceEngine(InferenceEngine):
    def infer(self, input: InferenceInput) -> List[Finding]:
        # 1. 检索 Knowledge
        if not input.knowledge:
            input.knowledge = self.knowledge_store.retrieve(...)

        # 2. LLM 推理
        prompt = self._build_prompt(input.context, input.knowledge)
        llm_result = self.llm.complete(prompt)

        # 3. 产出 Finding（带 context_version 和 snapshot_id）
        finding = Finding(
            ...
            context_version=input.context.context_version,      # ← v10
            context_snapshot_id=input.context.snapshot_id,       # ← v10
            knowledge_sources=[d.document_id for d in input.knowledge],
        )
        return [finding]
```

---

## 11. Knowledge & Memory Store

### 11.1 KnowledgeDocument（v10 增加 Provenance）

```python
@dataclass
class KnowledgeDocument:
    document_id: str
    title: str
    content: str
    source_type: str                  # "runbook", "sop", "incident", "docs"
    relevance_score: float = 0.0

    # ← v10: Provenance（知识来源的可信度）
    origin: str = ""                  # "openstack-official", "community", "vendor"
    version: str = ""                 # "wallaby-2025-12", "1.2.3"
    trust_level: int = 3             # 1-5（5=官方认证，1=社区未验证）
    updated_at: Optional[datetime] = None
    owner: str = ""                   # 维护者/团队
    license: str = ""                 # "Apache-2.0", "CC-BY-4.0"

    source_url: str = ""
    created_at: datetime = field(default_factory=datetime.utcnow)
```

### 11.2 KnowledgeMemoryStore

同 v9。增加按 `trust_level` 过滤的能力。

---

## 12. Planner（v10 增加 Multi-Candidate）

### 12.1 定位

```
v9:   Finding → Workflow（单个方案）
v10:  Finding → List[WorkflowCandidate]（多个方案，Policy 选择）
```

### 12.2 Candidate 模型

```python
@dataclass
class WorkflowCandidate:
    """
    Workflow 候选方案。
    v10 新增——Planner 每次产出多个候选，Policy Engine 在候选间选择。
    """
    workflow: 'Workflow'
    confidence: float          # 0.0 ~ 1.0（此方案的成功概率）
    risk_score: int            # 1-100（来自 Capability.risk_score 的综合计算）
    risk_reason: str = ""      # 高风险的具体原因
    estimated_duration_ms: int = 0
    required_approval: bool = False  # 是否需要审批


@dataclass
class PlannerResult:
    """Planner 输出——多个候选方案。"""
    finding_id: str
    candidates: List[WorkflowCandidate]
    created_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def primary(self) -> Optional[WorkflowCandidate]:
        """按 confidence 降序排列的第一个方案。"""
        return self.candidates[0] if self.candidates else None
```

### 12.3 Planner

```python
class Planner:
    """
    Finding → List[WorkflowCandidate]。

    v1 实现：primary + 1~2 个 alternative
    v2 实现：LLM Planner：根据 IncidentContext + Knowledge 动态生成多个方案
    """

    def __init__(self, knowledge_store: KnowledgeMemoryStore,
                 registry: CapabilityRegistry):
        self.knowledge_store = knowledge_store
        self.registry = registry

    def plan(self, finding: Finding,
             context: IncidentContext) -> PlannerResult:
        """产出多个候选方案。"""
        candidates = []

        # Primary: 推荐方案（从 Finding.recommended_action 映射）
        primary = self._build_primary(finding, context)
        if primary:
            candidates.append(primary)

        # Alternative 1: 备选方案（同类操作的不同实现）
        alt1 = self._build_alternative(finding, context, primary)
        if alt1:
            candidates.append(alt1)

        # Alternative 2: 保守方案（只读诊断，不执行修复）
        alt2 = self._build_readonly_diagnostic(finding, context)
        if alt2:
            candidates.append(alt2)

        # 按 confidence 降序排列
        candidates.sort(key=lambda c: c.confidence, reverse=True)

        return PlannerResult(
            finding_id=finding.id,
            candidates=candidates,
        )

    def _compute_risk(self, finding: Finding,
                      workflow: Workflow) -> int:
        """根据所有 Capability 的 risk_score 综合计算。"""
        max_risk = 0
        for step in workflow.steps:
            cap = self.registry.get(step.capability)
            if cap:
                max_risk = max(max_risk, cap.risk_score)
        return max_risk

    def _build_primary(self, finding, context) -> Optional[WorkflowCandidate]:
        action = finding.recommended_action
        if action not in self._workflow_templates:
            return None
        wf = copy.deepcopy(self._workflow_templates[action])
        wf.workflow_id = uuid4().hex
        wf.trigger = "inference"
        wf = self._interpolate(wf, context)
        return WorkflowCandidate(
            workflow=wf,
            confidence=min(finding.confidence, 0.95),
            risk_score=self._compute_risk(finding, wf),
        )

    def _build_alternative(self, finding, context,
                           primary) -> Optional[WorkflowCandidate]:
        """备选方案——不同实现，风险/成本不同。"""
        # 例如：重启失败 → 用 evacuate 替代
        alt_map = {
            "restart_service": "migrate_vm",
            "restart_network": "reboot_host",
        }
        alt_action = alt_map.get(finding.recommended_action)
        if not alt_action:
            return None
        return WorkflowCandidate(
            workflow=self._build_workflow(alt_action, context),
            confidence=finding.confidence * 0.8,
            risk_score=min(self._compute_risk(finding, primary.workflow) + 10, 100),
            risk_reason="备选方案风险更高",
        )

    def _build_readonly_diagnostic(self, finding,
                                   context) -> WorkflowCandidate:
        """只读诊断方案——不执行修复，只收集更多信息。"""
        return WorkflowCandidate(
            workflow=self._build_workflow("collect_diagnostic", context),
            confidence=0.95,  # 只读操作几乎总是安全的
            risk_score=5,     # 风险最低
        )

    def plan_with_policy(self, finding, context) -> tuple:
        """
        一次性完成 Plan + Policy 评估。
        返回 (PlannerResult, PolicyDecision)。
        """
        result = self.plan(finding, context)
        # Policy Engine 在所有候选方案中选择
        decision = self.policy_engine.evaluate_candidates(
            result, context, finding)
        return result, decision
```

---

## 13. Policy Engine（v10 改为 OPA）

### 13.1 定位

**v10 核心变更。** 从自定义 DSL（Callable condition）改为 **OPA（Open Policy Agent）**。

```
v9:   Policy(condition=lambda ctx: "restart" in ctx.workflow.name)
v10:  policy/no_restart_biz_hours.rego（Rego 语言，OPA 评估）

为什么换 OPA：
  - CNCF 毕业项目，Kubernetes 生态标准
  - Rego 语言专门做策略评估
  - `opa test` 原生测试框架
  - 策略可以 Git 管理 + CI 测试
  - 不需要自己维护 DSL 解析器
  - 性能基准 < 1ms 评估
```

### 13.2 架构

```python
@dataclass
class PolicyEvaluationRequest:
    """Policy 评估请求。"""
    candidates: List[WorkflowCandidate]    # Planner 产出的候选
    context: IncidentContext
    finding: Finding
    resource_type: ResourceType
    resource_id: str


class PolicyDecision(Enum):
    ALLOW = "allow"
    DENY = "deny"
    PENDING_APPROVAL = "pending_approval"
    CANDIDATE_SELECTED = "candidate_selected"  # ← v10：从多个候选中选择


@dataclass
class PolicyEvaluationResult:
    """Policy 评估结果。"""
    decision: PolicyDecision
    selected_candidate: Optional[WorkflowCandidate] = None  # ← v10
    reason: str = ""
    matched_rules: List[str] = field(default_factory=list)
    evaluated_rules: int = 0
```

### 13.3 PolicyEngine（OPA）

```python
class PolicyEngine:
    """
    Policy Engine——基于 OPA (Open Policy Agent) 的安全治理层。

    策略用 Rego 语言编写，存储在 policies/ 目录下：
      policies/
        ├── no_restart_biz_hours.rego
        ├── prod_needs_approval.rego
        ├── no_bulk_operation.rego
        ├── deny_high_risk.rego
        └── policy_test.rego

    OPA 评估流程：
      1. 将请求转为 OPA input（JSON）
      2. 调用 OPA REST API / Library
      3. OPA 按 Rego 规则评估
      4. 返回 decision + reason
    """

    def __init__(self, opa_endpoint: str = "http://localhost:8181",
                 policy_dir: str = "/etc/logoscope/policies"):
        self.opa = OPAClient(opa_endpoint)
        self.policy_dir = policy_dir

    def evaluate(self, request: PolicyEvaluationRequest,
                 candidate: WorkflowCandidate) -> PolicyEvaluationResult:
        """评估单个候选方案。"""
        input = self._build_opa_input(request, candidate)
        result = self.opa.evaluate("logoscope/policy", input)

        return PolicyEvaluationResult(
            decision=PolicyDecision(result.get("decision", "deny")),
            reason=result.get("reason", ""),
            matched_rules=result.get("matched", []),
            evaluated_rules=result.get("evaluated", 0),
        )

    def evaluate_candidates(self, planner_result: 'PlannerResult',
                            context: IncidentContext,
                            finding: Finding) -> PolicyEvaluationResult:
        """评估所有候选方案，选择最佳合规方案。"""
        request = PolicyEvaluationRequest(
            candidates=planner_result.candidates,
            context=context,
            finding=finding,
            resource_type=context.resource_type,
            resource_id=context.resource_id,
        )

        # 1. 检查每个候选是否合规
        passing = []
        for candidate in request.candidates:
            result = self.evaluate(request, candidate)
            if result.decision == PolicyDecision.ALLOW:
                passing.append((candidate, result))

        # 2. 在合规候选中选择 confidence 最高的
        if passing:
            passing.sort(key=lambda x: x[0].confidence, reverse=True)
            best_candidate, best_result = passing[0]
            return PolicyEvaluationResult(
                decision=PolicyDecision.CANDIDATE_SELECTED,
                selected_candidate=best_candidate,
                reason=f"Selected from {len(passing)} passing candidates",
                matched_rules=best_result.matched_rules,
                evaluated_rules=best_result.evaluated_rules,
            )

        # 3. 所有候选都被拒绝
        # 检查是否有需要审批的
        pending = [c for c in request.candidates
                   if self._check_pending(c, request)]
        if pending:
            return PolicyEvaluationResult(
                decision=PolicyDecision.PENDING_APPROVAL,
                selected_candidate=pending[0],
                reason="Best candidate needs manual approval",
            )

        return PolicyEvaluationResult(
            decision=PolicyDecision.DENY,
            reason="All candidates denied by policy",
        )

    def _build_opa_input(self, request: PolicyEvaluationRequest,
                         candidate: WorkflowCandidate) -> dict:
        """将请求转为 OPA input JSON。"""
        return {
            "candidate": {
                "confidence": candidate.confidence,
                "risk_score": candidate.risk_score,
                "steps": [
                    {
                        "capability": s.capability,
                        "params": s.params,
                        # OPA 可以从 capability 名称推断 effect
                    }
                    for s in candidate.workflow.steps
                ],
            },
            "resource": {
                "type": request.resource_type.value,
                "id": request.resource_id,
            },
            "finding": {
                "severity": request.finding.severity,
                "category": request.finding.category,
                "confidence": request.finding.confidence,
            },
            "context": {
                "current_state": request.context.current_state,
            },
            "environment": {
                "time": {
                    "hour": datetime.utcnow().hour,
                    "day_of_week": datetime.utcnow().weekday(),
                },
            },
        }
```

### 13.4 Rego 策略示例

```rego
# policies/no_restart_biz_hours.rego
package logoscope.policy

# 禁止在工作时间（9:00-18:00）执行重启类操作
default deny = false

deny = "restart_during_business_hours" {
    input.candidate.risk_score >= 60
    input.environment.time.hour >= 9
    input.environment.time.hour <= 18
}

# 高风险操作（risk_score >= 80）默认拒绝
default high_risk_deny = false

high_risk_deny = "high_risk_operation" {
    input.candidate.risk_score >= 80
}

# 生产环境需要审批
default need_approval = false

need_approval = "prod_operation" {
    contains(input.resource.id, "prod")
    input.candidate.risk_score >= 40
}
```

```rego
# policies/policy.rego（主策略入口）
package logoscope.policy

# 综合决策
decision = "deny" {
    deny != ""
}

decision = "pending_approval" {
    deny == ""
    need_approval != ""
}

decision = "allow" {
    deny == ""
    need_approval == ""
}

reason = reason {
    deny = deny
    deny != ""
    reason := sprintf("Policy denied: %v", [deny])
}

reason = "Operation requires manual approval" {
    deny == ""
    need_approval != ""
}

reason = "All policies passed" {
    deny == ""
    need_approval == ""
}
```

### 13.5 OPA 集成优势

| 场景 | v9 自定义 DSL | v10 OPA |
|------|---------------|---------|
| 策略语言 | Python Callable | Rego（专用策略语言） |
| 测试框架 | 无 | `opa test`（原生支持） |
| Git 管理 | 代码 + 策略混在一起 | `policies/*.rego` 独立版本管理 |
| 热加载 | 需重启 Python | OPA 支持 REST API 动态更新 |
| 生态集成 | 仅 Logoscope | Kubernetes / Envoy / Istio 通用 |
| 性能 | Lambda 评估 | < 1ms（编译后 Native） |
| 策略可视化 | 无 | OPA Compile → Graph |

### 13.6 Policy + Capability Effect 联动

```rego
# policies/capability_effect.rego
package logoscope.policy

# 通过 Capability effect 推断操作风险
restart_operation {
    # effect=RESTART 的操作是重启类
    input.candidate.steps[_].capability == "ssh.restart_service"
}

delete_operation {
    input.candidate.steps[_].capability == "openstack.delete_volume"
}

# 禁止删除操作
deny = "delete_not_allowed" {
    delete_operation
}
```

---

## 14. Topology Engine

同 v9。

---

## 15. Workflow Engine

### 15.1 Capability（v10 增加 Effect Model）

```python
class EffectType(Enum):
    READ = "read"              # 只读操作
    WRITE = "write"            # 写入/修改
    RESTART = "restart"        # 重启服务/进程
    DELETE = "delete"          # 删除资源
    NETWORK = "network"        # 网络变更
    STORAGE = "storage"        # 存储操作
    CREATE = "create"          # 创建资源
    EXECUTE = "execute"        # 执行命令


@dataclass
class ParameterDef:
    name: str
    type: type
    required: bool = True
    default: Any = None
    description: str = ""


@dataclass
class Capability:
    """
    执行能力注册。
    v10 增加 effect 和 risk_score——Policy Engine 据此决策。
    """
    capability_id: str
    name: str                              # "ssh.execute_command"
    provider: str                          # "ssh-executor", "k8s-executor"

    effect: EffectType = EffectType.EXECUTE  # ← v10：声明副作用类型
    risk_score: int = 50                     # ← v10：风险评分 1-100
    risk_reason: str = ""                    # ← v10：高风险的说明

    parameters: Dict[str, ParameterDef] = field(default_factory=dict)
    output_type: type = str
    timeout_seconds: int = 30
    retry_count: int = 0

    @property
    def short_name(self) -> str:
        return self.name.split(".")[-1]
```

### 15.2 Capability 注册示例

```python
registry = CapabilityRegistry()

# 只读操作——风险低
registry.register(Capability(
    capability_id="ssh.check_process",
    name="ssh.check_process",
    provider="ssh-executor",
    effect=EffectType.READ,             # ← v10
    risk_score=10,                       # ← v10（只读，几乎无风险）
    parameters={
        "host": ParameterDef("host", str, True),
        "process": ParameterDef("process", str, True),
    },
))

# 重启操作——风险高
registry.register(Capability(
    capability_id="ssh.restart_service",
    name="ssh.restart_service",
    provider="ssh-executor",
    effect=EffectType.RESTART,          # ← v10
    risk_score=70,                       # ← v10（重启可能导致服务中断）
    risk_reason="Restarting a service causes temporary downtime",
    parameters={
        "host": ParameterDef("host", str, True),
        "service": ParameterDef("service", str, True),
    },
))

# 删除操作——风险最高
registry.register(Capability(
    capability_id="openstack.delete_volume",
    name="openstack.delete_volume",
    provider="openstack-api",
    effect=EffectType.DELETE,           # ← v10
    risk_score=95,                       # ← v10（删除不可逆）
    risk_reason="Deleting a volume causes permanent data loss",
    parameters={
        "volume_id": ParameterDef("volume_id", str, True),
    },
))
```

### 15.3 Workflow Engine + Capability

同 v9。

---

## 16. Feedback Loop

同 v9。

---

## 17. Platform Events

同 v9。

---

## 18. 置信度模型

（同 v7，不变）

---

## 19. API

```text
# Context API（v10：context_version）
GET /api/v1/context/build?type=INSTANCE&id=abc-123&context=incident
  → { context, context_version, snapshot_id }

# Planner（v10：多个 candidate）
POST /api/v1/planner/plan
  → { finding_id, candidates: [{workflow, confidence, risk_score}, ...] }

# Policy Engine（v10：OPA）
POST /api/v1/policies/evaluate   # 用 OPA 评估
POST /api/v1/policies/evaluate-candidates  # 评估候选方案
GET  /api/v1/policies/rego       # 查看当前加载的 Rego 策略
POST /api/v1/policies/reload     # 热加载 Rego 策略

# Capability（v10：effect + risk_score）
GET /api/v1/capabilities/executors
  → [{ capability_id, effect, risk_score, ... }]

# Event Lineage
GET /api/v1/lineage/trace/{event_id}

# Knowledge & Memory Store
GET    /api/v1/knowledge/retrieve?query=...
POST   /api/v1/knowledge/sources/register
POST   /api/v1/memory/feedback
GET    /api/v1/memory/forget?days=90&min_confidence=0.2  # ← v10

# Topology / Inventory / State / Interaction（同 v8）
...

# Projection Management
GET   /api/v1/projections/status
POST  /api/v1/projections/rebuild
POST  /api/v1/projections/{name}/traffic-split

# Schema Registry
GET  /api/v1/schemas?event_type=normalized.event
POST /api/v1/schemas/register
POST /api/v1/schemas/migrate

# Platform Events
GET /api/v1/platform/events?category=policy
```

---

## 20. 实施阶段

### Phase 0: Foundation（~2 周）

| 模块 | 内容 |
|------|------|
| Raw Event Store | WAL + Kafka `platform.raw`，EventEnvelope 含 parent_event_ids |
| Schema Registry | Schema 注册 + Migration + EventEnvelope |
| Event Bus | 10 topics，domain 命名 |

### Phase 1: Event Pipeline + Semantic Engine（~1.5 周）

| 模块 | 内容 |
|------|------|
| Event Pipeline | Aggregate / Dedup / Sample / Enrich / Route |
| Semantic Engine | 只 normalize，维护血缘 |

### Phase 2: Projection Framework + Inventory/State（~2 周）

| 模块 | 内容 |
|------|------|
| Projection Checkpoint | Partition + Offset，lag |
| Versioned Projection | 多算法并行 + 流量切换 |
| EntityProjector / Inventory / State / Timeline | |

### Phase 3: Interaction + Correlation（~2 周）

| 模块 | 内容 |
|------|------|
| InteractionProjector + Correlation Engine | |
| DynamicRel Projection | ClickHouse，多窗口 |
| Lineage API | 血缘追踪查询 |

### Phase 4: Graph + Context API（~2 周）

| 模块 | 内容 |
|------|------|
| Graph Projection | entity + interaction → Neo4j，只存拓扑 |
| **Context API（含 context_version）** | **4 种 ContextType + version** |
| Context Snapshot | |

### Phase 5: Knowledge + Inference + Planner（~2 周）

| 模块 | 内容 |
|------|------|
| Knowledge & Memory Store | Static Knowledge + Memory + Provenance |
| Inference Engine | LLM + Rule，Context + Knowledge → Finding（含 context_version） |
| **Planner（多 Candidate）** | **Finding → List[WorkflowCandidate]** |

### Phase 6: Policy + Workflow + Feedback（~2 周）

| 模块 | 内容 |
|------|------|
| **Policy Engine（OPA）** | **Rego 策略 + OPA 评估 + 候选方案选择** |
| **Capability（Effect Model）** | **Capability.risk_score + Capability.effect** |
| Workflow Engine | Command/Event 分离 + Capability |
| Feedback Loop | Evaluation + Memory 写入 |

---

## 21. 测试策略

```python
def test_context_version():
    """Context 有 version，相同输入产出相同 version"""
    api = ContextAPI(...)
    r1 = api.build(ResourceType.INSTANCE, "abc-123")
    r2 = api.build(ResourceType.INSTANCE, "abc-123")
    assert r1.context_version == r2.context_version


def test_finding_references_context():
    """Finding 引用 context_version"""
    api = ContextAPI(...)
    result = api.build(ResourceType.INSTANCE, "abc-123")
    finding = Finding(
        id="f1", severity="warning",
        context_version=result.context_version,
        context_snapshot_id=result.snapshot_id,
    )
    assert finding.context_version == result.context_version
    assert finding.context_snapshot_id == result.snapshot_id


def test_planner_multiple_candidates():
    """Planner 产出多个候选方案"""
    planner = Planner(knowledge_store, registry)
    result = planner.plan(mock_finding, mock_context)
    assert len(result.candidates) >= 2  # primary + diagnostic
    assert result.primary is not None
    # 按 confidence 降序
    for i in range(len(result.candidates) - 1):
        assert result.candidates[i].confidence >= result.candidates[i+1].confidence


def test_planner_candidate_risk():
    """Candidate 带 risk_score"""
    planner = Planner(knowledge_store, registry)
    result = planner.plan(mock_finding, mock_context)
    for c in result.candidates:
        assert 1 <= c.risk_score <= 100
        assert 0 <= c.confidence <= 1.0


def test_policy_opa_evaluate():
    """Policy Engine 用 OPA 评估"""
    engine = PolicyEngine(opa_endpoint="http://localhost:8181")
    request = PolicyEvaluationRequest(
        candidates=[WorkflowCandidate(
            workflow=Workflow(name="restart_service", steps=[
                WorkflowStep(capability="ssh.restart_service",
                             params={"host": "prod-db-01", "service": "mysql"})
            ]),
            confidence=0.9,
            risk_score=70,  # 来自 Capability.risk_score
        )],
        context=mock_context,
        finding=mock_finding,
    )
    # 测试 OPA 集成
    result = engine.evaluate(request, request.candidates[0])
    assert result.decision in (PolicyDecision.ALLOW, PolicyDecision.DENY,
                                PolicyDecision.PENDING_APPROVAL)


def test_policy_selects_best_candidate():
    """Policy Engine 从多个候选中选择最佳合规方案"""
    engine = PolicyEngine(...)
    result = PlannerResult(
        finding_id="f1",
        candidates=[
            WorkflowCandidate(workflow=wf1, confidence=0.9, risk_score=70),
            WorkflowCandidate(workflow=wf2, confidence=0.6, risk_score=20),
            WorkflowCandidate(workflow=wf3, confidence=0.95, risk_score=85),
        ],
    )
    decision = engine.evaluate_candidates(result, mock_context, mock_finding)
    assert decision.decision == PolicyDecision.CANDIDATE_SELECTED
    # 应该选择合规中 confidence 最高的
    # （假设 risk 70 被拒绝，risk 20 和 risk 85 中 20 通过，confidence 0.6）
    assert decision.selected_candidate.workflow == wf2


def test_capability_effect():
    """Capability 声明 effect 类型"""
    cap = Capability(
        capability_id="ssh.restart_service",
        name="ssh.restart_service",
        provider="ssh-executor",
        effect=EffectType.RESTART,
        risk_score=70,
    )
    assert cap.effect == EffectType.RESTART
    assert cap.risk_score == 70


def test_policy_uses_capability_effect():
    """Policy 通过 Capability effect 做决策"""
    registry = CapabilityRegistry()
    registry.register(Capability(
        capability_id="openstack.delete_volume",
        provider="openstack-api",
        effect=EffectType.DELETE,
        risk_score=95,
    ))
    # OPA Rego 识别 effect=DELETE → deny
    steps = [WorkflowStep(capability="openstack.delete_volume",
                          params={"volume_id": "vol-001"})]
    opa_input = {
        "candidate": {
            "steps": [{"capability": s.capability, "params": s.params}
                      for s in steps],
            "risk_score": 95,
        },
    }
    # 调用 OPA 评估 delete 策略
    # 期望：DENY（delete_not_allowed）


def test_context_version_changes_with_input():
    """不同输入产生不同 context_version"""
    api = ContextAPI(...)
    r1 = api.build(ResourceType.INSTANCE, "abc-123", time_window="1 HOUR")
    r2 = api.build(ResourceType.INSTANCE, "abc-123", time_window="6 HOUR")
    assert r1.context_version != r2.context_version  # time_window 不同


def test_context_version_reproducible():
    """相同输入始终产生相同 context_version"""
    api = ContextAPI(...)
    versions = []
    for _ in range(3):
        r = api.build(ResourceType.INSTANCE, "abc-123")
        versions.append(r.context_version)
    assert len(set(versions)) == 1  # 全部相同


def test_event_envelope_with_lineage():
    """EventEnvelope 必须携带 parent_event_ids"""
    env = EventEnvelope(
        event_id="test-001",
        parent_event_ids=["parent-001"],
    )
    assert len(env.parent_event_ids) == 1


def test_knowledge_memory_store_types():
    """Knowledge & Memory Store 区分 Static/Memory"""
    ...


def test_feedback_loop_writes_memory():
    """Feedback Loop 将执行结果写入 Memory"""
    ...
```

---

## 22. 性能考量

同 v9。

---

## 23. 向后兼容

| 影响点 | 策略 |
|--------|------|
| `context_version` | 存量 Finding.context_version=""，不影响功能 |
| Policy 迁移（Callable → OPA） | 过渡期双运行（Callable + OPA），Phase 6 切换 |
| Capability effect/risk | 默认 effect=EXECUTE, risk_score=50，不声明不影响策略 |
| Planner 多 Candidate | 存量调用 `planner.plan()` 返回单元素列表，接口兼容 |
| OPA endpoints | 新部署新端点，旧 Policy API 保持（内部转为 OPA） |

---

## 24. v9 → v10 变更对照

| 维度 | v9 | v10 |
|------|----|-----|
| **Context 版本** | 无 | **context_version**（sha256，可重现） |
| **Finding** | 无 Context 引用 | **+context_version + context_snapshot_id** |
| **Policy Engine** | 自定义 DSL（Callable） | **OPA (Open Policy Agent) + Rego** |
| **Planner** | 单个 Workflow | **List[WorkflowCandidate]**（多候选） |
| **Capability** | execute() | **+effect (READ/WRITE/RESTART/DELETE) + risk_score** |
| **Policy + Capability** | 靠名字匹配 | **OPA 直接使用 Capability.effect 决策** |
| **KnowledgeDocument** | source_type + content | **+provenance（origin, version, trust_level）** |
| **API** | — | **+policies/rego, policies/evaluate-candidates, capabilities/effect** |
| **测试** | 30+ | **35+**（context version, planner candidates, OPA, effect model） |
