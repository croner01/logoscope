"""
LangChain 追问运行时入口。
"""

from typing import Any, Dict, List

__all__ = ["run_followup_langchain"]


async def run_followup_langchain(
    *,
    question: str,
    analysis_context: Dict[str, Any],
    compacted_history: List[Dict[str, Any]],
    compacted_summary: str,
    references: List[Dict[str, str]],
    subgoals: List[Dict[str, Any]],
    reflection: Dict[str, Any],
    long_term_memory: Dict[str, Any],
    llm_enabled: bool,
    llm_requested: bool,
    token_budget: int,
    token_warning: bool,
    llm_timeout_seconds: int,
    llm_service: Any,
    fallback_builder: Any,
    llm_first_token_timeout_seconds: int = 20,
    stream_token_callback: Any = None,
) -> Dict[str, Any]:
    """惰性导入运行时，避免 package import 带入重依赖。"""
    from ai.langchain_runtime.service import run_followup_langchain as _run_followup_langchain

    return await _run_followup_langchain(
        question=question,
        analysis_context=analysis_context,
        compacted_history=compacted_history,
        compacted_summary=compacted_summary,
        references=references,
        subgoals=subgoals,
        reflection=reflection,
        long_term_memory=long_term_memory,
        llm_enabled=llm_enabled,
        llm_requested=llm_requested,
        token_budget=token_budget,
        token_warning=token_warning,
        llm_timeout_seconds=llm_timeout_seconds,
        llm_first_token_timeout_seconds=llm_first_token_timeout_seconds,
        llm_service=llm_service,
        fallback_builder=fallback_builder,
        stream_token_callback=stream_token_callback,
    )
