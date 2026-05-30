"""
Analysis result normalization helper functions.

Extracted from `api/ai.py` to reduce route module size while preserving
the legacy response contract.
"""

import re
from typing import Any, Dict, List, Optional


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _truncate_text(value: Any, max_len: int) -> str:
    text = _as_str(value)
    if max_len <= 0:
        return ""
    return text[:max_len]


def _normalize_kb_draft_severity(value: Any, default: str = "medium") -> str:
    severity = _as_str(value, default).strip().lower()
    aliases = {
        "sev0": "critical",
        "sev1": "high",
        "sev2": "medium",
        "sev3": "low",
        "p0": "critical",
        "p1": "high",
        "p2": "medium",
        "p3": "low",
    }
    normalized = aliases.get(severity, severity)
    if normalized not in {"critical", "high", "medium", "low", "unknown"}:
        return default
    return normalized


def _normalize_string_list(raw: Any, max_items: int = 8, min_length: int = 4) -> List[str]:
    items: List[str] = []
    for item in _as_list(raw):
        text = _as_str(item).strip().lstrip("-").lstrip("*").strip()
        if len(text) >= min_length:
            items.append(text)

    unique_items: List[str] = []
    seen = set()
    for item in items:
        if item not in seen:
            seen.add(item)
            unique_items.append(item)
        if len(unique_items) >= max_items:
            break
    return unique_items


def _normalize_root_causes(raw: Any) -> List[Dict[str, Any]]:
    """统一 root causes 字段格式。"""
    normalized: List[Dict[str, Any]] = []

    for item in _as_list(raw):
        if isinstance(item, dict):
            title = _as_str(item.get("title") or item.get("name") or item.get("cause") or item.get("span_id"))
            description = _as_str(item.get("description") or item.get("detail") or item.get("reason"))
            if title or description:
                normalized_item: Dict[str, Any] = {
                    "title": title or description or "unknown",
                    "description": description,
                }
                if _as_str(item.get("icon")):
                    normalized_item["icon"] = _as_str(item.get("icon"))
                if _as_str(item.get("color")):
                    normalized_item["color"] = _as_str(item.get("color"))
                evidence = _as_list(item.get("evidence"))
                if evidence:
                    normalized_item["evidence"] = evidence
                normalized.append(normalized_item)
            continue

        text = _as_str(item)
        if text:
            normalized.append({"title": text, "description": ""})

    return normalized


def _normalize_solutions(raw: Any) -> List[Dict[str, Any]]:
    """统一 solutions 字段格式。"""
    normalized: List[Dict[str, Any]] = []

    for item in _as_list(raw):
        if isinstance(item, dict):
            title = _as_str(
                item.get("title")
                or item.get("name")
                or item.get("suggestion")
                or item.get("recommendation")
            )
            description = _as_str(item.get("description") or item.get("detail") or item.get("reason"))
            steps = [step for step in _as_list(item.get("steps")) if isinstance(step, str)]
            if title or description or steps:
                normalized_item: Dict[str, Any] = {
                    "title": title or description or "建议项",
                    "description": description,
                    "steps": steps,
                }
                resources = [resource for resource in _as_list(item.get("resources")) if isinstance(resource, str)]
                if resources:
                    normalized_item["resources"] = resources
                normalized.append(normalized_item)
            continue

        text = _as_str(item)
        if text:
            normalized.append(
                {
                    "title": text,
                    "description": "",
                    "steps": [],
                }
            )

    return normalized


def _normalize_handling_ideas(raw: Any) -> List[Dict[str, str]]:
    """统一 handling ideas（处理思路）字段格式。"""
    normalized: List[Dict[str, str]] = []

    for item in _as_list(raw):
        if isinstance(item, dict):
            title = _as_str(item.get("title") or item.get("idea") or item.get("name") or item.get("stage"))
            description = _as_str(
                item.get("description") or item.get("detail") or item.get("reason") or item.get("action")
            )
            if title or description:
                normalized.append({"title": title or description or "处理思路", "description": description})
            continue

        text = _as_str(item)
        if text:
            normalized.append({"title": text, "description": ""})

    return normalized


def _normalize_data_flow_path(raw: Any) -> List[Dict[str, Any]]:
    """归一化数据路径 path 节点。"""
    normalized: List[Dict[str, Any]] = []
    for index, item in enumerate(_as_list(raw), start=1):
        if isinstance(item, dict):
            step = int(_as_float(item.get("step"), _as_float(item.get("index"), float(index))))
            component = _as_str(
                item.get("component")
                or item.get("service")
                or item.get("service_name")
                or item.get("node")
                or item.get("name")
            )
            source = _as_str(item.get("from") or item.get("source") or item.get("source_service"))
            target = _as_str(item.get("to") or item.get("target") or item.get("target_service"))
            operation = _as_str(
                item.get("operation")
                or item.get("action")
                or item.get("span")
                or item.get("span_name")
                or item.get("method")
            )
            evidence = _as_str(item.get("evidence") or item.get("proof") or item.get("note"))
            status = _as_str(item.get("status"), "unknown")
            latency_ms = _as_float(item.get("latency_ms"), 0.0)

            if not component and (source or target):
                component = f"{source or 'unknown'} -> {target or 'unknown'}"

            if component or operation or evidence:
                payload: Dict[str, Any] = {
                    "step": max(step, 1),
                    "component": component or "unknown",
                    "operation": operation,
                    "status": status or "unknown",
                }
                if source:
                    payload["from"] = source
                if target:
                    payload["to"] = target
                if evidence:
                    payload["evidence"] = evidence
                if latency_ms > 0:
                    payload["latency_ms"] = latency_ms
                normalized.append(payload)
            continue

        text = _as_str(item)
        if text:
            normalized.append({"step": index, "component": text, "operation": "", "status": "unknown"})
    return normalized


def _normalize_data_flow(raw: Any) -> Dict[str, Any]:
    """统一 data flow（数据路径）字段格式。"""
    if raw is None:
        return {}

    summary = ""
    path_source: Any = []
    evidence_source: Any = []
    confidence = 0.0

    if isinstance(raw, dict):
        summary = _as_str(
            raw.get("summary")
            or raw.get("description")
            or raw.get("path_summary")
            or raw.get("flow_summary")
            or raw.get("request_path_summary")
        )
        path_source = raw.get("path") if raw.get("path") is not None else raw.get("steps")
        if path_source is None:
            path_source = raw.get("hops") if raw.get("hops") is not None else raw.get("flow")
        if path_source is None:
            path_source = raw.get("nodes")
        evidence_source = raw.get("evidence") if raw.get("evidence") is not None else raw.get("observations")
        if evidence_source is None:
            evidence_source = raw.get("notes")
        confidence = _as_float(raw.get("confidence"), 0.0)
    elif isinstance(raw, str):
        summary = _as_str(raw)
    else:
        path_source = raw

    path = _normalize_data_flow_path(path_source)
    evidence = [item for item in _normalize_string_list(evidence_source, max_items=20, min_length=2)]

    if not summary and path:
        summary = " -> ".join([_as_str(item.get("component"), "unknown") for item in path[:6]])

    if not summary and not path and not evidence:
        return {}

    return {
        "summary": summary,
        "path": path,
        "evidence": evidence,
        "confidence": max(0.0, min(1.0, confidence)),
    }


_SOLUTION_STEP_PATTERN = re.compile(r"^\s*(?:\d+[.)]|[-*])\s*(.+?)\s*$")


def _solutions_to_text(raw: Any) -> str:
    """将结构化 solutions 转换为便于人工编辑的纯文本。"""
    lines: List[str] = []
    for index, item in enumerate(_normalize_solutions(raw), start=1):
        title = _as_str(item.get("title"))
        description = _as_str(item.get("description"))
        steps = [str(step).strip() for step in _as_list(item.get("steps")) if _as_str(step)]
        if lines:
            lines.append("")
        lines.append(f"方案{index}: {title or '未命名方案'}")
        if description:
            lines.append(f"说明: {description}")
        if steps:
            lines.append("步骤:")
            for step_index, step in enumerate(steps, start=1):
                lines.append(f"{step_index}. {step}")
    return "\n".join(lines).strip()


def _normalize_solutions_from_text(solution_text: Any) -> List[Dict[str, Any]]:
    """把纯文本方案解析为结构化 solutions。"""
    text = _truncate_text(_as_str(solution_text), 6000).strip()
    if not text:
        return []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []

    steps: List[str] = []
    description_lines: List[str] = []
    for line in lines:
        matched = _SOLUTION_STEP_PATTERN.match(line)
        if matched:
            step = _as_str(matched.group(1)).strip()
            if step:
                steps.append(step)
            continue
        cleaned = line
        if cleaned.startswith("方案"):
            cleaned = cleaned.split(":", 1)[-1].strip()
        if cleaned.startswith("说明:"):
            cleaned = cleaned.split(":", 1)[-1].strip()
        if cleaned.startswith("步骤:"):
            continue
        if cleaned:
            description_lines.append(cleaned)

    title = description_lines[0] if description_lines else "执行标准知识库处置步骤"
    description = "\n".join(description_lines[1:]) if len(description_lines) > 1 else (
        description_lines[0] if description_lines else ""
    )
    normalized = _normalize_solutions(
        [
            {
                "title": _truncate_text(title, 120),
                "description": _truncate_text(description, 1500),
                "steps": _normalize_string_list(steps, max_items=20, min_length=2),
            }
        ]
    )
    return normalized


def _format_solution_text_standard(
    raw_text: str,
    *,
    summary: str = "",
    service_name: str = "",
    problem_type: str = "",
    severity: str = "",
) -> str:
    """规则模式下把方案文本格式化为标准模板。"""
    normalized = _normalize_solutions_from_text(raw_text)
    steps: List[str] = []
    if normalized:
        first = normalized[0]
        steps = [str(step).strip() for step in _as_list(first.get("steps")) if _as_str(step)]
    if not steps:
        steps = _normalize_string_list(raw_text.splitlines(), max_items=8, min_length=2)
    if not steps:
        steps = ["收集关键指标（错误率、延迟、资源水位）并定位异常时间窗口。"]

    summary_text = _truncate_text(_as_str(summary), 200)
    service = _as_str(service_name, "unknown")
    ptype = _as_str(problem_type, "unknown")
    sev = _normalize_kb_draft_severity(severity, default="medium")
    step_lines = "\n".join([f"{index}. {item}" for index, item in enumerate(steps[:12], start=1)])
    return (
        f"【目标】\n恢复 {service} 服务稳定性，避免 {ptype} 问题再次发生。\n\n"
        f"【问题上下文】\n服务={service}，类型={ptype}，级别={sev}。\n"
        f"{summary_text or '根据当前会话信息整理。'}\n\n"
        f"【处理步骤】\n{step_lines}\n\n"
        "【验证方式】\n"
        "1. 观察 15-30 分钟核心指标（错误率、P95 延迟、吞吐）是否恢复基线。\n"
        "2. 核查业务关键接口无新增错误日志。\n\n"
        "【回滚方案】\n若关键指标持续恶化，回滚最近一次配置/发布变更并恢复默认阈值。\n\n"
        "【风险与注意】\n严格按灰度范围执行，避免一次性全量变更。"
    )


def _normalize_similar_cases(raw: Any) -> List[Dict[str, str]]:
    """统一 similar cases 字段格式。"""
    normalized: List[Dict[str, str]] = []

    for item in _as_list(raw):
        if isinstance(item, dict):
            title = _as_str(item.get("title") or item.get("summary") or item.get("case_title") or item.get("problem"))
            description = _as_str(
                item.get("description")
                or item.get("detail")
                or item.get("resolution")
                or item.get("relevance_reason")
            )
            if title or description:
                normalized.append({"title": title or description or "similar-case", "description": description})
            continue

        text = _as_str(item)
        if text:
            normalized.append({"title": text, "description": ""})

    return normalized


def _normalize_overview(result: Dict[str, Any], fallback_description: str = "") -> Dict[str, Any]:
    """统一 overview 字段格式。"""
    overview = result.get("overview")
    overview_data = overview if isinstance(overview, dict) else {}

    return {
        "problem": _as_str(overview_data.get("problem") or result.get("problem_type") or result.get("problem"), "unknown"),
        "severity": _as_str(overview_data.get("severity") or result.get("severity"), "unknown"),
        "description": _as_str(
            overview_data.get("description") or result.get("summary") or result.get("description"),
            fallback_description,
        ),
        "confidence": _as_float(overview_data.get("confidence", result.get("confidence", 0.0)), 0.0),
    }


def _normalize_analysis_result(
    raw_result: Any,
    analysis_method: Optional[str] = None,
    fallback_description: str = "",
) -> Dict[str, Any]:
    """
    统一 trace/log AI 返回结构。

    兼容新老字段并输出标准格式：
    - overview
    - dataFlow
    - rootCauses
    - handlingIdeas
    - solutions
    - similarCases
    """
    result: Dict[str, Any] = raw_result if isinstance(raw_result, dict) else {}
    solution_source = result.get("solutions") if result.get("solutions") is not None else result.get("suggestions")
    if solution_source is None:
        solution_source = result.get("recommendations")

    handling_ideas_source = (
        result.get("handlingIdeas") if result.get("handlingIdeas") is not None else result.get("handling_ideas")
    )
    if handling_ideas_source is None:
        handling_ideas_source = result.get("analysis_ideas")

    data_flow_source = result.get("dataFlow") if result.get("dataFlow") is not None else result.get("data_flow")
    if data_flow_source is None:
        data_flow_source = result.get("path_analysis")
    if data_flow_source is None:
        data_flow_source = result.get("request_path")

    normalized: Dict[str, Any] = {
        "overview": _normalize_overview(result, fallback_description=fallback_description),
        "rootCauses": _normalize_root_causes(
            result.get("rootCauses") if result.get("rootCauses") is not None else result.get("root_causes")
        ),
        "solutions": _normalize_solutions(solution_source),
        "metrics": _as_list(result.get("metrics")),
        "similarCases": _normalize_similar_cases(
            result.get("similarCases") if result.get("similarCases") is not None else result.get("similar_cases")
        ),
    }
    normalized_data_flow = _normalize_data_flow(data_flow_source)
    if normalized_data_flow:
        normalized["dataFlow"] = normalized_data_flow

    normalized_handling_ideas = _normalize_handling_ideas(handling_ideas_source)
    if normalized_handling_ideas:
        normalized["handlingIdeas"] = normalized_handling_ideas

    final_method = analysis_method or _as_str(result.get("analysis_method"))
    if final_method:
        normalized["analysis_method"] = final_method

    model = _as_str(result.get("model"))
    if model:
        normalized["model"] = model

    if isinstance(result.get("cached"), bool):
        normalized["cached"] = result.get("cached")

    if result.get("latency_ms") is not None:
        normalized["latency_ms"] = int(_as_float(result.get("latency_ms"), 0))

    if _as_str(result.get("error")):
        normalized["error"] = _as_str(result.get("error"))

    return normalized
