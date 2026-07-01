"""MemoryRecord — 操作记忆记录。"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class MemoryRecord:
    """操作记忆——记录之前执行的操作和结果。"""
    record_id: str
    record_type: str  # "repair", "diagnose", "inspect"
    outcome: str      # "success", "failure"
    action_taken: str
    error_message: str = ""
    duration_ms: int = 0
    timestamp: datetime = field(default_factory=datetime.utcnow)
    context_hash: str = ""
