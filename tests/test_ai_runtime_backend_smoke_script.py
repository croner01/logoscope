"""Contract checks for scripts/ai-runtime-backend-smoke.sh."""

from pathlib import Path


def test_backend_smoke_includes_openhands_branch():
    script = (Path(__file__).resolve().parents[1] / "scripts" / "ai-runtime-backend-smoke.sh").read_text(
        encoding="utf-8"
    )

    assert "SMOKE_OPENHANDS" in script
    assert "case7_openhands_create_run_contract" in script
    assert "case8_openhands_preview_actions_contract" in script
    assert "case9_openhands_preview_action_exec_path" in script
    assert "'runtime_backend': 'openhands'" in script
