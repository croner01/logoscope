"""
runtime_config_helpers 行为测试
"""

from pathlib import Path
import sys
import types


if "fastapi" not in sys.modules:
    _fastapi_stub = types.ModuleType("fastapi")

    class _HTTPException(Exception):
        def __init__(self, status_code: int, detail=None):
            super().__init__(str(detail))
            self.status_code = status_code
            self.detail = detail

    _fastapi_stub.HTTPException = _HTTPException
    sys.modules["fastapi"] = _fastapi_stub

from ai.runtime_config_helpers import (  # noqa: E402
    _kb_provider_defaults,
    _normalize_kb_provider_name,
    _persist_env_updates_to_deployment_file,
    _resolve_kb_deployment_file_path,
)


def test_kb_provider_defaults_matches_ragflow_contract():
    defaults = _kb_provider_defaults("ragflow")
    assert defaults["health_path"] == "/api/v1/datasets"
    assert defaults["search_path"] == "/api/v1/retrieval"
    assert defaults["upsert_path"] == "/api/v1/datasets/{dataset_id}/documents"


def test_normalize_kb_provider_name_supports_local_only_alias():
    assert _normalize_kb_provider_name("local_only") == "disabled"
    assert _normalize_kb_provider_name("generic_rest") == "generic_rest"


def test_resolve_kb_deployment_file_path_prefers_extra_over_env(monkeypatch):
    monkeypatch.setenv("KB_DEPLOYMENT_FILE_PATH", "/tmp/from-env.yaml")
    assert _resolve_kb_deployment_file_path({"deployment_file": "/tmp/from-extra.yaml"}) == "/tmp/from-extra.yaml"


def test_persist_env_updates_to_deployment_file_keeps_value_from_intact(tmp_path: Path):
    deployment_file = tmp_path / "ai-service.yaml"
    deployment_file.write_text(
        "\n".join(
            [
                "env:",
                "  - name: FOO",
                '    value: "old"',
                "  - name: SECRET",
                "    valueFrom:",
                "      secretKeyRef:",
                "        name: my-secret",
                "        key: apiKey",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = _persist_env_updates_to_deployment_file(
        {"FOO": "new", "SECRET": "should-not-overwrite", "BAR": "added"},
        str(deployment_file),
    )

    assert result["persisted"] is True
    assert "FOO" in result["updated_keys"]
    assert "BAR" in result["added_keys"]
    assert "SECRET" not in result["updated_keys"]
    content = deployment_file.read_text(encoding="utf-8")
    assert 'value: "new"' in content
    assert "- name: BAR" in content
    assert "valueFrom:" in content
    assert "should-not-overwrite" not in content


def test_persist_env_updates_to_deployment_file_handles_empty_env_block_indent(tmp_path: Path):
    deployment_file = tmp_path / "ai-service-empty-env.yaml"
    deployment_file.write_text(
        "\n".join(
            [
                "spec:",
                "  template:",
                "    spec:",
                "      containers:",
                "        - name: ai-service",
                "          env:",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = _persist_env_updates_to_deployment_file({"FOO": "bar"}, str(deployment_file))

    assert result["persisted"] is True
    content = deployment_file.read_text(encoding="utf-8")
    assert "          - name: FOO" in content
    assert '            value: "bar"' in content
