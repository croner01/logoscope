"""
NATS JetStream适配器

实现MessageQueue接口，使用NATS JetStream作为消息队列
"""
import asyncio
import json
from typing import Callable, Optional, Any
import logging

try:
    from nats.aio.client import Client as NATSClient
    from nats.errors import TimeoutError, NoServersError
    NATS_AVAILABLE = True
except ImportError:
    NATS_AVAILABLE = False

from .interface import MessageQueue, Message

logger = logging.getLogger(__name__)


class NATSQueue(MessageQueue):
    """NATS JetStream队列实现"""

    def __init__(
        self,
        servers: str = "nats:4222"
    ):
        """
        初始化NATS客户端

        Args:
            servers: NATS服务器地址（逗号分隔）
        """
        if not NATS_AVAILABLE:
            raise ImportError("nats-py not installed. Run: pip install nats-py")

        self.servers = servers
        self.nc: Optional[NATSClient] = None
        self._connected = False

    async def connect(self) -> bool:
        """
        连接到NATS服务器

        Returns:
            bool: 是否连接成功
        """
        if not NATS_AVAILABLE:
            logger.error("NATS client not available")
            return False

        try:
            self.nc = NATSClient()

            await self.nc.connect(servers=self.servers)
            self._connected = True
            logger.info(f"Connected to NATS: {self.servers}")
            return True

        except (TimeoutError, NoServersError) as e:
            logger.error(f"Failed to connect to NATS: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error connecting to NATS: {e}")
            return False

    async def publish(
        self,
        subject: str,
        message: bytes,
        headers: dict = None
    ) -> bool:
        """
        发布消息

        Args:
            subject: 主题名称
            message: 消息数据
            headers: 可选的消息头

        Returns:
            bool: 是否发布成功
        """
        if not self.is_connected():
            logger.warning("NATS not connected, attempting to reconnect...")
            await self.connect()

        try:
            await self.nc.publish(subject, message, headers=headers)
            logger.debug(f"Published message to {subject} ({len(message)} bytes)")
            return True

        except Exception as e:
            logger.error(f"Failed to publish message: {e}")
            return False

    async def subscribe(
        self,
        subject: str,
        callback: Callable[[Message], Any],
        queue_group: str = None
    ) -> None:
        """
        订阅主题

        Args:
            subject: 主题名称
            callback: 消息处理回调
            queue_group: 消费者组名称
        """
        if not self.is_connected():
            await self.connect()

        async def wrapper(msg):
            """封装NATS消息为统一格式"""
            message = Message(
                subject=msg.subject,
                data=msg.data,
                headers=msg.headers
            )
            try:
                await callback(message)
            except Exception as e:
                logger.error(f"Error in message callback: {e}")

        try:
            if queue_group:
                await self.nc.subscribe(
                    subject,
                    queue_group,
                    cb=wrapper
                )
                logger.info(f"Subscribed to {subject} (queue group: {queue_group})")
            else:
                await self.nc.subscribe(
                    subject,
                    cb=wrapper
                )
                logger.info(f"Subscribed to {subject}")
        except Exception as e:
            logger.error(f"Failed to subscribe: {e}")

    async def close(self) -> None:
        """关闭连接"""
        if self.nc:
            try:
                await self.nc.close()
                self._connected = False
                logger.info("NATS connection closed")
            except Exception as e:
                logger.error(f"Error closing NATS connection: {e}")

    def is_connected(self) -> bool:
        """检查连接状态"""
        return self._connected and self.nc is not None
