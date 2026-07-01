import pytest
from shared_src.expression.models import Expression
from shared_src.worldview.facade import WorldView


INSTANCE = "INSTANCE"
SERVICE = "SERVICE"


class MockState:
    def __init__(self):
        self._data = {
            ("INSTANCE", "vm-1"): "ACTIVE",
            ("INSTANCE", "vm-2"): "ERROR",
            ("SERVICE", "rabbitmq"): "running",
            ("HOST", "compute-01"): "alive",
        }

    def get_state(self, entity_type, entity_name):
        return self._data.get((entity_type, entity_name))

    def get_states(self, entities):
        return [self._data.get((t, n)) for t, n in entities]

    def get_timeline(self, entity_id, window="1 HOUR"):
        return []

    def has_state_changed(self, entity_id, window_minutes=5):
        return False

    def resolve_field(self, field_path, entity_type, entity_name):
        if field_path == "resource.status":
            return self.get_state(entity_type, entity_name)
        if field_path == "host.host_status":
            return self.get_state("HOST", entity_name)
        if field_path == "ssh.accessible":
            return True
        if field_path == "service.exists":
            return True
        if field_path == "service.tags":
            return ["production", "rabbitmq"]
        return None


class MockTopology:
    def get_dependents(self, t, n): return []
    def get_dependencies(self, t, n): return []
    def get_impact_set(self, t, n, depth=3): return []
    def query_path(self, ft, fn, tt, tn): return []
    def estimate_vm_count(self, t, n, depth=3): return 0


class MockHistory:
    def get_recent_events(self, count=50): return []
    def get_alarms(self): return []
    def get_events_by_type(self, t): return []


@pytest.fixture
def worldview():
    return WorldView(topology=MockTopology(), state=MockState(), history=MockHistory())


class TestExpression:
    def test_expression_evaluate_eq(self, worldview):
        """Expression == 操作符"""
        expr = Expression("resource.status", "==", "ACTIVE")
        assert expr.evaluate(worldview, INSTANCE, "vm-1") == True
        assert expr.evaluate(worldview, INSTANCE, "vm-2") == False

    def test_expression_exists(self, worldview):
        expr = Expression("ssh.accessible", "exists")
        assert expr.evaluate(worldview, INSTANCE, "vm-1") == True

    def test_expression_not_exists(self, worldview):
        expr = Expression("nonexistent.field", "not_exists")
        assert expr.evaluate(worldview, INSTANCE, "vm-1") == True

    def test_expression_contains(self, worldview):
        expr = Expression("service.tags", "contains", "production")
        assert expr.evaluate(worldview, SERVICE, "rabbitmq") == True

    def test_expression_not_eq(self, worldview):
        expr = Expression("resource.status", "!=", "ERROR")
        assert expr.evaluate(worldview, INSTANCE, "vm-1") == True  # vm-1 is ACTIVE
        assert expr.evaluate(worldview, INSTANCE, "vm-2") == False  # vm-2 is ERROR

    def test_predefined_expressions(self):
        from shared_src.expression.models import expr_status_eq, expr_host_alive, expr_service_exists
        assert expr_status_eq("ACTIVE").field == "resource.status"
        assert expr_host_alive().value == "alive"
        assert expr_service_exists().operator == "=="

    def test_expression_str(self):
        expr = Expression("resource.status", "==", "ACTIVE")
        assert str(expr) == "resource.status == ACTIVE"

    def test_expression_default_values(self):
        expr = Expression("resource.status", "==")
        assert expr.value is None
