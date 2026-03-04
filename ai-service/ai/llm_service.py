"""
LLM 服务模块

提供大语言模型集成，支持：
- OpenAI API (GPT-4, GPT-3.5)
- Claude API (Claude-3)
- 本地模型支持
- 响应缓存
- 限流控制

Date: 2026-02-22
"""

import os
import json
import logging
import hashlib
import asyncio
import re
from typing import Dict, Any, List, Optional, Literal
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


def _safe_int(value: Any, default: int = 0) -> int:
    """将任意值安全转换为 int。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _usage_to_dict(usage: Any) -> Dict[str, int]:
    """兼容不同 SDK 的 usage 结构。"""
    if usage is None:
        return {}

    if isinstance(usage, dict):
        prompt_tokens = _safe_int(usage.get("prompt_tokens", usage.get("input_tokens", 0)))
        completion_tokens = _safe_int(usage.get("completion_tokens", usage.get("output_tokens", 0)))
        total_tokens = _safe_int(usage.get("total_tokens", prompt_tokens + completion_tokens))
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }

    prompt_tokens = _safe_int(getattr(usage, "prompt_tokens", getattr(usage, "input_tokens", 0)))
    completion_tokens = _safe_int(getattr(usage, "completion_tokens", getattr(usage, "output_tokens", 0)))
    total_tokens = _safe_int(getattr(usage, "total_tokens", prompt_tokens + completion_tokens))

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def _extract_first_json_dict(text: str) -> Optional[Dict[str, Any]]:
    """从混合文本中提取首个可解析的 JSON 对象。"""
    decoder = json.JSONDecoder()
    content = str(text or "")
    for index, ch in enumerate(content):
        if ch not in ("{", "["):
            continue
        try:
            parsed, _ = decoder.raw_decode(content[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _parse_llm_json(content: str) -> Optional[Dict[str, Any]]:
    """解析 LLM 输出，兼容 markdown code block 与前后解释文本。"""
    candidates: List[str] = []
    raw = str(content or "").strip()
    if raw:
        candidates.append(raw)

    fenced_blocks = re.findall(r"```(?:json)?\s*([\s\S]*?)```", raw, flags=re.IGNORECASE)
    for block in fenced_blocks:
        block_text = block.strip()
        if block_text:
            candidates.append(block_text)

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        parsed = _extract_first_json_dict(candidate)
        if parsed is not None:
            return parsed

    return None


def _resolve_provider() -> str:
    """解析 LLM provider，未显式配置时根据 key 自动推断。"""
    provider = (os.getenv("LLM_PROVIDER", "") or "").strip().lower()
    if provider:
        return provider

    if os.getenv("DEEPSEEK_API_KEY"):
        return "deepseek"

    return "openai"


def _resolve_llm_model(provider: str) -> str:
    """根据 provider 解析默认模型。"""
    configured_model = os.getenv("LLM_MODEL")
    if configured_model:
        return configured_model

    if provider == "deepseek":
        return os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    if provider == "local":
        return os.getenv("LOCAL_MODEL_NAME", "qwen2.5:7b")

    return "gpt-4"


def _resolve_llm_api_key(provider: str) -> Optional[str]:
    """按 provider 优先级解析 API key。"""
    explicit = os.getenv("LLM_API_KEY")
    if explicit:
        return explicit

    if provider == "openai":
        return os.getenv("OPENAI_API_KEY")
    if provider == "claude":
        return os.getenv("ANTHROPIC_API_KEY")
    if provider == "deepseek":
        return os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")
    if provider == "local":
        return os.getenv("LOCAL_MODEL_API_KEY")

    return None


def _resolve_llm_api_base(provider: str) -> Optional[str]:
    """按 provider 优先级解析 API base。"""
    explicit = os.getenv("LLM_API_BASE")
    if explicit:
        return explicit

    if provider == "openai":
        return os.getenv("OPENAI_API_BASE")
    if provider == "deepseek":
        return os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com/v1")
    if provider == "local":
        return (
            os.getenv("LOCAL_MODEL_API_BASE")
            or os.getenv("LOCAL_MODEL_BASE_URL")
            or os.getenv("LOCAL_MODEL_PATH")
            or "http://localhost:11434/v1"
        )

    return None


@dataclass
class LLMConfig:
    """LLM 配置"""
    provider: Literal["openai", "claude", "local", "deepseek"] = "openai"
    model: str = "gpt-4"
    api_key: Optional[str] = None
    api_base: Optional[str] = None
    max_tokens: int = 2000
    temperature: float = 0.7
    timeout: int = 60
    cache_enabled: bool = True
    cache_ttl: int = 3600  # 1 hour
    rate_limit: int = 60  # requests per minute


@dataclass
class LLMResponse:
    """LLM 响应"""
    content: str
    model: str
    provider: str
    usage: Dict[str, int] = field(default_factory=dict)
    cached: bool = False
    latency_ms: int = 0
    error: Optional[str] = None


class BaseLLMProvider(ABC):
    """LLM 提供者基类"""

    def __init__(self, config: LLMConfig):
        self.config = config
        self._cache: Dict[str, tuple[str, datetime]] = {}

    def _get_cache_key(self, prompt: str, system_prompt: str = "") -> str:
        """生成缓存键"""
        content = f"{system_prompt}|{prompt}"
        return hashlib.md5(content.encode()).hexdigest()

    def _get_cached(self, key: str) -> Optional[str]:
        """获取缓存"""
        if not self.config.cache_enabled:
            return None

        cached = self._cache.get(key)
        if cached:
            content, timestamp = cached
            if datetime.now() - timestamp < timedelta(seconds=self.config.cache_ttl):
                return content
            else:
                del self._cache[key]
        return None

    def _set_cache(self, key: str, content: str):
        """设置缓存"""
        if self.config.cache_enabled:
            self._cache[key] = (content, datetime.now())

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        system_prompt: str = "",
        **kwargs
    ) -> LLMResponse:
        """生成响应"""
        pass


class OpenAIProvider(BaseLLMProvider):
    """OpenAI 提供者"""

    def __init__(
        self,
        config: LLMConfig,
        provider_name: str = "openai",
        api_key_env: str = "OPENAI_API_KEY",
        api_base_env: str = "OPENAI_API_BASE",
        default_api_base: str = "https://api.openai.com/v1",
    ):
        super().__init__(config)
        self.provider_name = provider_name
        self.api_key = config.api_key or os.getenv(api_key_env)
        self.api_base = config.api_base or os.getenv(api_base_env, default_api_base)
        self._client = None

    async def _get_client(self):
        """获取 OpenAI 客户端"""
        if self._client is None:
            try:
                from openai import AsyncOpenAI
                if not self.api_key:
                    raise ValueError(f"{self.provider_name} API key is not configured")
                self._client = AsyncOpenAI(
                    api_key=self.api_key,
                    base_url=self.api_base,
                    timeout=self.config.timeout,
                )
            except ImportError:
                raise ImportError("请安装 openai: pip install openai")
        return self._client

    async def generate(
        self,
        prompt: str,
        system_prompt: str = "",
        **kwargs
    ) -> LLMResponse:
        """生成响应"""
        cache_key = self._get_cache_key(prompt, system_prompt)
        cached = self._get_cached(cache_key)
        if cached:
            return LLMResponse(
                content=cached,
                model=self.config.model,
                provider=self.provider_name,
                cached=True,
            )

        start_time = datetime.now()

        try:
            client = await self._get_client()

            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

            response = await client.chat.completions.create(
                model=kwargs.get("model", self.config.model),
                messages=messages,
                max_tokens=kwargs.get("max_tokens", self.config.max_tokens),
                temperature=kwargs.get("temperature", self.config.temperature),
            )

            content = response.choices[0].message.content
            latency_ms = int((datetime.now() - start_time).total_seconds() * 1000)

            self._set_cache(cache_key, content)

            return LLMResponse(
                content=content,
                model=response.model,
                provider=self.provider_name,
                usage=_usage_to_dict(getattr(response, "usage", None)),
                latency_ms=latency_ms,
            )

        except Exception as e:
            logger.error(f"{self.provider_name} API error: {e}")
            return LLMResponse(
                content="",
                model=self.config.model,
                provider=self.provider_name,
                error=str(e),
            )


class ClaudeProvider(BaseLLMProvider):
    """Claude 提供者"""

    def __init__(self, config: LLMConfig):
        super().__init__(config)
        self.api_key = config.api_key or os.getenv("ANTHROPIC_API_KEY")
        self._client = None

    async def _get_client(self):
        """获取 Claude 客户端"""
        if self._client is None:
            try:
                from anthropic import AsyncAnthropic
                self._client = AsyncAnthropic(
                    api_key=self.api_key,
                    timeout=self.config.timeout,
                )
            except ImportError:
                raise ImportError("请安装 anthropic: pip install anthropic")
        return self._client

    async def generate(
        self,
        prompt: str,
        system_prompt: str = "",
        **kwargs
    ) -> LLMResponse:
        """生成响应"""
        cache_key = self._get_cache_key(prompt, system_prompt)
        cached = self._get_cached(cache_key)
        if cached:
            return LLMResponse(
                content=cached,
                model=self.config.model,
                provider="claude",
                cached=True,
            )

        start_time = datetime.now()

        try:
            client = await self._get_client()

            response = await client.messages.create(
                model=kwargs.get("model", self.config.model),
                max_tokens=kwargs.get("max_tokens", self.config.max_tokens),
                system=system_prompt if system_prompt else None,
                messages=[{"role": "user", "content": prompt}],
            )

            content = response.content[0].text
            latency_ms = int((datetime.now() - start_time).total_seconds() * 1000)

            self._set_cache(cache_key, content)

            return LLMResponse(
                content=content,
                model=response.model,
                provider="claude",
                usage=_usage_to_dict(getattr(response, "usage", None)),
                latency_ms=latency_ms,
            )

        except Exception as e:
            logger.error(f"Claude API error: {e}")
            return LLMResponse(
                content="",
                model=self.config.model,
                provider="claude",
                error=str(e),
            )


class LocalModelProvider(BaseLLMProvider):
    """本地模型提供者（用于测试或离线环境）"""

    def __init__(self, config: LLMConfig):
        super().__init__(config)
        self.api_key = (
            config.api_key
            or os.getenv("LOCAL_MODEL_API_KEY")
            or os.getenv("LLM_API_KEY")
            or "local"
        )
        self.api_base = (
            config.api_base
            or os.getenv("LOCAL_MODEL_API_BASE")
            or os.getenv("LOCAL_MODEL_BASE_URL")
            or os.getenv("LOCAL_MODEL_PATH")
            or "http://localhost:11434/v1"
        )
        self._client = None

    async def _get_client(self):
        """获取本地模型兼容客户端（OpenAI compatible）。"""
        if self._client is None:
            try:
                from openai import AsyncOpenAI
                self._client = AsyncOpenAI(
                    api_key=self.api_key,
                    base_url=self.api_base,
                    timeout=self.config.timeout,
                )
            except ImportError:
                raise ImportError("请安装 openai: pip install openai")
        return self._client

    async def generate(
        self,
        prompt: str,
        system_prompt: str = "",
        **kwargs
    ) -> LLMResponse:
        """生成响应（本地模型，OpenAI 兼容接口）。"""
        cache_key = self._get_cache_key(prompt, system_prompt)
        cached = self._get_cached(cache_key)
        if cached:
            return LLMResponse(
                content=cached,
                model=self.config.model,
                provider="local",
                cached=True,
            )

        start_time = datetime.now()

        try:
            client = await self._get_client()

            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

            response = await client.chat.completions.create(
                model=kwargs.get("model", self.config.model),
                messages=messages,
                max_tokens=kwargs.get("max_tokens", self.config.max_tokens),
                temperature=kwargs.get("temperature", self.config.temperature),
            )

            content = response.choices[0].message.content
            latency_ms = int((datetime.now() - start_time).total_seconds() * 1000)
            self._set_cache(cache_key, content)

            return LLMResponse(
                content=content,
                model=getattr(response, "model", self.config.model),
                provider="local",
                usage=_usage_to_dict(getattr(response, "usage", None)),
                latency_ms=latency_ms,
            )
        except Exception as e:
            logger.error(f"Local model API error: {e}")
            return LLMResponse(
                content="",
                model=self.config.model,
                provider="local",
                error=str(e),
            )


class DeepSeekProvider(OpenAIProvider):
    """DeepSeek 提供者（OpenAI 兼容 API）。"""

    def __init__(self, config: LLMConfig):
        super().__init__(
            config=config,
            provider_name="deepseek",
            api_key_env="DEEPSEEK_API_KEY",
            api_base_env="DEEPSEEK_API_BASE",
            default_api_base="https://api.deepseek.com/v1",
        )


class LLMService:
    """LLM 服务"""

    def __init__(self, config: Optional[LLMConfig] = None):
        self.config = config or LLMConfig()
        self._provider = self._create_provider()

    def _create_provider(self) -> BaseLLMProvider:
        """创建提供者"""
        providers = {
            "openai": OpenAIProvider,
            "claude": ClaudeProvider,
            "local": LocalModelProvider,
            "deepseek": DeepSeekProvider,
        }
        provider_class = providers.get(self.config.provider)
        if not provider_class:
            raise ValueError(f"Unknown provider: {self.config.provider}")
        return provider_class(self.config)

    async def analyze_log(
        self,
        log_content: str,
        service_name: str = "",
        context: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """分析日志"""
        system_prompt = """你是一个专业的日志分析专家。你的任务是分析日志内容，识别问题类型、根本原因，并提供解决方案。

请以 JSON 格式返回分析结果，包含以下字段：
{
    "problem_type": "问题类型（database/memory/network/performance/disk/auth/other）",
    "severity": "严重程度（critical/high/medium/low）",
    "summary": "问题摘要（一句话描述）",
    "root_causes": ["根因1", "根因2"],
    "solutions": [
        {
            "title": "解决方案标题",
            "description": "详细描述",
            "steps": ["步骤1", "步骤2"]
        }
    ],
    "similar_cases": ["相似案例描述"],
    "confidence": 0.85
}

请确保返回有效的 JSON 格式。"""

        prompt = f"""请分析以下日志内容：

服务名称: {service_name}
日志内容:
```
{log_content}
```

{f'上下文信息: {json.dumps(context, ensure_ascii=False)}' if context else ''}

请识别问题类型、分析根本原因，并提供解决方案。"""

        response = await self._provider.generate(prompt, system_prompt)

        if response.error:
            return {
                "error": response.error,
                "problem_type": "unknown",
                "severity": "unknown",
                "summary": f"分析失败: {response.error}",
                "root_causes": [],
                "solutions": [],
                "similar_cases": [],
                "confidence": 0,
            }

        result = _parse_llm_json(response.content)
        if result is not None:
            result["cached"] = response.cached
            result["latency_ms"] = response.latency_ms
            result["model"] = response.model
            return result
        return {
            "error": "Failed to parse LLM response",
            "raw_response": response.content,
            "problem_type": "unknown",
            "severity": "unknown",
            "summary": "LLM 返回格式错误",
            "root_causes": [],
            "solutions": [],
            "similar_cases": [],
            "confidence": 0,
        }

    async def analyze_trace(
        self,
        trace_data: str,
        service_name: str = "",
    ) -> Dict[str, Any]:
        """分析追踪链路"""
        system_prompt = """你是一个专业的分布式系统追踪分析专家。你的任务是分析分布式追踪数据，识别性能瓶颈、异常调用和潜在问题。

请以 JSON 格式返回分析结果，包含以下字段：
{
    "summary": "链路分析摘要",
    "total_duration_ms": 总耗时毫秒,
    "bottleneck_spans": ["瓶颈节点"],
    "error_spans": ["错误节点"],
    "recommendations": ["优化建议"],
    "confidence": 0.85
}"""

        prompt = f"""请分析以下分布式追踪数据：

服务名称: {service_name}
追踪数据:
```
{trace_data}
```

请识别性能瓶颈、异常调用，并提供优化建议。"""

        response = await self._provider.generate(prompt, system_prompt)

        if response.error:
            return {
                "error": response.error,
                "summary": f"分析失败: {response.error}",
            }

        result = _parse_llm_json(response.content)
        if result is not None:
            result["cached"] = response.cached
            result["latency_ms"] = response.latency_ms
            return result
        return {
            "error": "Failed to parse LLM response",
            "raw_response": response.content,
        }

    async def chat(
        self,
        message: str,
        context: Dict[str, Any] = None,
    ) -> str:
        """通用对话"""
        system_prompt = "你是一个专业的可观测性和日志分析助手。"
        
        prompt = message
        if context:
            prompt = f"上下文信息:\n{json.dumps(context, ensure_ascii=False)}\n\n{message}"

        response = await self._provider.generate(prompt, system_prompt)
        return response.content


_llm_service: Optional[LLMService] = None


def get_llm_service() -> LLMService:
    """获取 LLM 服务实例"""
    global _llm_service
    if _llm_service is None:
        provider = _resolve_provider()
        config = LLMConfig(
            provider=provider,
            model=_resolve_llm_model(provider),
            api_key=_resolve_llm_api_key(provider),
            api_base=_resolve_llm_api_base(provider),
            max_tokens=int(os.getenv("LLM_MAX_TOKENS", "2000")),
            temperature=float(os.getenv("LLM_TEMPERATURE", "0.7")),
            cache_enabled=os.getenv("LLM_CACHE_ENABLED", "true").lower() == "true",
        )
        _llm_service = LLMService(config)
    return _llm_service


def reset_llm_service() -> None:
    """重置 LLM 服务实例，使运行时配置更新后立即生效。"""
    global _llm_service
    _llm_service = None
