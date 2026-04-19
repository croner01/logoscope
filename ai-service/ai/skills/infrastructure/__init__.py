"""Infrastructure skills module for deduplication, feedback, and scope detection."""

from ai.skills.infrastructure.dedup import (
    CommandDeduplicator,
    DeduplicationResult,
    ExecutionCacheEntry,
    get_deduplicator,
    set_deduplicator,
)
from ai.skills.infrastructure.feedback import (
    CommandFeedback,
    CommandRiskLevel,
    OutputFormatter,
    ResultInterpreter,
    DANGEROUS_PATTERNS,
    get_formatter,
    get_interpreter,
)
from ai.skills.infrastructure.scope import (
    ScopeAutoDetector,
    ScopeDetectionResult,
    TargetKind,
    TargetScope,
    get_detector,
)

__all__ = [
    "CommandDeduplicator",
    "DeduplicationResult",
    "ExecutionCacheEntry",
    "get_deduplicator",
    "set_deduplicator",
    "CommandFeedback",
    "CommandRiskLevel",
    "OutputFormatter",
    "ResultInterpreter",
    "DANGEROUS_PATTERNS",
    "get_formatter",
    "get_interpreter",
    "ScopeAutoDetector",
    "ScopeDetectionResult",
    "TargetKind",
    "TargetScope",
    "get_detector",
]
