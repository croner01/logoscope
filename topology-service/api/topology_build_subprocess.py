"""
Hybrid topology build subprocess runner.

Run heavy topology build in an isolated subprocess to protect API process.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Dict


def _normalize_for_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _normalize_for_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_for_json(item) for item in value]
    if isinstance(value, set):
        normalized_items = [_normalize_for_json(item) for item in value]
        try:
            return sorted(normalized_items)
        except Exception:
            return normalized_items
    return value


def _coerce_timeout_seconds(value: Any, default_value: int = 45) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default_value
    return max(3, min(300, parsed))


async def run_hybrid_topology_build_in_subprocess(
    *,
    storage_config: Dict[str, Any],
    build_kwargs: Dict[str, Any],
    timeout_seconds: int = 45,
    python_executable: str | None = None,
) -> Dict[str, Any]:
    """
    Execute `HybridTopologyBuilder.build_topology` in isolated subprocess.
    """
    timeout_value = _coerce_timeout_seconds(timeout_seconds)
    payload = {
        "storage_config": _normalize_for_json(storage_config or {}),
        "build_kwargs": _normalize_for_json(build_kwargs or {}),
    }
    input_bytes = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")

    python_bin = python_executable or sys.executable or "python3"
    worker_cwd = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    cmd = [python_bin, "-m", "api.topology_build_worker"]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=worker_cwd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(input=input_bytes),
            timeout=float(timeout_value),
        )
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise RuntimeError(f"topology build subprocess timeout after {timeout_value}s") from exc

    stdout_text = (stdout_bytes or b"").decode("utf-8", errors="replace").strip()
    stderr_text = (stderr_bytes or b"").decode("utf-8", errors="replace").strip()
    lines = [line.strip() for line in stdout_text.splitlines() if line.strip()]

    def _extract_worker_error_text() -> str:
        if not lines:
            return ""
        try:
            response = json.loads(lines[-1])
        except Exception:
            return ""
        if not isinstance(response, dict):
            return ""
        if response.get("ok"):
            return ""
        error_type = str(response.get("error_type") or "RuntimeError")
        error_text = str(response.get("error") or "unknown worker error")
        return f"{error_type}: {error_text}"

    if process.returncode != 0:
        worker_error = _extract_worker_error_text()
        detail = worker_error or stderr_text[:800]
        raise RuntimeError(
            f"topology build subprocess exited with code {process.returncode}; detail={detail}"
        )

    # Worker prints a single JSON line, parse robustly from last non-empty line.
    if not lines:
        raise RuntimeError("topology build subprocess returned empty output")

    try:
        response = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"topology build subprocess returned invalid JSON: {lines[-1][:800]}"
        ) from exc

    if not isinstance(response, dict):
        raise RuntimeError("topology build subprocess response is not an object")

    if not response.get("ok"):
        error_type = str(response.get("error_type") or "RuntimeError")
        error_text = str(response.get("error") or "unknown worker error")
        raise RuntimeError(f"{error_type}: {error_text}")

    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("topology build subprocess response has invalid result")
    return result
