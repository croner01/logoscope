"""
Tests for ai.llm_stream_helpers.
"""

import asyncio
import time

import pytest

from ai.llm_stream_helpers import collect_chat_response


class _BrokenAfterFirstChunkLLM:
    def __init__(self):
        self.chat_calls = 0

    async def chat(self, message, context=None):
        self.chat_calls += 1
        return "fallback-answer"

    async def chat_stream(self, message, context=None):
        yield "first-"
        raise RuntimeError("stream broken")


class _SlowStreamLLM:
    async def chat(self, message, context=None):
        return "fallback-answer"

    async def chat_stream(self, message, context=None):
        await asyncio.sleep(3.2)
        yield "a"
        await asyncio.sleep(4.5)
        yield "b"


class _FailFastStreamLLM:
    def __init__(self):
        self.chat_calls = 0

    async def chat(self, message, context=None):
        self.chat_calls += 1
        return "fallback-answer"

    async def chat_stream(self, message, context=None):
        raise RuntimeError("no stream")
        yield  # pragma: no cover


def test_collect_chat_response_stream_error_after_first_chunk_keeps_partial_without_retry():
    llm = _BrokenAfterFirstChunkLLM()

    result = asyncio.run(
        collect_chat_response(
            llm_service=llm,
            message="hello",
            context={},
            total_timeout_seconds=20,
            first_token_timeout_seconds=5,
        )
    )

    assert result == "first-"
    assert llm.chat_calls == 0


def test_collect_chat_response_total_timeout_includes_first_token_wait():
    llm = _SlowStreamLLM()
    started = time.perf_counter()

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(
            collect_chat_response(
                llm_service=llm,
                message="hello",
                context={},
                total_timeout_seconds=5,
                first_token_timeout_seconds=4,
            )
        )

    elapsed = time.perf_counter() - started
    assert elapsed < 6.2


def test_collect_chat_response_stream_fail_before_first_chunk_fallbacks_to_chat():
    llm = _FailFastStreamLLM()

    result = asyncio.run(
        collect_chat_response(
            llm_service=llm,
            message="hello",
            context={},
            total_timeout_seconds=10,
            first_token_timeout_seconds=3,
        )
    )

    assert result == "fallback-answer"
    assert llm.chat_calls == 1

