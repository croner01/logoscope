"""Scope auto-detection and target resolution skills."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class TargetKind(str):
    """Target kind classification."""
    K8S_CLUSTER = "k8s_cluster"
    K8S_NAMESPACE = "k8s_namespace"
    K8S_POD = "k8s_pod"
    K8S_SERVICE = "k8s_service"
    HOST_NODE = "host_node"
    DATABASE = "database"
    CLICKHOUSE = "clickhouse"
    MARIADB = "mariadb"
    OPENSTACK_PROJECT = "openstack_project"
    HTTP_ENDPOINT = "http_endpoint"
    RUNTIME_NODE = "runtime_node"


class TargetScope(BaseModel):
    """Resolved execution target scope."""
    target_kind: str = TargetKind.RUNTIME_NODE
    target_identity: str = "runtime:local"
    namespace: Optional[str] = None
    service_name: Optional[str] = None
    database: Optional[str] = None
    labels: List[str] = []
    resource: Optional[str] = None
    node_name: Optional[str] = None
    confidence: float = 0.0
    metadata: Dict[str, Any] = {}


@dataclass
class ScopeDetectionResult:
    """Result of scope detection analysis."""
    detected_scope: TargetScope
    confidence: float
    evidence: List[str]
    suggestions: List[str]


class ScopeAutoDetector:
    """
    Automatically detects execution scope from log content and context.
    """

    # Patterns for detecting scope from logs
    K8S_PATTERNS = [
        (r"namespace[:\s=]+['\"]?([a-z0-9_-]+)", "namespace"),
        (r"pod[s]?[:\s=]+['\"]?([a-z0-9_-]+/[a-z0-9_-]+)", "pod"),
        (r"deployment[s]?[:\s=]+['\"]?([a-z0-9_-]+)", "deployment"),
        (r"service[s]?[:\s=]+['\"]?([a-z0-9_-]+)", "service"),
        (r"container[s]?[:\s=]+['\"]?([a-z0-9_-]+)", "container"),
        (r"islap", "namespace"),  # Default namespace
    ]

    DATABASE_PATTERNS = [
        (r"database[:\s=]+['\"]?([a-z0-9_]+)", "database"),
        (r"table[:\s=]+['\"]?([a-z0-9_.]+)", "table"),
        (r"clickhouse", "clickhouse"),
        (r"mariadb", "mariadb"),
        (r"mysql", "mariadb"),
    ]

    HOST_PATTERNS = [
        (r"host[name]?[:\s=]+['\"]?([a-z0-9_.-]+)", "hostname"),
        (r"node[name]?[:\s=]+['\"]?([a-z0-9_.-]+)", "nodename"),
        (r"server[:\s=]+['\"]?([a-z0-9_.-]+)", "server"),
        (r"ip[:\s=]+['\"]?(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})", "ip"),
    ]

    def detect_from_log(self, log_content: str) -> ScopeDetectionResult:
        """Detect scope from log content."""
        evidence = []
        scope = TargetScope()
        max_confidence = 0.0

        # Try K8s patterns
        for pattern, scope_type in self.K8S_PATTERNS:
            match = re.search(pattern, log_content, re.IGNORECASE)
            if match:
                value = match.group(1)
                evidence.append(f"Detected {scope_type}: {value}")
                max_confidence = max(max_confidence, 0.7)

                if scope_type == "namespace":
                    scope.namespace = value
                    scope.target_kind = TargetKind.K8S_NAMESPACE
                elif scope_type == "pod":
                    scope.resource = value
                    scope.target_kind = TargetKind.K8S_POD
                elif scope_type == "service":
                    scope.service_name = value
                    scope.target_kind = TargetKind.K8S_SERVICE

        # Try database patterns
        for pattern, scope_type in self.DATABASE_PATTERNS:
            match = re.search(pattern, log_content, re.IGNORECASE)
            if match:
                value = match.group(1)
                evidence.append(f"Detected {scope_type}: {value}")
                max_confidence = max(max_confidence, 0.8)

                if scope_type == "database":
                    scope.database = value
                    scope.target_kind = TargetKind.DATABASE
                elif value.lower() == "clickhouse":
                    scope.target_kind = TargetKind.CLICKHOUSE
                elif value.lower() in ("mariadb", "mysql"):
                    scope.target_kind = TargetKind.MARIADB

        # Try host patterns
        for pattern, scope_type in self.HOST_PATTERNS:
            match = re.search(pattern, log_content, re.IGNORECASE)
            if match:
                value = match.group(1)
                evidence.append(f"Detected {scope_type}: {value}")
                max_confidence = max(max_confidence, 0.6)

                scope.node_name = value
                scope.target_kind = TargetKind.HOST_NODE

        scope.confidence = max_confidence

        suggestions = []
        if not scope.namespace:
            suggestions.append("Consider specifying namespace with -n flag")
        if not scope.service_name:
            suggestions.append("Consider adding service label selector with -l app=<service>")

        return ScopeDetectionResult(
            detected_scope=scope,
            confidence=max_confidence,
            evidence=evidence,
            suggestions=suggestions,
        )

    def resolve_target(
        self,
        scope: TargetScope,
        command: str,
    ) -> Dict[str, Any]:
        """
        Resolve target for command execution.

        Returns executor profile and target configuration.
        """
        resolved = {
            "target_kind": scope.target_kind,
            "target_identity": scope.target_identity,
            "executor_type": "local_process",
            "executor_profile": "local-default",
            "requires_write_permission": False,
        }

        # Determine executor based on target kind
        if scope.target_kind in (TargetKind.K8S_CLUSTER, TargetKind.K8S_NAMESPACE,
                                  TargetKind.K8S_POD, TargetKind.K8S_SERVICE):
            resolved["executor_type"] = "sandbox_pod"
            resolved["executor_profile"] = "toolbox-k8s-readonly"
            if "delete" in command or "patch" in command or "replace" in command:
                resolved["requires_write_permission"] = True
                resolved["executor_profile"] = "toolbox-k8s-mutating"

        elif scope.target_kind in (TargetKind.CLICKHOUSE,):
            resolved["executor_type"] = "sandbox_pod"
            resolved["executor_profile"] = "toolbox-clickhouse-readonly"
            resolved["target_identity"] = f"database:{scope.database or 'logs'}"

        elif scope.target_kind in (TargetKind.MARIADB,):
            resolved["executor_type"] = "sandbox_pod"
            resolved["executor_profile"] = "toolbox-mysql-readonly"
            resolved["target_identity"] = f"database:{scope.database or 'default'}"

        elif scope.target_kind == TargetKind.HOST_NODE:
            resolved["executor_type"] = "ssh_gateway"
            resolved["executor_profile"] = "host-ssh-readonly"

        # Update target identity based on scope
        if scope.namespace:
            if scope.target_kind in (TargetKind.K8S_NAMESPACE, TargetKind.K8S_POD,
                                      TargetKind.K8S_SERVICE):
                resolved["target_identity"] = f"namespace:{scope.namespace}"

        if scope.node_name:
            resolved["target_identity"] = f"node:{scope.node_name}"

        return resolved


# Global detector
_global_detector: Optional[ScopeAutoDetector] = None


def get_detector() -> ScopeAutoDetector:
    """Get the global scope detector instance."""
    global _global_detector
    if _global_detector is None:
        _global_detector = ScopeAutoDetector()
    return _global_detector
