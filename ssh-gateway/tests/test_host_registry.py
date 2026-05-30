"""Tests for the ClickHouse-backed host registry module."""

from __future__ import annotations

from unittest.mock import ANY, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _patch_clickhouse_execute():
    """Mock _clickhouse_execute to avoid real CH connections."""
    with patch("core.host_registry._clickhouse_execute") as mock:
        mock.return_value = ""
        yield mock


@pytest.fixture(autouse=True)
def _disable_lazy_schema_lock():
    """Reset schema-ready flag before each test."""
    import core.host_registry as hr

    hr._CLICKHOUSE_SCHEMA_READY = False
    yield
    hr._CLICKHOUSE_SCHEMA_READY = False


class TestEnsureSchema:
    def test_creates_database_and_table(self, _patch_clickhouse_execute):
        from core.host_registry import ensure_schema

        ensure_schema()
        calls = [c[0][0] for c in _patch_clickhouse_execute.call_args_list]
        assert any("CREATE DATABASE" in c for c in calls)
        assert any("CREATE TABLE" in c for c in calls)
        assert any("ssh_host_registry" in c for c in calls)

    def test_skips_if_already_ready(self, _patch_clickhouse_execute):
        from core.host_registry import ensure_schema

        ensure_schema()  # first call — creates schema
        _patch_clickhouse_execute.reset_mock()
        ensure_schema()  # second call — should skip
        _patch_clickhouse_execute.assert_not_called()


class TestRegisterHost:
    def test_register_new_host(self, _patch_clickhouse_execute):
        from core.host_registry import register_host

        record = register_host(
            name="node-5", host="10.0.0.5", port=22,
            user="admin", key_file="/etc/ssh-keys/admin/id_rsa",
            labels={"region": "us-east-1"},
        )
        assert record["name"] == "node-5"
        assert record["host"] == "10.0.0.5"
        assert record["port"] == 22
        assert record["user"] == "admin"
        assert record["labels_json"] == '{"region": "us-east-1"}'
        _patch_clickhouse_execute.assert_called_once()

    def test_register_sanitizes_port(self, _patch_clickhouse_execute):
        from core.host_registry import register_host

        record = register_host(name="n1", host="10.0.0.1", port=99999)
        assert record["port"] == 65535


class TestGetHost:
    def test_get_existing_host(self, _patch_clickhouse_execute):
        from core.host_registry import get_host

        _patch_clickhouse_execute.return_value = (
            '{"name":"node-3","host":"10.0.0.3","port":22,"user":"root",'
            '"key_file":"/etc/ssh-keys/default/id_rsa",'
            '"labels_json":"{}","created_at":"2026-05-30 00:00:00.000",'
            '"updated_at":"2026-05-30 00:00:00.000"}\n'
        )
        host = get_host("node-3")
        assert host is not None
        assert host["name"] == "node-3"
        assert host["host"] == "10.0.0.3"
        assert host["labels"] == {}

    def test_get_nonexistent_host(self, _patch_clickhouse_execute):
        from core.host_registry import get_host

        _patch_clickhouse_execute.return_value = ""
        assert get_host("nonexistent") is None

    def test_get_empty_name(self, _patch_clickhouse_execute):
        from core.host_registry import get_host

        assert get_host("") is None
        assert get_host("   ") is None


class TestUnregisterHost:
    def test_unregister_soft_deletes(self, _patch_clickhouse_execute):
        from core.host_registry import unregister_host

        ok = unregister_host("node-3")
        assert ok is True
        sql = _patch_clickhouse_execute.call_args[0][0]
        assert "is_deleted" in sql

    def test_unregister_empty_name(self, _patch_clickhouse_execute):
        from core.host_registry import unregister_host

        assert unregister_host("") is False
        _patch_clickhouse_execute.assert_not_called()


class TestListHosts:
    def test_list_hosts(self, _patch_clickhouse_execute):
        from core.host_registry import list_hosts

        _patch_clickhouse_execute.return_value = (
            '{"name":"node-3","host":"10.0.0.3","port":22,"user":"root",'
            '"key_file":"/etc/ssh-keys/default/id_rsa",'
            '"labels_json":"{}","created_at":"2026-05-30 00:00:00.000",'
            '"updated_at":"2026-05-30 00:00:00.000"}\n'
            '{"name":"node-4","host":"10.0.0.4","port":22,"user":"admin",'
            '"key_file":"/etc/ssh-keys/admin/id_rsa",'
            '"labels_json":"{\\"region\\":\\"us-east-1\\"}","created_at":"2026-05-30 00:00:00.000",'
            '"updated_at":"2026-05-30 00:00:00.000"}\n'
        )
        hosts = list_hosts()
        assert len(hosts) == 2
        assert hosts[0]["name"] == "node-3"
        assert hosts[1]["name"] == "node-4"
        assert hosts[1]["labels"] == {"region": "us-east-1"}


class TestResolveNodeConfigFallback:
    def test_fallback_to_clickhouse_when_yaml_misses(self, tmp_path, monkeypatch):
        """When YAML config doesn't have the node, should query ClickHouse."""
        import yaml
        from app import _resolve_node_config

        # Create empty YAML
        config_dir = tmp_path / "ssh-hosts"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        with open(config_path, "w") as f:
            yaml.dump({}, f)

        monkeypatch.setattr("app._HOSTS_CONFIG", str(config_path))

        # Mock get_host to return a host from CH
        mock_host = {
            "name": "node-ch",
            "host": "10.0.0.99",
            "user": "admin",
            "port": 2222,
            "key_file": "/etc/ssh-keys/admin/id_rsa",
        }
        with patch("core.host_registry.get_host", return_value=mock_host):
            cfg = _resolve_node_config("node-ch")

        assert cfg is not None
        assert cfg["host"] == "10.0.0.99"
        assert cfg["user"] == "admin"
        assert cfg["port"] == 2222

    def test_yaml_takes_priority(self, tmp_path, monkeypatch):
        """YAML config should take priority over ClickHouse."""
        import yaml
        from app import _resolve_node_config

        config_dir = tmp_path / "ssh-hosts"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        with open(config_path, "w") as f:
            yaml.dump(
                {"node-yaml": {"host": "10.0.0.1", "user": "root", "port": 22}},
                f,
            )

        monkeypatch.setattr("app._HOSTS_CONFIG", str(config_path))

        # Even if CH returns a different result, YAML should win
        with patch("core.host_registry.get_host") as mock_get_host:
            cfg = _resolve_node_config("node-yaml")
            mock_get_host.assert_not_called()

        assert cfg["host"] == "10.0.0.1"

    def test_ch_unavailable_safe_fallback(self, tmp_path, monkeypatch):
        """If CH is unavailable, should return None (same as unknown node)."""
        import yaml
        from app import _resolve_node_config

        config_dir = tmp_path / "ssh-hosts"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        with open(config_path, "w") as f:
            yaml.dump({}, f)

        monkeypatch.setattr("app._HOSTS_CONFIG", str(config_path))

        # Mock get_host to raise an exception (CH unavailable)
        with patch("core.host_registry.get_host", side_effect=RuntimeError("CH down")):
            cfg = _resolve_node_config("node-ch")

        assert cfg is None
