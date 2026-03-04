"""
告警规则引擎

提供告警规则管理和评估功能：
- 规则定义和存储
- 规则评估引擎
- 告警状态管理
- 告警通知触发

Date: 2026-02-22
"""

import logging
import json
import re
from typing import Dict, Any, List, Optional, Callable
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from enum import Enum
import asyncio

logger = logging.getLogger(__name__)


class AlertSeverity(str, Enum):
    """告警严重级别"""
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class AlertState(str, Enum):
    """告警状态"""
    FIRING = "firing"
    RESOLVED = "resolved"
    PENDING = "pending"
    SILENCED = "silenced"


@dataclass
class AlertRule:
    """告警规则"""
    id: str
    name: str
    description: str
    severity: AlertSeverity
    enabled: bool = True
    condition: Dict[str, Any] = field(default_factory=dict)
    duration: int = 60  # 持续时间（秒）
    labels: Dict[str, str] = field(default_factory=dict)
    annotations: Dict[str, str] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "severity": self.severity.value,
            "enabled": self.enabled,
            "condition": self.condition,
            "duration": self.duration,
            "labels": self.labels,
            "annotations": self.annotations,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class Alert:
    """告警实例"""
    id: str
    rule_id: str
    rule_name: str
    severity: AlertSeverity
    state: AlertState
    labels: Dict[str, str] = field(default_factory=dict)
    annotations: Dict[str, str] = field(default_factory=dict)
    starts_at: str = ""
    ends_at: str = ""
    fingerprint: str = ""
    value: float = 0.0
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "severity": self.severity.value,
            "state": self.state.value,
            "labels": self.labels,
            "annotations": self.annotations,
            "starts_at": self.starts_at,
            "ends_at": self.ends_at,
            "fingerprint": self.fingerprint,
            "value": self.value,
            "message": self.message,
        }


class RuleEvaluator:
    """规则评估器"""

    @staticmethod
    def evaluate_log_rule(log: Dict[str, Any], condition: Dict[str, Any]) -> bool:
        """评估日志规则"""
        condition_type = condition.get("type", "simple")

        if condition_type == "simple":
            return RuleEvaluator._evaluate_simple(log, condition)
        elif condition_type == "pattern":
            return RuleEvaluator._evaluate_pattern(log, condition)
        elif condition_type == "threshold":
            return RuleEvaluator._evaluate_threshold(log, condition)
        elif condition_type == "compound":
            return RuleEvaluator._evaluate_compound(log, condition)

        return False

    @staticmethod
    def _evaluate_simple(log: Dict[str, Any], condition: Dict[str, Any]) -> bool:
        """简单条件评估"""
        field = condition.get("field", "level")
        operator = condition.get("operator", "eq")
        value = condition.get("value")

        log_value = log.get(field)

        if operator == "eq":
            return log_value == value
        elif operator == "neq":
            return log_value != value
        elif operator == "contains":
            return value in str(log_value) if log_value else False
        elif operator == "regex":
            return bool(re.search(value, str(log_value))) if log_value else False
        elif operator == "gt":
            try:
                return float(log_value) > float(value)
            except (ValueError, TypeError):
                return False
        elif operator == "lt":
            try:
                return float(log_value) < float(value)
            except (ValueError, TypeError):
                return False

        return False

    @staticmethod
    def _evaluate_pattern(log: Dict[str, Any], condition: Dict[str, Any]) -> bool:
        """模式匹配评估"""
        pattern = condition.get("pattern", "")
        fields = condition.get("fields", ["message"])

        for field in fields:
            value = str(log.get(field, ""))
            if re.search(pattern, value, re.IGNORECASE):
                return True

        return False

    @staticmethod
    def _evaluate_threshold(log: Dict[str, Any], condition: Dict[str, Any]) -> bool:
        """阈值评估"""
        metric_field = condition.get("metric_field")
        threshold = condition.get("threshold", 0)
        operator = condition.get("operator", "gt")

        try:
            value = float(log.get(metric_field, 0))
            if operator == "gt":
                return value > threshold
            elif operator == "lt":
                return value < threshold
            elif operator == "gte":
                return value >= threshold
            elif operator == "lte":
                return value <= threshold
        except (ValueError, TypeError):
            return False

        return False

    @staticmethod
    def _evaluate_compound(log: Dict[str, Any], condition: Dict[str, Any]) -> bool:
        """复合条件评估"""
        logic = condition.get("logic", "and")
        conditions = condition.get("conditions", [])

        if not conditions:
            return False

        results = []
        for cond in conditions:
            cond_type = cond.get("type", "simple")
            if cond_type == "simple":
                results.append(RuleEvaluator._evaluate_simple(log, cond))
            elif cond_type == "pattern":
                results.append(RuleEvaluator._evaluate_pattern(log, cond))
            elif cond_type == "threshold":
                results.append(RuleEvaluator._evaluate_threshold(log, cond))

        if logic == "and":
            return all(results)
        elif logic == "or":
            return any(results)

        return False


class AlertManager:
    """告警管理器"""

    def __init__(self, storage_adapter=None):
        self.storage = storage_adapter
        self._rules: Dict[str, AlertRule] = {}
        self._alerts: Dict[str, Alert] = {}
        self._pending_alerts: Dict[str, datetime] = {}
        self._evaluator = RuleEvaluator()
        self._notification_handlers: List[Callable] = []

        self._initialize_default_rules()

    def _initialize_default_rules(self):
        """初始化默认规则"""
        default_rules = [
            AlertRule(
                id="rule-error-rate",
                name="高错误率告警",
                description="服务错误率超过阈值时触发",
                severity=AlertSeverity.HIGH,
                condition={
                    "type": "simple",
                    "field": "level",
                    "operator": "eq",
                    "value": "error"
                },
                duration=60,
                labels={"category": "availability"},
                annotations={"summary": "服务出现错误日志"},
                created_at=datetime.now().isoformat(),
            ),
            AlertRule(
                id="rule-timeout",
                name="超时告警",
                description="检测到超时错误",
                severity=AlertSeverity.HIGH,
                condition={
                    "type": "pattern",
                    "pattern": "timeout|timed out",
                    "fields": ["message"]
                },
                duration=30,
                labels={"category": "performance"},
                annotations={"summary": "检测到超时问题"},
                created_at=datetime.now().isoformat(),
            ),
            AlertRule(
                id="rule-oom",
                name="内存溢出告警",
                description="检测到 OOM 错误",
                severity=AlertSeverity.CRITICAL,
                condition={
                    "type": "pattern",
                    "pattern": "out of memory|oom|heap.*overflow",
                    "fields": ["message"]
                },
                duration=0,
                labels={"category": "resource"},
                annotations={"summary": "检测到内存溢出"},
                created_at=datetime.now().isoformat(),
            ),
            AlertRule(
                id="rule-db-connection",
                name="数据库连接异常告警",
                description="数据库连接池问题",
                severity=AlertSeverity.HIGH,
                condition={
                    "type": "compound",
                    "logic": "or",
                    "conditions": [
                        {"type": "pattern", "pattern": "connection.*refused", "fields": ["message"]},
                        {"type": "pattern", "pattern": "pool.*exhausted", "fields": ["message"]},
                        {"type": "pattern", "pattern": "database.*error", "fields": ["message"]},
                    ]
                },
                duration=60,
                labels={"category": "database"},
                annotations={"summary": "数据库连接异常"},
                created_at=datetime.now().isoformat(),
            ),
            AlertRule(
                id="rule-auth-failure",
                name="认证失败告警",
                description="认证失败次数异常",
                severity=AlertSeverity.MEDIUM,
                condition={
                    "type": "compound",
                    "logic": "or",
                    "conditions": [
                        {"type": "pattern", "pattern": "authentication.*failed", "fields": ["message"]},
                        {"type": "pattern", "pattern": "unauthorized", "fields": ["message"]},
                        {"type": "pattern", "pattern": "token.*expired", "fields": ["message"]},
                    ]
                },
                duration=120,
                labels={"category": "security"},
                annotations={"summary": "认证失败"},
                created_at=datetime.now().isoformat(),
            ),
        ]

        for rule in default_rules:
            self._rules[rule.id] = rule

    def add_rule(self, rule: AlertRule):
        """添加规则"""
        self._rules[rule.id] = rule

    def get_rule(self, rule_id: str) -> Optional[AlertRule]:
        """获取规则"""
        return self._rules.get(rule_id)

    def get_all_rules(self) -> List[AlertRule]:
        """获取所有规则"""
        return list(self._rules.values())

    def delete_rule(self, rule_id: str):
        """删除规则"""
        self._rules.pop(rule_id, None)

    def register_notification_handler(self, handler: Callable):
        """注册通知处理器"""
        self._notification_handlers.append(handler)

    async def evaluate_log(self, log: Dict[str, Any]) -> List[Alert]:
        """评估日志并生成告警"""
        triggered_alerts = []

        for rule in self._rules.values():
            if not rule.enabled:
                continue

            if self._evaluator.evaluate_log_rule(log, rule.condition):
                alert = await self._create_or_update_alert(rule, log)
                if alert:
                    triggered_alerts.append(alert)

        return triggered_alerts

    async def _create_or_update_alert(self, rule: AlertRule, log: Dict[str, Any]) -> Optional[Alert]:
        """创建或更新告警"""
        import hashlib
        import uuid

        fingerprint = self._generate_fingerprint(rule, log)

        existing_alert = None
        for alert in self._alerts.values():
            if alert.fingerprint == fingerprint and alert.state == AlertState.FIRING:
                existing_alert = alert
                break

        if existing_alert:
            return existing_alert

        alert = Alert(
            id=f"alert-{uuid.uuid4().hex[:8]}",
            rule_id=rule.id,
            rule_name=rule.name,
            severity=rule.severity,
            state=AlertState.FIRING,
            labels={**rule.labels, "service": log.get("service_name", "unknown")},
            annotations=rule.annotations,
            starts_at=datetime.now().isoformat(),
            fingerprint=fingerprint,
            message=log.get("message", ""),
        )

        self._alerts[alert.id] = alert

        await self._notify_handlers(alert)

        return alert

    def _generate_fingerprint(self, rule: AlertRule, log: Dict[str, Any]) -> str:
        """生成告警指纹"""
        content = f"{rule.id}:{log.get('service_name', '')}:{log.get('level', '')}"
        return hashlib.md5(content.encode()).hexdigest()

    async def _notify_handlers(self, alert: Alert):
        """通知处理器"""
        for handler in self._notification_handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(alert)
                else:
                    handler(alert)
            except Exception as e:
                logger.error(f"Notification handler error: {e}")

    def get_active_alerts(self) -> List[Alert]:
        """获取活跃告警"""
        return [a for a in self._alerts.values() if a.state == AlertState.FIRING]

    def get_all_alerts(self, limit: int = 100) -> List[Alert]:
        """获取所有告警"""
        alerts = list(self._alerts.values())
        alerts.sort(key=lambda a: a.starts_at, reverse=True)
        return alerts[:limit]

    def resolve_alert(self, alert_id: str) -> Optional[Alert]:
        """解决告警"""
        alert = self._alerts.get(alert_id)
        if alert:
            alert.state = AlertState.RESOLVED
            alert.ends_at = datetime.now().isoformat()
        return alert

    def silence_alert(self, alert_id: str, duration_minutes: int = 60) -> Optional[Alert]:
        """静默告警"""
        alert = self._alerts.get(alert_id)
        if alert:
            alert.state = AlertState.SILENCED
        return alert

    def get_alert_stats(self) -> Dict[str, Any]:
        """获取告警统计"""
        alerts = list(self._alerts.values())

        return {
            "total": len(alerts),
            "firing": len([a for a in alerts if a.state == AlertState.FIRING]),
            "resolved": len([a for a in alerts if a.state == AlertState.RESOLVED]),
            "silenced": len([a for a in alerts if a.state == AlertState.SILENCED]),
            "by_severity": {
                "critical": len([a for a in alerts if a.severity == AlertSeverity.CRITICAL]),
                "high": len([a for a in alerts if a.severity == AlertSeverity.HIGH]),
                "medium": len([a for a in alerts if a.severity == AlertSeverity.MEDIUM]),
                "low": len([a for a in alerts if a.severity == AlertSeverity.LOW]),
            },
            "rules_count": len(self._rules),
            "rules_enabled": len([r for r in self._rules.values() if r.enabled]),
        }

    def cleanup_old_alerts(self, max_age_hours: int = 24, max_count: int = 1000) -> int:
        """
        清理旧告警，防止内存泄漏
        
        Args:
            max_age_hours: 保留的最大告警年龄（小时）
            max_count: 最大保留告警数量
            
        Returns:
            清理的告警数量
        """
        now = datetime.now()
        cutoff_time = now - timedelta(hours=max_age_hours)
        
        # 先按时间过滤
        to_remove = []
        for alert_id, alert in self._alerts.items():
            if alert.state == AlertState.RESOLVED:
                try:
                    alert_time = datetime.fromisoformat(alert.starts_at)
                    if alert_time < cutoff_time:
                        to_remove.append(alert_id)
                except (ValueError, TypeError):
                    to_remove.append(alert_id)
        
        # 如果告警数量仍然超过限制，删除最旧的
        remaining_count = len(self._alerts) - len(to_remove)
        if remaining_count > max_count:
            all_alerts = sorted(
                self._alerts.items(),
                key=lambda x: x[1].starts_at
            )
            for alert_id, _ in all_alerts[:remaining_count - max_count]:
                if alert_id not in to_remove:
                    to_remove.append(alert_id)
        
        # 执行删除
        for alert_id in to_remove:
            del self._alerts[alert_id]
        
        if to_remove:
            logger.info(f"Cleaned up {len(to_remove)} old alerts")
        
        return len(to_remove)


_alert_manager: Optional[AlertManager] = None


def get_alert_manager(storage_adapter=None) -> AlertManager:
    """获取告警管理器实例"""
    global _alert_manager
    if _alert_manager is None:
        _alert_manager = AlertManager(storage_adapter)
    return _alert_manager
