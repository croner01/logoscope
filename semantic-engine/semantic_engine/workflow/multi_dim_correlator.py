"""MultiDimCorrelator — 基于多维 UUID 关联的 Workflow 聚类引擎。

用 Union-Find 在共享的 P0-P1 UUID（instance/volume/port/migration/image）上
做图聚类，替代单一 global_request_id 分组，使得即使没有 global_request_id
传播的服务（如 Nova）也能通过共享资源 UUID 被关联为 workflow。

关联评分模型：
    P0 (5.0): Instance UUID, Volume UUID, Port UUID
    P1 (4.0): Migration UUID, Image UUID
    P2 (2.0): Request ID（仅补充，不参与聚类）
    P3 (1.0): Host（仅补充，不参与聚类）
    聚类阈值: >= 3.0
"""

import hashlib
import logging
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ── UUID 正则 ──────────────────────────────────────────────────────────────────

# 括号模式: [resource_type: uuid]
_INSTANCE_BRACKET_RE = re.compile(
    r'\[instance:\s*([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}|[a-f0-9]{32})\]',
    re.IGNORECASE,
)
_VOLUME_BRACKET_RE = re.compile(
    r'\[volume:\s*([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}|[a-f0-9]{32})\]',
    re.IGNORECASE,
)
_PORT_BRACKET_RE = re.compile(
    r'\[port:\s*([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}|[a-f0-9]{32})\]',
    re.IGNORECASE,
)
_MIGRATION_BRACKET_RE = re.compile(
    r'\[migration:\s*([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}|[a-f0-9]{32})\]',
    re.IGNORECASE,
)
_IMAGE_BRACKET_RE = re.compile(
    r'\[image:\s*([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}|[a-f0-9]{32})\]',
    re.IGNORECASE,
)
_SNAPSHOT_BRACKET_RE = re.compile(
    r'\[snapshot:\s*([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}|[a-f0-9]{32})\]',
    re.IGNORECASE,
)

# HTTP 请求行: "METHOD /path HTTP/1.1"
_HTTP_RE = re.compile(r'"((?:POST|GET|PUT|DELETE))\s+(\S+)\s+HTTP/\d\.\d"')

# 路径中的 UUID（36 位带连字符或 32 位纯 hex）
_PATH_UUID_RE = re.compile(
    r'/([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}|[a-f0-9]{32})',
    re.IGNORECASE,
)
_UUID36_FULL = re.compile(
    r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$',
    re.IGNORECASE,
)
_UUID32_FULL = re.compile(r'^[a-f0-9]{32}$', re.IGNORECASE)

# Request ID: req-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
_REQ_ID_RE = re.compile(
    r'(req-[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})',
    re.IGNORECASE,
)

# ── HTTP 路径中的 resource type → dimension 映射 ──────────────────────────────

# 从右向左匹配: 找到 /{resource}/{uuid} 模式
# key = resource type in path, value = dimension name
_RESOURCE_TYPE_TO_DIM: Dict[str, str] = {
    'servers': 'instance',
    'volumes': 'volume',
    'snapshots': 'snapshot',
    'images': 'image',
    'os-volumes-attachments': 'volume_attachment',
    'os-ports': 'port',
    'os-migrations': 'migration',
    'ports': 'port',
    'migrations': 'migration',
}

# ── OpenStack 服务名前缀（用于日志查询过滤）─────────────────────────────────

OPENSTACK_SERVICE_PREFIXES = (
    'nova-', 'cinder-', 'neutron-', 'glance-', 'heat-',
    'keystone-', 'ironic-', 'manila-', 'designate-',
)

# ── 关联权重配置 ──────────────────────────────────────────────────────────────

CORRELATION_WEIGHTS: Dict[str, float] = {
    'instance': 5.0,      # P0
    'volume': 5.0,        # P0
    'port': 5.0,          # P0
    'migration': 4.0,     # P1
    'image': 4.0,         # P1
    'snapshot': 3.0,      # P1
    'volume_attachment': 3.0,  # P1
    # 以下维度不参与聚类（weight < 3.0），仅用于补充元信息
    'request_id': 2.0,    # P2
    'host': 1.0,          # P3
}

CLUSTER_WEIGHT_THRESHOLD = 3.0


# ── 数据结构 ───────────────────────────────────────────────────────────────────


@dataclass
class UUIDSet:
    """单条日志条目中提取的所有资源 UUID。"""
    instance: Set[str] = field(default_factory=set)
    volume: Set[str] = field(default_factory=set)
    port: Set[str] = field(default_factory=set)
    migration: Set[str] = field(default_factory=set)
    image: Set[str] = field(default_factory=set)
    snapshot: Set[str] = field(default_factory=set)
    request_ids: Set[str] = field(default_factory=set)

    def is_empty(self) -> bool:
        """没有任何维度有值。"""
        return not any([
            self.instance, self.volume, self.port,
            self.migration, self.image, self.snapshot,
        ])

    def iter_clusterable(self) -> List[Tuple[str, str]]:
        """返回所有参与聚类的 (dimension, uuid) 对（权重 >= 3.0）。"""
        pairs: List[Tuple[str, str]] = []
        for uuid in self.instance:
            pairs.append(('instance', uuid))
        for uuid in self.volume:
            pairs.append(('volume', uuid))
        for uuid in self.port:
            pairs.append(('port', uuid))
        for uuid in self.migration:
            pairs.append(('migration', uuid))
        for uuid in self.image:
            pairs.append(('image', uuid))
        for uuid in self.snapshot:
            pairs.append(('snapshot', uuid))
        return pairs


@dataclass
class CorrelatedGroup:
    """聚类结果：一组相关联的日志条目。

    Attributes:
        entry_indices: 在原始 entries 列表中的索引
        shared_dimensions: 组内共享的维度 → UUID 集合（被 >= 2 条共享）
        confidence: 聚类置信度 (0.0 - 1.0)
    """
    entry_indices: List[int]
    shared_dimensions: Dict[str, Set[str]] = field(default_factory=dict)
    confidence: float = 0.0


# ── Union-Find ─────────────────────────────────────────────────────────────────


class UnionFind:
    """Disjoint Set Union（并查集），带路径压缩和按秩合并。"""

    def __init__(self, n: int):
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x: int) -> int:
        """查找根节点（带路径压缩）。"""
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, x: int, y: int) -> None:
        """合并两个集合（按秩合并）。"""
        px, py = self.find(x), self.find(y)
        if px == py:
            return
        if self.rank[px] < self.rank[py]:
            px, py = py, px
        self.parent[py] = px
        if self.rank[px] == self.rank[py]:
            self.rank[px] += 1

    def clusters(self) -> Dict[int, List[int]]:
        """返回 {root: [member_indices]} 映射。"""
        result: Dict[int, List[int]] = {}
        for i in range(len(self.parent)):
            root = self.find(i)
            result.setdefault(root, []).append(i)
        return result


# ── 工具函数 ───────────────────────────────────────────────────────────────────


def _is_valid_uuid(val: str) -> bool:
    """验证 UUID 格式是否正确。"""
    if len(val) == 36:
        return bool(_UUID36_FULL.match(val))
    if len(val) == 32:
        return bool(_UUID32_FULL.match(val))
    return False


def _cluster_id(entry_indices: List[int], entries: List[Dict]) -> str:
    """从聚类成员生成确定性 cluster_id。

    用所有条目 id 的 MD5 前缀作为 cluster_id。
    """
    raw = "|".join([
        str(entries[i].get("id", "")) for i in sorted(entry_indices)[:10]
    ])
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def _get_field(row: Dict, key: str, default: str = "") -> str:
    """从 dict 行中安全取字段值。"""
    return str(row.get(key, default) or default)


# ── UUID 提取 ──────────────────────────────────────────────────────────────────


def extract_resource_uuids(entry: Dict) -> UUIDSet:
    """从单条日志条目提取所有可见的资源 UUID 和 request ID。

    Args:
        entry: 日志条目的 dict，须包含 'message' 字段，可选 'service_name'、
               'node_name'、'host_ip'、'openstack_request_id'

    Returns:
        UUIDSet: 各维度的 UUID 集合
    """
    result = UUIDSet()
    message = _get_field(entry, "message")
    if not message:
        return result

    # ── 1. 括号模式提取 ────────────────────────────────────────────────────
    # [instance: uuid]
    for m in _INSTANCE_BRACKET_RE.finditer(message):
        val = m.group(1).strip()
        if _is_valid_uuid(val):
            result.instance.add(val)

    # [volume: uuid]
    for m in _VOLUME_BRACKET_RE.finditer(message):
        val = m.group(1).strip()
        if _is_valid_uuid(val):
            result.volume.add(val)

    # [port: uuid]
    for m in _PORT_BRACKET_RE.finditer(message):
        val = m.group(1).strip()
        if _is_valid_uuid(val):
            result.port.add(val)

    # [migration: uuid]
    for m in _MIGRATION_BRACKET_RE.finditer(message):
        val = m.group(1).strip()
        if _is_valid_uuid(val):
            result.migration.add(val)

    # [image: uuid]
    for m in _IMAGE_BRACKET_RE.finditer(message):
        val = m.group(1).strip()
        if _is_valid_uuid(val):
            result.image.add(val)

    # [snapshot: uuid]
    for m in _SNAPSHOT_BRACKET_RE.finditer(message):
        val = m.group(1).strip()
        if _is_valid_uuid(val):
            result.snapshot.add(val)

    # ── 2. HTTP 路径提取 ────────────────────────────────────────────────────
    # 从 "METHOD /v2.1/{tenant}/{resource}/{uuid}/... HTTP/1.1" 中提取
    for http_match in _HTTP_RE.finditer(message):
        path = http_match.group(2)
        # 从路径中从右向左提取 UUID，识别 resource type
        path_parts = path.strip("/").split("/")
        for i, part in enumerate(path_parts):
            # 检查 part 是否已知 resource type
            if part in _RESOURCE_TYPE_TO_DIM:
                # 下一个部分应该是 UUID
                if i + 1 < len(path_parts):
                    candidate = path_parts[i + 1]
                    if _is_valid_uuid(candidate):
                        dim = _RESOURCE_TYPE_TO_DIM[part]
                        getattr(result, dim).add(candidate)

    # ── 3. Request ID ──────────────────────────────────────────────────────
    for m in _REQ_ID_RE.finditer(message):
        result.request_ids.add(m.group(1))

    # 补充：从行的 openstack_request_id 字段
    rid = _get_field(entry, "openstack_request_id")
    if rid and rid.startswith("req-"):
        result.request_ids.add(rid)

    return result


# ── MultiDimCorrelator ─────────────────────────────────────────────────────────


class MultiDimCorrelator:
    """多维关联聚类引擎。

    从日志条目提取多个维度的 UUID，用 Union-Find 在共享的 P0-P1 UUID 上
    做图聚类，替代单一 global_request_id 分组。

    用法:
        correlator = MultiDimCorrelator()
        groups = correlator.cluster_entries(entries)
        for group in groups:
            print(f"cluster confidence={group.confidence}, "
                  f"{len(group.entry_indices)} entries")
    """

    def __init__(
        self,
        cluster_weight_threshold: float = CLUSTER_WEIGHT_THRESHOLD,
        min_cluster_size: int = 2,
    ):
        self.threshold = cluster_weight_threshold
        self.min_cluster_size = min_cluster_size
        # 参与聚类的维度 = weight >= threshold
        self.cluster_dims = [
            dim for dim, w in CORRELATION_WEIGHTS.items()
            if w >= self.threshold
        ]

    # ── 公共 API ──────────────────────────────────────────────────────────

    def cluster_entries(self, entries: List[Dict]) -> List[CorrelatedGroup]:
        """对日志条目列表进行多维关联聚类。

        Args:
            entries: 日志条目列表，每条须包含 message 等字段

        Returns:
            List[CorrelatedGroup]: 聚类后的组（已过滤 < min_cluster_size 的组）
        """
        if not entries:
            return []

        # 1. 提取每条日志的所有 UUID
        extracted = [extract_resource_uuids(e) for e in entries]

        # 2. 构建倒排索引: (dim, uuid) → [entry_indices]
        inverted = self._build_inverted_index(extracted)

        # 3. Union-Find 聚类
        uf = UnionFind(len(entries))
        self._cluster(uf, inverted)
        raw_clusters = uf.clusters()

        # 4. 构建 CorrelatedGroup
        groups: List[CorrelatedGroup] = []
        for root, indices in raw_clusters.items():
            if len(indices) < self.min_cluster_size:
                continue

            group = CorrelatedGroup(entry_indices=sorted(indices))
            group.shared_dimensions = self._compute_shared_dimensions(
                extracted, indices
            )
            group.confidence = self._compute_confidence(
                entries, extracted, indices
            )
            groups.append(group)

        groups.sort(key=lambda g: g.confidence, reverse=True)
        return groups

    # ── 内部方法 ──────────────────────────────────────────────────────────

    def _build_inverted_index(
        self, extracted: List[UUIDSet],
    ) -> Dict[str, Dict[str, List[int]]]:
        """构建倒排索引: dimension → uuid_value → [entry_indices]。

        仅索引参与聚类的维度（weight >= threshold）。
        """
        inverted: Dict[str, Dict[str, List[int]]] = {}
        for dim in self.cluster_dims:
            inverted[dim] = defaultdict(list)

        for i, uuids in enumerate(extracted):
            for dim, uuid_val in uuids.iter_clusterable():
                if dim in inverted:
                    inverted[dim][uuid_val].append(i)

        # 过滤只出现在 1 条日志中的 UUID（不产生聚类效果）
        for dim in list(inverted.keys()):
            for uuid_val in list(inverted[dim].keys()):
                if len(inverted[dim][uuid_val]) < 2:
                    del inverted[dim][uuid_val]
            if not inverted[dim]:
                del inverted[dim]

        return inverted

    def _cluster(
        self, uf: UnionFind,
        inverted: Dict[str, Dict[str, List[int]]],
    ) -> None:
        """用 Union-Find 合并共享 UUID 的条目。"""
        for dim, uuid_map in inverted.items():
            for uuid_val, indices in uuid_map.items():
                if len(indices) >= 2:
                    first = indices[0]
                    for idx in indices[1:]:
                        uf.union(first, idx)

    def _compute_shared_dimensions(
        self, extracted: List[UUIDSet], indices: List[int],
    ) -> Dict[str, Set[str]]:
        """计算组内被 >= 2 条日志共享的维度。"""
        # UUID 出现频次统计
        freq: Dict[str, Dict[str, int]] = {}
        for dim in CORRELATION_WEIGHTS:
            freq[dim] = defaultdict(int)

        for i in indices:
            uuids = extracted[i]
            for dim, uuid_val in uuids.iter_clusterable():
                freq[dim][uuid_val] += 1

        result: Dict[str, Set[str]] = {}
        for dim, val_count in freq.items():
            shared = {v for v, c in val_count.items() if c >= 2}
            if shared:
                result[dim] = shared
        return result

    def _compute_confidence(
        self, entries: List[Dict],
        extracted: List[UUIDSet],
        indices: List[int],
    ) -> float:
        """计算聚类置信度 (0.0 - 1.0)。

        因子:
        - 占权重比: 共享维度的总权重 / 所有维度的总权重
        - 时间连贯性: 时间跨度越短置信度越高 (指数衰减)
        - 服务多样性: 组内不同服务数量（更多服务 = 更完整的 workflow）
        """
        if len(indices) < 2:
            return 0.0

        # 1. 权重占比
        total_weight = sum(CORRELATION_WEIGHTS.values())
        shared = self._compute_shared_dimensions(extracted, indices)
        earned = sum(CORRELATION_WEIGHTS.get(dim, 0) for dim in shared)
        weight_ratio = earned / total_weight if total_weight > 0 else 0

        # 2. 时间连贯性
        timestamps = []
        for i in indices:
            ts = entries[i].get("timestamp")
            if ts:
                timestamps.append(ts)
        temporal_score = 0.5
        if len(timestamps) >= 2:
            # 简单地用字符串排序近似（ISO 格式可排序）
            timestamps.sort()
            span_seconds = 0.0
            try:
                # 尝试将 ISO 字符串转为 datetime
                from datetime import datetime
                dt_list = []
                for t in timestamps:
                    if isinstance(t, datetime):
                        dt_list.append(t)
                    elif isinstance(t, str):
                        dt_list.append(
                            datetime.fromisoformat(t.replace('Z', '+00:00'))
                        )
                if len(dt_list) >= 2:
                    span = (max(dt_list) - min(dt_list)).total_seconds()
                    span_seconds = span if span > 0 else 0
            except Exception:
                pass
            # 典型 workflow < 300s → 高置信度; > 3600s → 衰减到低置信度
            temporal_score = math.exp(-span_seconds / 600.0)

        # 3. 服务多样性
        services = set()
        for i in indices:
            svc = _get_field(entries[i], "service_name")
            if svc:
                services.add(svc)
        service_score = min(1.0, len(services) / 5.0)

        # 4. 综合评分
        confidence = (
            0.50 * weight_ratio +
            0.20 * temporal_score +
            0.30 * service_score
        )
        return round(min(1.0, max(0.0, confidence)), 4)

    # ── 统计 ──────────────────────────────────────────────────────────────

    def summarize(
        self, entries: List[Dict], groups: List[CorrelatedGroup],
    ) -> Dict[str, int]:
        """生成聚类统计摘要。"""
        return {
            "total_entries": len(entries),
            "total_clusters": len(groups),
            "clustered_entries": sum(len(g.entry_indices) for g in groups),
            "singleton_entries": len(entries) - sum(
                len(g.entry_indices) for g in groups
            ),
        }
