import pytest
from shared_src.context.hasher import CanonicalContextHasher


class TestCanonicalContextHasher:
    def test_canonical_hash_deterministic(self):
        """相同输入始终产出相同 hash"""
        hasher = CanonicalContextHasher()
        h1 = hasher.hash({"resource": "INSTANCE:abc-123", "state": "ACTIVE"})
        h2 = hasher.hash({"resource": "INSTANCE:abc-123", "state": "ACTIVE"})
        assert h1 == h2

    def test_canonical_hash_no_timestamp(self):
        """hash 只使用 content 字段，不依赖时间"""
        hasher = CanonicalContextHasher()
        h1 = hasher.hash({"resource": "INSTANCE:abc-123", "topology": "data1"})
        h2 = hasher.hash({"resource": "INSTANCE:abc-123", "topology": "data1"})
        assert h1 == h2

    def test_merkle_hash(self):
        """Merkle DAG 模式"""
        hasher = CanonicalContextHasher()
        topo_hash = hasher.hash({"nodes": ["A", "B", "C"]})
        state_hash = hasher.hash({"status": "ACTIVE"})
        root = hasher.merkle_hash({
            "topology": topo_hash,
            "state": state_hash,
        })
        assert root.startswith("ctx_")
        assert len(root) > 10

    def test_rfc8785_sort_keys(self):
        """RFC 8785 确定性 JSON——字典 key 排序"""
        hasher = CanonicalContextHasher()
        h1 = hasher.hash({"z": 1, "a": 2})
        h2 = hasher.hash({"a": 2, "z": 1})
        assert h1 == h2  # 排序后相同

    def test_merkle_changes_when_part_changes(self):
        """部分内容变更，Merkle root 变化"""
        hasher = CanonicalContextHasher()
        h1 = hasher.merkle_hash({"topo": "hash_a", "state": "hash_b"})
        h2 = hasher.merkle_hash({"topo": "hash_a", "state": "hash_c"})
        assert h1 != h2  # 只有 state 变了，root 也变

    def test_hash_prefix(self):
        """hash 以 ctx_ 开头"""
        hasher = CanonicalContextHasher()
        h = hasher.hash({"key": "value"})
        assert h.startswith("ctx_")

    def test_different_inputs_different_hash(self):
        hasher = CanonicalContextHasher()
        h1 = hasher.hash({"key": "value1"})
        h2 = hasher.hash({"key": "value2"})
        assert h1 != h2

    def test_nested_content(self):
        """嵌套内容的确定性 hash"""
        hasher = CanonicalContextHasher()
        h1 = hasher.hash({
            "resource": {"type": "SERVICE", "name": "nova-api"},
            "dependencies": ["rabbitmq", "neutron"],
        })
        h2 = hasher.hash({
            "resource": {"type": "SERVICE", "name": "nova-api"},
            "dependencies": ["rabbitmq", "neutron"],
        })
        assert h1 == h2
