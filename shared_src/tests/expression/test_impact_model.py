import pytest
from shared_src.expression.impact_model import ImpactModel


class TestImpactModel:
    def test_impact_model_creation(self):
        model = ImpactModel(severity="temporary", duration="30s", scope="service")
        assert model.severity == "temporary"
        assert model.duration == "30s"
        assert model.scope == "service"

    def test_permanent_impact(self):
        model = ImpactModel("permanent", "permanent", "data")
        assert model.severity == "permanent"
