"""Provider abstraction for OpenHands-backed planning."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Dict, Protocol

from ai.runtime_v4.backend.base import RuntimeBackendRequest


class OpenHandsProvider(Protocol):
    """Provider interface for OpenHands planning/session adapters."""

    def run(self, request: RuntimeBackendRequest) -> Dict[str, Any]:
        ...


class StaticOpenHandsProvider:
    """Fallback provider used before a real OpenHands SDK session is wired in."""

    def run(self, request: RuntimeBackendRequest) -> Dict[str, Any]:
        _ = request
        return {}


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return int(default)
        return int(str(value).strip())
    except Exception:
        return int(default)


_provider: OpenHandsProvider | None = None
_provider_cache_key: tuple[str, bool, str, str, int] | None = None


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _provider_factory_spec() -> str:
    return _as_str(os.getenv("AI_RUNTIME_V4_OPENHANDS_PROVIDER_FACTORY")).strip()


def _helper_enabled() -> bool:
    return _as_bool(os.getenv("AI_RUNTIME_V4_OPENHANDS_HELPER_ENABLED"), default=False)


def _helper_python_spec() -> str:
    return _as_str(os.getenv("AI_RUNTIME_V4_OPENHANDS_HELPER_PYTHON") or "/opt/openharness-venv/bin/python").strip()


def _helper_script_spec() -> str:
    return _as_str(
        os.getenv("AI_RUNTIME_V4_OPENHANDS_HELPER_SCRIPT") or "/app/ai/runtime_v4/backend/openhands_helper.py"
    ).strip()


def _helper_timeout_seconds() -> int:
    return max(10, min(_as_int(os.getenv("AI_RUNTIME_V4_OPENHANDS_HELPER_TIMEOUT_SECONDS"), 90), 300))


def _provider_cache_identity() -> tuple[str, bool, str, str, int]:
    return (
        _provider_factory_spec(),
        _helper_enabled(),
        _helper_python_spec(),
        _helper_script_spec(),
        _helper_timeout_seconds(),
    )


def _resolve_helper_python(spec: str) -> str:
    safe_spec = _as_str(spec).strip()
    if not safe_spec:
        raise RuntimeError("OpenHands helper python is not configured")
    if os.path.isabs(safe_spec):
        if not os.path.exists(safe_spec):
            raise RuntimeError(f"OpenHands helper python not found: {safe_spec}")
        return safe_spec
    resolved = shutil.which(safe_spec)
    if not resolved:
        raise RuntimeError(f"OpenHands helper python not found: {safe_spec}")
    return resolved


def _resolve_helper_script(spec: str) -> str:
    safe_spec = _as_str(spec).strip()
    if not safe_spec:
        raise RuntimeError("OpenHands helper script is not configured")
    resolved = Path(safe_spec).expanduser()
    if not resolved.exists():
        raise RuntimeError(f"OpenHands helper script not found: {resolved}")
    return str(resolved.resolve())


class SubprocessOpenHandsProvider:
    """Run the real OpenHarness package inside an isolated helper interpreter."""

    def __init__(
        self,
        *,
        helper_python: str,
        helper_script: str,
        timeout_seconds: int,
    ) -> None:
        self.helper_python = _resolve_helper_python(helper_python)
        self.helper_script = _resolve_helper_script(helper_script)
        self.timeout_seconds = max(10, int(timeout_seconds))

    def _build_request_payload(self, request: RuntimeBackendRequest) -> Dict[str, Any]:
        return {
            "request": {
                "run_id": _as_str(request.run_id),
                "question": _as_str(request.question),
                "analysis_context": dict(request.analysis_context or {}),
                "runtime_options": dict(request.runtime_options or {}),
            }
        }

    def run(self, request: RuntimeBackendRequest) -> Dict[str, Any]:
        helper_env = dict(os.environ)
        helper_env.setdefault("AI_RUNTIME_V4_OPENHANDS_ENABLED", "true")
        completed = subprocess.run(
            [self.helper_python, self.helper_script],
            input=json.dumps(self._build_request_payload(request), ensure_ascii=False),
            capture_output=True,
            text=True,
            check=False,
            env=helper_env,
            timeout=self.timeout_seconds,
        )
        stderr_text = _as_str(completed.stderr).strip()
        stdout_text = _as_str(completed.stdout).strip()
        if completed.returncode != 0:
            raise RuntimeError(
                f"OpenHands helper execution failed with exit code {completed.returncode}: {stderr_text or stdout_text}"
            )
        if not stdout_text:
            raise RuntimeError("OpenHands helper returned empty stdout")
        try:
            payload = json.loads(stdout_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"OpenHands helper returned invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("OpenHands helper returned a non-object payload")
        return payload


def _load_provider_from_factory_spec(spec: str) -> OpenHandsProvider:
    safe_spec = _as_str(spec).strip()
    if not safe_spec:
        return StaticOpenHandsProvider()
    module_name, sep, attr_name = safe_spec.partition(":")
    if not module_name.strip() or not sep or not attr_name.strip():
        raise RuntimeError(
            "OpenHands provider factory must use '<module>:<callable>' format"
        )
    try:
        module = importlib.import_module(module_name.strip())
    except Exception as exc:
        raise RuntimeError(f"OpenHands provider factory import failed: {safe_spec}: {exc}") from exc
    try:
        factory = getattr(module, attr_name.strip())
    except AttributeError as exc:
        raise RuntimeError(f"OpenHands provider factory attribute missing: {safe_spec}") from exc
    if not callable(factory):
        raise RuntimeError(f"OpenHands provider factory is not callable: {safe_spec}")
    try:
        provider = factory()
    except Exception as exc:
        raise RuntimeError(f"OpenHands provider factory execution failed: {safe_spec}: {exc}") from exc
    if provider is None:
        raise RuntimeError(f"OpenHands provider factory returned None: {safe_spec}")
    return provider


def _build_default_provider() -> OpenHandsProvider:
    if _helper_enabled():
        return SubprocessOpenHandsProvider(
            helper_python=_helper_python_spec(),
            helper_script=_helper_script_spec(),
            timeout_seconds=_helper_timeout_seconds(),
        )
    return StaticOpenHandsProvider()


def validate_openhands_provider_readiness() -> None:
    spec = _provider_factory_spec()
    if spec:
        _load_provider_from_factory_spec(spec)
        return
    if _helper_enabled():
        _build_default_provider()


def get_openhands_provider() -> OpenHandsProvider:
    global _provider, _provider_cache_key
    cache_key = _provider_cache_identity()
    if _provider is None or _provider_cache_key != cache_key:
        spec = _provider_factory_spec()
        if spec:
            _provider = _load_provider_from_factory_spec(spec)
        else:
            _provider = _build_default_provider()
        _provider_cache_key = cache_key
    return _provider


def reset_openhands_provider() -> None:
    global _provider, _provider_cache_key
    _provider = None
    _provider_cache_key = None


__all__ = [
    "OpenHandsProvider",
    "StaticOpenHandsProvider",
    "SubprocessOpenHandsProvider",
    "get_openhands_provider",
    "reset_openhands_provider",
    "validate_openhands_provider_readiness",
]
