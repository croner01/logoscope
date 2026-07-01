import pytest
from shared_src.policy.models import UtilityWeights


class TestUtilityWeights:
    def test_default_weights(self):
        w = UtilityWeights()
        assert w.success == 0.5
        assert w.risk == 0.3
        assert w.cost == 0.1
        assert w.blast == 0.05

    def test_custom_weights(self):
        w = UtilityWeights(success=0.4, risk=0.4, cost=0.1, blast=0.1)
        assert w.success == 0.4
        assert w.risk == 0.4

    def test_weights_sum(self):
        w = UtilityWeights(success=0.5, risk=0.3, cost=0.1, blast=0.1)
        assert abs(w.success + w.risk + w.cost + w.blast - 1.0) < 0.001
