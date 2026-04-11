"""
JSON dict parsing helpers for LLM text outputs.
"""

import json
import re
from typing import Any, Callable, Dict, Optional


def _extract_first_json_dict(
    text: str,
    *,
    as_str: Callable[[Any, str], str],
) -> Optional[Dict[str, Any]]:
    """从混合文本中提取首个 JSON 对象。"""
    decoder = json.JSONDecoder()
    content = as_str(text)
    for index, ch in enumerate(content):
        if ch not in ("{", "["):
            continue
        try:
            parsed, _ = decoder.raw_decode(content[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _parse_llm_json_dict(
    content: str,
    *,
    as_str: Callable[[Any, str], str],
) -> Optional[Dict[str, Any]]:
    """解析 LLM 输出中的 JSON 对象，兼容 markdown 代码块。"""
    raw = as_str(content)
    if not raw:
        return None

    candidates = [raw]
    fenced_blocks = re.findall(r"```(?:json)?\s*([\s\S]*?)```", raw, flags=re.IGNORECASE)
    for block in fenced_blocks:
        block_text = as_str(block)
        if block_text:
            candidates.append(block_text)

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        parsed = _extract_first_json_dict(candidate, as_str=as_str)
        if parsed is not None:
            return parsed

    return None
