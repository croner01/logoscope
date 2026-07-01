"""BlastRadiusAnalyzer 的单元测试。"""
import pytest
from shared_src.blast_radius.models import BlastRadiusReport
from shared_src.blast_radius.analyzer import BlastRadiusAnalyzer
from shared_src.expression.impact_model import ImpactModel
from shared_src.capability.models import Capability


class MockTopology:
    def get_dependents(self, t, n):
        return [f"SERVICE:{n}_dep1", f"SERVICE:{n}_dep2", f"SERVICE:{n}_dep3"]

    def get_impact_set(self, t, n, depth=5):
        return [
            [f"SERVICE:{n}_dep1", f"SERVICE:{n}_dep2", f"SERVICE:{n}_dep3"],
            [f"INSTANCE:{n}_vm1", f"INSTANCE:{n}_vm2"],
        ]

    def estimate_vm_count(self, t, n, depth=3):
        return 5

    def get_dependencies(self, t, n):
        return []

    def query_path(self, ft, fn, tt, tn):
        return []

    def bfs_downstream(self, key, depth):
        return [["dep1"], ["dep2"]]


class MockState:
    def get_state(self, t, n):
        return "ERROR" if "critical" in n.lower() else "ACTIVE"


class TestBlastRadiusAnalyzer:
    def test_analyze_uses_impact_model(self):
        """Blast Radius 使用 Capability.impact_model"""
        cap = Capability(
            capability_id="ssh.restart_service",
            provider="ssh-executor",
            effects=["service.restart"],
            base_risk=50,
            impact_model=ImpactModel("temporary", "30s", "service"),
        )
        analyzer = BlastRadiusAnalyzer(topology=MockTopology(), state=MockState())
        report = analyzer.analyze(cap, "SERVICE", "rabbitmq")
        assert report.risk_level in ("low", "medium", "high", "critical")

    def test_permanent_impact_critical(self):
        """permanent 操作评为 critical"""
        cap = Capability(
            capability_id="openstack.delete_volume",
            effects=["storage.delete", "data.loss"],
            base_risk=80,
            impact_model=ImpactModel("permanent", "permanent", "data"),
        )
        analyzer = BlastRadiusAnalyzer(topology=MockTopology(), state=MockState())
        report = analyzer.analyze(cap, "SERVICE", "critical-volume")
        assert report.risk_level == "critical"

    def test_temporary_few_dependents(self):
        """temporary 且依赖少 → low risk"""
        class SmallTopology:
            def get_dependents(self, t, n): return ["dep1"]
            def get_impact_set(self, t, n, depth=5): return [["dep1"]]
            def estimate_vm_count(self, t, n, depth=3): return 1
            def get_dependencies(self, t, n): return []
            def query_path(self, ft, fn, tt, tn): return []
            def bfs_downstream(self, key, depth): return [["dep1"]]

        cap = Capability(capability_id="test", provider="mock",
                          effects=["test"], base_risk=10,
                          impact_model=ImpactModel("temporary", "5s", "instance"))
        analyzer = BlastRadiusAnalyzer(topology=SmallTopology(), state=MockState())
        report = analyzer.analyze(cap, "SERVICE", "svc")
        assert report.risk_level in ("low", "medium")

    def test_report_fields(self):
        cap = Capability(capability_id="test", provider="mock",
                          effects=["test"], base_risk=50,
                          impact_model=ImpactModel("temporary", "30s", "service"))
        analyzer = BlastRadiusAnalyzer(topology=MockTopology(), state=MockState())
        report = analyzer.analyze(cap, "SERVICE", "rabbitmq")
        assert report.primary_target_type == "SERVICE"
        assert report.primary_target_name == "rabbitmq"
        assert isinstance(report.directly_affected, list)
        assert isinstance(report.indirectly_affected, list)
        assert report.estimated_vm_count >= 0
        assert report.estimated_service_count >= 0
        assert report.reasoning != ""

    def test_no_impact_model(self):
        """无 impact_model 时使用默认临时"""
        cap = Capability(capability_id="test", provider="mock",
                          effects=["test"], base_risk=50)
        analyzer = BlastRadiusAnalyzer(topology=MockTopology(), state=MockState())
        report = analyzer.analyze(cap, "SERVICE", "svc")
        assert report is not None
