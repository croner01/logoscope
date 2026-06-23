"""运行时后端实现。"""
from ai.runtime.backends.langgraph import LangGraphBackend

try:
    from ai.runtime.backends.claude_sdk import ClaudeSdkBackend
except ImportError:
    ClaudeSdkBackend = None  # 可选依赖（需要 anthropic SDK）

__all__ = ["LangGraphBackend", "ClaudeSdkBackend"]
