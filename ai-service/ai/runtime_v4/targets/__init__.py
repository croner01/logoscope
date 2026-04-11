"""Runtime v4 target/capability registry."""

from ai.runtime_v4.targets.service import (
    RuntimeV4TargetRegistry,
    ensure_runtime_v4_default_targets,
    get_runtime_v4_target_registry,
    set_runtime_v4_target_storage,
)


__all__ = [
    "RuntimeV4TargetRegistry",
    "ensure_runtime_v4_default_targets",
    "get_runtime_v4_target_registry",
    "set_runtime_v4_target_storage",
]
