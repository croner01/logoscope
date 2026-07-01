"""
Capability 数据模型。

v15: 与 Expression 集成，替代字符串 precondition/postcondition。
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

# 受控副作用词汇表——Blast Radius 和 Policy 依赖此词汇来理解操作影响。
# 新增副作用时需同时更新映射规则（见 blast_radius/analyzer.py）。
VALID_EFFECTS: List[str] = [
    "service.restart",      # 服务重启——临时中断
    "service.stop",         # 服务停止——永久停服
    "service.start",        # 服务启动
    "process.modify",       # 进程/配置修改
    "vm.migrate",           # VM 迁移——网络临时中断
    "vm.create",            # 创建 VM
    "vm.delete",            # 删除 VM——永久
    "network.modify",       # 网络变更
    "storage.create",       # 创建存储
    "storage.delete",       # 删除存储——永久数据丢失
    "storage.attach",       # 挂载存储
    "storage.detach",       # 卸载存储
    "config.update",        # 配置更新
    "cluster.modify",       # 集群操作
    "diagnostic.collect",   # 诊断采集——只读
    "read",                 # 只读操作
]


@dataclass
class ParameterDef:
    """Capability 参数定义。"""
    name: str = ""
    type: str = "string"  # string, integer, boolean, select
    required: bool = False
    default: Any = None
    description: str = ""
    choices: List[str] = field(default_factory=list)


@dataclass
class Capability:
    """
    Capability — 可执行的操作能力。

    通过 Expression 表达前置/后置条件，ImpactModel 表达影响评估。
    """
    capability_id: str = ""
    provider: str = ""
    effects: List[str] = field(default_factory=list)
    base_risk: int = 50  # v15 规范：默认风险 50（中等）
    preconditions: List[Any] = field(default_factory=list)  # List[Expression]
    postconditions: List[Any] = field(default_factory=list)  # List[Expression]
    impact_model: Any = None
    rollback_capability: str = ""
    estimated_duration_ms: int = 0
    estimated_cost: float = 0.0
    parameters: List[ParameterDef] = field(default_factory=list)
    description: str = ""

    def __post_init__(self):
        """验证 effect 字符串合法性（仅警告，不阻断）。"""
        for e in self.effects:
            if e not in VALID_EFFECTS:
                logger.warning(
                    "Unknown effect '%s' on capability '%s'. "
                    "Add to VALID_EFFECTS in capability/models.py "
                    "for Blast Radius and Policy accuracy.",
                    e, self.capability_id or "unnamed",
                )
