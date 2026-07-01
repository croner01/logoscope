"""StateQuery — 状态查询接口（WorldView 的查询组件之一）。"""
from typing import Dict, List, Optional, Any, Tuple


class StateQuery:
    """
    状态查询——查询实体的当前状态和状态演化。

    - get_state: 查询单个实体的当前状态
    - get_states: 批量查询
    - get_timeline: 状态演化链
    - has_state_changed: 窗口内是否变化
    - resolve_field: 供 Expression.evaluate() 使用
    """

    def __init__(self, state_projection, timeline_projection):
        self.state = state_projection
        self.timeline = timeline_projection

    def get_state(self, entity_type: str, entity_name: str) -> Optional[str]:
        """当前状态。"""
        return self.state.query(entity_type, entity_name)

    def get_states(self, entities: List[Tuple[str, str]]) -> List[Optional[str]]:
        """批量查询。"""
        return [self.state.query(t, n) for t, n in entities]

    def get_timeline(self, entity_id: str, window: str = "1 HOUR") -> List:
        """状态演化链。"""
        return self.timeline.get_timeline(entity_id, window)

    def has_state_changed(self, entity_id: str, window_minutes: int = 5) -> bool:
        """指定窗口内状态是否变化过。"""
        return self.timeline.has_state_changed(entity_id, window_minutes)

    def resolve_field(self, field_path: str, entity_type: str,
                      entity_name: str) -> Any:
        """
        按字段路径解析当前值——供 Expression 求值使用。

        "resource.status" → self.get_state(entity_type, entity_name)
        "host.host_status" → self.get_state("HOST", host_for_entity)
        """
        parts = field_path.split(".")
        if len(parts) >= 2:
            if parts[0] == "resource":
                return self.get_state(entity_type, entity_name)
            elif parts[0] == "host":
                # 简化：直接返回空（host 映射在 StateQuery 之外）
                return self.get_state("HOST", entity_name)
        return None
