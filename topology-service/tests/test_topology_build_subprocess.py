"""
Tests for topology build subprocess runner.
"""
import asyncio
import os
import sys

import pytest

# 添加 topology-service 根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import topology_build_subprocess as runner


class _FakeProcess:
    def __init__(self, returncode, stdout_text="", stderr_text=""):
        self.returncode = returncode
        self._stdout = stdout_text.encode("utf-8")
        self._stderr = stderr_text.encode("utf-8")
        self.killed = False

    async def communicate(self, input=None):  # noqa: A002
        _ = input
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True


@pytest.mark.asyncio
async def test_runner_extracts_worker_error_from_stdout(monkeypatch):
    async def _fake_create_subprocess_exec(*args, **kwargs):
        _ = (args, kwargs)
        return _FakeProcess(
            returncode=1,
            stdout_text='{"ok": false, "error_type": "ValueError", "error": "bad payload"}',
            stderr_text="",
        )

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _fake_create_subprocess_exec,
    )

    with pytest.raises(RuntimeError) as exc:
        await runner.run_hybrid_topology_build_in_subprocess(
            storage_config={"clickhouse": {"host": "127.0.0.1", "port": 9000}},
            build_kwargs={"time_window": "1 HOUR"},
        )

    assert "ValueError: bad payload" in str(exc.value)


@pytest.mark.asyncio
async def test_runner_uses_stderr_when_stdout_not_structured(monkeypatch):
    async def _fake_create_subprocess_exec(*args, **kwargs):
        _ = (args, kwargs)
        return _FakeProcess(
            returncode=2,
            stdout_text="plain error",
            stderr_text="traceback line",
        )

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _fake_create_subprocess_exec,
    )

    with pytest.raises(RuntimeError) as exc:
        await runner.run_hybrid_topology_build_in_subprocess(
            storage_config={"clickhouse": {"host": "127.0.0.1", "port": 9000}},
            build_kwargs={"time_window": "1 HOUR"},
        )

    assert "traceback line" in str(exc.value)

