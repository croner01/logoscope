"""
Tests for ai/langchain_runtime/tools.py
"""

from unittest.mock import patch

from ai.langchain_runtime.tools import collect_tool_observations


class TestCollectToolObservations:
    """Test langchain runtime tools."""

    def test_web_search_invalid_timeout_env_falls_back_to_default(self, monkeypatch):
        """非法 timeout 配置不应抛异常，工具应返回可用结构。"""
        monkeypatch.setenv("AI_FOLLOWUP_WEB_SEARCH_ENABLED", "true")
        monkeypatch.setenv("AI_FOLLOWUP_WEB_SEARCH_ENDPOINT", "http://127.0.0.1:9/search")
        monkeypatch.setenv("AI_FOLLOWUP_WEB_SEARCH_TIMEOUT_SECONDS", "abc")

        observations = collect_tool_observations(
            question="query-service timeout",
            analysis_context={},
            references=[],
            subgoals=[],
            reflection={},
        )

        web = observations.get("web_search") or {}
        assert isinstance(web, dict)
        assert web.get("status") in {"ok", "error", "unavailable"}

    def test_log_query_fallback_returns_high_signal_logs(self):
        """query 未命中时应回退到高信号 ERROR/traceback 日志。"""
        analysis_context = {
            "followup_related_logs": [
                {"timestamp": "2026-03-14T10:00:00Z", "level": "INFO", "service_name": "svc-a", "message": "ok"},
                {
                    "timestamp": "2026-03-14T10:00:02Z",
                    "level": "ERROR",
                    "service_name": "svc-b",
                    "message": "Traceback: db timeout\nline-1\nline-2",
                },
            ]
        }

        observations = collect_tool_observations(
            question="non-existing-keyword",
            analysis_context=analysis_context,
            references=[],
            subgoals=[],
            reflection={},
        )
        items = observations.get("log_query") or []
        assert isinstance(items, list)
        assert items
        assert any(str(item.get("level")) == "ERROR" for item in items if isinstance(item, dict))

    def test_web_search_uses_context_results_when_remote_disabled(self, monkeypatch):
        """远端关闭时，web_search 应使用 analysis_context 中的结果。"""
        monkeypatch.setenv("AI_FOLLOWUP_WEB_SEARCH_ENABLED", "false")
        with patch("ai.langchain_runtime.tools.urlopen") as mock_urlopen:
            observations = collect_tool_observations(
                question="kafka lag",
                analysis_context={
                    "web_search_results": [
                        {"title": "Kafka Lag", "snippet": "consumer lag troubleshooting", "url": "https://example.com"}
                    ]
                },
                references=[],
                subgoals=[],
                reflection={},
            )
        mock_urlopen.assert_not_called()
        web = observations.get("web_search") or {}
        assert web.get("status") == "ok"
        assert web.get("source") == "context"
        assert len(web.get("results") or []) == 1
