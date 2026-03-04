"""Tests for extracted hybrid topology utility helpers."""

from collections import Counter
from datetime import datetime, timezone

from graph import hybrid_topology_utils


def test_parse_message_target_patterns_filters_invalid_values():
    assert hybrid_topology_utils.parse_message_target_patterns("url,kv,invalid") == {"url", "kv"}
    assert hybrid_topology_utils.parse_message_target_patterns("") == {"url"}


def test_sanitize_interval_and_alias_map():
    assert hybrid_topology_utils.sanitize_interval("30 minute", default_value="1 HOUR") == "30 MINUTE"
    assert hybrid_topology_utils.sanitize_interval("1 HOUR; DROP TABLE x", default_value="1 HOUR") == "1 HOUR"
    assert hybrid_topology_utils.parse_service_alias_map("pay=payment;inv=inventory") == {
        "pay": "payment",
        "inv": "inventory",
    }


def test_extract_host_candidates_and_timestamp_parser():
    candidates = hybrid_topology_utils.extract_host_candidates_from_token(
        "outbound|8080||orders.svc.cluster.local:8080/path"
    )
    assert "orders.svc.cluster.local" in candidates

    parsed = hybrid_topology_utils.timestamp_to_datetime("2026-03-01T00:00:00Z")
    assert isinstance(parsed, datetime)
    assert parsed.tzinfo == timezone.utc


def test_match_service_from_host_respects_exclude_and_alias():
    known = {
        "orders": "orders",
        "payment-gateway": "payment-gateway",
    }
    assert (
        hybrid_topology_utils.match_service_from_host(
            "orders.svc.cluster.local:8080",
            known,
            exclude_hosts={"localhost"},
        )
        == "orders"
    )
    assert (
        hybrid_topology_utils.match_service_from_host(
            "payment_gateway.internal",
            known,
            exclude_hosts=set(),
        )
        == "payment-gateway"
    )
    assert (
        hybrid_topology_utils.match_service_from_host(
            "localhost",
            known,
            exclude_hosts={"localhost"},
        )
        == ""
    )


def test_extract_message_target_services_from_multi_patterns():
    known = {
        "orders": "orders",
        "inventory": "inventory",
        "checkout": "checkout",
    }
    message = (
        "upstream_cluster=outbound|8080||orders.svc.cluster.local "
        "target=inventory:8080 "
        "calling https://checkout.svc.cluster.local/pay"
    )

    targets = hybrid_topology_utils.extract_message_target_services(
        message,
        known,
        enabled=True,
        patterns={"url", "kv", "proxy"},
        max_targets_per_log=3,
        exclude_hosts={"localhost"},
    )
    resolved = {svc for svc, _hint in targets}

    assert {"orders", "inventory", "checkout"}.issubset(resolved)


def test_merge_nodes_and_edges_preserve_data_source_semantics():
    merged_nodes = hybrid_topology_utils.merge_nodes(
        traces_nodes=[{"id": "checkout", "metrics": {"trace_count": 3}}],
        logs_nodes=[{"id": "checkout", "metrics": {"log_count": 9}}, {"id": "payment", "metrics": {"log_count": 5}}],
        metrics_nodes=[{"id": "checkout", "metrics": {"metric_count": 7}}],
    )
    node_map = {item["id"]: item for item in merged_nodes}
    assert node_map["checkout"]["metrics"]["trace_count"] == 3
    assert node_map["checkout"]["metrics"]["log_count"] == 9
    assert node_map["checkout"]["metrics"]["metric_count"] == 7
    assert set(node_map["checkout"]["metrics"]["data_sources"]) == {"traces", "logs", "metrics"}
    assert node_map["payment"]["metrics"]["data_source"] == "logs"

    merged_edges = hybrid_topology_utils.merge_edges(
        traces_edges=[
            {"source": "checkout", "target": "payment", "metrics": {"confidence": 0.8}},
        ],
        logs_edges=[
            {"source": "checkout", "target": "payment", "metrics": {"reason": "heuristic"}},
            {"source": "checkout", "target": "inventory", "metrics": {"confidence": 0.3}},
        ],
        metrics_edges=[
            {"source": "checkout", "target": "payment", "metrics": {}},
        ],
        metrics_boost=0.1,
    )
    edge_map = {(item["source"], item["target"]): item for item in merged_edges}
    cp_metrics = edge_map[("checkout", "payment")]["metrics"]
    assert cp_metrics["confidence"] == 0.9
    assert "logs_heuristic" in cp_metrics["data_sources"]
    assert "metrics" in cp_metrics["data_sources"]
    assert cp_metrics["reason"] == "heuristic"
    assert edge_map[("checkout", "inventory")]["metrics"]["data_source"] == "logs_heuristic"


def test_merge_nodes_prefers_non_default_namespace_when_sources_conflict():
    merged_nodes = hybrid_topology_utils.merge_nodes(
        traces_nodes=[{
            "id": "ai-service",
            "metrics": {
                "trace_count": 3,
                "namespace": "",
                "service_namespace": "",
            },
        }],
        logs_nodes=[{
            "id": "ai-service",
            "namespace": "islap",
            "metrics": {
                "log_count": 9,
                "namespace": "islap",
                "service_namespace": "islap",
            },
        }],
        metrics_nodes=[],
    )
    node_map = {item["id"]: item for item in merged_nodes}
    ai_node = node_map["ai-service"]
    assert ai_node["namespace"] == "islap"
    assert ai_node["metrics"]["namespace"] == "islap"
    assert ai_node["metrics"]["service_namespace"] == "islap"

    merged_nodes = hybrid_topology_utils.merge_nodes(
        traces_nodes=[{
            "id": "checkout",
            "namespace": "prod-blue",
            "metrics": {
                "namespace": "prod-blue",
                "service_namespace": "prod-blue",
            },
        }],
        logs_nodes=[{
            "id": "checkout",
            "namespace": "default",
            "metrics": {
                "namespace": "default",
                "service_namespace": "default",
            },
        }],
        metrics_nodes=[],
    )
    node_map = {item["id"]: item for item in merged_nodes}
    assert node_map["checkout"]["metrics"]["service_namespace"] == "prod-blue"


def test_apply_aggregated_edge_metrics_only_overwrites_empty_values():
    edges = [
        {
            "source": "checkout",
            "target": "payment",
            "metrics": {"p95": 0.0, "p99": 120.0, "call_count": None},
        }
    ]
    aggregated = {
        "checkout->payment": {
            "p95": 95.0,
            "p99": 199.0,
            "call_count": 42,
        }
    }

    hybrid_topology_utils.apply_aggregated_edge_metrics(edges, aggregated)
    metrics = edges[0]["metrics"]
    assert metrics["p95"] == 95.0
    assert metrics["p99"] == 120.0
    assert metrics["call_count"] == 42


def test_apply_contract_schema_uses_node_and_edge_contract_hooks():
    nodes = [{"id": "checkout"}, {"id": "payment"}]
    edges = [{"source": "checkout", "target": "payment"}]

    def _node_contract(node):
        return {"id": node["id"], "node_key": f"svc::{node['id']}"}

    def _edge_contract(edge, source_node=None, target_node=None):
        return {
            "source": edge["source"],
            "target": edge["target"],
            "edge_key": f"{source_node['id']}->{target_node['id']}",
        }

    contract_nodes, contract_edges = hybrid_topology_utils.apply_contract_schema(
        nodes,
        edges,
        apply_node_contract_fn=_node_contract,
        apply_edge_contract_fn=_edge_contract,
    )

    assert contract_nodes[0]["node_key"] == "svc::checkout"
    assert contract_edges[0]["edge_key"] == "checkout->payment"


def test_service_relation_heuristics_and_reasons():
    assert hybrid_topology_utils.is_service_pair_related("frontend", "backend") is True
    assert hybrid_topology_utils.is_service_pair_related("order-service", "postgres-db") is True
    assert hybrid_topology_utils.is_service_pair_related("checkout", "inventory") is False

    assert hybrid_topology_utils.should_call("frontend", "payment") is True
    assert hybrid_topology_utils.should_call("redis-cache", "checkout") is False
    assert hybrid_topology_utils.should_call("registry", "frontend") is False

    assert hybrid_topology_utils.get_relation_reason("frontend", "postgres-db") == "frontend_pattern, data_access_pattern"
    assert hybrid_topology_utils.get_relation_reason("backend", "registry") == "backend_pattern, image_pull_pattern"
    assert hybrid_topology_utils.get_relation_reason("worker", "checkout") == "heuristic_pattern"


def test_get_data_sources_only_keeps_non_empty_sources():
    sources = hybrid_topology_utils.get_data_sources(
        traces_data={"nodes": [{"id": "a"}], "edges": []},
        logs_data={"nodes": [], "edges": []},
        metrics_data={"nodes": [], "edges": [{"source": "a", "target": "b"}]},
    )
    assert sources == ["traces", "metrics"]


def test_extract_request_id_from_attrs_and_message():
    assert hybrid_topology_utils.extract_request_id({"request_id": "req-123456"}) == "req-123456"
    assert (
        hybrid_topology_utils.extract_request_id({}, "x-request-id=req-abcdef12")
        == "req-abcdef12"
    )
    assert hybrid_topology_utils.extract_request_id({}, "no request id here") == ""


def test_dedup_edges_by_metric_score_prefers_stronger_edge():
    edges = [
        {
            "source": "frontend",
            "target": "payment",
            "metrics": {"call_count": 5, "confidence": 0.3},
        },
        {
            "source": "frontend",
            "target": "payment",
            "metrics": {"call_count": 8, "confidence": 0.9},
        },
        {
            "source": "frontend",
            "target": "order",
            "metrics": {"call_count": 1, "confidence": 0.2},
        },
    ]

    deduped = hybrid_topology_utils.dedup_edges_by_metric_score(edges)
    edge_map = {(item["source"], item["target"]): item for item in deduped}

    assert len(deduped) == 2
    assert edge_map[("frontend", "payment")]["metrics"]["confidence"] == 0.9
    assert edge_map[("frontend", "order")]["metrics"]["call_count"] == 1


def test_resolve_inference_runtime_settings_defaults_and_overrides():
    defaults = hybrid_topology_utils.resolve_inference_runtime_settings(
        inference_mode=None,
        default_inference_mode="rule",
        message_target_enabled=None,
        default_message_target_enabled=True,
        message_target_patterns=None,
        default_message_target_patterns={"url", "kv"},
        resolve_message_target_patterns_override_fn=lambda _value: None,
        message_target_min_support=None,
        default_message_target_min_support=2,
        message_target_max_per_log=None,
        default_message_target_max_per_log=3,
        resolve_inference_mode_override_fn=lambda value, default: default if value is None else str(value),
    )
    assert defaults["effective_inference_mode"] == "rule"
    assert defaults["effective_message_target_enabled"] is True
    assert defaults["effective_patterns"] == {"url", "kv"}
    assert defaults["effective_min_support"] == 2
    assert defaults["effective_max_per_log"] == 3
    assert defaults["method_name"] == "request_id_then_trace_id_then_message_target_then_time_window"

    overrides = hybrid_topology_utils.resolve_inference_runtime_settings(
        inference_mode="hybrid_score",
        default_inference_mode="rule",
        message_target_enabled=False,
        default_message_target_enabled=True,
        message_target_patterns="rpc,proxy",
        default_message_target_patterns={"url"},
        resolve_message_target_patterns_override_fn=lambda value: {"rpc", "proxy"} if value else None,
        message_target_min_support=999,
        default_message_target_min_support=2,
        message_target_max_per_log=0,
        default_message_target_max_per_log=3,
        resolve_inference_mode_override_fn=lambda value, default: value or default,
    )
    assert overrides["effective_inference_mode"] == "hybrid_score"
    assert overrides["effective_message_target_enabled"] is False
    assert overrides["effective_patterns"] == {"rpc", "proxy"}
    assert overrides["effective_min_support"] == 20
    assert overrides["effective_max_per_log"] == 1
    assert overrides["method_name"] == "request_id_then_trace_id_then_time_window_hybrid_score"


def test_build_inference_empty_stats_contract():
    stats = hybrid_topology_utils.build_inference_empty_stats(
        method_name="request_id_then_trace_id_then_time_window",
        message_target_enabled=False,
        inference_mode="rule",
        message_target_patterns={"url", "kv"},
        message_target_min_support=2,
        message_target_max_per_log=3,
    )

    assert stats["total_candidates"] == 0
    assert stats["request_id_groups"] == 0
    assert stats["trace_id_groups"] == 0
    assert stats["message_target_edges"] == 0
    assert stats["method"] == "request_id_then_trace_id_then_time_window"
    assert stats["message_target_enabled"] is False
    assert stats["inference_mode"] == "rule"
    assert stats["message_target_patterns"] == ["kv", "url"]
    assert stats["message_target_min_support"] == 2
    assert stats["message_target_max_per_log"] == 3


def test_partition_prepared_inference_records_groups_by_priority():
    prepared = [
        {"id": "1", "service_name": "checkout", "request_id": "req-1", "trace_id": "trace-a"},
        {"id": "2", "service_name": "payment", "request_id": "", "trace_id": "trace-a"},
        {"id": "3", "service_name": "inventory", "request_id": None, "trace_id": ""},
    ]

    buckets = hybrid_topology_utils.partition_prepared_inference_records(prepared)

    assert list(buckets["request_groups"].keys()) == ["req-1"]
    assert list(buckets["trace_groups"].keys()) == ["trace-a"]
    assert [item["id"] for item in buckets["fallback_records"]] == ["3"]


def test_compute_dropped_bidirectional_edges_keeps_stronger_direction():
    edge_acc = {
        ("checkout", "payment"): {
            "count": 8,
            "weighted_score": 8.0,
            "method_counts": Counter({"time_window": 8}),
        },
        ("payment", "checkout"): {
            "count": 4,
            "weighted_score": 4.0,
            "method_counts": Counter({"time_window": 4}),
        },
    }

    dropped = hybrid_topology_utils.compute_dropped_bidirectional_edges(
        edge_acc,
        inference_mode="rule",
        min_support_time_window=4,
    )
    assert ("payment", "checkout") in dropped
    assert ("checkout", "payment") not in dropped


def test_build_inference_stats_contract_and_avg_score():
    stats = hybrid_topology_utils.build_inference_stats(
        total_candidates=10,
        request_id_groups=2,
        request_id_edges=3,
        trace_id_groups=2,
        trace_id_edges=2,
        message_target_edges=1,
        time_window_edges=0,
        dropped_bidirectional_edges=1,
        filtered_edges=5,
        method_name="request_id_then_trace_id_then_message_target_then_time_window",
        message_target_enabled=True,
        inference_mode="hybrid_score",
        message_target_patterns={"url", "kv"},
        message_target_min_support=2,
        message_target_max_per_log=3,
        evidence_sufficiency_scores=[80.0, 70.0, 90.0],
    )

    assert stats["total_candidates"] == 10
    assert stats["filtered_edges"] == 5
    assert stats["message_target_patterns"] == ["kv", "url"]
    assert stats["avg_evidence_sufficiency_score"] == 80.0
    assert stats["evidence_sparse"] is False


def test_build_inference_method_policies_contract():
    policies = hybrid_topology_utils.build_inference_method_policies(
        min_support_request_id=1,
        min_support_trace_id=2,
        min_support_message_target=3,
        min_support_time_window=4,
    )
    assert policies["min_support"]["request_id"] == 1
    assert policies["min_support"]["trace_id"] == 2
    assert policies["min_support"]["message_target"] == 3
    assert policies["min_support"]["time_window"] == 4
    assert policies["base_confidence"]["request_id"] == 0.80
    assert policies["reason"]["message_target"] == "message_endpoint_pattern"


def test_compute_directional_consistency_in_rule_and_hybrid_modes():
    edge_acc = {
        ("checkout", "payment"): {
            "count": 10,
            "weighted_score": 12.0,
        },
        ("payment", "checkout"): {
            "count": 5,
            "weighted_score": 7.0,
        },
    }
    rule_value = hybrid_topology_utils.compute_directional_consistency(
        edge_acc,
        source="checkout",
        target="payment",
        support_value=10.0,
        inference_mode="rule",
    )
    hybrid_value = hybrid_topology_utils.compute_directional_consistency(
        edge_acc,
        source="checkout",
        target="payment",
        support_value=16.0,
        inference_mode="hybrid_score",
    )

    assert rule_value == 10.0 / (10.0 + 5.0)
    assert hybrid_value == 16.0 / (16.0 + (5.0 + 7.0 * 0.5))


def test_build_inference_confidence_explain_variants():
    explain_rule = hybrid_topology_utils.build_inference_confidence_explain(
        method="trace_id",
        inference_mode="rule",
        support_value=2.0,
        min_support=1.0,
        dominant_ratio=1.0,
        namespace_consistency=1.0,
        temporal_stability=1.0,
        directional_consistency=1.0,
    )
    assert explain_rule == "trace_id matched"

    explain_hybrid = hybrid_topology_utils.build_inference_confidence_explain(
        method="message_target",
        inference_mode="hybrid_score",
        support_value=3.5,
        min_support=2.0,
        dominant_ratio=0.75,
        namespace_consistency=0.66,
        temporal_stability=0.50,
        directional_consistency=0.8,
    )
    assert explain_hybrid.startswith("message url host matched; support=3.50/2.00;")
    assert "dominance=0.75" in explain_hybrid
    assert "direction=0.80" in explain_hybrid


def test_compute_rule_mode_evidence_sufficiency_diversity_bonus():
    low_diversity = hybrid_topology_utils.compute_rule_mode_evidence_sufficiency(
        count=2,
        base_support=4,
        method_count_size=1,
    )
    high_diversity = hybrid_topology_utils.compute_rule_mode_evidence_sufficiency(
        count=2,
        base_support=4,
        method_count_size=2,
    )
    assert low_diversity == 49.5
    assert high_diversity == 62.5


def test_resolve_dominant_inference_method_and_support_value_modes():
    method = hybrid_topology_utils.resolve_dominant_inference_method(
        Counter({"request_id": 5, "trace_id": 2})
    )
    fallback = hybrid_topology_utils.resolve_dominant_inference_method({}, default="time_window")
    support_rule = hybrid_topology_utils.compute_support_value(
        count=6,
        weighted_score=8.0,
        inference_mode="rule",
    )
    support_hybrid = hybrid_topology_utils.compute_support_value(
        count=6,
        weighted_score=8.0,
        inference_mode="hybrid_score",
    )

    assert method == "request_id"
    assert fallback == "time_window"
    assert support_rule == 6.0
    assert support_hybrid == 10.0


def test_compute_inference_feature_ratios_defaults_and_density():
    item = {
        "method_counts": Counter({"message_target": 3, "time_window": 1}),
        "namespace_match_total": 4,
        "namespace_match_hits": 3,
    }
    ratios = hybrid_topology_utils.compute_inference_feature_ratios(
        item,
        count=4,
        weighted_score=2.0,
        dominant_method="message_target",
    )

    assert ratios["dominant_count"] == 3
    assert ratios["method_count_size"] == 2
    assert ratios["dominant_ratio"] == 0.75
    assert ratios["diversity_ratio"] == 2.0 / 3.0
    assert ratios["namespace_consistency"] == 0.75
    assert ratios["weighted_density"] == 0.5
    assert ratios["method_breakdown"] == {"message_target": 3, "time_window": 1}

    default_ratios = hybrid_topology_utils.compute_inference_feature_ratios(
        {"method_counts": Counter()},
        count=0,
        weighted_score=0.0,
        dominant_method="time_window",
    )
    assert default_ratios["namespace_consistency"] == 0.55


def test_build_inferred_edge_payload_contract_fields():
    payload = hybrid_topology_utils.build_inferred_edge_payload(
        source="frontend",
        target="payment",
        count=7,
        confidence=0.8123,
        reason="request_id_correlation",
        dominant_method="request_id",
        method_breakdown={"request_id": 7},
        evidence_chain=[{"request_id": "req-1"}],
        confidence_explain="request_id matched",
        evidence_sufficiency_score=88.45,
        inference_mode="rule",
        support_value=7.0,
        min_support_required=1.0,
        namespace_consistency=0.9,
        temporal_stability=0.8,
        directional_consistency=1.0,
        last_seen=datetime(2026, 3, 1, 9, 0, 0, tzinfo=timezone.utc),
    )

    assert payload["id"] == "frontend-payment-inferred"
    assert payload["metrics"]["confidence"] == 0.812
    assert payload["metrics"]["reason"] == "request_id_correlation"
    assert payload["metrics"]["inference_method_breakdown"] == {"request_id": 7}
    assert payload["metrics"]["evidence_sufficiency_score"] == 88.45
    assert payload["metrics"]["last_seen"] == "2026-03-01T09:00:00+00:00"


def test_compute_inference_confidence_and_evidence_rule_mode():
    result = hybrid_topology_utils.compute_inference_confidence_and_evidence(
        inference_mode="rule",
        dominant_method="trace_id",
        support_value=5.0,
        dynamic_min_support=2.0,
        dominant_ratio=0.8,
        diversity_ratio=0.5,
        namespace_consistency=0.75,
        temporal_stability=0.7,
        weighted_density=1.0,
        directional_consistency=0.9,
        count=5,
        base_support=2,
        method_count_size=1,
        method_base_confidence={"trace_id": 0.66},
    )

    assert result["confidence"] == 0.735
    assert result["evidence_sufficiency_score"] == 87.0


def test_compute_inference_confidence_and_evidence_hybrid_mode_with_callback():
    callback_calls = []

    def _score_hybrid_edge(**kwargs):
        callback_calls.append(kwargs)
        return {"confidence": 0.9123, "evidence_score": 77.129}

    result = hybrid_topology_utils.compute_inference_confidence_and_evidence(
        inference_mode="hybrid_score",
        dominant_method="request_id",
        support_value=9.0,
        dynamic_min_support=3.0,
        dominant_ratio=1.0,
        diversity_ratio=0.66,
        namespace_consistency=0.9,
        temporal_stability=0.8,
        weighted_density=1.2,
        directional_consistency=0.95,
        count=9,
        base_support=1,
        method_count_size=2,
        method_base_confidence={"request_id": 0.8},
        score_hybrid_edge_fn=_score_hybrid_edge,
    )

    assert len(callback_calls) == 1
    assert callback_calls[0]["method"] == "request_id"
    assert callback_calls[0]["support_value"] == 9.0
    assert result["confidence"] == 0.9123
    assert result["evidence_sufficiency_score"] == 77.13


def test_evaluate_inference_edge_filters_when_support_below_threshold():
    edge_acc = {
        ("frontend", "payment"): {
            "count": 4,
            "weighted_score": 6.0,
            "method_counts": Counter({"trace_id": 4}),
            "namespace_match_total": 1,
            "namespace_match_hits": 1,
            "temporal_gaps": [0.1, 0.2],
            "evidence_chain": [{"trace_id": "t-1"}],
            "last_seen": datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc),
        }
    }
    item = edge_acc[("frontend", "payment")]

    result = hybrid_topology_utils.evaluate_inference_edge(
        edge_acc=edge_acc,
        source="frontend",
        target="payment",
        item=item,
        inference_mode="rule",
        service_log_volume={"frontend": 100},
        method_min_support={"trace_id": 2, "time_window": 4},
        method_base_confidence={"trace_id": 0.66},
        method_reason={"trace_id": "trace_id_correlation"},
        default_min_support=4,
        estimate_dynamic_support_fn=lambda **_kwargs: 8,
        temporal_stability_fn=lambda _gaps: 0.8,
        score_hybrid_edge_fn=None,
    )

    assert result is None


def test_evaluate_inference_edge_builds_payload_in_hybrid_mode():
    edge_acc = {
        ("frontend", "payment"): {
            "count": 6,
            "weighted_score": 8.0,
            "method_counts": Counter({"request_id": 6}),
            "namespace_match_total": 2,
            "namespace_match_hits": 2,
            "temporal_gaps": [0.05, 0.04],
            "evidence_chain": [{"request_id": "r-1"}],
            "last_seen": datetime(2026, 3, 1, 11, 0, 0, tzinfo=timezone.utc),
        },
        ("payment", "frontend"): {
            "count": 1,
            "weighted_score": 1.0,
            "method_counts": Counter({"time_window": 1}),
            "namespace_match_total": 1,
            "namespace_match_hits": 1,
        },
    }
    item = edge_acc[("frontend", "payment")]
    callback_calls = []

    def _score_hybrid_edge(**kwargs):
        callback_calls.append(kwargs)
        return {"confidence": 0.9, "evidence_score": 66.666}

    result = hybrid_topology_utils.evaluate_inference_edge(
        edge_acc=edge_acc,
        source="frontend",
        target="payment",
        item=item,
        inference_mode="hybrid_score",
        service_log_volume={"frontend": 200},
        method_min_support={"request_id": 1, "time_window": 4},
        method_base_confidence={"request_id": 0.8},
        method_reason={"request_id": "request_id_correlation"},
        default_min_support=4,
        estimate_dynamic_support_fn=lambda **_kwargs: 3,
        temporal_stability_fn=lambda _gaps: 0.92,
        score_hybrid_edge_fn=_score_hybrid_edge,
    )

    assert len(callback_calls) == 1
    assert result is not None
    assert result["evidence_sufficiency_score"] == 66.67
    assert result["payload"]["id"] == "frontend-payment-inferred"
    assert result["payload"]["metrics"]["confidence"] == 0.9
    assert result["payload"]["metrics"]["reason"] == "request_id_correlation"
    assert result["payload"]["metrics"]["last_seen"] == "2026-03-01T11:00:00+00:00"


def test_accumulate_group_sequence_edges_hybrid_mode_uses_group_evidence_and_weight():
    records = [
        {"id": "1", "service_name": "frontend", "namespace": "prod", "ts": datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc)},
        {"id": "2", "service_name": "frontend", "namespace": "prod", "ts": datetime(2026, 3, 1, 10, 0, 1, tzinfo=timezone.utc)},
        {"id": "3", "service_name": "payment", "namespace": "prod", "ts": datetime(2026, 3, 1, 10, 0, 2, tzinfo=timezone.utc)},
    ]
    captured_calls = []

    def _add_inferred_fn(**kwargs):
        captured_calls.append(kwargs)
        return True

    added = hybrid_topology_utils.accumulate_group_sequence_edges(
        groups={"req-1": records, "req-empty": [records[0]]},
        group_field_name="request_id",
        method="request_id",
        inference_mode="hybrid_score",
        hybrid_weight=1.2,
        dedup_sequence_fn=hybrid_topology_utils.dedup_service_sequence,
        add_inferred_fn=_add_inferred_fn,
        normalize_namespace_fn=lambda value: str(value or "").strip().lower(),
    )

    assert added == 1
    assert len(captured_calls) == 1
    assert captured_calls[0]["method"] == "request_id"
    assert captured_calls[0]["weight"] == 1.2
    assert captured_calls[0]["evidence"]["request_id"] == "req-1"
    assert captured_calls[0]["delta_sec"] == 2.0


def test_accumulate_group_sequence_edges_rule_mode_uses_default_weight():
    records = [
        {"id": "11", "service_name": "frontend", "namespace": "prod", "ts": datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc)},
        {"id": "12", "service_name": "inventory", "namespace": "prod", "ts": datetime(2026, 3, 1, 10, 0, 1, tzinfo=timezone.utc)},
    ]
    captured_calls = []

    def _add_inferred_fn(**kwargs):
        captured_calls.append(kwargs)
        return True

    added = hybrid_topology_utils.accumulate_group_sequence_edges(
        groups={"trace-1": records},
        group_field_name="trace_id",
        method="trace_id",
        inference_mode="rule",
        hybrid_weight=1.05,
        dedup_sequence_fn=hybrid_topology_utils.dedup_service_sequence,
        add_inferred_fn=_add_inferred_fn,
        normalize_namespace_fn=lambda value: str(value or "").strip().lower(),
    )

    assert added == 1
    assert captured_calls[0]["weight"] == 1.0
    assert captured_calls[0]["evidence"]["trace_id"] == "trace-1"


def test_accumulate_message_target_edges_applies_evidence_and_weight_by_mode():
    prepared = [
        {
            "id": "m-1",
            "service_name": "frontend",
            "message": "call payment",
            "ts": datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc),
        },
        {
            "id": "m-2",
            "service_name": "payment",
            "message": "self call should be skipped",
            "ts": datetime(2026, 3, 1, 12, 0, 1, tzinfo=timezone.utc),
        },
    ]

    def _extract_targets(**kwargs):
        if kwargs["message"] == "call payment":
            return [("payment", "url_host"), ("frontend", "self")]
        return [("payment", "self")]

    captured_rule_calls = []
    captured_hybrid_calls = []

    def _add_inferred_rule(**kwargs):
        captured_rule_calls.append(kwargs)
        return True

    def _add_inferred_hybrid(**kwargs):
        captured_hybrid_calls.append(kwargs)
        return True

    added_rule = hybrid_topology_utils.accumulate_message_target_edges(
        prepared=prepared,
        inference_mode="rule",
        extract_message_target_services_fn=_extract_targets,
        add_inferred_fn=_add_inferred_rule,
        patterns={"url", "kv"},
        max_targets_per_log=3,
    )
    added_hybrid = hybrid_topology_utils.accumulate_message_target_edges(
        prepared=prepared,
        inference_mode="hybrid_score",
        extract_message_target_services_fn=_extract_targets,
        add_inferred_fn=_add_inferred_hybrid,
        patterns={"url", "kv"},
        max_targets_per_log=3,
    )

    assert added_rule == 1
    assert len(captured_rule_calls) == 1
    assert captured_rule_calls[0]["source"] == "frontend"
    assert captured_rule_calls[0]["target"] == "payment"
    assert captured_rule_calls[0]["method"] == "message_target"
    assert captured_rule_calls[0]["weight"] == 1.0
    assert captured_rule_calls[0]["evidence"]["message_hint"] == "url_host"

    assert added_hybrid == 1
    assert len(captured_hybrid_calls) == 1
    assert captured_hybrid_calls[0]["method"] == "message_target"
    assert captured_hybrid_calls[0]["weight"] == 1.1


def test_accumulate_time_window_fallback_edges_hybrid_mode_scores_ranked_candidates():
    records = [
        {
            "id": "l-1",
            "service_name": "frontend",
            "namespace": "prod",
            "message": "request upstream payment",
            "ts": datetime(2026, 3, 1, 13, 0, 0, tzinfo=timezone.utc),
        },
        {
            "id": "l-2",
            "service_name": "payment",
            "namespace": "prod",
            "message": "received request",
            "ts": datetime(2026, 3, 1, 13, 0, 0, 300000, tzinfo=timezone.utc),
        },
        {
            "id": "l-3",
            "service_name": "inventory",
            "namespace": "prod",
            "message": "received request",
            "ts": datetime(2026, 3, 1, 13, 0, 0, 700000, tzinfo=timezone.utc),
        },
    ]
    captured_calls = []

    def _add_inferred_fn(**kwargs):
        captured_calls.append(kwargs)
        return True

    added = hybrid_topology_utils.accumulate_time_window_fallback_edges(
        fallback_records=records,
        inference_mode="hybrid_score",
        max_candidates_per_log=3,
        max_delta_sec=1.0,
        is_likely_outbound_message_fn=lambda text: "upstream" in text,
        is_likely_inbound_message_fn=lambda text: "received" in text,
        add_inferred_fn=_add_inferred_fn,
        normalize_namespace_fn=lambda value: str(value or "").strip().lower(),
    )

    assert added == 3
    assert len(captured_calls) == 3
    first = captured_calls[0]
    assert first["source"] == "frontend"
    assert first["target"] == "payment"
    assert first["method"] == "time_window"
    assert first["evidence"]["candidate_rank"] == 1
    assert first["weight"] == first["evidence"]["window_score"]
    assert first["namespace_match"] is True
    assert round(first["delta_sec"], 1) == 0.3


def test_accumulate_time_window_fallback_edges_rule_mode_uses_adjacent_pairs_only():
    records = [
        {
            "id": "r-1",
            "service_name": "frontend",
            "namespace": "prod",
            "message": "start",
            "ts": datetime(2026, 3, 1, 14, 0, 0, tzinfo=timezone.utc),
        },
        {
            "id": "r-2",
            "service_name": "payment",
            "namespace": "prod",
            "message": "handle",
            "ts": datetime(2026, 3, 1, 14, 0, 0, 200000, tzinfo=timezone.utc),
        },
        {
            "id": "r-3",
            "service_name": "inventory",
            "namespace": "prod",
            "message": "handle",
            "ts": datetime(2026, 3, 1, 14, 0, 2, tzinfo=timezone.utc),
        },
    ]
    captured_calls = []

    def _add_inferred_fn(**kwargs):
        captured_calls.append(kwargs)
        return True

    added = hybrid_topology_utils.accumulate_time_window_fallback_edges(
        fallback_records=records,
        inference_mode="rule",
        max_candidates_per_log=5,
        max_delta_sec=1.0,
        is_likely_outbound_message_fn=lambda _text: False,
        is_likely_inbound_message_fn=lambda _text: False,
        add_inferred_fn=_add_inferred_fn,
        normalize_namespace_fn=lambda value: str(value or "").strip().lower(),
    )

    assert added == 1
    assert len(captured_calls) == 1
    assert captured_calls[0]["source"] == "frontend"
    assert captured_calls[0]["target"] == "payment"
    assert captured_calls[0]["weight"] == 1.0
    assert captured_calls[0]["method"] == "time_window"
    assert round(captured_calls[0]["delta_sec"], 1) == 0.2
