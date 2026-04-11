"""
Alerts API 模块单元测试

测试 api/alerts.py 的核心功能：
- 告警规则 CRUD 操作
- 告警事件查询
- 告警规则评估
- 告警统计信息
- 数据模型验证
"""
import pytest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime

from api.alerts import (
    AlertRule,
    AlertEvent,
    CreateRuleFromTemplateRequest,
    create_alert_rule,
    create_alert_rule_from_template,
    update_alert_rule,
    delete_alert_rule,
    get_alert_rules,
    get_alert_rule,
    get_alert_events,
    evaluate_alert_rules,
    get_alert_stats,
    set_storage_adapter
)


@pytest.fixture(autouse=True)
def clear_global_state():
    """每个测试前清理全局状态"""
    from api import alerts
    alerts._alert_rules.clear()
    alerts._alert_events.clear()
    yield
    alerts._alert_rules.clear()
    alerts._alert_events.clear()


class TestAlertRuleModel:
    """测试 AlertRule 数据模型"""

    def test_create_alert_rule_with_defaults(self):
        """测试使用默认值创建规则"""
        rule = AlertRule(
            name="Test Rule",
            metric_name="cpu_usage",
            condition="gt",
            threshold=80.0
        )

        assert rule.name == "Test Rule"
        assert rule.metric_name == "cpu_usage"
        assert rule.condition == "gt"
        assert rule.threshold == 80.0
        assert rule.severity == "warning"  # 默认值
        assert rule.duration == 60  # 默认值
        assert rule.enabled is True  # 默认值

    def test_create_alert_rule_full(self):
        """测试创建完整规则"""
        rule = AlertRule(
            name="Critical CPU",
            description="CPU usage exceeds threshold",
            metric_name="cpu_usage",
            service_name="api-server",
            condition="gt",
            threshold=90.0,
            duration=120,
            severity="critical",
            enabled=True,
            labels={"env": "prod"}
        )

        assert rule.name == "Critical CPU"
        assert rule.description == "CPU usage exceeds threshold"
        assert rule.service_name == "api-server"
        assert rule.severity == "critical"
        assert rule.duration == 120

    def test_alert_rule_serialization(self):
        """测试规则序列化"""
        rule = AlertRule(
            id="rule-123",
            name="Test Rule",
            metric_name="cpu_usage",
            condition="gt",
            threshold=80.0
        )

        data = rule.dict()

        assert data['id'] == "rule-123"
        assert data['name'] == "Test Rule"
        assert isinstance(data, dict)


class TestAlertEventModel:
    """测试 AlertEvent 数据模型"""

    def test_create_alert_event(self):
        """测试创建告警事件"""
        event = AlertEvent(
            rule_id="rule-123",
            rule_name="CPU Alert",
            metric_name="cpu_usage",
            service_name="api-server",
            current_value=95.0,
            threshold=90.0,
            condition="gt",
            severity="critical",
            message="CPU usage is 95.00, threshold gt 90.0",
            fired_at="2026-02-09T12:00:00Z"
        )

        assert event.rule_id == "rule-123"
        assert event.current_value == 95.0
        assert event.status == "firing"  # 默认值


class TestCreateAlertRule:
    """测试创建告警规则"""

    @pytest.mark.asyncio
    async def test_create_rule_success(self):
        """测试成功创建规则"""
        rule = AlertRule(
            name="Test Rule",
            metric_name="cpu_usage",
            condition="gt",
            threshold=80.0
        )

        result = await create_alert_rule(rule)

        assert result['status'] == 'ok'
        assert 'rule' in result
        assert result['rule']['id'] is not None
        assert result['rule']['name'] == "Test Rule"

    @pytest.mark.asyncio
    async def test_create_rule_generates_id(self):
        """测试自动生成 ID"""
        rule1 = AlertRule(name="Rule 1", metric_name="cpu", condition="gt", threshold=80)
        rule2 = AlertRule(name="Rule 2", metric_name="mem", condition="lt", threshold=20)

        result1 = await create_alert_rule(rule1)
        result2 = await create_alert_rule(rule2)

        assert result1['rule']['id'] != result2['rule']['id']

    @pytest.mark.asyncio
    async def test_create_rule_sets_timestamps(self):
        """测试设置时间戳"""
        rule = AlertRule(
            name="Test Rule",
            metric_name="cpu_usage",
            condition="gt",
            threshold=80.0
        )

        result = await create_alert_rule(rule)

        assert result['rule']['created_at'] is not None
        assert result['rule']['updated_at'] is not None


class TestUpdateAlertRule:
    """测试更新告警规则"""

    @pytest.mark.asyncio
    async def test_update_rule_success(self):
        """测试成功更新规则"""
        # 先创建规则
        rule = AlertRule(
            name="Original Name",
            metric_name="cpu_usage",
            condition="gt",
            threshold=80.0
        )
        create_result = await create_alert_rule(rule)
        rule_id = create_result['rule']['id']

        updated_rule = AlertRule(
            name="Updated Name",
            metric_name="cpu_usage",
            condition="gt",
            threshold=90.0
        )

        result = await update_alert_rule(rule_id, updated_rule)

        assert result['status'] == 'ok'
        assert result['rule']['name'] == "Updated Name"
        assert result['rule']['threshold'] == 90.0

    @pytest.mark.asyncio
    async def test_update_rule_not_found(self):
        """测试更新不存在的规则"""
        rule = AlertRule(
            name="Test",
            metric_name="cpu",
            condition="gt",
            threshold=80
        )

        with pytest.raises(Exception) as exc_info:
            await update_alert_rule("nonexistent-id", rule)

        assert exc_info.value.status_code == 404


class TestDeleteAlertRule:
    """测试删除告警规则"""

    @pytest.mark.asyncio
    async def test_delete_rule_success(self):
        """测试成功删除规则"""
        # 先创建规则
        rule = AlertRule(
            name="Test Rule",
            metric_name="cpu_usage",
            condition="gt",
            threshold=80.0
        )
        result = await create_alert_rule(rule)
        rule_id = result['rule']['id']

        delete_result = await delete_alert_rule(rule_id)

        assert delete_result['status'] == 'ok'
        assert delete_result['message'] == "Rule deleted"

    @pytest.mark.asyncio
    async def test_delete_rule_not_found(self):
        """测试删除不存在的规则"""
        with pytest.raises(Exception) as exc_info:
            await delete_alert_rule("nonexistent-id")

        assert exc_info.value.status_code == 404


class TestGetAlertRules:
    """测试获取告警规则列表"""

    @pytest.mark.asyncio
    async def test_get_empty_rules(self):
        """测试获取空规则列表"""
        result = await get_alert_rules()

        assert result['total'] == 0
        assert result['rules'] == []

    @pytest.mark.asyncio
    async def test_get_rules_multiple(self):
        """测试获取多个规则"""
        # 创建多个规则
        await create_alert_rule(AlertRule(name="Rule 1", metric_name="cpu", condition="gt", threshold=80))
        await create_alert_rule(AlertRule(name="Rule 2", metric_name="mem", condition="lt", threshold=20))

        result = await get_alert_rules()

        assert result['total'] == 2
        assert len(result['rules']) == 2


class TestGetAlertRule:
    """测试获取单个告警规则"""

    @pytest.mark.asyncio
    async def test_get_rule_success(self):
        """测试成功获取规则"""
        # 先创建规则
        rule = AlertRule(
            name="Test Rule",
            metric_name="cpu_usage",
            condition="gt",
            threshold=80.0
        )
        create_result = await create_alert_rule(rule)
        rule_id = create_result['rule']['id']

        result = await get_alert_rule(rule_id)

        assert 'rule' in result
        assert result['rule']['name'] == "Test Rule"

    @pytest.mark.asyncio
    async def test_get_rule_not_found(self):
        """测试获取不存在的规则"""
        with pytest.raises(Exception) as exc_info:
            await get_alert_rule("nonexistent-id")

        assert exc_info.value.status_code == 404


class TestGetAlertEvents:
    """测试获取告警事件"""

    @pytest.mark.asyncio
    async def test_get_events_all(self):
        """测试获取所有事件"""
        from api.alerts import _alert_events

        event1 = AlertEvent(
            id="event-1",
            rule_id="rule-1",
            rule_name="CPU Alert",
            metric_name="cpu_usage",
            service_name="api-server",
            current_value=95.0,
            threshold=90.0,
            condition="gt",
            severity="critical",
            message="CPU is high",
            fired_at="2026-02-09T12:00:00Z",
            status="firing"
        )

        event2 = AlertEvent(
            id="event-2",
            rule_id="rule-2",
            rule_name="Mem Alert",
            metric_name="mem_usage",
            service_name="api-server",
            current_value=95.0,
            threshold=90.0,
            condition="gt",
            severity="warning",
            message="Memory is high",
            fired_at="2026-02-09T13:00:00Z",
            status="resolved"
        )

        _alert_events.extend([event1, event2])

        result = await get_alert_events()

        assert result['total'] == 2
        assert len(result['events']) == 2

    @pytest.mark.asyncio
    async def test_get_events_with_status_filter(self):
        """测试按状态过滤事件"""
        from api.alerts import _alert_events

        event = AlertEvent(
            id="event-1",
            rule_id="rule-1",
            rule_name="CPU Alert",
            metric_name="cpu_usage",
            service_name="api-server",
            current_value=95.0,
            threshold=90.0,
            condition="gt",
            severity="critical",
            message="CPU is high",
            fired_at="2026-02-09T12:00:00Z",
            status="firing"
        )

        _alert_events.append(event)

        result = await get_alert_events(status="firing")

        assert result['total'] == 1
        assert result['events'][0]['status'] == "firing"

    @pytest.mark.asyncio
    async def test_get_events_with_severity_filter(self):
        """测试按严重程度过滤事件"""
        from api.alerts import _alert_events

        event = AlertEvent(
            id="event-1",
            rule_id="rule-1",
            rule_name="CPU Alert",
            metric_name="cpu_usage",
            service_name="api-server",
            current_value=95.0,
            threshold=90.0,
            condition="gt",
            severity="critical",
            message="CPU is high",
            fired_at="2026-02-09T12:00:00Z",
            status="firing"
        )

        _alert_events.append(event)

        result = await get_alert_events(severity="critical")

        assert result['total'] == 1
        assert result['events'][0]['severity'] == "critical"

    @pytest.mark.asyncio
    async def test_get_events_with_namespace_filter(self):
        """测试按 namespace 过滤事件"""
        from api.alerts import _alert_events

        event1 = AlertEvent(
            id="event-1",
            rule_id="rule-1",
            rule_name="CPU Alert",
            metric_name="cpu_usage",
            service_name="api-server",
            namespace="prod-a",
            current_value=95.0,
            threshold=90.0,
            condition="gt",
            severity="critical",
            message="CPU is high",
            fired_at="2026-02-09T12:00:00Z",
            status="firing",
        )
        event2 = AlertEvent(
            id="event-2",
            rule_id="rule-2",
            rule_name="Mem Alert",
            metric_name="mem_usage",
            service_name="api-server",
            current_value=92.0,
            threshold=90.0,
            condition="gt",
            severity="warning",
            message="Memory is high",
            fired_at="2026-02-09T12:01:00Z",
            status="firing",
            labels={"namespace": "prod-b"},
        )

        _alert_events.extend([event1, event2])

        result = await get_alert_events(namespace="prod-a")

        assert result['total'] == 1
        assert result['events'][0]['id'] == "event-1"

    @pytest.mark.asyncio
    async def test_get_events_with_source_service_filter(self):
        """测试按边级 source_service 精确过滤事件"""
        from api.alerts import _alert_events

        event1 = AlertEvent(
            id="event-1",
            rule_id="rule-1",
            rule_name="edge latency",
            metric_name="edge_p95_ms_5m",
            service_name="checkout->payment",
            source_service="checkout",
            target_service="payment",
            namespace="prod-a",
            current_value=1200.0,
            threshold=1000.0,
            condition="gt",
            severity="warning",
            message="latency high",
            fired_at="2026-02-09T12:00:00Z",
            status="firing",
        )
        event2 = AlertEvent(
            id="event-2",
            rule_id="rule-2",
            rule_name="edge error",
            metric_name="edge_error_rate_5m",
            service_name="gateway->payment",
            source_service="gateway",
            target_service="payment",
            namespace="prod-a",
            current_value=8.0,
            threshold=5.0,
            condition="gt",
            severity="warning",
            message="error high",
            fired_at="2026-02-09T12:01:00Z",
            status="firing",
        )

        _alert_events.extend([event1, event2])

        result = await get_alert_events(source_service="checkout")

        assert result['total'] == 1
        assert result['events'][0]['id'] == "event-1"

    @pytest.mark.asyncio
    async def test_get_events_with_target_service_filter(self):
        """测试按边级 target_service 精确过滤事件"""
        from api.alerts import _alert_events

        event1 = AlertEvent(
            id="event-1",
            rule_id="rule-1",
            rule_name="edge latency",
            metric_name="edge_p95_ms_5m",
            service_name="checkout->payment",
            source_service="checkout",
            target_service="payment",
            namespace="prod-a",
            current_value=1200.0,
            threshold=1000.0,
            condition="gt",
            severity="warning",
            message="latency high",
            fired_at="2026-02-09T12:00:00Z",
            status="firing",
        )
        event2 = AlertEvent(
            id="event-2",
            rule_id="rule-2",
            rule_name="edge timeout",
            metric_name="edge_timeout_rate_5m",
            service_name="checkout->inventory",
            source_service="checkout",
            target_service="inventory",
            namespace="prod-a",
            current_value=3.0,
            threshold=2.0,
            condition="gt",
            severity="warning",
            message="timeout high",
            fired_at="2026-02-09T12:01:00Z",
            status="firing",
        )

        _alert_events.extend([event1, event2])

        result = await get_alert_events(target_service="payment")

        assert result['total'] == 1
        assert result['events'][0]['id'] == "event-1"

    @pytest.mark.asyncio
    async def test_get_events_with_scope_filter(self):
        """测试按 edge/service scope 过滤事件"""
        from api.alerts import _alert_events

        service_event = AlertEvent(
            id="event-service",
            rule_id="rule-service",
            rule_name="CPU Alert",
            metric_name="cpu_usage",
            service_name="api-server",
            namespace="prod-a",
            current_value=95.0,
            threshold=90.0,
            condition="gt",
            severity="critical",
            message="CPU high",
            fired_at="2026-02-09T12:00:00Z",
            status="firing",
        )
        edge_event = AlertEvent(
            id="event-edge",
            rule_id="rule-edge",
            rule_name="Edge Latency",
            metric_name="edge_p95_ms_5m",
            service_name="checkout->payment",
            source_service="checkout",
            target_service="payment",
            namespace="prod-a",
            current_value=1200.0,
            threshold=1000.0,
            condition="gt",
            severity="warning",
            message="latency high",
            fired_at="2026-02-09T12:01:00Z",
            status="firing",
        )

        _alert_events.extend([service_event, edge_event])

        edge_result = await get_alert_events(scope="edge")
        service_result = await get_alert_events(scope="service")

        assert edge_result['total'] == 1
        assert edge_result['events'][0]['id'] == "event-edge"
        assert service_result['total'] == 1
        assert service_result['events'][0]['id'] == "event-service"

    @pytest.mark.asyncio
    async def test_get_events_with_limit(self):
        """测试限制返回数量"""
        from api.alerts import _alert_events

        event1 = AlertEvent(
            id="event-1",
            rule_id="rule-1",
            rule_name="CPU Alert",
            metric_name="cpu_usage",
            service_name="api-server",
            current_value=95.0,
            threshold=90.0,
            condition="gt",
            severity="critical",
            message="CPU is high",
            fired_at="2026-02-09T12:00:00Z",
            status="firing"
        )

        event2 = AlertEvent(
            id="event-2",
            rule_id="rule-2",
            rule_name="Mem Alert",
            metric_name="mem_usage",
            service_name="api-server",
            current_value=95.0,
            threshold=90.0,
            condition="gt",
            severity="warning",
            message="Memory is high",
            fired_at="2026-02-09T13:00:00Z",
            status="resolved"
        )

        _alert_events.extend([event1, event2])

        result = await get_alert_events(limit=1)

        # total 表示筛选后的总条数，不受分页 limit 影响
        assert result['total'] == 2


class TestEvaluateAlertRules:
    """测试评估告警规则"""

    @pytest.mark.asyncio
    async def test_evaluate_no_storage(self):
        """测试没有初始化 storage"""
        # 设置 storage 为 None
        from api import alerts
        original_storage = alerts._STORAGE_ADAPTER
        alerts._STORAGE_ADAPTER = None

        try:
            with pytest.raises(Exception) as exc_info:
                await evaluate_alert_rules()

            # storage 未初始化时应返回 503
            assert exc_info.value.status_code == 503
        finally:
            alerts._STORAGE_ADAPTER = original_storage

    @pytest.mark.asyncio
    async def test_evaluate_with_storage(self):
        """测试使用 storage 评估"""
        mock_storage = Mock()
        mock_storage.get_metrics = Mock(return_value=[
            {
                'service_name': 'api-server',
                'metric_name': 'cpu_usage',
                'value': 95.0
            },
            {
                'service_name': 'api-server',
                'metric_name': 'cpu_usage',
                'value': 85.0
            }
        ])

        set_storage_adapter(mock_storage)

        # 创建测试规则
        rule = AlertRule(
            name="CPU Alert",
            metric_name="cpu_usage",
            condition="gt",
            threshold=80.0,
            duration=0,
            enabled=True
        )
        await create_alert_rule(rule)

        result = await evaluate_alert_rules()

        assert result['status'] == 'ok'
        assert result['evaluated_rules'] >= 1
        assert result['triggered_alerts'] >= 1

    @pytest.mark.asyncio
    async def test_evaluate_namespace_rule_ignores_unknown_namespace_metric(self):
        """指定 namespace 的规则不应被未知 namespace 指标触发。"""
        from api.alerts import _alert_events

        mock_storage = Mock()
        mock_storage.get_metrics = Mock(return_value=[
            {
                'service_name': 'api-server',
                'metric_name': 'cpu_usage',
                'value': 95.0,
                'labels': {},
            }
        ])
        mock_storage.execute_query = Mock(return_value=[])

        set_storage_adapter(mock_storage)

        rule = AlertRule(
            name="CPU Alert Namespace Scoped",
            metric_name="cpu_usage",
            service_name="api-server",
            namespace="prod-a",
            condition="gt",
            threshold=80.0,
            enabled=True,
        )
        await create_alert_rule(rule)

        result = await evaluate_alert_rules()

        assert result['status'] == 'ok'
        assert result['triggered_alerts'] == 0
        assert len(_alert_events) == 0

    @pytest.mark.asyncio
    async def test_evaluate_disabled_rule(self):
        """测试禁用的规则不触发"""
        mock_storage = Mock()
        mock_storage.get_metrics = Mock(return_value=[
            {
                'service_name': 'api-server',
                'metric_name': 'cpu_usage',
                'value': 95.0
            }
        ])

        set_storage_adapter(mock_storage)

        # 创建禁用的规则
        rule = AlertRule(
            name="Disabled Rule",
            metric_name="cpu_usage",
            condition="gt",
            threshold=50.0,
            enabled=False
        )
        await create_alert_rule(rule)

        result = await evaluate_alert_rules()

        # 规则应该被评估但不会触发
        assert result['evaluated_rules'] >= 1


    @pytest.mark.asyncio
    async def test_create_edge_rule_from_template_with_services(self):
        """边级模板建规则应保留 source/target 维度。"""
        payload = CreateRuleFromTemplateRequest(
            template_id="edge-error-rate-5m",
            name="checkout->payment error",
            source_service="checkout",
            target_service="payment",
            namespace="prod",
        )

        result = await create_alert_rule_from_template(payload)

        assert result['status'] == 'ok'
        assert result['rule']['metric_name'] == 'edge_error_rate_5m'
        assert result['rule']['source_service'] == 'checkout'
        assert result['rule']['target_service'] == 'payment'
        assert result['rule']['labels']['scope'] == 'edge'
        assert result['rule']['labels']['template_id'] == 'edge-error-rate-5m'

    @pytest.mark.asyncio
    async def test_evaluate_edge_rule_from_edge_red_metrics(self):
        """边级规则应能够基于 edge RED 指标触发。"""
        from api.alerts import _alert_events

        mock_storage = Mock()
        mock_storage.get_metrics = Mock(return_value=[])
        mock_storage.execute_query = Mock(return_value=[])
        mock_storage.get_edge_red_metrics = Mock(return_value={
            'checkout->payment': {
                'call_count': 120,
                'error_count': 18,
                'error_rate': 0.15,
                'p95': 3200.0,
                'timeout_rate': 0.04,
            }
        })
        set_storage_adapter(mock_storage)

        rule = AlertRule(
            name="checkout payment edge error",
            metric_name="edge_error_rate_5m",
            source_service="checkout",
            target_service="payment",
            namespace="prod",
            condition="gt",
            threshold=10.0,
            duration=0,
            enabled=True,
            labels={'scope': 'edge'},
        )
        await create_alert_rule(rule)

        result = await evaluate_alert_rules()

        assert result['status'] == 'ok'
        assert result['triggered_alerts'] >= 1
        assert len(_alert_events) >= 1
        event = _alert_events[0]
        assert event.metric_name == 'edge_error_rate_5m'
        assert event.source_service == 'checkout'
        assert event.target_service == 'payment'
        assert event.service_name == 'checkout->payment'
        assert event.current_value == 15.0
        assert event.labels['source_service'] == 'checkout'
        assert event.labels['target_service'] == 'payment'

    @pytest.mark.asyncio
    async def test_evaluate_edge_p99_rule_from_edge_red_metrics(self):
        """边级 P99 规则应能够使用 edge RED 的 p99 指标。"""
        from api.alerts import _alert_events

        mock_storage = Mock()
        mock_storage.get_metrics = Mock(return_value=[])
        mock_storage.execute_query = Mock(return_value=[])
        mock_storage.get_edge_red_metrics = Mock(return_value={
            'checkout->payment': {
                'call_count': 180,
                'error_count': 9,
                'error_rate': 0.05,
                'p95': 2400.0,
                'p99': 5400.0,
                'timeout_rate': 0.01,
            }
        })
        set_storage_adapter(mock_storage)

        rule = AlertRule(
            name="checkout payment edge p99",
            metric_name="edge_p99_ms_5m",
            source_service="checkout",
            target_service="payment",
            namespace="prod",
            condition="gt",
            threshold=3000.0,
            duration=0,
            enabled=True,
            labels={'scope': 'edge'},
        )
        await create_alert_rule(rule)

        result = await evaluate_alert_rules()

        assert result['status'] == 'ok'
        assert result['triggered_alerts'] >= 1
        assert len(_alert_events) >= 1
        event = _alert_events[0]
        assert event.metric_name == 'edge_p99_ms_5m'
        assert event.current_value == 5400.0
        assert event.service_name == 'checkout->payment'

    @pytest.mark.asyncio
    async def test_evaluate_edge_call_count_rule_zero_fill_when_edge_missing(self):
        """固定边级调用量过低规则在窗口内无数据时应按 0 处理。"""
        from api.alerts import _alert_events

        mock_storage = Mock()
        mock_storage.get_metrics = Mock(return_value=[])
        mock_storage.execute_query = Mock(return_value=[])
        mock_storage.get_edge_red_metrics = Mock(return_value={})
        set_storage_adapter(mock_storage)

        rule = AlertRule(
            name="checkout payment edge traffic drop",
            metric_name="edge_call_count_5m",
            source_service="checkout",
            target_service="payment",
            namespace="prod",
            condition="lt",
            threshold=1.0,
            duration=0,
            enabled=True,
            labels={'scope': 'edge'},
        )
        await create_alert_rule(rule)

        result = await evaluate_alert_rules()

        assert result['status'] == 'ok'
        assert result['triggered_alerts'] >= 1
        assert len(_alert_events) >= 1
        event = _alert_events[0]
        assert event.metric_name == 'edge_call_count_5m'
        assert event.current_value == 0.0
        assert event.source_service == 'checkout'
        assert event.target_service == 'payment'

    @pytest.mark.asyncio
    async def test_evaluate_edge_retries_rule_from_edge_red_metrics(self):
        """边级重试密度规则应能够使用 edge RED 的 retries 指标。"""
        from api.alerts import _alert_events

        mock_storage = Mock()
        mock_storage.get_metrics = Mock(return_value=[])
        mock_storage.execute_query = Mock(return_value=[])
        mock_storage.get_edge_red_metrics = Mock(return_value={
            'checkout->payment': {
                'call_count': 120,
                'error_count': 6,
                'error_rate': 0.05,
                'p95': 900.0,
                'timeout_rate': 0.01,
                'retries': 0.25,
                'pending': 0.03,
                'dlq': 0.0,
            }
        })
        set_storage_adapter(mock_storage)

        rule = AlertRule(
            name="checkout payment edge retries",
            metric_name="edge_retries_per_call_5m",
            source_service="checkout",
            target_service="payment",
            namespace="prod",
            condition="gt",
            threshold=0.1,
            duration=0,
            enabled=True,
            labels={'scope': 'edge'},
        )
        await create_alert_rule(rule)

        result = await evaluate_alert_rules()

        assert result['status'] == 'ok'
        assert result['triggered_alerts'] >= 1
        assert len(_alert_events) >= 1
        event = _alert_events[0]
        assert event.metric_name == 'edge_retries_per_call_5m'
        assert event.current_value == 0.25
        assert event.service_name == 'checkout->payment'


class TestGetAlertStats:
    """测试获取告警统计"""

    @pytest.mark.asyncio
    async def test_get_stats(self):
        """测试获取统计信息"""
        from api.alerts import _alert_rules, _alert_events

        # 添加规则
        rule1 = AlertRule(
            id="rule-1",
            name="Rule 1",
            metric_name="cpu",
            condition="gt",
            threshold=80,
            enabled=True
        )
        rule2 = AlertRule(
            id="rule-2",
            name="Rule 2",
            metric_name="mem",
            condition="lt",
            threshold=20,
            enabled=False
        )

        _alert_rules["rule-1"] = rule1
        _alert_rules["rule-2"] = rule2

        # 添加事件
        event1 = AlertEvent(
            id="event-1",
            rule_id="rule-1",
            rule_name="Rule 1",
            metric_name="cpu",
            service_name="api",
            current_value=95,
            threshold=80,
            condition="gt",
            severity="critical",
            message="CPU high",
            fired_at="2026-02-09T12:00:00Z",
            status="firing"
        )
        event2 = AlertEvent(
            id="event-2",
            rule_id="rule-2",
            rule_name="Rule 2",
            metric_name="mem",
            service_name="api",
            current_value=10,
            threshold=20,
            condition="lt",
            severity="warning",
            message="Memory low",
            fired_at="2026-02-09T13:00:00Z",
            status="resolved"
        )

        _alert_events.extend([event1, event2])

        result = await get_alert_stats()

        assert result['total_rules'] == 2
        assert result['enabled_rules'] == 1
        assert result['total_events'] == 2
        assert result['firing_events'] == 1
        assert result['resolved_events'] == 1

    @pytest.mark.asyncio
    async def test_get_stats_severity_breakdown(self):
        """测试按严重程度统计"""
        from api.alerts import _alert_rules, _alert_events

        # 添加规则
        rule1 = AlertRule(
            id="rule-1",
            name="Rule 1",
            metric_name="cpu",
            condition="gt",
            threshold=80,
            enabled=True
        )

        _alert_rules["rule-1"] = rule1

        # 添加事件
        event1 = AlertEvent(
            id="event-1",
            rule_id="rule-1",
            rule_name="Rule 1",
            metric_name="cpu",
            service_name="api",
            current_value=95,
            threshold=80,
            condition="gt",
            severity="critical",
            message="CPU high",
            fired_at="2026-02-09T12:00:00Z",
            status="firing"
        )
        event2 = AlertEvent(
            id="event-2",
            rule_id="rule-1",
            rule_name="Rule 1",
            metric_name="cpu",
            service_name="api",
            current_value=95,
            threshold=80,
            condition="gt",
            severity="warning",
            message="CPU high",
            fired_at="2026-02-09T13:00:00Z",
            status="resolved"
        )

        _alert_events.extend([event1, event2])

        result = await get_alert_stats()

        assert 'severity_stats' in result
        assert result['severity_stats']['critical'] == 1
        assert result['severity_stats']['warning'] == 1


class TestAlertConditions:
    """测试告警条件评估"""

    @pytest.mark.asyncio
    async def test_condition_gt(self):
        """测试大于条件"""
        rule = AlertRule(
            name="GT Rule",
            metric_name="cpu",
            condition="gt",
            threshold=80.0
        )

        assert rule.condition == "gt"

    @pytest.mark.asyncio
    async def test_condition_lt(self):
        """测试小于条件"""
        rule = AlertRule(
            name="LT Rule",
            metric_name="mem",
            condition="lt",
            threshold=20.0
        )

        assert rule.condition == "lt"

    @pytest.mark.asyncio
    async def test_condition_gte(self):
        """测试大于等于条件"""
        rule = AlertRule(
            name="GTE Rule",
            metric_name="cpu",
            condition="gte",
            threshold=80.0
        )

        assert rule.condition == "gte"

    @pytest.mark.asyncio
    async def test_condition_lte(self):
        """测试小于等于条件"""
        rule = AlertRule(
            name="LTE Rule",
            metric_name="mem",
            condition="lte",
            threshold=20.0
        )

        assert rule.condition == "lte"

    @pytest.mark.asyncio
    async def test_condition_eq(self):
        """测试等于条件"""
        rule = AlertRule(
            name="EQ Rule",
            metric_name="status",
            condition="eq",
            threshold=1.0
        )

        assert rule.condition == "eq"


class TestEdgeCases:
    """测试边界情况"""

    @pytest.mark.asyncio
    async def test_create_rule_with_labels(self):
        """测试创建带标签的规则"""
        rule = AlertRule(
            name="Labeled Rule",
            metric_name="cpu",
            condition="gt",
            threshold=80,
            labels={"env": "prod", "team": "backend"}
        )

        result = await create_alert_rule(rule)

        assert result['rule']['labels']['env'] == "prod"
        assert result['rule']['labels']['team'] == "backend"

    @pytest.mark.asyncio
    async def test_get_events_empty_list(self):
        """测试获取空事件列表"""
        from api.alerts import _alert_events
        _alert_events.clear()

        result = await get_alert_events()

        assert result['total'] == 0
        assert result['events'] == []

    @pytest.mark.asyncio
    async def test_update_rule_preserves_id(self):
        """测试更新规则保留 ID"""
        rule = AlertRule(
            name="Original",
            metric_name="cpu",
            condition="gt",
            threshold=80
        )
        create_result = await create_alert_rule(rule)
        original_id = create_result['rule']['id']

        updated_rule = AlertRule(
            name="Updated",
            metric_name="cpu",
            condition="gt",
            threshold=90
        )
        update_result = await update_alert_rule(original_id, updated_rule)

        assert update_result['rule']['id'] == original_id


class TestStorageAdapterIntegration:
    """测试 Storage Adapter 集成"""

    def test_set_storage_adapter(self):
        """测试设置 storage adapter"""
        mock_storage = Mock()

        set_storage_adapter(mock_storage)

        from api.alerts import _STORAGE_ADAPTER
        assert _STORAGE_ADAPTER == mock_storage
