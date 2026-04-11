"""Backward-compatible graph.storage_adapter shim."""

from typing import Any, Dict, List, Optional


class StorageAdapter:
    """轻量兼容适配器，仅用于历史测试导入路径。"""

    def __init__(self) -> None:
        self.ch_client = None

    def execute_query(self, query: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        return []

