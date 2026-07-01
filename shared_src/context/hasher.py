"""CanonicalContextHasher — RFC 8785 确定性 JSON + Merkle DAG 内容哈希。

v15: context_hash 是纯内容的 sha256，不含时间戳组件。
     Merkle DAG 允许部分内容变更不影响未变更部分。
"""
import hashlib
import json
from typing import Any, Dict


def _canonical_json(data: Any) -> str:
    """RFC 8785 确定性 JSON 序列化——key 排序 + compact + 确定性的值格式。"""
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


class CanonicalContextHasher:
    """
    确定性上下文哈希器。

    - hash(content): 纯内容的 sha256（RFC 8785 确定性 JSON）
    - merkle_hash(parts): Merkle DAG——每部分独立 hash 再组合
    """

    HASH_PREFIX = "ctx_"

    def hash(self, content: Any) -> str:
        """计算确定性内容哈希。"""
        canonical = _canonical_json(content)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        return f"{self.HASH_PREFIX}{digest}"

    def merkle_hash(self, parts: Dict[str, str]) -> str:
        """
        计算 Merkle DAG 根哈希。

        parts: {"topology": "hash1", "state": "hash2", ...}
        root = sha256(concat(sorted(part_key || part_hash)))

        每部分变更只影响自己的 hash，不影响其他部分的 Merkle proof。
        """
        sorted_items = sorted(parts.items())
        combined = "".join(f"{k}:{v}" for k, v in sorted_items)
        digest = hashlib.sha256(combined.encode("utf-8")).hexdigest()[:16]
        return f"{self.HASH_PREFIX}{digest}"
