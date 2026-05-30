"""
kb_case_content_helpers 行为测试
"""

from dataclasses import dataclass
import sys
import types

import pytest

if "fastapi" not in sys.modules:
    _fastapi_stub = types.ModuleType("fastapi")

    class _HTTPException(Exception):
        def __init__(self, status_code: int, detail=None):
            super().__init__(str(detail))
            self.status_code = status_code
            self.detail = detail

    _fastapi_stub.HTTPException = _HTTPException
    sys.modules["fastapi"] = _fastapi_stub

from ai.kb_case_content_helpers import (
    _collect_requested_content_fields,
    _require_editable_fields_for_case_content_update,
)


@dataclass
class _Req:
    problem_type: str | None = None
    severity: str | None = None
    summary: str | None = None
    service_name: str | None = None
    root_causes: list[str] | None = None
    solutions: list[str] | None = None
    solutions_text: str | None = None
    analysis_summary: str | None = None
    resolution: str | None = None
    tags: list[str] | None = None


def test_collect_requested_content_fields_keeps_legacy_semantics():
    request = _Req(
        problem_type="error",
        solutions_text="1. check logs",
        analysis_summary="summary",
    )

    assert _collect_requested_content_fields(request) == [
        "problem_type",
        "solutions",
        "analysis_summary",
    ]


def test_require_editable_fields_for_case_content_update_rejects_empty():
    with pytest.raises(Exception) as exc:
        _require_editable_fields_for_case_content_update(_Req())

    assert getattr(exc.value, "status_code", None) == 400
    assert isinstance(getattr(exc.value, "detail", None), dict)
    assert getattr(exc.value, "detail", {}).get("code") == "KBR-003"
