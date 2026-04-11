"""
LLM Service 单元测试

验证 provider 解析与本地模型 provider 可用性。
"""
import asyncio
import sys
from types import SimpleNamespace

from ai import llm_service


def _reset_singleton() -> None:
    llm_service._llm_service = None


def test_get_llm_service_auto_selects_deepseek(monkeypatch):
    """未配置 LLM_PROVIDER 时，若存在 DEEPSEEK_API_KEY 应自动选择 deepseek。"""
    _reset_singleton()
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test-key")
    monkeypatch.delenv("LLM_API_BASE", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_BASE", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)

    service = llm_service.get_llm_service()

    assert service.config.provider == "deepseek"
    assert service.config.api_key == "deepseek-test-key"
    assert service.config.api_base == "https://api.deepseek.com/v1"
    assert service.config.model == "deepseek-chat"

    _reset_singleton()


def test_local_model_provider_uses_openai_compatible_client(monkeypatch):
    """local provider 应通过 OpenAI 兼容接口返回结果。"""
    _reset_singleton()

    class FakeCompletions:
        async def create(self, **kwargs):
            assert kwargs["model"] == "qwen2.5:7b"
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"summary":"ok"}'))],
                model="qwen2.5:7b",
                usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18),
            )

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(AsyncOpenAI=FakeAsyncOpenAI))

    provider = llm_service.LocalModelProvider(
        llm_service.LLMConfig(
            provider="local",
            model="qwen2.5:7b",
            api_key="local-test-key",
            api_base="http://localhost:11434/v1",
            cache_enabled=False,
        )
    )

    response = asyncio.run(provider.generate(prompt="diagnose this log", system_prompt="be precise"))

    assert response.provider == "local"
    assert response.error is None
    assert response.model == "qwen2.5:7b"
    assert response.content == '{"summary":"ok"}'
    assert response.usage["total_tokens"] == 18


def test_base_provider_cache_respects_max_entries():
    class _DummyProvider(llm_service.BaseLLMProvider):
        async def generate(self, prompt: str, system_prompt: str = "", **kwargs):
            return llm_service.LLMResponse(content="", model="dummy", provider="dummy")

    provider = _DummyProvider(
        llm_service.LLMConfig(
            provider="openai",
            cache_enabled=True,
            cache_ttl=3600,
            cache_max_entries=2,
        )
    )

    provider._set_cache("k1", "v1")
    provider._set_cache("k2", "v2")
    provider._set_cache("k3", "v3")

    assert provider._get_cached("k1") is None
    assert provider._get_cached("k2") == "v2"
    assert provider._get_cached("k3") == "v3"
