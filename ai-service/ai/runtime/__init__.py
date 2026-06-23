"""AI runtime layer — unified diagnosis engine, state, tools, prompt, memory, events."""
from ai.runtime.engine import run_diagnosis, RuntimeResult
from ai.runtime.state import RuntimeState, Action, Observation
from ai.runtime.memory import SessionMemory
from ai.runtime.events import EventEmitter

# 注册所有后端（import 触发 register_backend）
import ai.runtime.backends  # noqa: F401

__all__ = ["run_diagnosis", "RuntimeResult", "RuntimeState", "Action", "Observation", "SessionMemory", "EventEmitter"]
