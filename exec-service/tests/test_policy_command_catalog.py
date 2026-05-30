"""
Tests for config-driven command catalog in policy classification.
"""

import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import policy


def _reset_catalog_cache() -> None:
    policy._COMMAND_CATALOG_CACHE["loaded_at"] = 0.0
    policy._COMMAND_CATALOG_CACHE["path"] = ""
    policy._COMMAND_CATALOG_CACHE["mtime"] = -1.0
    policy._COMMAND_CATALOG_CACHE["data"] = None


def test_openstack_list_is_readonly_and_whitelisted(monkeypatch):
    monkeypatch.delenv("EXEC_COMMAND_CATALOG_FILE", raising=False)
    _reset_catalog_cache()

    meta = policy.classify_command("openstack server list --long")
    assert meta["supported"] is True
    assert meta["command_type"] == "query"
    assert meta["command_family"] == "openstack"
    assert meta["executor_profile"] == "toolbox-openstack-readonly"

    whitelist = policy.evaluate_query_whitelist("openstack server list --long", meta)
    assert whitelist["whitelisted"] is True


def test_openstack_delete_is_mutating(monkeypatch):
    monkeypatch.delenv("EXEC_COMMAND_CATALOG_FILE", raising=False)
    _reset_catalog_cache()

    meta = policy.classify_command("openstack server delete test-vm")
    assert meta["supported"] is True
    assert meta["command_type"] == "repair"
    assert meta["requires_write_permission"] is True
    assert meta["executor_profile"] == "toolbox-openstack-mutating"


def test_psql_mysql_readonly_and_mutating_classification(monkeypatch):
    monkeypatch.delenv("EXEC_COMMAND_CATALOG_FILE", raising=False)
    _reset_catalog_cache()

    pg_read = policy.classify_command('psql -d temporal -c "select * from queue limit 5"')
    assert pg_read["supported"] is True
    assert pg_read["command_type"] == "query"
    assert pg_read["command_family"] == "postgres"
    assert pg_read["executor_profile"] == "toolbox-postgres-readonly"

    pg_write = policy.classify_command('psql -d temporal -c "update queue set kind=\'x\'"')
    assert pg_write["supported"] is True
    assert pg_write["command_type"] == "repair"
    assert pg_write["requires_write_permission"] is True
    assert pg_write["executor_profile"] == "toolbox-postgres-mutating"

    mysql_read = policy.classify_command('mysql -D temporal -e "show processlist"')
    assert mysql_read["supported"] is True
    assert mysql_read["command_type"] == "query"
    assert mysql_read["command_family"] == "mysql"
    assert mysql_read["executor_profile"] == "toolbox-mysql-readonly"


def test_sql_whitelist_rejects_multi_statement(monkeypatch):
    monkeypatch.delenv("EXEC_COMMAND_CATALOG_FILE", raising=False)
    _reset_catalog_cache()

    command = 'mysql -D temporal -e "select 1; select 2"'
    meta = policy.classify_command(command)
    whitelist = policy.evaluate_query_whitelist(command, meta)
    assert whitelist["whitelisted"] is False
    assert "multi-statement" in whitelist["reason"]


def test_catalog_file_override_supports_new_readonly_openstack_verb(monkeypatch, tmp_path):
    custom_catalog_path = tmp_path / "command_catalog.override.json"
    custom_catalog_path.write_text(
        json.dumps(
            {
                "openstack": {
                    "readonly_verbs": ["inspect"],
                    "mutating_verbs": [],
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EXEC_COMMAND_CATALOG_FILE", str(custom_catalog_path))
    _reset_catalog_cache()

    meta = policy.classify_command("openstack server inspect test-vm")
    assert meta["supported"] is True
    assert meta["command_type"] == "query"

