"""Auto-seed k8s_cluster targets from new source_cluster values in ClickHouse."""
from __future__ import annotations

import logging
from typing import Any, Dict, List

import requests
from clickhouse_driver import Client as ClickHouseClient

logger = logging.getLogger(__name__)


def _as_str(value: Any, default: str = "") -> str:
    return str(value) if isinstance(value, str) else default


def get_ch_client(host: str = "localhost", port: int = 9000) -> ClickHouseClient:
    """Create a ClickHouse native protocol client."""
    return ClickHouseClient(host=host, port=port)


def discover_new_clusters(ch_client) -> List[Dict[str, Any]]:
    """Find source_cluster values not yet registered as k8s_cluster targets.

    Queries ClickHouse for distinct (source_cluster, namespace) pairs and
    compares against existing target identities. Returns candidates for
    new target registration.

    Returns:
        List of target dicts ready for registration via register_target().
    """
    # Get all distinct source_cluster values seen in logs
    rows = ch_client.execute("""
        SELECT source_cluster, namespace, count() as cnt
        FROM logs.logs
        WHERE source_cluster != ''
        GROUP BY source_cluster, namespace
        ORDER BY cnt DESC
    """)

    # Get existing target identities from ClickHouse target table
    existing = ch_client.execute("""
        SELECT target_identity FROM logs.ai_runtime_v4_targets
        WHERE target_kind = 'k8s_cluster'
    """)
    existing_set = {row[0] for row in existing}

    new_targets = []
    for cluster_id, namespace, _cnt in rows:
        target_identity = f"namespace:{namespace}/cluster:{cluster_id}"
        if target_identity in existing_set:
            continue
        # Also check legacy format (namespace-only identity)
        legacy_identity = f"namespace:{namespace}"
        if legacy_identity in existing_set:
            continue
        new_targets.append({
            "target_id": f"auto-k8s-cluster-namespace-{namespace}",
            "target_kind": "k8s_cluster",
            "target_identity": target_identity,
            "cluster_id": cluster_id,
            "namespace": namespace,
            "display_name": f"{namespace} namespace ({cluster_id})",
            "description": f"auto-seeded kubernetes diagnosis target for {namespace} on {cluster_id}",
            "capabilities": ["read_logs", "restart_workload", "helm_read", "helm_mutation"],
            "credential_scope": {"namespace": namespace},
            "metadata": {
                "cluster_id": cluster_id,
                "namespace": namespace,
                "risk_tier": "high",
                "preferred_executor_profiles": ["toolbox-k8s-readonly", "toolbox-k8s-mutating"],
            },
            "status": "active",
        })
    return new_targets


def register_target(ai_service_url: str, target: Dict[str, Any]) -> bool:
    url = f"{ai_service_url.rstrip('/')}/api/v2/targets"
    try:
        r = requests.post(url, json=target, timeout=10)
        if r.status_code in (200, 201):
            logger.info("registered target: %s (%s)", target["target_identity"], r.status_code)
            return True
        logger.warning(
            "failed to register target %s: %s %s",
            target["target_identity"], r.status_code, r.text[:200],
        )
        return False
    except requests.RequestException as exc:
        logger.error("request error registering target %s: %s", target["target_identity"], exc)
        return False


def run_auto_seed(
    ch_host: str = "localhost",
    ch_port: int = 9000,
    ai_service_url: str = "http://localhost:8090",
) -> int:
    """Run auto-seed once. Returns number of new targets registered."""
    client = get_ch_client(host=ch_host, port=ch_port)
    new_targets = discover_new_clusters(client)
    registered = 0
    for target in new_targets:
        if register_target(ai_service_url, target):
            registered += 1
    if registered:
        logger.info("auto-seed: %d new k8s_cluster targets registered", registered)
    return registered
