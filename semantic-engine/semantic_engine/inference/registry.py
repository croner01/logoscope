"""InferenceRegistry — 按 event_type 分配 InferencePipeline。"""
from typing import Dict, Optional
from .pipeline import InferencePipeline


class InferenceRegistry:
    """
    Inference Registry——映射 event_type -> Pipeline。

    - register(event_type, pipeline): 注册
    - get(event_type): 获取 pipeline
    """

    def __init__(self):
        self._pipelines: Dict[str, InferencePipeline] = {}

    def register(self, event_type: str, pipeline: InferencePipeline) -> None:
        self._pipelines[event_type] = pipeline

    def get(self, event_type: str) -> Optional[InferencePipeline]:
        return self._pipelines.get(event_type)
