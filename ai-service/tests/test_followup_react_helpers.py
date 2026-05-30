"""
Tests for followup react memory helpers.
"""

from ai.followup_react_helpers import _build_followup_react_memory, _merge_reflection_with_react_memory


def test_build_followup_react_memory_extracts_next_actions_and_failed_commands():
    messages = [
        {
            "role": "assistant",
            "metadata": {
                "react_loop": {
                    "summary": "plan=2, observed=1, success=0, failed=1, replan=true",
                    "replan": {"needed": True, "next_actions": ["复核并重试命令：echo health-check"]},
                },
                "action_observations": [
                    {
                        "status": "failed",
                        "command": "echo health-check",
                    }
                ],
            },
        }
    ]
    memory = _build_followup_react_memory(messages)
    assert memory["hits"] == 1
    assert "复核并重试命令：echo health-check" in memory["next_actions"]
    assert "echo health-check" in memory["failed_commands"]


def test_merge_reflection_with_react_memory_prioritizes_memory_actions():
    reflection = {"next_actions": ["输出可观测指标与回归脚本清单"]}
    react_memory = {
        "next_actions": ["复核并重试命令：echo health-check"],
        "failed_commands": ["echo health-check"],
    }
    merged = _merge_reflection_with_react_memory(reflection, react_memory)
    assert merged["next_actions"][0] == "复核并重试命令：echo health-check"
    assert any("echo health-check" in item for item in merged["next_actions"])
    assert merged.get("react_memory_loaded") is True
