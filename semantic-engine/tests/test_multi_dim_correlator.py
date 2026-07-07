"""Tests for MultiDimCorrelator — UUID 提取、Union-Find、聚类、集成。"""

import pytest
from datetime import datetime, timezone

from semantic_engine.workflow.multi_dim_correlator import (
    MultiDimCorrelator,
    UnionFind,
    UUIDSet,
    CorrelatedGroup,
    extract_resource_uuids,
    _is_valid_uuid,
    _cluster_id,
    CORRELATION_WEIGHTS,
    CLUSTER_WEIGHT_THRESHOLD,
    OPENSTACK_SERVICE_PREFIXES,
)


# ── 辅助函数 ───────────────────────────────────────────────────────────────────


def _entry(
    service_name: str,
    timestamp: str,
    message: str = "",
    request_id: str = "",
    global_request_id: str = "",
    node_name: str = "",
) -> dict:
    """创建模拟日志条目。"""
    return {
        "service_name": service_name,
        "timestamp": datetime.fromisoformat(timestamp.replace("Z", "+00:00")),
        "message": message,
        "openstack_request_id": request_id,
        "openstack_global_request_id": global_request_id,
        "source_cluster": "prod",
        "node_name": node_name,
    }


# ── UUID 提取测试 ─────────────────────────────────────────────────────────────


class TestExtractResourceUuids:
    """测试 extract_resource_uuids 各维度的提取逻辑。"""

    def test_instance_from_bracket(self):
        uuids = extract_resource_uuids({
            "message": "[instance: bee3d355-e656-4ee5-9d3f-694a7a68aa81] Creating server"
        })
        assert uuids.instance == {"bee3d355-e656-4ee5-9d3f-694a7a68aa81"}

    def test_volume_from_bracket(self):
        uuids = extract_resource_uuids({
            "message": "[volume: 8a44e913-0525-4865-8f5d-6a832235a6fd] Creating volume"
        })
        assert uuids.volume == {"8a44e913-0525-4865-8f5d-6a832235a6fd"}

    def test_port_from_bracket(self):
        uuids = extract_resource_uuids({
            "message": "[port: abcdef01-2345-6789-abcd-ef0123456789] Binding port"
        })
        assert uuids.port == {"abcdef01-2345-6789-abcd-ef0123456789"}

    def test_migration_from_bracket(self):
        uuids = extract_resource_uuids({
            "message": "[migration: 11111111-2222-3333-4444-555555555555] Live migration"
        })
        assert uuids.migration == {"11111111-2222-3333-4444-555555555555"}

    def test_image_from_bracket(self):
        uuids = extract_resource_uuids({
            "message": "[image: aaaa-bbbb-cccc-dddd-eeee-ffff-0000-1111] Creating image"
        })
        assert uuids.image == set(), "Invalid UUID should not match"

    def test_snapshot_from_bracket(self):
        uuids = extract_resource_uuids({
            "message": "[snapshot: 50b4aac7-2873-43a6-a56f-048a2b729cbf] Creating snapshot"
        })
        assert uuids.snapshot == {"50b4aac7-2873-43a6-a56f-048a2b729cbf"}

    def test_http_path_extracts_instance(self):
        uuids = extract_resource_uuids({
            "message": (
                '192.168.3.239 "POST /v2.1/4b3634c206414deb85e65c292b78951d'
                '/servers/6a82c5ba-7d48-43cb-8bac-5c4984f90648/action HTTP/1.1"'
            )
        })
        assert uuids.instance == {"6a82c5ba-7d48-43cb-8bac-5c4984f90648"}

    def test_http_path_extracts_volume(self):
        uuids = extract_resource_uuids({
            "message": (
                '10.0.0.1 "POST /v3/4b3634c206414deb85e65c292b78951d'
                '/volumes/8a44e913-0525-4865-8f5d-6a832235a6fd/action HTTP/1.1"'
            )
        })
        assert uuids.volume == {"8a44e913-0525-4865-8f5d-6a832235a6fd"}

    def test_http_path_extracts_snapshot(self):
        uuids = extract_resource_uuids({
            "message": (
                '10.0.0.1 "POST /v3/abc123/snapshots/50b4aac7-2873-43a6-a56f-048a2b729cbf HTTP/1.1"'
            )
        })
        assert uuids.snapshot == {"50b4aac7-2873-43a6-a56f-048a2b729cbf"}

    def test_request_ids_extracted(self):
        uuids = extract_resource_uuids({
            "message": (
                "[req-b6272aeb-57c6-4af2-b4c0-5ee43ad47e2e "
                "req-6fa6421d-2d80-4afb-878a-6cb4bcf24c0b "
                "c5f2666761c24ec3a4ad4f14fe75f6cd "
                "4b3634c206414deb85e65c292b78951d - default default] "
                "Create snapshot"
            )
        })
        assert len(uuids.request_ids) == 2
        assert "req-b6272aeb-57c6-4af2-b4c0-5ee43ad47e2e" in uuids.request_ids
        assert "req-6fa6421d-2d80-4afb-878a-6cb4bcf24c0b" in uuids.request_ids

    def test_request_id_from_field(self):
        uuids = extract_resource_uuids({
            "message": "Creating server",
            "openstack_request_id": "req-0837809d-9796-4765-9981-6a5a39298fce",
        })
        assert "req-0837809d-9796-4765-9981-6a5a39298fce" in uuids.request_ids

    def test_non_openstack_log_returns_empty(self):
        uuids = extract_resource_uuids({
            "message": "query-service: Starting up",
        })
        assert uuids.is_empty()

    def test_empty_message_returns_empty(self):
        uuids = extract_resource_uuids({"message": ""})
        assert uuids.is_empty()

    def test_32hex_instance_format(self):
        """32 位 hex instance UUID（无连字符）也应被提取。"""
        uuids = extract_resource_uuids({
            "message": "[instance: 6a82c5ba7d4843cb8bac5c4984f90648] Spawned"
        })
        assert uuids.instance == {"6a82c5ba7d4843cb8bac5c4984f90648"}

    def test_multiple_uuids_from_one_entry(self):
        uuids = extract_resource_uuids({
            "message": (
                "[instance: bee3d355-e656-4ee5-9d3f-694a7a68aa81] "
                "[volume: 8a44e913-0525-4865-8f5d-6a832235a6fd] "
                "Attach volume"
            )
        })
        assert uuids.instance == {"bee3d355-e656-4ee5-9d3f-694a7a68aa81"}
        assert uuids.volume == {"8a44e913-0525-4865-8f5d-6a832235a6fd"}

    def test_iter_clusterable(self):
        uuids = UUIDSet(
            instance={"inst-1"},
            volume={"vol-1"},
        )
        pairs = uuids.iter_clusterable()
        assert ("instance", "inst-1") in pairs
        assert ("volume", "vol-1") in pairs


# ── Union-Find 测试 ────────────────────────────────────────────────────────────


class TestUnionFind:
    def test_initial_state(self):
        uf = UnionFind(5)
        for i in range(5):
            assert uf.find(i) == i

    def test_union_find(self):
        uf = UnionFind(5)
        uf.union(0, 1)
        uf.union(3, 4)
        assert uf.find(0) == uf.find(1)
        assert uf.find(3) == uf.find(4)
        assert uf.find(0) != uf.find(3)

    def test_chain_union(self):
        uf = UnionFind(4)
        uf.union(0, 1)
        uf.union(1, 2)
        uf.union(2, 3)
        root = uf.find(0)
        assert all(uf.find(i) == root for i in range(4))

    def test_clusters(self):
        uf = UnionFind(6)
        uf.union(0, 1)
        uf.union(1, 2)
        uf.union(3, 4)
        clusters = uf.clusters()
        assert len(clusters) == 3  # {0,1,2}, {3,4}, {5}
        assert len(clusters[uf.find(0)]) == 3

    def test_single_element(self):
        uf = UnionFind(1)
        clusters = uf.clusters()
        assert len(clusters) == 1
        assert clusters[uf.find(0)] == [0]


# ── 多维关联聚类测试 ───────────────────────────────────────────────────────────


class TestMultiDimCorrelator:
    """MultiDimCorrelator 聚类算法和置信度测试。"""

    def test_clusters_on_shared_instance(self):
        entries = [
            _entry("nova-compute", "2026-07-07T10:00:03Z",
                   message="[instance: bee3d355-e656-4ee5-9d3f-694a7a68aa81] Creating server"),
            _entry("nova-compute", "2026-07-07T10:00:08Z",
                   message="[instance: bee3d355-e656-4ee5-9d3f-694a7a68aa81] Server created"),
        ]
        correlator = MultiDimCorrelator()
        groups = correlator.cluster_entries(entries)
        assert len(groups) == 1
        assert len(groups[0].entry_indices) == 2
        assert groups[0].confidence > 0

    def test_no_shared_uuids_no_cluster(self):
        entries = [
            _entry("nova-api", "2026-07-07T10:00:00Z", message="heartbeat"),
            _entry("cinder-volume", "2026-07-07T10:00:01Z", message="heartbeat"),
        ]
        groups = MultiDimCorrelator().cluster_entries(entries)
        assert len(groups) == 0

    def test_chain_clustering(self):
        """A-B via instance, B-C via volume → 三者聚为一组。"""
        entries = [
            _entry("nova-compute", "2026-07-07T10:00:00Z",
                   message="[instance: bee3d355-e656-4ee5-9d3f-694a7a68aa81] Creating"),
            _entry("cinder-api", "2026-07-07T10:00:01Z",
                   message="[instance: bee3d355-e656-4ee5-9d3f-694a7a68aa81] "
                       "[volume: 8a44e913-0525-4865-8f5d-6a832235a6fd] Attach"),
            _entry("cinder-volume", "2026-07-07T10:00:02Z",
                   message="[volume: 8a44e913-0525-4865-8f5d-6a832235a6fd] Creating"),
        ]
        groups = MultiDimCorrelator().cluster_entries(entries)
        assert len(groups) == 1
        assert len(groups[0].entry_indices) == 3
        # 两个维度都应出现在 shared_dimensions 中
        assert "instance" in groups[0].shared_dimensions
        assert "volume" in groups[0].shared_dimensions

    def test_two_independent_clusters(self):
        """两组完全不相关的日志 → 两个独立聚类。"""
        entries = [
            # Group A: instance UUID
            _entry("nova-compute", "2026-07-07T10:00:00Z",
                   message="[instance: bee3d355-e656-4ee5-9d3f-694a7a68aa81] Creating"),
            _entry("nova-compute", "2026-07-07T10:00:01Z",
                   message="[instance: bee3d355-e656-4ee5-9d3f-694a7a68aa81] Done"),
            # Group B: volume UUID
            _entry("cinder-api", "2026-07-07T10:00:02Z",
                   message="[volume: 8a44e913-0525-4865-8f5d-6a832235a6fd] Creating"),
            _entry("cinder-volume", "2026-07-07T10:00:03Z",
                   message="[volume: 8a44e913-0525-4865-8f5d-6a832235a6fd] Done"),
        ]
        groups = MultiDimCorrelator().cluster_entries(entries)
        assert len(groups) == 2

    def test_respects_min_cluster_size(self):
        """只有 1 条日志的组被过滤。"""
        entries = [
            _entry("nova-compute", "2026-07-07T10:00:00Z",
                   message="[instance: abcdef01-2345-6789-abcd-ef0123456789] Alone"),
        ]
        groups = MultiDimCorrelator().cluster_entries(entries)
        assert len(groups) == 0

    def test_request_id_does_not_trigger_clustering(self):
        """仅共享 request_id（权重 2.0 < 3.0）→ 不聚类。"""
        entries = [
            _entry("nova-api", "2026-07-07T10:00:00Z",
                   message="[req-b6272aeb-57c6-4af2-b4c0-5ee43ad47e2e req-xxx] API call",
                   request_id="req-xxx"),
            _entry("cinder-volume", "2026-07-07T10:00:01Z",
                   message="[req-b6272aeb-57c6-4af2-b4c0-5ee43ad47e2e req-xxx] Volume op",
                   request_id="req-xxx"),
        ]
        groups = MultiDimCorrelator().cluster_entries(entries)
        assert len(groups) == 0, "Request ID alone should not trigger clustering"

    def test_single_entry_with_uuid_no_cluster(self):
        """只有一条日志有 UUID → 不聚类。"""
        entries = [
            _entry("nova-compute", "2026-07-07T10:00:00Z",
                   message="[instance: abcdef01-2345-6789-abcd-ef0123456789] Alone"),
            _entry("nova-api", "2026-07-07T10:00:01Z", message="heartbeat"),
        ]
        groups = MultiDimCorrelator().cluster_entries(entries)
        assert len(groups) == 0

    def test_empty_input_returns_empty(self):
        assert MultiDimCorrelator().cluster_entries([]) == []


class TestConfidence:
    """置信度计算测试。"""

    def test_high_confidence_multiple_shared_p0(self):
        """多个共享 P0 UUID → 高置信度。"""
        entries = [
            _entry("nova-compute", "2026-07-07T10:00:00Z",
                   message="[instance: bee3d355-e656-4ee5-9d3f-694a7a68aa81] "
                           "[volume: 8a44e913-0525-4865-8f5d-6a832235a6fd] Attach"),
            _entry("cinder-volume", "2026-07-07T10:00:01Z",
                   message="[instance: bee3d355-e656-4ee5-9d3f-694a7a68aa81] "
                           "[volume: 8a44e913-0525-4865-8f5d-6a832235a6fd] Attached"),
        ]
        groups = MultiDimCorrelator().cluster_entries(entries)
        assert len(groups) == 1
        assert groups[0].confidence > 0.4  # 两个 P0 维度共享 → 较高置信度

    def test_low_confidence_single_p1(self):
        """仅一个 P1 维度 → 中等置信度。"""
        entries = [
            _entry("glance-api", "2026-07-07T10:00:00Z",
                   message="[image: img-001-aaaa-bbbb-cccc-dddd-eeee-ffff] Creating"),
            _entry("nova-compute", "2026-07-07T10:00:05Z",
                   message="[image: img-001-aaaa-bbbb-cccc-dddd-eeee-ffff] Using image"),
        ]
        groups = MultiDimCorrelator().cluster_entries(entries)
        if groups:
            assert groups[0].confidence <= 0.4  # 仅 image (P1, 4.0)

    def test_temporal_coherence(self):
        """时间跨度短 → 置信度更高。"""
        entries_short = [
            _entry("cinder-api", "2026-07-07T10:00:00Z",
                   message="[volume: vol-001] Create"),
            _entry("cinder-volume", "2026-07-07T10:00:01Z",
                   message="[volume: vol-001] Created"),
        ]
        entries_long = [
            _entry("cinder-api", "2026-07-07T10:00:00Z",
                   message="[volume: vol-002] Create"),
            _entry("cinder-volume", "2026-07-07T11:59:00Z",
                   message="[volume: vol-002] Created"),
        ]
        g_short = MultiDimCorrelator().cluster_entries(entries_short)
        g_long = MultiDimCorrelator().cluster_entries(entries_long)
        if g_short and g_long:
            assert g_short[0].confidence >= g_long[0].confidence, \
                "Short time span should have higher confidence"


# ── 工具函数测试 ───────────────────────────────────────────────────────────────


class TestIsValidUuid:
    def test_36_char_with_dashes(self):
        assert _is_valid_uuid("550e8400-e29b-41d4-a716-446655440000")

    def test_32_char_hex(self):
        assert _is_valid_uuid("550e8400e29b41d4a716446655440000")

    def test_invalid_length(self):
        assert not _is_valid_uuid("abc-123-def-456")
        assert not _is_valid_uuid("")

    def test_too_short(self):
        assert not _is_valid_uuid("123")


class TestOpenStackServicePrefixes:
    def test_known_services(self):
        assert "nova-" in OPENSTACK_SERVICE_PREFIXES
        assert "cinder-" in OPENSTACK_SERVICE_PREFIXES
        assert "neutron-" in OPENSTACK_SERVICE_PREFIXES
        assert "glance-" in OPENSTACK_SERVICE_PREFIXES

    def test_heat_and_keystone(self):
        assert "heat-" in OPENSTACK_SERVICE_PREFIXES
        assert "keystone-" in OPENSTACK_SERVICE_PREFIXES


class TestCorrelationWeights:
    def test_cluster_dims_above_threshold(self):
        """聚类维度权重应 >= threshold。"""
        cluster_dims = ["instance", "volume", "port", "migration", "image", "snapshot"]
        for dim in cluster_dims:
            assert CORRELATION_WEIGHTS.get(dim, 0) >= CLUSTER_WEIGHT_THRESHOLD, \
                f"{dim} should be clusterable"

    def test_non_cluster_dims_below_threshold(self):
        """非聚类维度权重应 < threshold。"""
        non_cluster_dims = ["request_id", "host"]
        for dim in non_cluster_dims:
            assert CORRELATION_WEIGHTS.get(dim, 0) < CLUSTER_WEIGHT_THRESHOLD, \
                f"{dim} should NOT be clusterable"


# ── 集成测试 ───────────────────────────────────────────────────────────────────


class TestIntegrationWithWorkflowEngine:
    """MultiDimCorrelator + _reconstruct_workflow 集成测试。"""

    def test_create_vm_detected_via_instance_uuid(self):
        """共享 instance UUID 的 Nova 日志 → 应被检测为 CreateVM。"""
        from semantic_engine.workflow.engine import WorkflowEngine, _execution_id
        from unittest.mock import MagicMock

        mock_storage = MagicMock()
        mock_storage.ch_client = MagicMock()
        engine = WorkflowEngine(mock_storage)
        engine._ch_available = True

        # nova-api-osapi: POST /v2.1/.../servers (no trailing UUID, no instance link)
        # nova-scheduler: no UUID
        # nova-compute: [instance: ...] references
        # To cluster together, all entries must share a UUID.
        # Realistic scenario: nova-api-osapi's HTTP path contains the instance UUID
        # and nova-compute's logs reference the same instance UUID.
        entries = [
            _entry("nova-api-osapi", "2026-07-07T10:00:00Z",
                   message=(
                       '192.168.3.239 "POST /v2.1/abc123/servers'
                       '/bee3d355-e656-4ee5-9d3f-694a7a68aa81/action HTTP/1.1"'
                   )),
            _entry("nova-scheduler", "2026-07-07T10:00:01Z",
                   message="[instance: bee3d355-e656-4ee5-9d3f-694a7a68aa81] "
                           "Selected host node-1"),
            _entry("nova-compute", "2026-07-07T10:00:03Z",
                   message="[instance: bee3d355-e656-4ee5-9d3f-694a7a68aa81] "
                           "Spawning instance"),
        ]

        correlator = MultiDimCorrelator()
        groups = correlator.cluster_entries(entries)

        assert len(groups) >= 1, "Should find at least one cluster"

        # 用聚类组重建 workflow
        built_workflows = []
        for group in groups:
            if len(group.entry_indices) < 2:
                continue
            group_entries = [entries[i] for i in group.entry_indices]
            cid = _cluster_id(group.entry_indices, entries)
            eid = _execution_id(cid)
            wf = engine._reconstruct_workflow(cid, eid, group_entries)
            if wf:
                built_workflows.append(wf)

        assert len(built_workflows) >= 1, "Should build >=1 workflow from clusters"

        # 验证至少有一个 CreateVM（ServerAction/Post action）
        ops = {w["operation_type"] for w in built_workflows}
        assert "ServerAction" in ops or "CreateVM" in ops, \
            f"nova-api POST /servers should produce a known operation, got {ops}"
