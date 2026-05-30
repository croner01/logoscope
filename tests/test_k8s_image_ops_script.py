"""Contract checks for scripts/k8s-image-ops.sh."""

from pathlib import Path


def test_k8s_image_ops_builds_from_script_project_root():
    script = (Path(__file__).resolve().parents[1] / "scripts" / "k8s-image-ops.sh").read_text(
        encoding="utf-8"
    )

    assert "PROJECT_ROOT=" in script
    assert "cd \"$PROJECT_ROOT\"" in script
    assert "cd /root/logoscope" not in script
