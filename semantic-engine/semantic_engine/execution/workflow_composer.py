"""WorkflowComposer — 将 PlanIntent 转为 Workflow。"""
from typing import Optional
from shared_src.workflow.models import Workflow, WorkflowStep
from shared_src.capability.registry import CapabilityRegistry
from shared_src.capability.models import Capability
from shared_src.expression.models import Expression


class WorkflowComposer:
    """
    Workflow Composer——将 PlanIntent 转为可执行的 Workflow。

    - 匹配 Capability（通过 action → capability_id 映射）
    - 检查 Expression preconditions
    - 组合 WorkflowStep
    """

    ACTION_CAPABILITY_MAP = {
        "restart_service": "ssh.restart_service",
        "collect_diagnostic": "ssh.collect_logs",
        "failover": "openstack.migrate_vm",
    }

    def __init__(self, capability_registry: CapabilityRegistry):
        self.registry = capability_registry

    def compose(self, intent, worldview) -> Optional[Workflow]:
        """将 PlanIntent 转为 Workflow（如果前置条件满足）。"""
        action = intent.action if hasattr(intent, "action") else ""
        cap_id = self.ACTION_CAPABILITY_MAP.get(action)
        if not cap_id:
            return None

        cap = self.registry.get(cap_id)
        if not cap:
            return None

        if not self._check_preconditions(cap, worldview):
            return None

        return Workflow(
            name=action,
            steps=[WorkflowStep(capability=cap_id, params={
                "entity_type": intent.entity_type,
                "entity_name": intent.entity_name,
            })],
        )

    def _check_preconditions(self, capability: Capability, worldview) -> bool:
        """使用 Expression 自动检查前置条件。"""
        for expr in capability.preconditions:
            if not expr.evaluate(worldview,
                                  capability.capability_id.split(".")[0].upper(),
                                  ""):
                return False
        return True
