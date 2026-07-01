from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any
from datetime import datetime
from ..event.envelope import EventEnvelope
from .checkpoint import ProjectionCheckpoint


@dataclass
class ProjectionStatus:
    """Projection 的运行时状态快照。"""
    projection_epoch: str
    event_count: int
    checkpoint: ProjectionCheckpoint
    is_rebuilding: bool = False
    rebuild_progress: float = 0.0
    lag: int = 0


class Projection(ABC):
    """所有 Projection 的统一基类。

    每个 Projection 封装一个确定的读取-计算过程：
    - 消费一个或多个 topic 的 EventEnvelope
    - 产出某种物化视图（表、索引、缓存等）
    - 支持增量 apply 和全量 rebuild
    """

    name: str = ""
    epoch: str = ""

    @property
    def upstream_topics(self) -> List[str]:
        return []

    @abstractmethod
    def apply(self, envelope: EventEnvelope):
        """增量应用一个事件到当前投影。"""
        ...

    @abstractmethod
    def rebuild(self, event_source: Any):
        """从事件源全量重建投影。"""
        ...

    @abstractmethod
    def checkpoint(self) -> ProjectionCheckpoint:
        """返回当前消费进度 checkpoint。"""
        ...

    @abstractmethod
    def status(self) -> ProjectionStatus:
        """返回投影的运行时状态。"""
        ...
