"""InferencePipeline — 5 阶段推理流水线。"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from .models import InferenceInput, InferenceOutput
from .finding import Finding


class Preprocessor(ABC):
    @abstractmethod
    def process(self, input_data: InferenceInput) -> Any: ...


class LLMEngine(ABC):
    @abstractmethod
    def infer(self, processed_input: Any) -> List[Dict]: ...


class Postprocessor(ABC):
    @abstractmethod
    def process(self, raw_findings: List[Dict]) -> List[Dict]: ...


class Validator(ABC):
    @abstractmethod
    def validate(self, finding_dict: Dict) -> bool: ...


class Normalizer(ABC):
    @abstractmethod
    def normalize(self, validated_findings: List[Dict]) -> List[Finding]: ...


class InferencePipeline:
    """
    5 阶段推理流水线。

    1. Preprocess -> 2. LLM Engine -> 3. Postprocess -> 4. Validate -> 5. Normalize
    """

    def __init__(self, preprocessor: Preprocessor, llm_engine: LLMEngine,
                 postprocessor: Postprocessor, validator: Validator,
                 normalizer: Normalizer):
        self.preprocessor = preprocessor
        self.llm_engine = llm_engine
        self.postprocessor = postprocessor
        self.validator = validator
        self.normalizer = normalizer

    def run(self, input_data: InferenceInput) -> List[Finding]:
        processed = self.preprocessor.process(input_data)
        raw = self.llm_engine.infer(processed)
        postprocessed = self.postprocessor.process(raw)
        validated = [f for f in postprocessed if self.validator.validate(f)]
        return self.normalizer.normalize(validated)
