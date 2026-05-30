"""Tests for SSH Gateway service."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest
import yaml


class TestHealth:
    def test_health_returns_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestExecCommand:
    def test_missing_command_returns_400(self, client):
        resp = client.post("/exec", data={"node": "node-3"})
        assert resp.status_code == 400
        assert "command" in resp.text

    def test_missing_node_returns_400(self, client):
        resp = client.post("/exec", data={"command": "hostname"})
        assert resp.status_code == 400
        assert "node" in resp.text

    def test_empty_command_returns_400(self, client):
        resp = client.post("/exec", data={"command": "", "node": "node-3"})
        assert resp.status_code == 400

    def test_unknown_node_returns_400(self, client, tmp_path):
        config_dir = tmp_path / "ssh-hosts"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        with open(config_path, "w") as f:
            yaml.dump(
                {"node-3": {"host": "10.0.0.1", "user": "root", "port": 22}}, f
            )

        with patch("app._HOSTS_CONFIG", config_path):
            resp = client.post(
                "/exec", data={"command": "hostname", "node": "nonexistent"}
            )
        assert resp.status_code == 400
        assert "Unknown node" in resp.text

    def test_shell_injection_blocked(self, client):
        """Shell operator tokens like ; should be rejected."""
        resp = client.post(
            "/exec", data={"command": "hostname; rm -rf /", "node": "node-3"}
        )
        assert resp.status_code == 403

    def test_invalid_shell_syntax_blocked(self, client):
        """Unmatched quotes should be rejected."""
        resp = client.post(
            "/exec", data={"command": "echo 'hello", "node": "node-3"}
        )
        assert resp.status_code == 403

    def test_successful_execution(self, client, mock_subprocess_run, tmp_path):
        config_dir = tmp_path / "ssh-hosts"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        with open(config_path, "w") as f:
            yaml.dump(
                {
                    "node-3": {
                        "host": "10.0.0.1",
                        "user": "root",
                        "port": 22,
                        "key_file": "/tmp/key",
                    }
                },
                f,
            )

        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "node-3\n"
        mock_subprocess_run.return_value.stderr = ""

        with patch("app._HOSTS_CONFIG", config_path):
            resp = client.post(
                "/exec", data={"command": "hostname", "node": "node-3"}
            )

        assert resp.status_code == 200
        assert resp.text == "node-3\n"

    def test_command_failure_returns_500(
        self, client, mock_subprocess_run, tmp_path
    ):
        config_dir = tmp_path / "ssh-hosts"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        with open(config_path, "w") as f:
            yaml.dump(
                {
                    "node-3": {
                        "host": "10.0.0.1",
                        "user": "root",
                        "port": 22,
                        "key_file": "/tmp/key",
                    }
                },
                f,
            )

        mock_subprocess_run.return_value.returncode = 1
        mock_subprocess_run.return_value.stdout = ""
        mock_subprocess_run.return_value.stderr = "command not found"

        with patch("app._HOSTS_CONFIG", config_path):
            resp = client.post(
                "/exec", data={"command": "nonexistent", "node": "node-3"}
            )

        assert resp.status_code == 500
        assert "command not found" in resp.text

    def test_timeout_returns_504(self, client, mock_subprocess_run, tmp_path):
        config_dir = tmp_path / "ssh-hosts"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        with open(config_path, "w") as f:
            yaml.dump(
                {
                    "node-3": {
                        "host": "10.0.0.1",
                        "user": "root",
                        "port": 22,
                        "key_file": "/tmp/key",
                    }
                },
                f,
            )

        mock_subprocess_run.side_effect = subprocess.TimeoutExpired(
            cmd="ssh", timeout=5
        )

        with patch("app._HOSTS_CONFIG", config_path):
            resp = client.post(
                "/exec", data={"command": "sleep 100", "node": "node-3"}
            )

        assert resp.status_code == 504

    def test_json_body_accepted(self, client, mock_subprocess_run, tmp_path):
        config_dir = tmp_path / "ssh-hosts"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        with open(config_path, "w") as f:
            yaml.dump(
                {
                    "node-3": {
                        "host": "10.0.0.1",
                        "user": "root",
                        "port": 22,
                        "key_file": "/tmp/key",
                    }
                },
                f,
            )

        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "ok\n"
        mock_subprocess_run.return_value.stderr = ""

        with patch("app._HOSTS_CONFIG", config_path):
            resp = client.post(
                "/exec", json={"command": "echo ok", "node": "node-3"}
            )

        assert resp.status_code == 200
        assert resp.text == "ok\n"

    def test_output_truncation(self, client, mock_subprocess_run, tmp_path):
        config_dir = tmp_path / "ssh-hosts"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        with open(config_path, "w") as f:
            yaml.dump(
                {
                    "node-3": {
                        "host": "10.0.0.1",
                        "user": "root",
                        "port": 22,
                        "key_file": "/tmp/key",
                    }
                },
                f,
            )

        big_output = "x" * 300000
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = big_output
        mock_subprocess_run.return_value.stderr = ""

        with patch("app._HOSTS_CONFIG", config_path):
            with patch("app._MAX_OUTPUT_BYTES", 1024):
                resp = client.post(
                    "/exec", data={"command": "big_output", "node": "node-3"}
                )

        assert resp.status_code == 200
        assert len(resp.text) < 2000
        assert "truncated" in resp.text


class TestClipOutput:
    def test_clip_within_limit(self):
        from app import _clip_output

        result = _clip_output("hello", max_bytes=100)
        assert result == "hello"

    def test_clip_exceeds_limit(self):
        from app import _clip_output

        result = _clip_output("x" * 100, max_bytes=10)
        assert len(result) < 50
        assert "truncated" in result


class TestValidateCommandSafety:
    def test_valid_command(self):
        from app import _validate_command_safety

        assert (
            _validate_command_safety(
                "journalctl -u nova-scheduler --no-pager"
            )
            is None
        )

    def test_semicolon_injection(self):
        from app import _validate_command_safety

        assert (
            _validate_command_safety("hostname; rm -rf /") is not None
        )

    def test_pipe_rejected(self):
        from app import _validate_command_safety

        assert _validate_command_safety("cmd1 | cmd2") is not None

    def test_redirect_rejected(self):
        from app import _validate_command_safety

        assert (
            _validate_command_safety("echo hello > /etc/passwd") is not None
        )

    def test_backtick_injection(self):
        from app import _validate_command_safety

        assert _validate_command_safety("echo `rm -rf /`") is not None

    def test_unmatched_quote(self):
        from app import _validate_command_safety

        assert _validate_command_safety("echo 'hello") is not None
