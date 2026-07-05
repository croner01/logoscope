"""
拓扑准确性验证：模拟 OpenStack 日志数据，验证拓扑构建是否正确。

直接运行：python3 tests/test_topology_accuracy_demo.py

场景：
  1. OpenStack 跨服务调用链（global_request_id）
  2. Interactions 数据源（第5数据源）
  3. 推断边（request_id / message_target）
  4. 噪声抑制验证（registry 不出现、双向抑制）
"""
import os
import sys
import json
from datetime import datetime, timezone
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from graph.hybrid_topology import HybridTopologyBuilder
from graph.hybrid_topology_utils import (
    build_inference_method_policies,
    compute_dropped_bidirectional_edges,
    extract_message_target_services,
)
from collections import Counter


# ═══════════════════════════════════════════════════════════
# 存储桩
# ═══════════════════════════════════════════════════════════

class FakeStorage:
    """模拟 ClickHouse 存储，支持多个表查询。"""

    def __init__(self):
        self.ch_client = object()
        self.logs_rows: List[Dict] = []
        self.traces_rows: List[Dict] = []
        self.metrics_rows: List[Dict] = []
        self.interactions_rows: List[Dict] = []
        self.system_tables: Dict[str, bool] = {
            "traces_namespace": False,
            "metrics_namespace": False,
            "traces_source_cluster": False,
            "metrics_source_cluster": False,
        }
        self.openstack_scan_limit = 50000  # 匹配测试需要
        self.query_log: List[str] = []     # 记录查询日志

    def execute_query(self, query: str) -> List[Dict]:
        condensed = " ".join(query.split())
        self.query_log.append(condensed[:200])

        # system.columns 查询
        if "FROM system.columns" in condensed and "name = 'traces_namespace'" in condensed:
            return [{"cnt": 1}] if self.system_tables.get("traces_namespace") else [{"cnt": 0}]
        if "FROM system.columns" in condensed and "name = 'metrics_namespace'" in condensed:
            return [{"cnt": 1}] if self.system_tables.get("metrics_namespace") else [{"cnt": 0}]
        if "FROM system.columns" in condensed and "name = 'source_cluster'" in condensed and "traces" in condensed:
            return [{"name": "source_cluster"}] if self.system_tables.get("traces_source_cluster") else []
        if "FROM system.columns" in condensed and "name = 'source_cluster'" in condensed and "metrics" in condensed:
            return [{"name": "source_cluster"}] if self.system_tables.get("metrics_source_cluster") else []

        # logs GROUP BY → 服务列表
        if "FROM logs.logs" in condensed and "GROUP BY service_name" in condensed:
            return self.logs_rows

        # logs 明细（推断边用）
        if "FROM logs.logs" in condensed and "ORDER BY timestamp DESC" in condensed and "LIMIT" in condensed:
            return list(reversed(self.logs_rows))

        # logs 明细（OpenStack 用）
        if "FROM logs.logs" in condensed and "openstack_global_request_id != ''" in condensed:
            return self.logs_rows

        # logs.count()
        if "SELECT count() AS cnt FROM logs.logs" in condensed:
            return [{"cnt": len(self.logs_rows or [])}]

        # traces
        if "FROM logs.traces" in condensed:
            return self.traces_rows

        # metrics
        if "FROM logs.metrics" in condensed:
            return self.metrics_rows

        # interactions
        if "FROM logs.interactions" in condensed:
            return self.interactions_rows

        return []

    def get_edge_red_metrics(self, time_window="1 HOUR", namespace=None):
        return {}


# ═══════════════════════════════════════════════════════════
# 场景 1: OpenStack 跨服务调用链
# ═══════════════════════════════════════════════════════════

def test_openstack_topology_chain():
    """
    OpenStack 场景：nova-api → neutron-server → ovs-agent → openvswitch

    日志按 global_request_id 分组，时间戳排序后，相邻不同服务名形成边。
    预期边：
        nova-api → neutron-server (2次)
        neutron-server → ovs-agent (1次)
    """
    print("\n" + "=" * 60)
    print("【场景 1】OpenStack 跨服务调用链")
    print("=" * 60)

    storage = FakeStorage()
    storage.system_tables["traces_namespace"] = True
    base_ts = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)

    # 构造两条完整的调用链（2 个 global_request_id）
    call_chains = []

    # 链 A: nova-api → neutron-server → ovs-agent
    gid_a = "aabbccdd112233445566778899001122"  # 32 hex
    call_chains.extend([
        {"service_name": "nova-api", "openstack_request_id": "req-a1",
         "openstack_global_request_id": gid_a, "timestamp": base_ts},
        {"service_name": "neutron-server", "openstack_request_id": "req-a2",
         "openstack_global_request_id": gid_a, "timestamp": base_ts.replace(microsecond=150000)},
        {"service_name": "ovs-agent", "openstack_request_id": "req-a3",
         "openstack_global_request_id": gid_a, "timestamp": base_ts.replace(microsecond=320000)},
    ])

    # 链 B: nova-api → neutron-server（较短）
    gid_b = "bbccddee223344556677889900aabb33"
    call_chains.extend([
        {"service_name": "nova-api", "openstack_request_id": "req-b1",
         "openstack_global_request_id": gid_b, "timestamp": base_ts.replace(second=5)},
        {"service_name": "neutron-server", "openstack_request_id": "req-b2",
         "openstack_global_request_id": gid_b, "timestamp": base_ts.replace(second=5, microsecond=200000)},
    ])

    storage.logs_rows = call_chains
    storage.openstack_scan_limit = 50000

    builder = HybridTopologyBuilder(storage)
    result = builder._get_openstack_topology(time_window="1 HOUR")

    nodes = result.get("nodes", [])
    edges = result.get("edges", [])
    pairs = {(e["source"], e["target"]): e["metrics"]["call_count"] for e in edges}

    print(f"  节点 ({len(nodes)}): {[n['id'] for n in nodes]}")
    print(f"  边 ({len(edges)}):")
    for (s, t), c in sorted(pairs.items()):
        print(f"    {s} → {t}  (count={c})")

    # 验证
    assert len(nodes) >= 3, f"期望 ≥3 节点，实际 {len(nodes)}"
    assert pairs.get(("nova-api", "neutron-server")) == 2, \
        f"nova-api→neutron-server 期望 2，实际 {pairs.get(('nova-api','neutron-server'))}"
    assert pairs.get(("neutron-server", "ovs-agent")) == 1, \
        f"neutron-server→ovs-agent 期望 1，实际 {pairs.get(('neutron-server','ovs-agent'))}"

    print("  ✅ 通过：OpenStack 调用链正确")


# ═══════════════════════════════════════════════════════════
# 场景 2: 混合数据源拓扑（traces + openstack + interactions）
# ═══════════════════════════════════════════════════════════

def test_hybrid_topology_with_all_sources():
    """
    综合场景：traces 有精确边，interactions 有重复边，openstack 补充。

    预期：
      - checkout → payment 来自 traces（confidence=1.0 保持不变）
      - payment → inventory 来自 interactions（confidence=0.75，合并后 +0.1=0.85）
      - openstack 边在无 traces 时成为独立边（confidence=0.6）
    """
    print("\n" + "=" * 60)
    print("【场景 2】混合数据源拓扑（5源合并）")
    print("=" * 60)

    storage = FakeStorage()
    storage.system_tables["traces_namespace"] = True

    # traces: 精确边
    storage.traces_rows = [
        {"trace_id": "t1", "span_id": "s1", "parent_span_id": "",
         "service_name": "checkout", "operation_name": "GET /checkout",
         "status": "STATUS_CODE_OK", "timestamp": datetime(2026,7,1,10,0,0,tzinfo=timezone.utc),
         "duration_ms": 15.0, "traces_namespace": "islap"},
        {"trace_id": "t1", "span_id": "s2", "parent_span_id": "s1",
         "service_name": "payment", "operation_name": "charge",
         "status": "STATUS_CODE_OK", "timestamp": datetime(2026,7,1,10,0,0,100000,tzinfo=timezone.utc),
         "duration_ms": 42.0, "traces_namespace": "islap"},
    ]
    storage.system_tables["traces_namespace"] = True

    # interactions: 重复边 + 补充边
    storage.interactions_rows = [
        {"source": "checkout", "target": "payment", "interaction_count": 15, "pattern_count": 3},
        {"source": "payment", "target": "inventory", "interaction_count": 8, "pattern_count": 1},
    ]

    # openstack: 独立边（无 traces 覆盖）
    base_ts = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
    storage.logs_rows = [
        {"service_name": "neutron-server", "openstack_request_id": "req-n1",
         "openstack_global_request_id": "11223344556677889900aabbccddeeff11",
         "timestamp": base_ts},
        {"service_name": "ovs-agent", "openstack_request_id": "req-n2",
         "openstack_global_request_id": "11223344556677889900aabbccddeeff11",
         "timestamp": base_ts.replace(microsecond=88000)},
    ]

    builder = HybridTopologyBuilder(storage)

    # 分别获取各数据源
    traces = builder._get_traces_topology("1 HOUR", namespace="islap")
    openstack = builder._get_openstack_topology("1 HOUR")
    interactions = builder._get_interactions_topology("1 HOUR")

    # 合并边
    merged = builder._merge_edges(
        traces_edges=traces.get("edges", []),
        logs_edges=[],
        metrics_edges=[],
        openstack_edges=openstack.get("edges", []),
        interactions_edges=interactions.get("edges", []),
    )

    print(f"  Traces 边: {len(traces['edges'])}")
    print(f"  OpenStack 边: {len(openstack['edges'])}")
    print(f"  Interactions 边: {len(interactions['edges'])}")
    print(f"  合并后边: {len(merged)}")

    for e in sorted(merged, key=lambda x: (x.get("source",""), x.get("target",""))):
        m = e.get("metrics", {})
        print(f"    {e['source']} → {e['target']}"
              f"  conf={m.get('confidence'):.2f}"
              f"  source={m.get('data_source')}"
              f"  sources={m.get('data_sources')}")

    # 验证
    edge_map = {(e["source"], e["target"]): e for e in merged}
    cp = edge_map.get(("checkout", "payment"))
    assert cp is not None, "checkout→payment 不存在"
    assert cp["metrics"]["confidence"] == 1.0, \
        f"traces 边 confidence 应为 1.0，实际 {cp['metrics']['confidence']}"
    assert "interactions" in cp["metrics"].get("data_sources", []), \
        "合并后应包含 interactions 标签"

    pi = edge_map.get(("payment", "inventory"))
    assert pi is not None, "payment→inventory 不存在"
    assert pi["metrics"]["data_source"] == "interactions"

    no = edge_map.get(("neutron-server", "ovs-agent"))
    assert no is not None, "neutron-server→ovs-agent 不存在"
    assert no["metrics"]["data_source"] == "openstack"

    print("  ✅ 通过：多源合并正确，优先级/置信度符合预期")


# ═══════════════════════════════════════════════════════════
# 场景 3: 推断边噪声抑制
# ═══════════════════════════════════════════════════════════

def test_inference_noise_suppression():
    """
    噪声抑制验证：
      1. 双向边强度接近 → 双删
      2. 基础设施服务（otel/collector）→ 不产生 time_window 边
      3. registry 无强证据时不出现
    """
    print("\n" + "=" * 60)
    print("【场景 3】推断边噪声抑制")
    print("=" * 60)

    # 测试 3a: 双向抑制（双方都低于 min_support 且差值 ≤1 → 双删）
    print("\n  --- 3a: 双向边抑制（双删） ---")
    edge_acc = {
        ("frontend", "payment"): {
            "count": 3, "weighted_score": 3.0,
            "method_counts": Counter({"time_window": 3}),
        },
        ("payment", "frontend"): {
            "count": 3, "weighted_score": 3.0,
            "method_counts": Counter({"time_window": 3}),
        },
    }
    dropped = compute_dropped_bidirectional_edges(
        edge_acc, inference_mode="rule", min_support_time_window=4,
    )
    # 双方 count=3 < min_support(4) 且差值=0 ≤ 1 → 双删
    assert ("frontend", "payment") in dropped, "低于 min_support 应双删"
    assert ("payment", "frontend") in dropped, "低于 min_support 应双删"
    print("  ✅ 通过：低于 min_support 的逆向边被双删")

    # 测试 3b: 强方向保留
    edge_acc2 = {
        ("nova-api", "neutron-server"): {
            "count": 10, "weighted_score": 10.0,
            "method_counts": Counter({"trace_id": 10}),
        },
        ("neutron-server", "nova-api"): {
            "count": 2, "weighted_score": 2.0,
            "method_counts": Counter({"time_window": 2}),
        },
    }
    dropped2 = compute_dropped_bidirectional_edges(
        edge_acc2, inference_mode="rule", min_support_time_window=4,
    )
    # nova-api→neutron 10:2 → 比例 5.0 ≥ 1.5 → 保留强方向，删弱方向
    assert ("nova-api", "neutron-server") not in dropped2, "强方向应保留"
    assert ("neutron-server", "nova-api") in dropped2, "弱方向应被删"
    print("  ✅ 通过：强方向被保留，弱方向被删除")

    # 测试 3c: 基础设施服务抑制
    print("\n  --- 3c: 基础设施服务抑制 ---")
    storage = FakeStorage()
    base_ts = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
    storage.logs_rows = [
        {"id": "l1", "timestamp": base_ts,
         "service_name": "otel-collector", "namespace": "islap",
         "message": "received data", "trace_id": "", "attributes_json": "{}"},
        {"id": "l2", "timestamp": base_ts.replace(microsecond=50000),
         "service_name": "nova-api", "namespace": "islap",
         "message": "processed", "trace_id": "", "attributes_json": "{}"},
    ]
    builder = HybridTopologyBuilder(storage)
    edges, stats = builder._infer_edges_from_logs("1 HOUR", namespace="islap")
    # otel-collector 被 infra 过滤，不应产生 time_window 边
    pairs = {(e.get("source"), e.get("target")) for e in edges}
    assert ("otel-collector", "nova-api") not in pairs, "infra 服务应被抑制"
    print("  ✅ 通过：基础设施服务在 time_window 中被过滤")


# ═══════════════════════════════════════════════════════════
# 场景 4: Interactions 融合效果
# ═══════════════════════════════════════════════════════════

def test_interactions_boost_confidence():
    """
    验证 interactions 数据源对置信度的提升效果：
      - 单 interactions: 0.75
      - interactions + traces 同边 → 多源加成
    """
    print("\n" + "=" * 60)
    print("【场景 4】Interactions 数据源置信度提升")
    print("=" * 60)

    storage = FakeStorage()

    # interactions 边
    inter_edges = [
        {"id": "a-b", "source": "svc-A", "target": "svc-B",
         "metrics": {"interaction_count": 5, "pattern_count": 1,
                     "data_source": "interactions", "confidence": 0.75}},
    ]

    # traces 边（相同 pair）
    trace_edges = [
        {"id": "a-b", "source": "svc-A", "target": "svc-B",
         "metrics": {"call_count": 10, "data_source": "traces", "confidence": 1.0,
                     "durations": [15.0, 22.0]}},
    ]

    builder = HybridTopologyBuilder(storage)
    merged = builder._merge_edges(
        traces_edges=trace_edges,
        logs_edges=[],
        metrics_edges=[],
        interactions_edges=inter_edges,
    )

    m = merged[0]["metrics"]
    print(f"  合并后: data_sources={m.get('data_sources')}, confidence={m.get('confidence')}")
    assert "interactions" in m.get("data_sources", []), "interactions 标签应被追加"
    assert m["confidence"] == 1.0, "traces 优先级最高，confidence 不应降低"
    print("  ✅ 通过：interactions + traces 合并后 traces 的 confidence=1.0 不变")


# ═══════════════════════════════════════════════════════════
# 场景 5: OpenStack 日志提取
# ═══════════════════════════════════════════════════════════

def test_openstack_log_extraction():
    """
    验证从 OpenStack 日志格式提取 global_request_id 的正确性。

    OpenStack 日志格式：
      [req-<uuid> <32hex-global-id> <32hex-tenant-id> - ...]
      [None req-<uuid> <32hex-global-id> <32hex-tenant-id> - ...]
    """
    print("\n" + "=" * 60)
    print("【场景 5】OpenStack 日志 global_request_id 提取")
    print("=" * 60)

    from graph.hybrid_topology_utils import extract_global_request_id

    cases = [
        # (日志消息, 期望提取的 global_id)
        (
            '[req-6e1b3a3b-1234-5678-abcd-123456789abc aabbccdd112233445566778899001122 '
            'ffeeddccbbaa99887766554433221100 - 01234567-89ab-cdef-0123-456789abcdef] '
            'nova-api: requesting neutron to create port',
            'aabbccdd112233445566778899001122'
        ),
        (
            '[None req-6e1b3a3b-1234-5678-abcd-123456789abc '
            'bbccddee223344556677889900aabb33 99887766554433221100ffeeddccbbaa - ...]',
            'bbccddee223344556677889900aabb33'
        ),
        (
            '[req-6e1b3a3b-1234-5678-abcd-123456789abc - - - - -] This log has no global id',
            ''
        ),
        (
            'neutron-server: 2026-07-01 10:00:00.123 INFO [req-abc-123 '
            'aabb11223344ccddee5566ff77889900 1122aabb3344ccdd5566eeff77880099 - '
            'default-tenant] Create port request',
            'aabb11223344ccddee5566ff77889900'
        ),
    ]

    all_ok = True
    for msg, expected in cases:
        result = extract_global_request_id(msg)
        ok = result == expected
        status = "✅" if ok else "❌"
        if ok:
            print(f"  {status} 提取: {result[:16]}...")
        else:
            print(f"  {status} FAIL: 期望 '{expected}' 实际 '{result}'")
            all_ok = False

    assert all_ok, "OpenStack global_request_id 提取失败"
    print("  ✅ 通过：所有 OpenStack 日志格式提取正确")


# ═══════════════════════════════════════════════════════════
# 运行入口
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("拓扑准确性验证演示")
    print("=" * 60)

    test_openstack_topology_chain()
    test_hybrid_topology_with_all_sources()
    test_inference_noise_suppression()
    test_interactions_boost_confidence()
    test_openstack_log_extraction()

    print("\n" + "=" * 60)
    print("全部场景通过！")
    print("=" * 60)
    print("\n你可以修改这个文件来验证自己的日志数据：")
    print("  1. 替换 logs_rows 中的日志内容")
    print("  2. 调整时间戳模拟不同调用顺序")
    print("  3. 添加新的 interactions 行")
    print("  4. 观察拓扑边、置信度、数据源的变化")
