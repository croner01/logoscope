import pytest
from semantic_engine.inference.registry import InferenceRegistry
from semantic_engine.inference.pipeline import InferencePipeline


class MockPipeline(InferencePipeline):
    def run(self, input_data):
        from semantic_engine.inference.finding import Finding
        return [Finding(category="test", confidence=0.9)]


class TestInferenceRegistry:
    def test_register_and_get(self):
        registry = InferenceRegistry()
        pipeline = MockPipeline(preprocessor=None, llm_engine=None, postprocessor=None,
                                 validator=None, normalizer=None)
        registry.register("normalized.event", pipeline)
        assert registry.get("normalized.event") == pipeline

    def test_register_nonexistent_returns_none(self):
        registry = InferenceRegistry()
        assert registry.get("nonexistent") is None

    def test_register_multiple_types(self):
        registry = InferenceRegistry()
        p1 = MockPipeline(None, None, None, None, None)
        p2 = MockPipeline(None, None, None, None, None)
        registry.register("type.a", p1)
        registry.register("type.b", p2)
        assert registry.get("type.a") == p1
        assert registry.get("type.b") == p2
