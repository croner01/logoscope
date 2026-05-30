"""Pytest fixtures for SSH Gateway tests."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app():
    from app import app as _app

    return _app


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def mock_subprocess_run():
    with patch("app.subprocess.run") as mock:
        mock.return_value.returncode = 0
        mock.return_value.stdout = "node-3\n"
        mock.return_value.stderr = ""
        yield mock


@pytest.fixture
def node_config_fixture(tmp_path):
    """Create a temporary node config file."""
    import yaml

    config = {
        "node-3": {
            "host": "10.0.0.1",
            "user": "root",
            "port": 22,
            "key_file": "/etc/ssh-keys/node-3/id_rsa",
        }
    }
    config_dir = tmp_path / "ssh-hosts"
    config_dir.mkdir()
    config_path = config_dir / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump(config, f)
    return str(config_path)
