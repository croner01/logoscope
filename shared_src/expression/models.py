"""Expression — 结构化条件表达式。

v15: 替代字符串 precondition/postcondition。
     field + operator + value 的可编程格式。
"""
from dataclasses import dataclass
from typing import Any, Optional


VALID_OPERATORS = frozenset({
    "==", "!=", "in", "not_in", "exists", "not_exists", "contains",
})


@dataclass
class Expression:
    """
    结构化条件表达式——可被 Planner 和 Policy 自动求值。

    field: "resource.status", "host.host_status", "ssh.accessible"
    operator: "==", "!=", "in", "not_in", "exists", "not_exists", "contains"
    value: 比较值（None for "exists", "not_exists"）
    """

    field: str = ""
    operator: str = "=="
    value: Any = None

    def __post_init__(self):
        """初始化时验证操作符合法性。"""
        if self.operator not in VALID_OPERATORS:
            raise ValueError(
                f"Unknown operator: '{self.operator}'. "
                f"Valid operators: {sorted(VALID_OPERATORS)}"
            )

    def evaluate(self, worldview, entity_type: str, entity_name: str) -> bool:
        """使用 WorldView 求值。"""
        state = getattr(worldview, "state", worldview)
        actual = state.resolve_field(self.field, entity_type, entity_name)

        if self.operator == "==":
            return actual == self.value
        elif self.operator == "!=":
            return actual != self.value
        elif self.operator == "in":
            return actual in (self.value or [])
        elif self.operator == "not_in":
            return actual not in (self.value or [])
        elif self.operator == "exists":
            return actual is not None
        elif self.operator == "not_exists":
            return actual is None
        elif self.operator == "contains":
            # 支持所有容器类型 (list, str, dict, tuple, set, frozenset)
            if actual is None:
                return False
            try:
                return self.value in actual
            except TypeError:
                return False
        # 操作符已在 __post_init__ 验证，不会走到这里
        return False

    def __str__(self) -> str:
        return f"{self.field} {self.operator} {self.value}"


# 预定义常用 Expression
def expr_status_eq(status: str) -> Expression:
    return Expression(field="resource.status", operator="==", value=status)


def expr_host_alive() -> Expression:
    return Expression(field="host.host_status", operator="==", value="alive")


def expr_service_exists() -> Expression:
    return Expression(field="service.exists", operator="==", value=True)


def expr_not_pinned() -> Expression:
    return Expression(field="vm.pinned_to_host", operator="!=", value=True)
