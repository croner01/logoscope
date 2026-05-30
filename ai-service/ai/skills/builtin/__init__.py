"""Auto-import all built-in skills to trigger @register_skill decoration.

Import order matters for the mandatory Phase-1/2 skills: they must be imported
first so the registry has them registered before rule-based matching runs.
"""

# ── Phase 1 & 2: always-injected foundation skills ───────────────────────────
from ai.skills.builtin import log_flow_analyzer  # noqa: F401  priority=100
from ai.skills.builtin import cross_component_correlation  # noqa: F401  priority=90

# ── Domain-specific diagnostic skills ────────────────────────────────────────
from ai.skills.builtin import k8s_pod  # noqa: F401
from ai.skills.builtin import clickhouse_log  # noqa: F401
from ai.skills.builtin import network_check  # noqa: F401
from ai.skills.builtin import observability_log_correlation_gap  # noqa: F401
from ai.skills.builtin import observability_read_path_latency  # noqa: F401
from ai.skills.builtin import resource_usage  # noqa: F401
from ai.skills.builtin import runtime_diagnosis_orchestrator  # noqa: F401
from ai.skills.builtin import openstack_diagnostics  # noqa: F401
from ai.skills.builtin import mariadb_diagnostics  # noqa: F401
from ai.skills.builtin import linux_system_diagnostics  # noqa: F401
