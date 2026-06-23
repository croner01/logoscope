"""
LangChain follow-up 结构化输出模型。
"""

from typing import List, Literal, Optional, Union

from pydantic import BaseModel, Field


class RootCauseItem(BaseModel):
    """根因候选。"""

    title: str = ""
    confidence: str = "medium"
    evidence_ids: List[str] = Field(default_factory=list)


class ActionCommandSpecArgs(BaseModel):
    """结构化命令参数（ActionSpec v2）。"""

    namespace: str = "islap"
    pod_selector: str = ""
    target_kind: str = "clickhouse_cluster"
    target_identity: str = "database:logs"
    target_id: str = ""
    query: str
    timeout_s: int = 60


class ActionCommandSpec(BaseModel):
    """结构化命令定义（ActionSpec v2）。"""

    tool: Literal["kubectl_clickhouse_query"]
    args: ActionCommandSpecArgs


class GenericExecCommandSpecArgs(BaseModel):
    """通用只读命令参数（受 command_spec 编译器约束）。"""

    command: str = ""
    command_argv: List[str] = Field(default_factory=list)
    target_kind: str = "runtime_node"
    target_identity: str = "runtime:local"
    timeout_s: int = 60


class GenericExecCommandSpec(BaseModel):
    """generic_exec 结构化命令定义。"""

    tool: Literal["generic_exec"]
    args: GenericExecCommandSpecArgs


class ActionItem(BaseModel):
    """执行动作。"""

    priority: int = 1
    title: str = ""
    action: str = ""
    skill_name: str = ""
    command: str = Field(
        default="",
        description="display_only: 仅用于 UI 展示，不参与执行分支。",
    )
    command_spec: Optional[Union[ActionCommandSpec, GenericExecCommandSpec]] = None
    command_type: str = "unknown"
    risk_level: str = "high"
    executable: bool = False
    requires_write_permission: bool = False
    requires_elevation: bool = False
    requires_confirmation: bool = True
    expected_outcome: str = ""
    reason: str = ""


class StructuredAnswer(BaseModel):
    """追问分析结构化答案。"""

    conclusion: str = ""
    request_flow: List[str] = Field(default_factory=list)
    root_causes: List[RootCauseItem] = Field(default_factory=list)
    actions: List[ActionItem] = Field(default_factory=list)
    verification: List[str] = Field(default_factory=list)
    rollback: List[str] = Field(default_factory=list)
    missing_evidence: List[str] = Field(default_factory=list)
    summary: str = ""
