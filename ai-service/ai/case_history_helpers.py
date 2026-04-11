"""
Case history/change helper functions.
"""

import json
from typing import Any, Callable, Dict, List, Optional, Tuple


def _history_safe_value(
    value: Any,
    *,
    as_str: Callable[[Any, str], str],
    truncate_text: Callable[[str, int], str],
    max_depth: int = 3,
    max_list: int = 10,
    max_text_len: int = 300,
) -> Any:
    """将值裁剪为可追踪且可序列化的历史快照。"""
    if max_depth <= 0:
        return truncate_text(as_str(value), max_text_len)
    if isinstance(value, dict):
        safe: Dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= max_list:
                break
            safe[str(key)] = _history_safe_value(
                item,
                as_str=as_str,
                truncate_text=truncate_text,
                max_depth=max_depth - 1,
                max_list=max_list,
                max_text_len=max_text_len,
            )
        return safe
    if isinstance(value, list):
        return [
            _history_safe_value(
                item,
                as_str=as_str,
                truncate_text=truncate_text,
                max_depth=max_depth - 1,
                max_list=max_list,
                max_text_len=max_text_len,
            )
            for item in value[:max_list]
        ]
    if isinstance(value, str):
        return truncate_text(value, max_text_len)
    return value


def _history_compare_value(
    value: Any,
    *,
    as_str: Callable[[Any, str], str],
    truncate_text: Callable[[str, int], str],
    max_depth: int = 6,
    max_list: int = 400,
    max_text_len: int = 10000,
) -> Any:
    """用于变更判断的稳定比较值（尽量保留完整内容，避免误判无变化）。"""
    return _history_safe_value(
        value,
        as_str=as_str,
        truncate_text=truncate_text,
        max_depth=max_depth,
        max_list=max_list,
        max_text_len=max_text_len,
    )


def _history_snapshot_value(
    field_name: str,
    value: Any,
    *,
    as_str: Callable[[Any, str], str],
    truncate_text: Callable[[str, int], str],
) -> Any:
    """按字段生成用于历史展示的快照，兼顾可读性与信息完整度。"""
    if field_name in {"solutions", "root_causes", "tags"}:
        return _history_safe_value(
            value,
            as_str=as_str,
            truncate_text=truncate_text,
            max_depth=5,
            max_list=120,
            max_text_len=2000,
        )
    if field_name in {"summary", "analysis_summary", "resolution"}:
        return _history_safe_value(
            value,
            as_str=as_str,
            truncate_text=truncate_text,
            max_depth=4,
            max_list=40,
            max_text_len=3000,
        )
    return _history_safe_value(value, as_str=as_str, truncate_text=truncate_text)


def _history_values_equal(
    left: Any,
    right: Any,
    *,
    as_str: Callable[[Any, str], str],
    truncate_text: Callable[[str, int], str],
) -> bool:
    """用于历史变更检测的稳定比较。"""
    left_safe = _history_compare_value(left, as_str=as_str, truncate_text=truncate_text)
    right_safe = _history_compare_value(right, as_str=as_str, truncate_text=truncate_text)
    return json.dumps(left_safe, ensure_ascii=False, sort_keys=True) == json.dumps(right_safe, ensure_ascii=False, sort_keys=True)


def _build_case_content_change_summary(
    existing_case: Any,
    updated_case: Any,
    previous_analysis_summary: str,
    current_analysis_summary: str,
    *,
    as_str: Callable[[Any, str], str],
    as_list: Callable[[Any], List[Any]],
    truncate_text: Callable[[str, int], str],
    normalize_solutions: Callable[[Any], List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """构建知识库内容变更摘要（字段级 before/after）。"""
    tracked_fields: List[Tuple[str, Any, Any]] = [
        ("problem_type", as_str(getattr(existing_case, "problem_type", "")), as_str(getattr(updated_case, "problem_type", ""))),
        ("severity", as_str(getattr(existing_case, "severity", "")), as_str(getattr(updated_case, "severity", ""))),
        ("summary", as_str(getattr(existing_case, "summary", "")), as_str(getattr(updated_case, "summary", ""))),
        ("service_name", as_str(getattr(existing_case, "service_name", "")), as_str(getattr(updated_case, "service_name", ""))),
        ("root_causes", as_list(getattr(existing_case, "root_causes", [])), as_list(getattr(updated_case, "root_causes", []))),
        ("solutions", normalize_solutions(getattr(existing_case, "solutions", [])), normalize_solutions(getattr(updated_case, "solutions", []))),
        ("analysis_summary", as_str(previous_analysis_summary), as_str(current_analysis_summary)),
        ("resolution", as_str(getattr(existing_case, "resolution", "")), as_str(getattr(updated_case, "resolution", ""))),
        ("tags", as_list(getattr(existing_case, "tags", [])), as_list(getattr(updated_case, "tags", []))),
    ]

    changed_fields: List[str] = []
    changes: Dict[str, Any] = {}
    for field_name, before_value, after_value in tracked_fields:
        if _history_values_equal(
            before_value,
            after_value,
            as_str=as_str,
            truncate_text=truncate_text,
        ):
            continue
        changed_fields.append(field_name)
        changes[field_name] = {
            "before": _history_snapshot_value(
                field_name,
                before_value,
                as_str=as_str,
                truncate_text=truncate_text,
            ),
            "after": _history_snapshot_value(
                field_name,
                after_value,
                as_str=as_str,
                truncate_text=truncate_text,
            ),
        }
    return {"changed_fields": changed_fields, "changes": changes}


def _case_store_list_change_history(
    case_store: Any,
    case_id: str,
    *,
    warn: Callable[[str], None],
    limit: int = 100,
    event_type: str = "content_update",
) -> List[Dict[str, Any]]:
    method = getattr(case_store, "list_case_change_history", None)
    if not callable(method):
        return []
    try:
        result = method(case_id=case_id, limit=limit, event_type=event_type)
    except TypeError:
        result = method(case_id, limit, event_type)
    except Exception as exc:
        warn(f"Failed to list case change history from store: {exc}")
        return []
    return result if isinstance(result, list) else []


def _case_store_count_change_history(
    case_store: Any,
    case_id: str,
    *,
    warn: Callable[[str], None],
    event_type: str = "content_update",
) -> int:
    method = getattr(case_store, "count_case_change_history", None)
    if not callable(method):
        return 0
    try:
        value = method(case_id=case_id, event_type=event_type)
    except TypeError:
        value = method(case_id, event_type)
    except Exception as exc:
        warn(f"Failed to count case change history from store: {exc}")
        return 0
    try:
        return max(0, int(value))
    except Exception:
        return 0


def _case_store_append_change_history(
    case_store: Any,
    case_id: str,
    payload: Dict[str, Any],
    *,
    warn: Callable[[str], None],
) -> Dict[str, Any]:
    method = getattr(case_store, "append_case_change_history", None)
    if not callable(method):
        return payload
    try:
        result = method(case_id=case_id, event=payload)
    except TypeError:
        result = method(case_id, payload)
    except Exception as exc:
        warn(f"Failed to append case change history to store: {exc}")
        return payload
    return result if isinstance(result, dict) else payload
