"""Parameter normalization helpers extracted from query routes."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any, Dict, List, Optional, Set, Tuple


def normalize_optional_str(value: Any) -> Optional[str]:
    """Normalize optional string parameters."""
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    return None


def normalize_optional_str_list(value: Any) -> List[str]:
    """Normalize optional string list from list/set/comma-separated input."""
    if value is None:
        return []

    raw_values: List[str] = []
    if isinstance(value, str):
        raw_values = [value]
    elif isinstance(value, (list, tuple, set)):
        raw_values = [item for item in value if isinstance(item, str)]
    else:
        return []

    normalized: List[str] = []
    seen: Set[str] = set()
    for raw in raw_values:
        for part in raw.split(","):
            item = part.strip()
            if item and item not in seen:
                seen.add(item)
                normalized.append(item)
    return normalized


def normalize_level_values(value: Any) -> List[str]:
    """Normalize log levels and align WARNING/WARN aliases."""
    levels: List[str] = []
    for item in normalize_optional_str_list(value):
        normalized = item.upper()
        if normalized == "WARNING":
            normalized = "WARN"
        levels.append(normalized)
    return normalize_optional_str_list(levels)


def expand_level_match_values(levels: List[str]) -> List[str]:
    """Expand level matching values for WARN/WARNING compatibility."""
    expanded: List[str] = []
    for level in normalize_level_values(levels):
        if level == "WARN":
            expanded.extend(["WARN", "WARNING"])
        else:
            expanded.append(level)
    return normalize_optional_str_list(expanded)


def append_exact_match_filter(
    *,
    conditions: List[str],
    params: Dict[str, Any],
    column_name: str,
    param_prefix: str,
    values: List[str],
) -> List[str]:
    """Append safe parameterized exact-match SQL predicates."""
    normalized_values = normalize_optional_str_list(values)
    if not normalized_values:
        return []

    if len(normalized_values) == 1:
        param_name = param_prefix
        conditions.append(f"{column_name} = {{{param_name}:String}}")
        params[param_name] = normalized_values[0]
        return normalized_values

    sub_conditions: List[str] = []
    for idx, value in enumerate(normalized_values):
        param_name = f"{param_prefix}_{idx}"
        sub_conditions.append(f"{column_name} = {{{param_name}:String}}")
        params[param_name] = value
    conditions.append(f"({' OR '.join(sub_conditions)})")
    return normalized_values


def sanitize_interval(time_window: str, default_value: str = "7 DAY") -> str:
    """Normalize INTERVAL values for SQL safety and consistent formatting."""
    pattern = re.compile(r"^\s*(\d+)\s+([A-Za-z]+)\s*$")
    match = pattern.match(str(time_window or ""))
    if not match:
        return default_value

    amount = int(match.group(1))
    unit_raw = match.group(2).upper()
    valid_units = {
        "MINUTE": "MINUTE",
        "MINUTES": "MINUTE",
        "HOUR": "HOUR",
        "HOURS": "HOUR",
        "DAY": "DAY",
        "DAYS": "DAY",
        "WEEK": "WEEK",
        "WEEKS": "WEEK",
    }
    if amount <= 0 or unit_raw not in valid_units:
        return default_value
    return f"{amount} {valid_units[unit_raw]}"


def normalize_topology_context(
    service_name: Optional[str],
    search: Optional[str],
    start_time: Optional[str],
    end_time: Optional[str],
    source_service: Optional[str],
    target_service: Optional[str],
    time_window: Optional[str],
) -> Dict[str, Optional[str]]:
    """Normalize topology jump context into a single filter dictionary."""
    resolved_service_name = str(service_name or "").strip() or None
    resolved_search = str(search or "").strip() or None
    resolved_start_time = start_time
    resolved_end_time = end_time

    source = str(source_service or "").strip() or None
    target = str(target_service or "").strip() or None
    safe_window = sanitize_interval(time_window or "", default_value="1 HOUR") if time_window else None

    if not resolved_service_name and source:
        resolved_service_name = source
    if not resolved_search and target:
        resolved_search = target
    if not resolved_start_time and not resolved_end_time and safe_window:
        resolved_start_time = f"__RELATIVE_INTERVAL__::{safe_window}"

    return {
        "service_name": resolved_service_name,
        "search": resolved_search,
        "start_time": resolved_start_time,
        "end_time": resolved_end_time,
        "source_service": source,
        "target_service": target,
        "time_window": safe_window,
    }


def build_time_filter_clause(
    column_name: str,
    time_window: str,
    start_time: Optional[str],
    end_time: Optional[str],
    param_prefix: str = "kpi",
) -> Tuple[str, Dict[str, Any]]:
    """Build absolute-time filter first, fallback to interval filter."""
    if start_time and end_time:
        return (
            f"{column_name} >= parseDateTimeBestEffort({{{param_prefix}_start:String}}) "
            f"AND {column_name} < parseDateTimeBestEffort({{{param_prefix}_end:String}})",
            {
                f"{param_prefix}_start": start_time,
                f"{param_prefix}_end": end_time,
            },
        )

    safe_window = sanitize_interval(time_window, default_value="7 DAY")
    return f"{column_name} > now() - INTERVAL {safe_window}", {}


def safe_ratio(numerator: float, denominator: float) -> float:
    """Return ratio with zero-safe denominator handling."""
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def interval_to_timedelta(time_window: str, default_value: str = "7 DAY") -> timedelta:
    """Convert INTERVAL text into timedelta."""
    safe_window = sanitize_interval(time_window, default_value=default_value)
    amount_text, unit = safe_window.split()
    amount = int(amount_text)
    unit = unit.upper()

    if unit == "MINUTE":
        return timedelta(minutes=amount)
    if unit == "HOUR":
        return timedelta(hours=amount)
    if unit == "WEEK":
        return timedelta(weeks=amount)
    return timedelta(days=amount)
