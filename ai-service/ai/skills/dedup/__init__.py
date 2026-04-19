"""Deduplication skills module."""

from .command_dedup import CommandDeduplicationSkill
from .execution_cache import ExecutionCacheSkill

__all__ = [
    "CommandDeduplicationSkill",
    "ExecutionCacheSkill",
]
