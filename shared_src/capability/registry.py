"""CapabilityRegistry — 能力注册表。"""
from typing import Dict, List, Optional, Any
from .models import Capability


class CapabilityRegistry:
    """
    能力注册表——管理所有可用的 Capability。

    - register(cap): 注册
    - get(id): 查询
    - execute(id, params): 执行
    - list_capabilities(): 列出全部
    """

    def __init__(self):
        self._capabilities: Dict[str, Capability] = {}

    def register(self, cap: Capability):
        self._capabilities[cap.capability_id] = cap

    def get(self, capability_id: str) -> Optional[Capability]:
        return self._capabilities.get(capability_id)

    def list_capabilities(self) -> List[Capability]:
        return list(self._capabilities.values())

    def execute(self, capability_id: str, params: dict) -> Optional[Any]:
        cap = self._capabilities.get(capability_id)
        if not cap:
            return None
        # 生产环境：通过 provider 委派到具体执行器
        return {"capability_id": capability_id, "status": "executed", "params": params}
