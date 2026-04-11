"""
Temporal worker lifecycle for runtime v4.

Worker is optional and only starts when Temporal remote mode is enabled.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import os
from typing import Any, Dict, Optional

from ai.runtime_v4.temporal import activities
from ai.runtime_v4.temporal.client import get_temporal_runtime_config
from ai.runtime_v4.temporal.workflows import AIRuntimeRunWorkflow, temporal_workflow_available


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _outer_engine_mode() -> str:
    raw = _as_str(os.getenv("AI_RUNTIME_V4_OUTER_ENGINE"), "temporal_local").strip().lower()
    if raw in {"temporal_required", "temporal-strict", "temporal_strict"}:
        return "temporal_required"
    if raw in {"temporal", "temporal_v1"}:
        return "temporal"
    return "temporal_local"


def _worker_enabled() -> bool:
    raw = _as_str(os.getenv("AI_RUNTIME_V4_TEMPORAL_WORKER_ENABLED"), "true").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    return True


def temporal_worker_available() -> bool:
    try:
        import temporalio  # noqa: F401

        return True
    except Exception:
        return False


@dataclass
class TemporalWorkerRuntime:
    running: bool = False
    mode: str = "temporal_local"
    address: str = ""
    namespace: str = ""
    task_queue: str = ""
    error: str = ""
    task: Optional[asyncio.Task] = None
    worker: Any = None
    client: Any = None


_runtime = TemporalWorkerRuntime()


def get_temporal_worker_runtime() -> TemporalWorkerRuntime:
    return _runtime


async def start_temporal_worker() -> Optional[Dict[str, Any]]:
    mode = _outer_engine_mode()
    cfg = get_temporal_runtime_config()

    _runtime.mode = mode
    _runtime.address = cfg.address
    _runtime.namespace = cfg.namespace
    _runtime.task_queue = cfg.task_queue
    _runtime.error = ""

    if mode not in {"temporal", "temporal_required"}:
        return None
    if not _worker_enabled():
        if mode == "temporal_required":
            raise RuntimeError("temporal_required but AI_RUNTIME_V4_TEMPORAL_WORKER_ENABLED=false")
        return None
    if not temporal_worker_available():
        if mode == "temporal_required":
            raise RuntimeError("temporal_required but temporalio SDK is unavailable")
        return None
    if not temporal_workflow_available():
        if mode == "temporal_required":
            raise RuntimeError("temporal_required but workflow definitions are unavailable")
        return None
    if not cfg.address:
        if mode == "temporal_required":
            raise RuntimeError("temporal_required but AI_RUNTIME_V4_TEMPORAL_ADDRESS is empty")
        return None

    if _runtime.running and _runtime.task is not None:
        return {
            "status": "running",
            "mode": mode,
            "address": cfg.address,
            "namespace": cfg.namespace,
            "task_queue": cfg.task_queue,
        }

    try:
        from temporalio.client import Client
        from temporalio.worker import Worker

        client = await asyncio.wait_for(
            Client.connect(cfg.address, namespace=cfg.namespace),
            timeout=max(1, cfg.connect_timeout_seconds),
        )
        worker = Worker(
            client,
            task_queue=cfg.task_queue,
            workflows=[AIRuntimeRunWorkflow],
            activities=[
                activities.start_run_activity,
                activities.resolve_approval_activity,
                activities.submit_user_input_activity,
                activities.interrupt_run_activity,
            ],
        )
        task = asyncio.create_task(worker.run(), name="runtime-v4-temporal-worker")
        _runtime.running = True
        _runtime.worker = worker
        _runtime.client = client
        _runtime.task = task
        return {
            "status": "running",
            "mode": mode,
            "address": cfg.address,
            "namespace": cfg.namespace,
            "task_queue": cfg.task_queue,
        }
    except Exception as exc:
        _runtime.running = False
        _runtime.worker = None
        _runtime.client = None
        _runtime.task = None
        _runtime.error = _as_str(exc)
        if mode == "temporal_required":
            raise RuntimeError(f"temporal worker start failed: {_runtime.error}") from exc
        return None


async def stop_temporal_worker() -> None:
    task = _runtime.task
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    _runtime.running = False
    _runtime.task = None
    _runtime.worker = None
    _runtime.client = None
