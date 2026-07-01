from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class PipelineConfig:
    """Pipeline 配置。"""
    aggregate_window_seconds: int = 5
    dedup_initial_window_ms: int = 5000
    sample_rates: Dict[str, float] = field(default_factory=lambda: {
        "INFO": 1.0,
        "ERROR": 1.0,
        "WARN": 1.0,
        "DEBUG": 0.1,
    })
    host_map: Dict[str, str] = field(default_factory=dict)
