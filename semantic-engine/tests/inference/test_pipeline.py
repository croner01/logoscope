import pytest
from semantic_engine.inference.pipeline import (
    InferencePipeline, Preprocessor, LLMEngine, Postprocessor,
    Validator, Normalizer
)
from semantic_engine.inference.models import InferenceInput, InferenceOutput
from semantic_engine.inference.finding import Finding


class MockPreprocessor(Preprocessor):
    def process(self, input_data):
        return {"processed": True, "original": input_data}


class MockLLMEngine(LLMEngine):
    def infer(self, processed_input):
        return [{"category": "mock_finding", "confidence": 0.9}]


class MockPostprocessor(Postprocessor):
    def process(self, raw_findings):
        return [{"category": f, "confidence": c, "postprocessed": True}
                for f, c in [(f.get("category"), f.get("confidence")) for f in raw_findings]]


class MockValidator(Validator):
    def validate(self, finding_dict):
        return finding_dict.get("confidence", 0) >= 0.5


class MockNormalizer(Normalizer):
    def normalize(self, validated_findings):
        return [Finding(
            category=f["category"],
            confidence=f["confidence"],
            context_hash="ctx_test",
        ) for f in validated_findings if f]


class TestInferencePipeline:
    def test_pipeline_all_stages(self):
        """Pipeline 5 阶段完整执行"""
        pipeline = InferencePipeline(
            preprocessor=MockPreprocessor(),
            llm_engine=MockLLMEngine(),
            postprocessor=MockPostprocessor(),
            validator=MockValidator(),
            normalizer=MockNormalizer(),
        )
        result = pipeline.run(InferenceInput(context={"key": "value"}))
        assert len(result) > 0
        assert isinstance(result[0], Finding)

    def test_finding_no_recommended_action(self):
        """v15: Finding 不含 recommended_action"""
        finding = Finding(category="RabbitMQHeartbeatLost", confidence=0.91)
        assert not hasattr(finding, "recommended_action")

    def test_finding_has_knowledge_refs(self):
        finding = Finding(category="test",
                           knowledge_refs=[("kb-001", "v3"), ("kb-007", "v5")])
        assert len(finding.knowledge_refs) == 2

    def test_finding_has_context_hash(self):
        finding = Finding(category="test", context_hash="ctx_abc123")
        assert finding.context_hash == "ctx_abc123"

    def test_validation_filter(self):
        """Validator 过滤低置信度结果"""
        class StrictValidator(Validator):
            def validate(self, finding_dict):
                return finding_dict.get("confidence", 0) >= 0.8

        class TestNormalizer(Normalizer):
            def normalize(self, findings):
                return [Finding(category=f.get("category", "unknown"), confidence=f.get("confidence", 0))
                        for f in findings]

        pipeline = InferencePipeline(
            preprocessor=MockPreprocessor(),
            llm_engine=MockLLMEngine(),
            postprocessor=MockPostprocessor(),
            validator=StrictValidator(),
            normalizer=TestNormalizer(),
        )
        result = pipeline.run(InferenceInput(context={}))
        # Mock 的 confidence=0.9 > 0.8，应通过
        assert len(result) > 0
