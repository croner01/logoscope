"""Tests for command/compiler.py."""
from ai.command.compiler import compile_command
from ai.command.spec import CommandSpec, ToolType, CompiledCommand


class TestCompileCommand:
    def test_compiles_generic_exec(self):
        spec = CommandSpec(
            tool=ToolType.GENERIC_EXEC,
            command="kubectl get pods -n islap",
            target_kind="k8s_cluster",
            target_identity="namespace:islap",
            purpose="list pods",
        )
        compiled = compile_command(spec)
        assert isinstance(compiled, CompiledCommand)
        assert compiled.route == "remote"
        assert compiled.shell_command == "kubectl get pods -n islap"
        assert compiled.executor_profile == "toolbox-k8s-readonly"

    def test_compiles_simple_clickhouse_to_local(self):
        spec = CommandSpec(
            tool=ToolType.CLICKHOUSE_QUERY,
            command="SELECT * FROM logs.events WHERE service_name='api' LIMIT 10",
            target_kind="clickhouse_cluster",
            target_identity="database:logs",
            purpose="query logs",
        )
        compiled = compile_command(spec)
        assert compiled.route == "local"
        assert compiled.executor_profile == "query-service-readonly"

    def test_compiles_complex_clickhouse_to_remote(self):
        spec = CommandSpec(
            tool=ToolType.CLICKHOUSE_QUERY,
            command="SELECT service_name, COUNT(*) as cnt FROM logs.events GROUP BY service_name",
            target_kind="clickhouse_cluster",
            target_identity="database:logs",
            purpose="aggregate",
        )
        compiled = compile_command(spec)
        assert compiled.route == "remote"

    def test_shell_command_wraps_clickhouse_for_remote(self):
        spec = CommandSpec(
            tool=ToolType.CLICKHOUSE_QUERY,
            command="SELECT COUNT(*) FROM logs.events GROUP BY level",
            target_kind="clickhouse_cluster",
            target_identity="database:logs",
            purpose="count by level",
        )
        compiled = compile_command(spec)
        assert "clickhouse-client" in compiled.shell_command.lower()
        assert "SELECT" in compiled.shell_command

    def test_rejects_blocked_operators_in_generic_exec(self):
        spec = CommandSpec(
            tool=ToolType.GENERIC_EXEC,
            command="kubectl get pods | grep error",
            purpose="filtered list",
        )
        compiled = compile_command(spec)
        assert compiled.route == ""
        assert not compiled.shell_command

    def test_auto_wraps_pod_command_with_kubectl_exec(self):
        spec = CommandSpec(
            tool=ToolType.GENERIC_EXEC,
            command="ls -la /etc/config",
            target_kind="k8s_cluster",
            target_identity="pod:thanos-ruler-ecms-0/namespace:openstack",
            purpose="check config",
        )
        compiled = compile_command(spec)
        assert "kubectl exec thanos-ruler-ecms-0 -n openstack -- ls -la /etc/config" in compiled.shell_command
        assert compiled.route == "remote"
        assert compiled.executor_profile == "toolbox-k8s-readonly"

    def test_routes_host_command_to_ssh_gateway(self):
        spec = CommandSpec(
            tool=ToolType.GENERIC_EXEC,
            command="systemctl status kubelet",
            target_kind="host_node",
            target_identity="host:node-3",
            purpose="check kubelet",
        )
        compiled = compile_command(spec)
        assert compiled.executor_profile == "host-ssh-readonly"
        assert compiled.shell_command == "systemctl status kubelet"  # not wrapped

    def test_pod_command_without_target_goes_to_busybox(self):
        spec = CommandSpec(
            tool=ToolType.GENERIC_EXEC,
            command="ls /tmp",
            purpose="list temp",
        )
        compiled = compile_command(spec)
        assert compiled.executor_profile == "busybox-readonly"
        assert "kubectl exec" not in compiled.shell_command

    def test_unknown_command_with_pod_gets_kubectl_exec_wrap(self):
        spec = CommandSpec(
            tool=ToolType.GENERIC_EXEC,
            command="python3 --version",
            target_kind="k8s_cluster",
            target_identity="pod:my-pod/namespace:islap",
            purpose="check python",
        )
        compiled = compile_command(spec)
        assert "kubectl exec my-pod -n islap -- python3 --version" in compiled.shell_command

    def test_kubectl_command_passes_through_unchanged(self):
        spec = CommandSpec(
            tool=ToolType.GENERIC_EXEC,
            command="kubectl describe pod my-pod -n islap",
            target_kind="k8s_cluster",
            target_identity="pod:my-pod/namespace:islap",
            purpose="describe pod",
        )
        compiled = compile_command(spec)
        assert compiled.shell_command == "kubectl describe pod my-pod -n islap"
        assert compiled.executor_profile == "toolbox-k8s-readonly"
