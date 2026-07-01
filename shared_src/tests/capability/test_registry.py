import pytest
from shared_src.capability.models import Capability, ParameterDef
from shared_src.capability.registry import CapabilityRegistry
from shared_src.expression.models import Expression
from shared_src.expression.impact_model import ImpactModel


class TestCapabilityModels:
    def test_capability_expression_preconditions(self):
        """Capability 使用 Expression，不是字符串"""
        cap = Capability(
            capability_id="ssh.restart_service",
            provider="ssh-executor",
            effects=["service.restart", "process.modify"],
            base_risk=50,
            preconditions=[
                Expression("host.host_status", "==", "alive"),
                Expression("service.exists", "==", True),
            ],
            postconditions=[
                Expression("resource.status", "==", "running"),
            ],
            impact_model=ImpactModel("temporary", "30s", "service"),
            rollback_capability="ssh.restart_service",
        )
        assert all(isinstance(p, Expression) for p in cap.preconditions)
        assert cap.impact_model.severity == "temporary"
        assert cap.preconditions[0].field == "host.host_status"

    def test_capability_effect_tags(self):
        """Capability effects 是 List[str]"""
        cap = Capability(
            capability_id="openstack.delete_volume",
            effects=["storage.delete", "data.loss"],
            base_risk=80,
            provider="openstack-api",
        )
        assert "storage.delete" in cap.effects

    def test_capability_defaults(self):
        cap = Capability(capability_id="echo.test", provider="mock",
                          effects=["read"], base_risk=5)
        assert cap.preconditions == []
        assert cap.postconditions == []
        assert cap.impact_model is None
        assert cap.rollback_capability == ""

    def test_capability_estimated_cost(self):
        cap = Capability(capability_id="migrate", provider="nova",
                          effects=["vm.migrate"], base_risk=60,
                          estimated_duration_ms=30000, estimated_cost=5.0)
        assert cap.estimated_duration_ms == 30000
        assert cap.estimated_cost == 5.0


class TestCapabilityRegistry:
    def test_register_and_execute(self):
        registry = CapabilityRegistry()
        registry.register(Capability(
            capability_id="echo.test", provider="mock",
            effects=["read.process"], base_risk=5,
        ))
        result = registry.execute("echo.test", {"msg": "hello"})
        assert result is not None

    def test_execute_nonexistent(self):
        registry = CapabilityRegistry()
        result = registry.execute("nonexistent", {})
        assert result is None

    def test_get_capability(self):
        registry = CapabilityRegistry()
        cap = Capability(capability_id="test.1", provider="mock",
                          effects=["test"], base_risk=1)
        registry.register(cap)
        assert registry.get("test.1") == cap

    def test_list_capabilities(self):
        registry = CapabilityRegistry()
        registry.register(Capability(capability_id="cap.a", provider="p1", effects=["a"], base_risk=1))
        registry.register(Capability(capability_id="cap.b", provider="p2", effects=["b"], base_risk=2))
        caps = registry.list_capabilities()
        assert len(caps) == 2
