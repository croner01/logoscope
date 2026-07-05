"""ContextAPI — 上下文构建 API。"""
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple
from .hasher import CanonicalContextHasher
from .builders import IncidentContext, TopologyContext, WorkflowContext, RuleContext
from .snapshot import ContextSnapshot


class ContextType:
    INCIDENT = "incident"
    TOPOLOGY = "topology"
    WORKFLOW = "workflow"
    RULE = "rule"


@dataclass
class ContextResult:
    """上下文查询结果。"""
    entity_type: str
    entity_name: str
    context_hash: str
    context: Any
    projection_epoch: str = ""
    knowledge_refs: List[Tuple[str, str]] = field(default_factory=list)
    generated_at: datetime = field(default_factory=datetime.utcnow)
    snapshot_id: str = ""


class ContextAPI:
    """
    Context API——为 AI 组件构建上下文。

    - build(entity_type, entity_name) → ContextResult
    - 使用 CanonicalContextHasher 计算内容 hash
    - 通过 WorldView 查询状态（不直接访问存储）
    - Snapshot 由 Projection Layer 管理（use_snapshot=False 默认）
    """

    def __init__(self, worldview, hasher: CanonicalContextHasher,
                 knowledge_store=None):
        self.worldview = worldview
        self.hasher = hasher
        self.knowledge_store = knowledge_store
        self._snapshots: Dict[str, ContextSnapshot] = {}

        # 如果传入的 worldview 已经是 Facade，使用其子组件
        self.topology = getattr(worldview, "topology", worldview)
        self.state = getattr(worldview, "state", worldview)
        self.history = getattr(worldview, "history", worldview)

    def build(self, entity_type: str, entity_name: str,
              context_type: str = ContextType.INCIDENT,
              use_snapshot: bool = False) -> ContextResult:
        """构建上下文。"""
        content = {
            "entity": {"type": entity_type, "name": entity_name},
            "type": context_type,
        }

        # 1. 通过 WorldView 查询
        if context_type == ContextType.INCIDENT or context_type == ContextType.TOPOLOGY:
            state = self.state.get_state(entity_type, entity_name)
            dependencies = self.topology.get_dependencies(entity_type, entity_name)
            dependents = self.topology.get_dependents(entity_type, entity_name)
            impact = self.topology.get_impact_set(entity_type, entity_name)
            alarms = self.history.get_alarms()

            content["state"] = state
            content["dependencies"] = dependencies
            content["dependents"] = dependents
            content["impact_set"] = impact
            content["alarm_count"] = len(alarms)

        # 2. 计算内容 hash（不含时间戳）
        context_hash = self.hasher.hash(content)

        # 3. 构建上下文对象（隐藏存储实现）
        context_obj = self._build_context(context_type, entity_type, entity_name)

        # 4. 查询知识
        knowledge_refs = []
        if self.knowledge_store:
            docs = self.knowledge_store.retrieve(entity_name)
            knowledge_refs = [
                (getattr(d, "document_id", "unknown"), getattr(d, "version", "v1"))
                for d in docs
            ] if docs else []

        # 5. Snapshot（默认不生成）
        snapshot_id = ""
        if use_snapshot:
            snapshot_id = f"snap_{uuid.uuid4().hex[:12]}"
            self._snapshots[snapshot_id] = ContextSnapshot(
                snapshot_id=snapshot_id,
                entity_type=entity_type,
                entity_name=entity_name,
                context_data=content,
            )

        return ContextResult(
            entity_type=entity_type,
            entity_name=entity_name,
            context_hash=context_hash,
            context=context_obj,
            projection_epoch=datetime.utcnow().strftime("%Y%m%d%H%M"),
            knowledge_refs=knowledge_refs,
            snapshot_id=snapshot_id,
        )

    def get_snapshot(self, snapshot_id: str) -> Optional[ContextSnapshot]:
        return self._snapshots.get(snapshot_id)

    def _build_context(self, context_type: str, entity_type: str,
                       entity_name: str) -> Any:
        """构建不同类型的上下文对象。"""
        if context_type == ContextType.INCIDENT:
            alarms = self.history.get_alarms()
            return IncidentContext(
                alarms=alarms[:5],
                summary=f"Context for {entity_type}:{entity_name}",
            )
        elif context_type == ContextType.TOPOLOGY:
            return TopologyContext(
                dependents=self.topology.get_dependents(entity_type, entity_name),
                dependencies=self.topology.get_dependencies(entity_type, entity_name),
                impact_set=self.topology.get_impact_set(entity_type, entity_name),
                estimated_vm_count=self.topology.estimate_vm_count(entity_type, entity_name),
            )
        elif context_type == ContextType.WORKFLOW:
            return WorkflowContext()
        elif context_type == ContextType.RULE:
            return RuleContext()
        return IncidentContext(summary=f"Unknown context type {context_type} for {entity_type}:{entity_name}")
