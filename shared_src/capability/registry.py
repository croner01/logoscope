"""
CapabilityRegistry — Capability 注册和执行中心。
"""
from typing import Dict, Optional, Any, List
from .models import Capability


class CapabilityRegistry:
    """
    Capability 注册中心。

    - register(cap): 注册 Capability
    - execute(cap_id, params): 执行 Capability（返回结果或 None）
    - get(cap_id): 获取 Capability 定义
    - list_capabilities(): 列出所有已注册 Capability
    """

    def __init__(self):
        self._caps: Dict[str, Capability] = {}

    def register(self, cap: Capability):
        self._caps[cap.capability_id] = cap

    def execute(self, capability_id: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        cap = self._caps.get(capability_id)
        if cap is None:
            return None
        # 默认实现 — 返回执行结果占位
        return {"status": "ok", "capability": capability_id, "params": params}

    def get(self, capability_id: str) -> Optional[Capability]:
        return self._caps.get(capability_id)

    def list_capabilities(self) -> List[Capability]:
        return list(self._caps.values())
