"""Auto-import all built-in skills to trigger @register_skill decoration."""

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
