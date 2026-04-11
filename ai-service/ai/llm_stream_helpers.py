"""
LLM chat streaming helpers.
"""

import asyncio
import inspect
from typing import Any, Callable, Optional


def _as_str(value: Any, default: str = "") -> str:
    text = str(value).strip() if value is not None else ""
    return text if text else default


async def _await_maybe(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def collect_chat_response(
    *,
    llm_service: Any,
    message: str,
    context: Optional[dict],
    total_timeout_seconds: int,
    first_token_timeout_seconds: int,
    on_token: Optional[Callable[[str], Any]] = None,
) -> str:
    """
    Collect full chat response and optionally forward stream chunks.

    Falls back to non-stream `chat()` when streaming is unavailable.
    """
    safe_total_timeout = max(5, int(total_timeout_seconds or 0))
    safe_first_timeout = max(1, int(first_token_timeout_seconds or 0))
    stream_fn = getattr(llm_service, "chat_stream", None)
    if not callable(stream_fn):
        return _as_str(
            await asyncio.wait_for(
                llm_service.chat(message=message, context=context),
                timeout=safe_total_timeout,
            )
        )

    stream_obj = stream_fn(message=message, context=context)
    if not hasattr(stream_obj, "__aiter__"):
        return _as_str(
            await asyncio.wait_for(
                llm_service.chat(message=message, context=context),
                timeout=safe_total_timeout,
            )
        )

    loop = asyncio.get_running_loop()
    started = loop.time()
    chunks: list[str] = []
    try:
        iterator = stream_obj.__aiter__()
        first_chunk = await asyncio.wait_for(iterator.__anext__(), timeout=min(safe_first_timeout, safe_total_timeout))
        first_text = _as_str(first_chunk)
        if first_text:
            chunks.append(first_text)
            if on_token is not None:
                await _await_maybe(on_token(first_text))

        while True:
            elapsed = loop.time() - started
            remaining = safe_total_timeout - elapsed
            if remaining <= 0:
                raise asyncio.TimeoutError("llm stream total timeout")
            try:
                chunk = await asyncio.wait_for(iterator.__anext__(), timeout=remaining)
            except StopAsyncIteration:
                break
            chunk_text = _as_str(chunk)
            if not chunk_text:
                continue
            chunks.append(chunk_text)
            if on_token is not None:
                await _await_maybe(on_token(chunk_text))

        return "".join(chunks)
    except asyncio.TimeoutError:
        raise
    except Exception:
        if chunks:
            return "".join(chunks)
        elapsed = loop.time() - started
        remaining = max(1, safe_total_timeout - elapsed)
        return _as_str(
            await asyncio.wait_for(
                llm_service.chat(message=message, context=context),
                timeout=remaining,
            )
        )
