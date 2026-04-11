"""
回归测试：topology-service 适配层在脚本/本地模式下可稳定导入 shared_src。
"""
import importlib
import os
import sys

import pytest


MODULES = (
    "otel_init",
    "storage.deduplication",
    "storage.topology_snapshots",
    "graph.service_sync",
    "graph.hybrid_topology_enhanced",
    "utils.otlp",
    "utils.timestamp",
    "api.realtime_topology",
)

_SERVICE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


@pytest.mark.parametrize("module_name", MODULES)
def test_wrapper_module_can_import_shared_src_with_topology_service_only_path(
    module_name: str,
    monkeypatch: pytest.MonkeyPatch,
):
    """
    topology-service tests/conftest 仅注入 topology-service 目录到 sys.path，
    这里验证 wrapper 会主动补齐项目根目录与 shared_src 路径。
    """
    filtered_path = [
        path_entry
        for path_entry in sys.path
        if not (path_entry.endswith("/logoscope") or path_entry.endswith("/shared_src"))
    ]
    if _SERVICE_ROOT not in filtered_path:
        filtered_path.insert(0, _SERVICE_ROOT)
    monkeypatch.setattr(sys, "path", filtered_path)

    imported = importlib.import_module(module_name)
    imported = importlib.reload(imported)
    assert imported is not None
