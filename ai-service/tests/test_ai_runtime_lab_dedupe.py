"""Tests for ai-runtime-lab duplicate command dedupe helpers."""

from api.ai import _extract_success_commands_from_assistant_message


def test_extract_success_commands_from_assistant_message_filters_failed_and_dedupes():
    message = {
        "metadata": {
            "action_observations": [
                {"status": "executed", "exit_code": 0, "command": "kubectl get pods -n islap"},
                {"status": "executed", "exit_code": 1, "command": "kubectl get events -n islap"},
                {"status": "skipped", "exit_code": 0, "command": "kubectl get pods -n islap"},
                {"status": "executed", "exit_code": 0, "command": "kubectl   get  pods -n islap"},
            ]
        }
    }

    commands = _extract_success_commands_from_assistant_message(message)

    assert commands == ["kubectl get pods -n islap"]
