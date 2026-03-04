"""
消息队列抽象接口

定义统一的消息队列接口，遵循依赖倒置原则（DIP）
"""
from abc import ABC, abstractmethod
from typing import Callable, Optional, Any, Dict
from dataclasses import dataclass


@dataclass
class Message:
    """消息数据类"""
    subject: str  # 主题/频道
    data: bytes  # 消息数据（二进制）
    headers: Optional[Dict[str, str]] = None  # 可选的消息头

    def __post_init__(self):
        if self.headers is None:
            self.headers = {}


class MessageQueue(ABC):
    """消息队列抽象基类"""

    @abstractmethod
    async def publish(self, subject: str, message: bytes, headers: Dict[str, str] = None) -> bool:
        """
        发布消息到指定主题

        Args:
            subject: 主题名称（如 "logs.raw"）
            message: 消息数据（二进制）
            headers: 可选的消息头

        Returns:
            bool: 是否发布成功
        """
        pass

    @abstractmethod
    async def subscribe(
        self,
        subject: str,
        callback: Callable[[Message], Any],
        queue_group: str = None
    ) -> None:
        """
        订阅主题并注册回调函数

        Args:
            subject: 主题名称（可使用通配符，如 "logs.>"）
            callback: 消息处理回调函数
            queue_group: 消费者组名称（用于负载均衡）
        """
        pass

    @abstractmethod
    async def close(self) -> None:
        """关闭连接"""
        pass

    @abstractmethod
    def is_connected(self) -> bool:
        """检查是否已连接"""
        pass
