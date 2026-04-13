"""
LangChain follow-up 提示词模板。
"""

from typing import Any, Dict

try:
    from langchain_core.prompts import PromptTemplate
except Exception:  # pragma: no cover - 依赖可选
    PromptTemplate = None


FOLLOWUP_SYSTEM_PROMPT = """你是 SRE/可观测性专家，回答必须严格遵守：
1) 只基于已给证据，不编造日志、trace、调用链；
2) 优先输出"结论 → 请求流程 → 根因 → 修复步骤 → 验证/回滚"；
3) 若证据不足，必须明确 missing_evidence；
4) 输出 JSON，不要 markdown；
5) actions 必须优先输出 command_spec（结构化命令），command 仅作兼容字段：能用 command_spec 就不要拼自由文本 shell；
6) actions 必须提供 command_spec（tool + args）。SQL 查询优先用 kubectl_clickhouse_query（默认提供 target_kind=clickhouse_cluster、target_identity=database:<db>、query、timeout_s；仅旧链路兼容时才提供 pod_selector）；非 SQL 的系统查询命令用 generic_exec（必须提供 command 或 command_argv、target_kind、target_identity、timeout_s）。由系统编译成可执行命令，禁止自行压缩空格或拼接紧凑 shell；
7) command 需可执行且安全：默认优先使用 kubectl/rg/grep/cat/tail/head/jq/ls/echo/pwd 等当前自动执行链路稳定支持的只读命令；只有明确需要 HTTP/数据库直接取证时，再使用 curl（仅 GET/HEAD 或 -G 查询）或 clickhouse-client/clickhouse（仅 SELECT/SHOW/DESCRIBE/EXPLAIN 只读查询）；禁止脚本化链式拼接（| && || ;）与重定向（> >> < <<）及后台执行（&）；每个 action 只允许一条单步命令，pipeline_steps 最多 2-3 步；命令必须保留标准空格分词（命令、flag、参数之间要有空格），禁止输出 logs--tail / grep-ierror / head-20 / -it$(...) 这类紧凑写法；
8) 不能给可执行命令时，明确 executable=false 与 reason，不要伪造命令；禁止用 echo/printf 把人工说明、页面操作提示、监控检查建议包装成"伪命令"。
9) 必须遵守闭环顺序：先给"当前总结"，再基于 missing_evidence 生成命令；命令观察后再总结是否收敛；若未收敛继续补证据，直到可以给出最终结论。
10) 若上下文中列出了可用诊断技能（Diagnostic Skills），优先在 actions 中通过 skill_name 字段引用技能，系统会自动展开为结构化命令链；仅在技能不覆盖时才手动构造 command_spec。
"""


FOLLOWUP_USER_TEMPLATE = """问题：
{question}

会话记忆摘要：
{memory_summary}

跨会话历史记忆（长期）：
{long_term_memory_summary}

最近对话：
{recent_history}

任务拆解：
{subgoals_json}

反思结果：
{reflection_json}

可用工具观测：
{tool_observations_json}

证据片段：
{references_json}

{skill_catalog}{project_knowledge}
输出格式要求：
{format_instructions}
"""


def build_skill_catalog_section(analysis_context: Dict[str, Any]) -> str:
    """
    Build the skill catalog section for prompt injection.

    Returns an empty string if no skills are registered or the context
    doesn't warrant skill injection.
    """
    try:
        from ai.skills.base import SkillContext
        from ai.skills.matcher import build_skill_catalog_for_prompt

        skill_ctx = SkillContext.from_dict(analysis_context if isinstance(analysis_context, dict) else {})
        catalog = build_skill_catalog_for_prompt(skill_ctx, max_skills=4)
        if catalog:
            return catalog + "\n\n"
        return ""
    except Exception:
        return ""


def build_project_knowledge_section(analysis_context: Dict[str, Any]) -> str:
    """Build the project knowledge section for prompt injection."""
    safe_context = analysis_context if isinstance(analysis_context, dict) else {}
    prompt_block = str(safe_context.get("project_knowledge_prompt") or "").strip()
    if not prompt_block:
        return ""
    return f"## 项目知识（Project Knowledge）\n{prompt_block}\n\n"


def build_followup_prompt(payload: Dict[str, Any]) -> str:
    """构建追问提示词。"""
    safe_payload = dict(payload)
    # Inject skill catalog if not already provided
    if "skill_catalog" not in safe_payload:
        analysis_context = safe_payload.get("analysis_context") or {}
        safe_payload["skill_catalog"] = build_skill_catalog_section(analysis_context)
    if "project_knowledge" not in safe_payload:
        analysis_context = safe_payload.get("analysis_context") or {}
        safe_payload["project_knowledge"] = build_project_knowledge_section(analysis_context)

    if PromptTemplate is None:
        return FOLLOWUP_USER_TEMPLATE.format(**safe_payload)

    template = PromptTemplate.from_template(FOLLOWUP_USER_TEMPLATE)
    return template.format(**safe_payload)
