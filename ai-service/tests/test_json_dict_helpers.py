"""
json_dict_helpers 行为测试
"""

from ai.json_dict_helpers import _parse_llm_json_dict


def _as_str(value, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def test_parse_llm_json_dict_from_fenced_block():
    payload = _parse_llm_json_dict(
        "analysis:\n```json\n{\"a\": 1, \"b\": \"x\"}\n```",
        as_str=_as_str,
    )
    assert payload == {"a": 1, "b": "x"}


def test_parse_llm_json_dict_extracts_first_embedded_object():
    payload = _parse_llm_json_dict(
        "result: {\"ok\": true, \"count\": 2} trailing text",
        as_str=_as_str,
    )
    assert payload == {"ok": True, "count": 2}


def test_parse_llm_json_dict_returns_none_for_non_json():
    payload = _parse_llm_json_dict("no valid payload", as_str=_as_str)
    assert payload is None
