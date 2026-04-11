"""
Topology build coordinator tests.
"""
import asyncio
import os
import sys
import time
from typing import Any, Dict

import pytest

# 添加 topology-service 根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import topology_build_coordinator as coordinator
from api.topology_build_coordinator import (
    build_hybrid_topology_coalesced,
    configure_build_process_isolation,
)


@pytest.fixture(autouse=True)
def _reset_process_isolation_config():
    configure_build_process_isolation(
        enabled=False,
        storage_config=None,
        timeout_seconds=45,
        python_executable=None,
        fallback_local_on_error=True,
        max_concurrency=2,
        max_queue_size=64,
        acquire_timeout_seconds=2,
    )
    yield
    configure_build_process_isolation(
        enabled=False,
        storage_config=None,
        timeout_seconds=45,
        python_executable=None,
        fallback_local_on_error=True,
        max_concurrency=2,
        max_queue_size=64,
        acquire_timeout_seconds=2,
    )


class _FakeBuilder:
    def __init__(self, fail_first: bool = False):
        self.calls = 0
        self.fail_first = fail_first

    def build_topology(self, **kwargs):
        self.calls += 1
        time.sleep(0.05)
        if self.fail_first and self.calls == 1:
            raise RuntimeError("simulated-build-failure")
        return {
            "nodes": [{"id": "frontend"}],
            "edges": [],
            "metadata": {
                "generated_at": f"2026-03-01T00:00:0{self.calls}Z",
                "input": kwargs,
            },
        }


def test_same_params_concurrent_requests_are_coalesced():
    builder = _FakeBuilder()

    async def _run():
        return await asyncio.gather(
            build_hybrid_topology_coalesced(
                builder,
                time_window="1 HOUR",
                namespace="prod",
                confidence_threshold=0.3,
                inference_mode="rule",
            ),
            build_hybrid_topology_coalesced(
                builder,
                time_window="1 HOUR",
                namespace="prod",
                confidence_threshold=0.3,
                inference_mode="rule",
            ),
        )

    first, second = asyncio.run(_run())

    assert builder.calls == 1
    assert first == second
    assert first is not second
    first["metadata"]["input"]["time_window"] = "changed"
    assert second["metadata"]["input"]["time_window"] == "1 HOUR"


def test_different_params_do_not_share_build():
    builder = _FakeBuilder()

    async def _run():
        return await asyncio.gather(
            build_hybrid_topology_coalesced(
                builder,
                time_window="1 HOUR",
                namespace="prod",
                confidence_threshold=0.3,
                inference_mode="rule",
            ),
            build_hybrid_topology_coalesced(
                builder,
                time_window="1 HOUR",
                namespace="staging",
                confidence_threshold=0.3,
                inference_mode="rule",
            ),
        )

    asyncio.run(_run())
    assert builder.calls == 2


def test_failed_inflight_request_is_cleaned_up_for_retry():
    builder = _FakeBuilder(fail_first=True)

    async def _first_round():
        return await asyncio.gather(
            build_hybrid_topology_coalesced(
                builder,
                time_window="1 HOUR",
                namespace="prod",
                confidence_threshold=0.3,
                inference_mode="rule",
            ),
            build_hybrid_topology_coalesced(
                builder,
                time_window="1 HOUR",
                namespace="prod",
                confidence_threshold=0.3,
                inference_mode="rule",
            ),
            return_exceptions=True,
        )

    first_round = asyncio.run(_first_round())
    assert all(isinstance(item, RuntimeError) for item in first_round)

    async def _second_round():
        return await build_hybrid_topology_coalesced(
            builder,
            time_window="1 HOUR",
            namespace="prod",
            confidence_threshold=0.3,
            inference_mode="rule",
        )

    result = asyncio.run(_second_round())
    assert builder.calls == 2
    assert result["metadata"]["input"]["namespace"] == "prod"


def test_sequential_requests_do_not_deadlock():
    builder = _FakeBuilder()

    async def _run():
        first = await build_hybrid_topology_coalesced(
            builder,
            time_window="15 MINUTE",
            namespace="prod",
            confidence_threshold=0.6,
            inference_mode="hybrid_score",
        )
        second = await build_hybrid_topology_coalesced(
            builder,
            time_window="15 MINUTE",
            namespace="prod",
            confidence_threshold=0.6,
            inference_mode="hybrid_score",
        )
        return first, second

    first, second = asyncio.run(asyncio.wait_for(_run(), timeout=2.0))
    assert builder.calls == 2
    assert first["metadata"]["input"]["inference_mode"] == "hybrid_score"
    assert second["metadata"]["input"]["inference_mode"] == "hybrid_score"


def test_process_isolation_path_uses_subprocess_runner(monkeypatch):
    builder = _FakeBuilder()
    observed: Dict[str, Any] = {}

    async def _fake_subprocess_runner(
        *,
        storage_config: Dict[str, Any],
        build_kwargs: Dict[str, Any],
        timeout_seconds: int = 45,
        python_executable: str = None,
    ) -> Dict[str, Any]:
        observed["storage_config"] = storage_config
        observed["build_kwargs"] = build_kwargs
        observed["timeout_seconds"] = timeout_seconds
        observed["python_executable"] = python_executable
        return {
            "nodes": [{"id": "worker-frontend"}],
            "edges": [],
            "metadata": {"from": "process"},
        }

    monkeypatch.setattr(
        coordinator,
        "run_hybrid_topology_build_in_subprocess",
        _fake_subprocess_runner,
    )
    configure_build_process_isolation(
        enabled=True,
        storage_config={"clickhouse": {"host": "127.0.0.1", "port": 9000}},
        timeout_seconds=12,
        python_executable="/usr/bin/python3",
        fallback_local_on_error=False,
        max_concurrency=2,
        max_queue_size=64,
        acquire_timeout_seconds=2,
    )

    async def _run():
        return await build_hybrid_topology_coalesced(
            builder,
            time_window="5 MINUTE",
            namespace="prod",
            confidence_threshold=0.5,
            inference_mode="hybrid_score",
        )

    try:
        result = asyncio.run(_run())
    finally:
        configure_build_process_isolation(enabled=False, storage_config=None)

    assert builder.calls == 0
    assert result["metadata"]["from"] == "process"
    assert observed["storage_config"]["clickhouse"]["host"] == "127.0.0.1"
    assert observed["build_kwargs"]["time_window"] == "5 MINUTE"
    assert observed["build_kwargs"]["inference_mode"] == "hybrid_score"
    assert observed["timeout_seconds"] == 12
    assert observed["python_executable"] == "/usr/bin/python3"


def test_process_isolation_fallback_to_local_when_worker_fails(monkeypatch):
    builder = _FakeBuilder()

    async def _failing_subprocess_runner(**kwargs):
        _ = kwargs
        raise RuntimeError("worker failed")

    monkeypatch.setattr(
        coordinator,
        "run_hybrid_topology_build_in_subprocess",
        _failing_subprocess_runner,
    )
    configure_build_process_isolation(
        enabled=True,
        storage_config={"clickhouse": {"host": "127.0.0.1", "port": 9000}},
        timeout_seconds=12,
        fallback_local_on_error=True,
        max_concurrency=2,
        max_queue_size=64,
        acquire_timeout_seconds=2,
    )

    async def _run():
        return await build_hybrid_topology_coalesced(
            builder,
            time_window="5 MINUTE",
            namespace="prod",
            confidence_threshold=0.5,
            inference_mode="rule",
        )

    try:
        result = asyncio.run(_run())
    finally:
        configure_build_process_isolation(enabled=False, storage_config=None)

    assert builder.calls == 1
    assert result["metadata"]["input"]["namespace"] == "prod"


def test_process_isolation_no_fallback_raises_when_worker_fails(monkeypatch):
    builder = _FakeBuilder()

    async def _failing_subprocess_runner(**kwargs):
        _ = kwargs
        raise RuntimeError("worker failed hard")

    monkeypatch.setattr(
        coordinator,
        "run_hybrid_topology_build_in_subprocess",
        _failing_subprocess_runner,
    )
    configure_build_process_isolation(
        enabled=True,
        storage_config={"clickhouse": {"host": "127.0.0.1", "port": 9000}},
        timeout_seconds=12,
        fallback_local_on_error=False,
        max_concurrency=2,
        max_queue_size=64,
        acquire_timeout_seconds=2,
    )

    async def _run():
        return await build_hybrid_topology_coalesced(
            builder,
            time_window="5 MINUTE",
            namespace="prod",
            confidence_threshold=0.5,
            inference_mode="rule",
        )

    try:
        caught = None
        try:
            asyncio.run(_run())
        except RuntimeError as exc:
            caught = exc
    finally:
        configure_build_process_isolation(enabled=False, storage_config=None)

    assert caught is not None
    assert "worker failed hard" in str(caught)
    assert builder.calls == 0


def test_leader_cancellation_not_hang_followers(monkeypatch):
    builder = _FakeBuilder()

    async def _slow_build_with_isolation(hybrid_builder, build_kwargs):
        _ = (hybrid_builder, build_kwargs)
        await asyncio.sleep(10)
        return {"nodes": [], "edges": [], "metadata": {}}

    monkeypatch.setattr(
        coordinator,
        "_build_topology_with_isolation",
        _slow_build_with_isolation,
    )
    configure_build_process_isolation(enabled=False, storage_config=None)

    async def _run():
        leader = asyncio.create_task(
            build_hybrid_topology_coalesced(builder, time_window="1 HOUR")
        )
        await asyncio.sleep(0.05)
        follower = asyncio.create_task(
            build_hybrid_topology_coalesced(builder, time_window="1 HOUR")
        )
        await asyncio.sleep(0.2)
        leader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await leader

        # follower should observe leader failure/cancel quickly, not hang forever
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(follower, timeout=1.0)

    asyncio.run(_run())


def test_process_isolation_queue_full_without_fallback(monkeypatch):
    builder = _FakeBuilder()
    gate = asyncio.Event()

    async def _blocked_subprocess_runner(
        *,
        storage_config: Dict[str, Any],
        build_kwargs: Dict[str, Any],
        timeout_seconds: int = 45,
        python_executable: str = None,
    ) -> Dict[str, Any]:
        _ = (storage_config, timeout_seconds, python_executable)
        await gate.wait()
        return {
            "nodes": [{"id": build_kwargs.get("namespace", "unknown")}],
            "edges": [],
            "metadata": {"from": "process", "input": build_kwargs},
        }

    monkeypatch.setattr(
        coordinator,
        "run_hybrid_topology_build_in_subprocess",
        _blocked_subprocess_runner,
    )
    configure_build_process_isolation(
        enabled=True,
        storage_config={"clickhouse": {"host": "127.0.0.1", "port": 9000}},
        timeout_seconds=12,
        fallback_local_on_error=False,
        max_concurrency=1,
        max_queue_size=1,
        acquire_timeout_seconds=2,
    )

    async def _run():
        first = asyncio.create_task(
            build_hybrid_topology_coalesced(builder, time_window="1 HOUR", namespace="n1")
        )
        await asyncio.sleep(0.05)
        second = asyncio.create_task(
            build_hybrid_topology_coalesced(builder, time_window="1 HOUR", namespace="n2")
        )
        await asyncio.sleep(0.05)
        with pytest.raises(RuntimeError, match="queue full"):
            await build_hybrid_topology_coalesced(builder, time_window="1 HOUR", namespace="n3")
        gate.set()
        first_result, second_result = await asyncio.gather(first, second)
        assert first_result["metadata"]["from"] == "process"
        assert second_result["metadata"]["from"] == "process"

    asyncio.run(_run())
    assert builder.calls == 0


def test_process_isolation_acquire_timeout_fallback_to_local(monkeypatch):
    builder = _FakeBuilder()
    gate = asyncio.Event()

    async def _blocked_subprocess_runner(
        *,
        storage_config: Dict[str, Any],
        build_kwargs: Dict[str, Any],
        timeout_seconds: int = 45,
        python_executable: str = None,
    ) -> Dict[str, Any]:
        _ = (storage_config, build_kwargs, timeout_seconds, python_executable)
        await gate.wait()
        return {"nodes": [], "edges": [], "metadata": {"from": "process"}}

    monkeypatch.setattr(
        coordinator,
        "run_hybrid_topology_build_in_subprocess",
        _blocked_subprocess_runner,
    )
    configure_build_process_isolation(
        enabled=True,
        storage_config={"clickhouse": {"host": "127.0.0.1", "port": 9000}},
        timeout_seconds=12,
        fallback_local_on_error=True,
        max_concurrency=1,
        max_queue_size=8,
        acquire_timeout_seconds=1,
    )

    async def _run():
        first = asyncio.create_task(
            build_hybrid_topology_coalesced(builder, time_window="1 HOUR", namespace="process-ns")
        )
        await asyncio.sleep(0.05)
        # second request should timeout on subprocess slot then fallback to local builder
        second = await build_hybrid_topology_coalesced(
            builder,
            time_window="1 HOUR",
            namespace="fallback-ns",
        )
        gate.set()
        await first
        return second

    second_result = asyncio.run(_run())
    assert builder.calls == 1
    assert second_result["metadata"]["input"]["namespace"] == "fallback-ns"
