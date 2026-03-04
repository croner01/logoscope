"""
消息队列抽象层

提供统一的队列接口，支持多种消息队列实现（NATS、Redis等）
符合SOLID原则中的依赖倒置原则
"""
from .interface import MessageQueue, Message

try:
    from .nats_adapter import NATSQueue
except Exception:  # pragma: no cover - 依赖缺失时按可选模块处理
    NATSQueue = None

try:
    from .redis_adapter import RedisStreamAdapter
except Exception:  # pragma: no cover - 依赖缺失时按可选模块处理
    RedisStreamAdapter = None

__all__ = ['MessageQueue', 'Message', 'NATSQueue', 'RedisStreamAdapter']
