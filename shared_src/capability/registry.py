"""
CapabilityRegistry — Capability 注册和执行中心。

v15: 支持 handler 注册模式。Capability 定义（数据模型）与执行逻辑（handler）分离。
     register_handler() 将 Capability ID 与可调用对象关联，
     execute() 查找 handler 并分发执行。
"""
import logging
from typing import Dict, Optional, Any, List, Callable
from .models import Capability

logger = logging.getLogger(__name__)


class CapabilityRegistry:
    """
    Capability 注册中心。

    - register(cap): 注册 Capability 定义
    - register_handler(cap_id, handler): 注册执行处理器
    - execute(cap_id, params): 查找 handler 执行 Capability
    - get(cap_id): 获取 Capability 定义
    - list_capabilities(): 列出所有已注册 Capability
    """

    def __init__(self):
        self._caps: Dict[str, Capability] = {}
        self._handlers: Dict[str, Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]] = {}

    def register(self, cap: Capability):
        self._caps[cap.capability_id] = cap

    def register_handler(self, capability_id: str,
                         handler: Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]):
        """注册 Capability 的执行处理器。"""
        self._handlers[capability_id] = handler

    def execute(self, capability_id: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        cap = self._caps.get(capability_id)
        if cap is None:
            logger.warning("Capability not found: %s", capability_id)
            return None

        handler = self._handlers.get(capability_id)
        if handler is None:
            logger.warning("No handler registered for capability: %s", capability_id)
            return {
                "status": "no_handler",
                "capability_id": capability_id,
                "message": f"Capability '{capability_id}' has no registered handler",
            }

        try:
            logger.info("Executing capability: %s", capability_id)
            result = handler(params)
            return result
        except Exception as e:
            logger.exception("Capability execution failed: %s (%s)", capability_id, str(e))
            return {"status": "error", "capability_id": capability_id, "error": str(e)}

    def get(self, capability_id: str) -> Optional[Capability]:
        return self._caps.get(capability_id)

    def list_capabilities(self) -> List[Capability]:
        return list(self._caps.values())

    def list_handlers(self) -> List[str]:
        return list(self._handlers.keys())
