"""
Tests for streaming command runner.
"""

import asyncio
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.runner import stream_command


def test_stream_command_emits_stdout_and_stderr_chunks():
    outputs: list[tuple[str, str]] = []

    async def _run() -> None:
        result = await stream_command(
            'python3 -c "import sys,time; print(\'alpha\'); sys.stdout.flush(); time.sleep(0.05); sys.stderr.write(\'beta\\\\n\'); sys.stderr.flush()"',
            timeout_seconds=5,
            on_output=lambda stream, text: outputs.append((stream, text)),
        )
        assert result["exit_code"] == 0
        assert result["timed_out"] is False

    asyncio.run(_run())

    stdout_text = "".join(text for stream, text in outputs if stream == "stdout")
    stderr_text = "".join(text for stream, text in outputs if stream == "stderr")
    assert "alpha" in stdout_text
    assert "beta" in stderr_text


def test_stream_command_reports_timeout():
    outputs: list[tuple[str, str]] = []

    async def _run() -> None:
        result = await stream_command(
            'python3 -c "import sys,time; print(\'start\'); sys.stdout.flush(); time.sleep(5)"',
            timeout_seconds=1,
            on_output=lambda stream, text: outputs.append((stream, text)),
        )
        assert result["timed_out"] is True
        assert result["exit_code"] != 0

    asyncio.run(_run())

    stdout_text = "".join(text for stream, text in outputs if stream == "stdout")
    assert "start" in stdout_text


def test_stream_command_blocks_pipeline_and_chain_by_default():
    outputs: list[tuple[str, str]] = []

    async def _run() -> None:
        result = await stream_command(
            'echo alpha | cat && echo omega',
            timeout_seconds=5,
            on_output=lambda stream, text: outputs.append((stream, text)),
        )
        assert result["exit_code"] == 126
        assert result["timed_out"] is False

    asyncio.run(_run())

    stderr_text = "".join(text for stream, text in outputs if stream == "stderr")
    assert "shell syntax is disabled by policy" in stderr_text
