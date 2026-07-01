"""ContextSnapshot — 上下文快照（属于 Projection Layer）。"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict


@dataclass
class ContextSnapshot:
    """上下文快照——某个时间点的完整上下文视图。"""
    snapshot_id: str
    entity_type: str
    entity_name: str
    context_data: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)
    expires_at: datetime = field(default_factory=lambda: datetime.utcnow())
