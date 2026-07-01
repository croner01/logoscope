import pytest
from shared_src.expression.models import Expression
from semantic_engine.knowledge.constraint import Constraint


class TestConstraint:
    def test_constraint_uses_expression(self):
        """Constraint 使用 Expression 表达条件"""
        constraint = Constraint(
            constraint_id="c-001",
            applies_to="restart_service",
            condition=Expression("time.hour", "in", [9, 10, 11, 12, 13, 14, 15, 16, 17]),
            restriction="No restart during business hours",
            severity="warning",
        )
        assert isinstance(constraint.condition, Expression)
        assert constraint.severity == "warning"

    def test_constraint_error_severity(self):
        constraint = Constraint(
            constraint_id="c-002",
            applies_to="delete_volume",
            condition=Expression("resource.status", "==", "in_use"),
            restriction="Cannot delete volume that is in use",
            severity="error",
        )
        assert constraint.severity == "error"

    def test_constraint_defaults(self):
        constraint = Constraint(
            constraint_id="c-003",
            applies_to="*",
            restriction="Generic constraint",
        )
        assert constraint.condition is None
        assert constraint.severity == "error"
