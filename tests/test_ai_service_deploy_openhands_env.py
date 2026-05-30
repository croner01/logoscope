"""Deployment manifest checks for OpenHands runtime v4 flags."""

from pathlib import Path


def test_ai_service_manifest_declares_openhands_flag_disabled_by_default():
    manifest = (Path(__file__).resolve().parents[1] / "deploy" / "ai-service.yaml").read_text(
        encoding="utf-8"
    )

    assert "name: AI_RUNTIME_V4_OPENHANDS_ENABLED" in manifest
    assert 'value: "false"' in manifest
    assert "name: AI_RUNTIME_V4_OPENHANDS_HELPER_ENABLED" in manifest
    assert "name: AI_RUNTIME_V4_OPENHANDS_HELPER_PYTHON" in manifest
    assert "name: AI_RUNTIME_V4_OPENHANDS_HELPER_SCRIPT" in manifest
