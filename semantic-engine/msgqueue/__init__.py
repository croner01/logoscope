"""
消息队列抽象层

提供统一的队列接口，当前仅保留 Kafka 实现。
"""
from .interface import MessageQueue, Message

try:
    from .kafka_adapter import KafkaQueue
except Exception:  # pragma: no cover - 依赖缺失时按可选模块处理
    KafkaQueue = None

__all__ = ['MessageQueue', 'Message', 'KafkaQueue']
