"""
回归测试：语义引擎适配层在脚本/本地模式下可稳定导入 shared_src 包。
"""
import importlib
import sys

import pytest


MODULES = (
    "otel_init",
    "graph.confidence_calculator",
    "graph.service_sync",
    "graph.hybrid_topology_enhanced",
    "storage.deduplication",
    "storage.topology_snapshots",
    "utils.otlp",
    "utils.timestamp",
)


@pytest.mark.parametrize("module_name", MODULES)
def test_wrapper_module_can_import_shared_src_with_semantic_engine_only_path(
    module_name: str,
    monkeypatch: pytest.MonkeyPatch,
):
    """
    验证仅存在 semantic-engine 路径时，适配层仍可加载 shared_src。

    tests/conftest.py 默认只注入 semantic-engine 目录到 sys.path，
    该场景对应本地脚本运行 `python semantic-engine/start.py` 的导入行为。
    """
    filtered_path = [
        path_entry
        for path_entry in sys.path
        if not (path_entry.endswith("/logoscope") or path_entry.endswith("/shared_src"))
    ]
    monkeypatch.setattr(sys, "path", filtered_path)

    imported = importlib.import_module(module_name)
    imported = importlib.reload(imported)
    assert imported is not None
