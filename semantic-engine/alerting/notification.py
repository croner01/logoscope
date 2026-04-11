"""
告警通知服务

提供多种告警通知渠道：
- WebSocket 实时推送
- Webhook 回调
- 邮件通知（可扩展）
- Slack/钉钉通知（可扩展）

Date: 2026-02-22
"""

import logging
import json
import asyncio
from typing import Dict, Any, List, Optional, Callable
from datetime import datetime, timezone
from dataclasses import dataclass
import aiohttp

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    """Return timezone-aware UTC timestamp string."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class NotificationConfig:
    """通知配置"""
    webhook_url: Optional[str] = None
    email_enabled: bool = False
    slack_webhook: Optional[str] = None
    dingtalk_webhook: Optional[str] = None


class NotificationService:
    """告警通知服务"""

    def __init__(self, config: NotificationConfig = None):
        self.config = config or NotificationConfig()
        self._websocket_handlers: List[Callable] = []
        self._notification_history: List[Dict[str, Any]] = []

    def register_websocket_handler(self, handler: Callable):
        """注册 WebSocket 推送处理器"""
        self._websocket_handlers.append(handler)

    async def send_notification(self, alert: Dict[str, Any]):
        """发送告警通知"""
        notification_record = {
            "alert_id": alert.get("id"),
            "severity": alert.get("severity"),
            "message": alert.get("message"),
            "sent_at": _utc_now_iso(),
            "channels": [],
        }

        # WebSocket 实时推送
        await self._send_via_websocket(alert)
        notification_record["channels"].append("websocket")

        # Webhook 回调
        if self.config.webhook_url:
            await self._send_via_webhook(alert, self.config.webhook_url)
            notification_record["channels"].append("webhook")

        # Slack 通知
        if self.config.slack_webhook:
            await self._send_to_slack(alert, self.config.slack_webhook)
            notification_record["channels"].append("slack")

        # 钉钉通知
        if self.config.dingtalk_webhook:
            await self._send_to_dingtalk(alert, self.config.dingtalk_webhook)
            notification_record["channels"].append("dingtalk")

        self._notification_history.append(notification_record)

        # 保留最近 1000 条记录
        if len(self._notification_history) > 1000:
            self._notification_history = self._notification_history[-1000:]

        logger.info(f"Alert notification sent: {alert.get('id')} via {notification_record['channels']}")

    async def _send_via_websocket(self, alert: Dict[str, Any]):
        """通过 WebSocket 推送"""
        for handler in self._websocket_handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(alert)
                else:
                    handler(alert)
            except Exception as e:
                logger.error(f"WebSocket notification error: {e}")

    async def _send_via_webhook(self, alert: Dict[str, Any], webhook_url: str):
        """通过 Webhook 发送"""
        try:
            payload = {
                "alert_id": alert.get("id"),
                "rule_name": alert.get("rule_name"),
                "severity": alert.get("severity"),
                "state": alert.get("state"),
                "message": alert.get("message"),
                "labels": alert.get("labels", {}),
                "starts_at": alert.get("starts_at"),
                "timestamp": _utc_now_iso(),
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    webhook_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status >= 400:
                        logger.warning(f"Webhook notification failed: {response.status}")

        except asyncio.TimeoutError:
            logger.error(f"Webhook timeout: {webhook_url}")
        except Exception as e:
            logger.error(f"Webhook notification error: {e}")

    async def _send_to_slack(self, alert: Dict[str, Any], webhook_url: str):
        """发送到 Slack"""
        try:
            severity_colors = {
                "critical": "#FF0000",
                "high": "#FF6600",
                "medium": "#FFCC00",
                "low": "#36A64F",
                "info": "#808080",
            }

            color = severity_colors.get(alert.get("severity", "info"), "#808080")

            payload = {
                "attachments": [
                    {
                        "color": color,
                        "title": f"告警: {alert.get('rule_name', 'Unknown')}",
                        "fields": [
                            {"title": "严重级别", "value": alert.get("severity", "unknown"), "short": True},
                            {"title": "状态", "value": alert.get("state", "unknown"), "short": True},
                            {"title": "服务", "value": alert.get("labels", {}).get("service", "unknown"), "short": True},
                            {"title": "时间", "value": alert.get("starts_at", "unknown"), "short": True},
                            {"title": "消息", "value": alert.get("message", ""), "short": False},
                        ],
                        "footer": "Logoscope Alert",
                        "ts": int(datetime.now(timezone.utc).timestamp()),
                    }
                ]
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    webhook_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status >= 400:
                        logger.warning(f"Slack notification failed: {response.status}")

        except Exception as e:
            logger.error(f"Slack notification error: {e}")

    async def _send_to_dingtalk(self, alert: Dict[str, Any], webhook_url: str):
        """发送到钉钉"""
        try:
            severity_emojis = {
                "critical": "🔴",
                "high": "🟠",
                "medium": "🟡",
                "low": "🟢",
                "info": "⚪",
            }

            emoji = severity_emojis.get(alert.get("severity", "info"), "⚪")

            content = f"""{emoji} **告警通知**
**规则**: {alert.get('rule_name', 'Unknown')}
**级别**: {alert.get('severity', 'unknown')}
**状态**: {alert.get('state', 'unknown')}
**服务**: {alert.get('labels', {}).get('service', 'unknown')}
**时间**: {alert.get('starts_at', 'unknown')}
**消息**: {alert.get('message', '')}
"""

            payload = {
                "msgtype": "markdown",
                "markdown": {
                    "title": f"告警: {alert.get('rule_name', 'Unknown')}",
                    "text": content,
                }
            }

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    webhook_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status >= 400:
                        logger.warning(f"DingTalk notification failed: {response.status}")

        except Exception as e:
            logger.error(f"DingTalk notification error: {e}")

    def get_notification_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        """获取通知历史"""
        return self._notification_history[-limit:]


_notification_service: Optional[NotificationService] = None


def get_notification_service(config: NotificationConfig = None) -> NotificationService:
    """获取通知服务实例"""
    global _notification_service
    if _notification_service is None:
        _notification_service = NotificationService(config)
    return _notification_service


async def setup_notification_handlers(alert_manager, notification_service: NotificationService):
    """设置告警通知处理器"""
    async def on_alert(alert):
        await notification_service.send_notification(alert.to_dict())

    alert_manager.register_notification_handler(on_alert)
