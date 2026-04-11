"""
analysis_result_helpers 契约测试
"""

from ai.analysis_result_helpers import (
    _format_solution_text_standard,
    _normalize_analysis_result,
    _normalize_solutions_from_text,
)


def test_normalize_analysis_result_maps_legacy_fields():
    raw = {
        "problem_type": "timeout",
        "severity": "high",
        "summary": "gateway timeout",
        "root_causes": ["upstream overload"],
        "suggestions": ["scale deployment"],
        "handling_ideas": [{"title": "快速止血", "description": "先扩容"}],
        "path_analysis": {"path": ["edge", "gateway", "svc-a"], "confidence": 0.8},
    }

    normalized = _normalize_analysis_result(raw, analysis_method="rule-based")

    assert normalized["overview"]["problem"] == "timeout"
    assert normalized["overview"]["severity"] == "high"
    assert normalized["rootCauses"][0]["title"] == "upstream overload"
    assert normalized["solutions"][0]["title"] == "scale deployment"
    assert normalized["handlingIdeas"][0]["title"] == "快速止血"
    assert normalized["dataFlow"]["path"][0]["component"] == "edge"
    assert normalized["analysis_method"] == "rule-based"


def test_normalize_solutions_from_text_extracts_steps():
    solutions = _normalize_solutions_from_text(
        "方案1: 缓解故障\n步骤:\n1. 扩容实例\n2. 清理连接池\n3. 观察错误率"
    )

    assert len(solutions) == 1
    assert solutions[0]["title"] == "缓解故障"
    assert solutions[0]["steps"] == ["扩容实例", "清理连接池", "观察错误率"]


def test_format_solution_text_standard_contains_context():
    rendered = _format_solution_text_standard(
        "1. rollback\n2. verify",
        summary="release regression",
        service_name="query-service",
        problem_type="5xx spike",
        severity="p1",
    )

    assert "query-service" in rendered
    assert "5xx spike" in rendered
    assert "级别=high" in rendered
