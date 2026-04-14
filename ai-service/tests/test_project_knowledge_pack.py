"""Tests for ai.project_knowledge_pack."""

from pathlib import Path

import ai.project_knowledge_pack as project_knowledge_pack
from ai.project_knowledge_pack import (
    _default_knowledge_root,
    extract_markdown_sections,
    load_project_knowledge_registry,
    select_project_knowledge,
)


def test_extract_markdown_sections_reads_summary_and_sources():
    content = """# query-service

## Summary
query read path

## Preferred Evidence Sources
- query-service logs
- ClickHouse system.query_log

## Common Failures and Cautions
- do not confuse query-service symptoms with ClickHouse root cause

## Sources
- /root/logoscope/docs/api/reference.md
"""
    sections = extract_markdown_sections(content)

    assert sections["Summary"] == "query read path"
    assert "ClickHouse system.query_log" in sections["Preferred Evidence Sources"]
    assert "/root/logoscope/docs/api/reference.md" in sections["Sources"]


def test_load_project_knowledge_registry_loads_expected_assets():
    root = Path(__file__).resolve().parents[2]
    registry = load_project_knowledge_registry(root / "docs" / "superpowers" / "knowledge")

    assert "query-service" in registry["services"]
    assert "ai-runtime-diagnosis" in registry["paths"]
    assert registry["services"]["query-service"]["summary"]
    assert registry["paths"]["log-ingest-query"]["summary"]


def test_select_project_knowledge_prefers_service_and_log_path_for_query_failures():
    root = Path(__file__).resolve().parents[2]
    selection = select_project_knowledge(
        {
            "service_name": "query-service",
            "analysis_type": "log",
            "question": "query-service Code:241 clickhouse 慢查询怎么排查",
            "input_text": "ERROR query-service Code:241 request failed",
        },
        knowledge_root=root / "docs" / "superpowers" / "knowledge",
    )

    assert selection["knowledge_primary_service"] == "query-service"
    assert selection["knowledge_primary_path"] == "log-ingest-query"
    assert selection["knowledge_pack_version"] == "2026-04-14.v2"
    assert "ClickHouse" in selection["project_knowledge_prompt"]
    assert "execution/resource evidence" in selection["project_knowledge_prompt"]


def test_select_project_knowledge_prefers_trace_request_path_for_correlation_questions():
    root = Path(__file__).resolve().parents[2]
    selection = select_project_knowledge(
        {
            "analysis_type": "log",
            "question": "缺少 trace_id 但有 request_id 和 time window 时该怎么继续排查",
            "input_text": "request_id=req-001 time window 2026-04-14T10:30Z",
        },
        knowledge_root=root / "docs" / "superpowers" / "knowledge",
    )

    assert selection["knowledge_primary_service"] == ""
    assert selection["knowledge_primary_path"] == "trace-request-correlation"
    assert "not mandatory blockers" in selection["project_knowledge_prompt"]


def test_select_project_knowledge_can_fallback_to_path_when_service_missing():
    root = Path(__file__).resolve().parents[2]
    selection = select_project_knowledge(
        {
            "analysis_type": "log",
            "question": "topology edge preview 返回空结果",
            "input_text": "preview topology edge empty",
        },
        knowledge_root=root / "docs" / "superpowers" / "knowledge",
    )

    assert selection["knowledge_primary_service"] == ""
    assert selection["knowledge_primary_path"] == "topology-generation-preview"
    assert selection["project_knowledge_prompt"]


def test_default_knowledge_root_prefers_app_docs_layout(monkeypatch, tmp_path):
    app_root = tmp_path / "app"
    module_dir = app_root / "ai"
    knowledge_root = app_root / "docs" / "superpowers" / "knowledge"
    knowledge_root.mkdir(parents=True)
    fake_module_file = module_dir / "project_knowledge_pack.py"
    fake_module_file.parent.mkdir(parents=True)
    fake_module_file.write_text("# test module path\n", encoding="utf-8")

    monkeypatch.setattr(project_knowledge_pack, "__file__", str(fake_module_file))

    assert _default_knowledge_root() == knowledge_root


def test_select_project_knowledge_degrades_gracefully_when_assets_missing(tmp_path):
    selection = select_project_knowledge(
        {
            "analysis_type": "log",
            "service_name": "ai-service",
            "question": "runtime run 直接中断",
            "input_text": "follow-up runtime task failed",
        },
        knowledge_root=tmp_path / "missing-knowledge-root",
    )

    assert selection["knowledge_pack_version"] == "2026-04-14.v2"
    assert selection["knowledge_primary_service"] == ""
    assert selection["knowledge_primary_path"] == ""
    assert selection["knowledge_related_services"] == []
    assert selection["project_knowledge_prompt"] == ""
    assert selection["knowledge_selection_reason"] == "fallback=knowledge_unavailable"
