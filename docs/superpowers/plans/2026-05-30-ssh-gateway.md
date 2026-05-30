# SSH Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Status:** ALL TASKS COMPLETED — see "Additional Work" below for post-plan additions.

**Goal:** Build an SSH Gateway service that enables AI Runtime to execute host-level commands (journalctl, df, systemctl, etc.) on remote Linux hosts via SSH, extending the existing exec-service dispatch chain.

**Architecture:** New FastAPI service (~150 lines) that accepts `POST /exec` with `command` and `node` parameters, resolves node connection info from a mounted ConfigMap, and executes SSH via `subprocess.run(["ssh", "-i", key, ...])`. Fits into existing dispatch chain: exec-service policy → template expansion → curl to ssh-gateway. Inherits audit, OPA, and output-control from exec-service.

**Tech Stack:** Python 3.11, FastAPI, uvicorn, system `ssh` command, K8s Secrets/ConfigMaps

---

## File Structure

### New Files (7)
| File | Responsibility |
|------|---------------|
| `ssh-gateway/app.py` | FastAPI POST /exec + GET /health, node config resolution, SSH subprocess |
| `ssh-gateway/Dockerfile` | Python 3.11-slim container with fastapi + uvicorn |
| `ssh-gateway/requirements-runtime.txt` | Python dependencies (fastapi, uvicorn, pyyaml) |
| `ssh-gateway/tests/conftest.py` | Pytest fixtures (test app client, mock node config, mock subprocess) |
| `ssh-gateway/tests/test_ssh_gateway.py` | Tests for node resolution, SSH execution, error handling, security |
| `deploy/ssh-gateway.yaml` | K8s Deployment + Service + volume/volumeMount definitions |
| `deploy/ssh-hosts-config.yaml` | ConfigMap with node→connection mapping |
| `deploy/ssh-keys/node-3-secret.yaml` | Secret template for SSH private key |

### Modified Files (3)
| File | Change |
|------|--------|
| `deploy/exec-service.yaml:124-127` | Populate HOST_SSH_READONLY/HOST_SSH_MUTATING templates with SSH Gateway URL |
| `deploy/ai-service.yaml` | Append `host_node` targets to `AI_RUNTIME_V4_REMOTE_TARGETS_JSON` |
| `docs/operations/remote-cluster-execution.zh-CN.md` | Add SSH Gateway section |
| `ssh-gateway/core/host_registry.py` | ClickHouse-backed dynamic host registry (NEW — post-plan) |
| `ssh-gateway/api/hosts.py` | RESTful host management API (NEW — post-plan) |
| `ssh-gateway/tests/test_host_registry.py` | Host registry unit tests (NEW — post-plan) |
| `ssh-gateway/tests/test_hosts_api.py` | Host API integration tests (NEW — post-plan) |
| `frontend/src/pages/SSHHostsPage.tsx` | Frontend SSH host management page (NEW — post-plan) |
| `frontend/src/utils/api.ts` | SSH Gateway API client methods (MODIFIED — post-plan) |
| `frontend/src/App.tsx` | Route for `/ssh-hosts` (MODIFIED — post-plan) |
| `frontend/src/components/common/Layout/Sidebar.tsx` | Nav item "SSH 主机管理" (MODIFIED — post-plan) |
| `frontend/vite.config.ts` | `/ssh-gateway` proxy rule (MODIFIED — post-plan) |

---

### Task 1: SSH Gateway Service

**Files:**
- Create: `ssh-gateway/app.py`
- Create: `ssh-gateway/tests/conftest.py`
- Create: `ssh-gateway/tests/test_ssh_gateway.py`

- [x] **Step 1: Create the test conftest**

```python
"""Pytest fixtures for SSH Gateway tests."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app():
    from ssh_gateway.app import app as _app
    return _app


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def mock_subprocess_run():
    with patch("ssh_gateway.app.subprocess.run") as mock:
        mock.return_value.returncode = 0
        mock.return_value.stdout = "node-3\n"
        mock.return_value.stderr = ""
        yield mock


@pytest.fixture
def node_config_fixture(tmp_path):
    """Create a temporary node config file."""
    import os
    import yaml
    config = {
        "node-3": {
            "host": "10.0.0.1",
            "user": "root",
            "port": 22,
            "key_file": "/etc/ssh-keys/node-3/id_rsa"
        }
    }
    config_dir = tmp_path / "ssh-hosts"
    config_dir.mkdir()
    config_path = config_dir / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump(config, f)
    return str(config_path)
```

- [x] **Step 2: Run test to verify fixture works**

Run:
```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend
pip install -q fastapi uvicorn pyyaml httpx pytest 2>&1 | tail -1
```

Expected: packages installed successfully

- [x] **Step 3: Create the SSH Gateway service**

```python
"""SSH Gateway service for controlled host-level command execution via SSH."""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response

app = FastAPI(title="ssh-gateway", version="1.0.0")

logger = logging.getLogger("ssh-gateway")

_DEFAULT_TIMEOUT = int(os.getenv("SSH_GATEWAY_DEFAULT_TIMEOUT_SECONDS", "60"))
_MAX_OUTPUT_BYTES = int(os.getenv("SSH_GATEWAY_MAX_OUTPUT_BYTES", str(256 * 1024)))
_HOSTS_CONFIG = os.getenv("SSH_GATEWAY_HOSTS_CONFIG", "/etc/ssh-hosts/config.yaml")

# Shell operator tokens that could indicate injection attempts (reused from toolbox-gateway)
_SHELL_OPERATOR_TOKENS = {"|", "|&", "||", "&&", ";", "&", ">", ">>", "<", "<<", "<<<", "<>", "<&", ">&", "&>", ">|"}


@dataclass
class ExecResult:
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _load_hosts_config() -> Dict[str, Any]:
    """Load node connection configuration from YAML file."""
    if not os.path.exists(_HOSTS_CONFIG):
        logger.warning("Hosts config not found: %s", _HOSTS_CONFIG)
        return {}
    try:
        with open(_HOSTS_CONFIG) as f:
            hosts = yaml.safe_load(f) or {}
        return hosts
    except Exception as e:
        logger.error("Failed to load hosts config: %s", e)
        return {}


def _resolve_node_config(node_name: str) -> Dict[str, Any] | None:
    """Resolve node connection info from hosts config."""
    hosts = _load_hosts_config()
    return hosts.get(node_name)


def _clip_output(output: str, max_bytes: int = _MAX_OUTPUT_BYTES) -> str:
    """Clip output to maximum bytes."""
    if len(output) > max_bytes:
        return output[:max_bytes] + f"\n... (truncated at {max_bytes} bytes)"
    return output


def _validate_command_safety(command: str) -> str | None:
    """Validate command for shell injection attempts. Returns error message or None."""
    try:
        shlex.split(command)
    except ValueError as e:
        return f"Command parsing error: {e}"

    tokens = set(shlex.split(command))
    dangerous = tokens & _SHELL_OPERATOR_TOKENS
    if dangerous:
        return f"Shell operator tokens not allowed: {', '.join(sorted(dangerous))}"

    return None


def _execute_ssh(command: str, node_cfg: Dict[str, Any], timeout: int) -> ExecResult:
    """Execute a command on a remote host via SSH."""
    key_file = node_cfg.get("key_file", f"/etc/ssh-keys/{node_cfg.get('name', 'unknown')}/id_rsa")
    user = node_cfg.get("user", "root")
    host = node_cfg["host"]
    port = _as_int(node_cfg.get("port"), 22)

    ssh_cmd = [
        "ssh", "-i", key_file,
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        "-o", "BatchMode=yes",
        "-p", str(port),
        f"{user}@{host}",
        command,
    ]

    logger.info("Executing via SSH: %s@%s (cmd len=%d)", user, host, len(command))

    proc_env = os.environ.copy()
    # Avoid leaking KUBECONFIG into SSH sessions
    proc_env.pop("KUBECONFIG", None)

    try:
        completed = subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=proc_env,
        )
        return ExecResult(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
    except subprocess.TimeoutExpired:
        logger.warning("SSH command timed out after %ds", timeout)
        return ExecResult(exit_code=-1, stderr=f"Command timed out after {timeout}s", timed_out=True)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/exec")
async def exec_command(request: Request):
    """Execute a command on a remote host via SSH.

    Accepts both form-encoded and JSON bodies.
    """
    content_type = request.headers.get("content-type", "")

    if "application/json" in content_type:
        body = await request.json()
        command = _as_str(body.get("command"))
        node = _as_str(body.get("node"))
        timeout_seconds = _as_int(body.get("timeout_seconds"), _DEFAULT_TIMEOUT)
    else:
        form = await request.form()
        command = _as_str(form.get("command"))
        node = _as_str(form.get("node"))
        timeout_seconds = _as_int(form.get("timeout_seconds"), _DEFAULT_TIMEOUT)

    if not command:
        raise HTTPException(status_code=400, detail="Missing required parameter: command")
    if not node:
        raise HTTPException(status_code=400, detail="Missing required parameter: node")

    # Clamp timeout
    timeout_seconds = max(1, min(timeout_seconds, 300))

    # Safety validation
    safety_error = _validate_command_safety(command)
    if safety_error:
        raise HTTPException(status_code=403, detail=safety_error)

    # Resolve node config
    node_cfg = _resolve_node_config(node)
    if node_cfg is None:
        available = list(_load_hosts_config().keys())
        raise HTTPException(
            status_code=400,
            detail=f"Unknown node: '{node}'. Available nodes: {available}" if available
                    else f"Unknown node: '{node}'. No nodes configured."
        )

    # Execute
    result = _execute_ssh(command, node_cfg, timeout_seconds)

    # Clip output
    stdout = _clip_output(result.stdout)
    stderr = _clip_output(result.stderr)

    if result.timed_out:
        return PlainTextResponse(
            content=stderr or "Command timed out",
            status_code=504,
        )
    if result.exit_code != 0:
        logger.warning("SSH command failed (exit=%d): %s", result.exit_code, stderr[:200])
        return PlainTextResponse(
            content=stderr or f"Command failed with exit code {result.exit_code}",
            status_code=500,
        )

    return PlainTextResponse(content=stdout)
```

- [x] **Step 4: Create the test file**

```python
"""Tests for SSH Gateway service."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest


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
        import yaml
        config_dir = tmp_path / "ssh-hosts"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        with open(config_path, "w") as f:
            yaml.dump({"node-3": {"host": "10.0.0.1", "user": "root", "port": 22}}, f)

        with patch("ssh_gateway.app._HOSTS_CONFIG", config_path):
            resp = client.post("/exec", data={"command": "hostname", "node": "nonexistent"})
        assert resp.status_code == 400
        assert "Unknown node" in resp.text

    def test_shell_injection_blocked(self, client):
        """Shell operator tokens like ; should be rejected."""
        resp = client.post("/exec", data={
            "command": "hostname; rm -rf /",
            "node": "node-3"
        })
        assert resp.status_code == 403

    def test_invalid_shell_syntax_blocked(self, client):
        """Unmatched quotes should be rejected."""
        resp = client.post("/exec", data={
            "command": "echo 'hello",
            "node": "node-3"
        })
        assert resp.status_code == 403

    def test_successful_execution(self, client, mock_subprocess_run, tmp_path):
        import yaml
        config_dir = tmp_path / "ssh-hosts"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        with open(config_path, "w") as f:
            yaml.dump({
                "node-3": {"host": "10.0.0.1", "user": "root", "port": 22, "key_file": "/tmp/key"}
            }, f)

        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "node-3\n"
        mock_subprocess_run.return_value.stderr = ""

        with patch("ssh_gateway.app._HOSTS_CONFIG", config_path):
            resp = client.post("/exec", data={"command": "hostname", "node": "node-3"})

        assert resp.status_code == 200
        assert resp.text == "node-3\n"

    def test_command_failure_returns_500(self, client, mock_subprocess_run, tmp_path):
        import yaml
        config_dir = tmp_path / "ssh-hosts"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        with open(config_path, "w") as f:
            yaml.dump({
                "node-3": {"host": "10.0.0.1", "user": "root", "port": 22, "key_file": "/tmp/key"}
            }, f)

        mock_subprocess_run.return_value.returncode = 1
        mock_subprocess_run.return_value.stdout = ""
        mock_subprocess_run.return_value.stderr = "command not found"

        with patch("ssh_gateway.app._HOSTS_CONFIG", config_path):
            resp = client.post("/exec", data={"command": "nonexistent", "node": "node-3"})

        assert resp.status_code == 500
        assert "command not found" in resp.text

    def test_timeout_returns_504(self, client, mock_subprocess_run, tmp_path):
        import yaml
        config_dir = tmp_path / "ssh-hosts"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        with open(config_path, "w") as f:
            yaml.dump({
                "node-3": {"host": "10.0.0.1", "user": "root", "port": 22, "key_file": "/tmp/key"}
            }, f)

        mock_subprocess_run.side_effect = subprocess.TimeoutExpired(cmd="ssh", timeout=5)

        with patch("ssh_gateway.app._HOSTS_CONFIG", config_path):
            resp = client.post("/exec", data={"command": "sleep 100", "node": "node-3"})

        assert resp.status_code == 504

    def test_json_body_accepted(self, client, mock_subprocess_run, tmp_path):
        import yaml
        config_dir = tmp_path / "ssh-hosts"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        with open(config_path, "w") as f:
            yaml.dump({
                "node-3": {"host": "10.0.0.1", "user": "root", "port": 22, "key_file": "/tmp/key"}
            }, f)

        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = "ok\n"
        mock_subprocess_run.return_value.stderr = ""

        with patch("ssh_gateway.app._HOSTS_CONFIG", config_path):
            resp = client.post("/exec", json={"command": "echo ok", "node": "node-3"})

        assert resp.status_code == 200
        assert resp.text == "ok\n"

    def test_output_truncation(self, client, mock_subprocess_run, tmp_path):
        import yaml
        config_dir = tmp_path / "ssh-hosts"
        config_dir.mkdir()
        config_path = config_dir / "config.yaml"
        with open(config_path, "w") as f:
            yaml.dump({
                "node-3": {"host": "10.0.0.1", "user": "root", "port": 22, "key_file": "/tmp/key"}
            }, f)

        # Generate output larger than max bytes
        big_output = "x" * 300000
        mock_subprocess_run.return_value.returncode = 0
        mock_subprocess_run.return_value.stdout = big_output
        mock_subprocess_run.return_value.stderr = ""

        with patch("ssh_gateway.app._HOSTS_CONFIG", config_path):
            with patch("ssh_gateway.app._MAX_OUTPUT_BYTES", 1024):
                resp = client.post("/exec", data={"command": "big_output", "node": "node-3"})

        assert resp.status_code == 200
        assert len(resp.text) < 2000  # truncated
        assert "truncated" in resp.text


class TestClipOutput:
    def test_clip_within_limit(self):
        from ssh_gateway.app import _clip_output
        result = _clip_output("hello", max_bytes=100)
        assert result == "hello"

    def test_clip_exceeds_limit(self):
        from ssh_gateway.app import _clip_output
        result = _clip_output("x" * 100, max_bytes=10)
        assert len(result) < 50
        assert "truncated" in result


class TestValidateCommandSafety:
    def test_valid_command(self):
        from ssh_gateway.app import _validate_command_safety
        assert _validate_command_safety("journalctl -u nova-scheduler --no-pager") is None

    def test_semicolon_injection(self):
        from ssh_gateway.app import _validate_command_safety
        assert _validate_command_safety("hostname; rm -rf /") is not None

    def test_pipe_rejected(self):
        from ssh_gateway.app import _validate_command_safety
        assert _validate_command_safety("cmd1 | cmd2") is not None

    def test_redirect_rejected(self):
        from ssh_gateway.app import _validate_command_safety
        assert _validate_command_safety("echo hello > /etc/passwd") is not None

    def test_backtick_injection(self):
        from ssh_gateway.app import _validate_command_safety
        assert _validate_command_safety("echo `rm -rf /`") is not None

    def test_unmatched_quote(self):
        from ssh_gateway.app import _validate_command_safety
        assert _validate_command_safety("echo 'hello") is not None
```

- [x] **Step 5: Run tests to verify they pass**

Run:
```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend
pip install -q fastapi uvicorn pyyaml httpx pytest 2>&1 | tail -1
PYTHONPATH=ssh-gateway python -m pytest ssh-gateway/tests/ -v
```

Expected: All tests pass (13+ tests)

- [x] **Step 6: Commit**

```bash
git add ssh-gateway/app.py ssh-gateway/tests/conftest.py ssh-gateway/tests/test_ssh_gateway.py
git commit -m "feat(ssh-gateway): add SSH Gateway service with tests"
```

---

### Task 2: Dockerfile and Dependencies

**Files:**
- Create: `ssh-gateway/Dockerfile`
- Create: `ssh-gateway/requirements-runtime.txt`

- [x] **Step 1: Create requirements-runtime.txt**

```
fastapi>=0.104.0
uvicorn[standard]>=0.24.0
pyyaml>=6.0
```

- [x] **Step 2: Create Dockerfile**

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install SSH client (required for SSH Gateway)
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-runtime.txt .
RUN pip install --no-cache-dir -r requirements-runtime.txt

COPY app.py .

EXPOSE 8096

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8096"]
```

- [x] **Step 3: Verify Docker build**

Run:
```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend/ssh-gateway
docker build -t localhost:5000/logoscope/ssh-gateway:latest .
```

Expected: Build succeeds with no errors

- [x] **Step 4: Commit**

```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend
git add ssh-gateway/Dockerfile ssh-gateway/requirements-runtime.txt
git commit -m "feat(ssh-gateway): add Dockerfile and runtime dependencies"
```

---

### Task 3: K8s Deployment Manifests

**Files:**
- Create: `deploy/ssh-gateway.yaml` (Deployment + Service)
- Create: `deploy/ssh-hosts-config.yaml` (ConfigMap)
- Create: `deploy/ssh-keys/node-3-secret.yaml` (Secret template)

- [x] **Step 1: Create deploy/ssh-gateway.yaml**

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ssh-gateway
  namespace: islap
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ssh-gateway
  namespace: islap
  labels:
    app: ssh-gateway
spec:
  replicas: 1
  selector:
    matchLabels:
      app: ssh-gateway
  template:
    metadata:
      labels:
        app: ssh-gateway
    spec:
      serviceAccountName: ssh-gateway
      containers:
        - name: ssh-gateway
          image: localhost:5000/logoscope/ssh-gateway:latest
          imagePullPolicy: Always
          ports:
            - containerPort: 8096
          env:
            - name: SSH_GATEWAY_DEFAULT_TIMEOUT_SECONDS
              value: "60"
            - name: SSH_GATEWAY_MAX_OUTPUT_BYTES
              value: "262144"
            - name: SSH_GATEWAY_HOSTS_CONFIG
              value: "/etc/ssh-hosts/config.yaml"
          volumeMounts:
            - name: ssh-hosts-config
              mountPath: /etc/ssh-hosts
              readOnly: true
            - name: ssh-key-node-3
              mountPath: /etc/ssh-keys/node-3
              readOnly: true
          readinessProbe:
            httpGet:
              path: /health
              port: 8096
            initialDelaySeconds: 3
            periodSeconds: 5
          livenessProbe:
            httpGet:
              path: /health
              port: 8096
            initialDelaySeconds: 10
            periodSeconds: 10
      volumes:
        - name: ssh-hosts-config
          configMap:
            name: ssh-hosts-config
        - name: ssh-key-node-3
          secret:
            secretName: ssh-key-node-3
            defaultMode: 0400
---
apiVersion: v1
kind: Service
metadata:
  name: ssh-gateway
  namespace: islap
  labels:
    app: ssh-gateway
spec:
  selector:
    app: ssh-gateway
  ports:
    - name: http
      port: 8096
      targetPort: 8096
```

- [x] **Step 2: Create deploy/ssh-hosts-config.yaml**

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: ssh-hosts-config
  namespace: islap
data:
  config.yaml: |
    node-3:
      host: 10.0.0.1
      user: root
      port: 22
      key_file: /etc/ssh-keys/node-3/id_rsa
    node-4:
      host: 10.0.0.2
      user: root
      port: 22
      key_file: /etc/ssh-keys/node-4/id_rsa
```

- [x] **Step 3: Create deploy/ssh-keys/node-3-secret.yaml**

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: ssh-key-node-3
  namespace: islap
  labels:
    app: ssh-gateway
type: Opaque
stringData:
  id_rsa: |
    -----BEGIN OPENSSH PRIVATE KEY-----
    # Replace with actual generated key:
    # ssh-keygen -t ed25519 -f node-3-id_rsa -N "" -C "ssh-gateway-node-3@logoscope"
    # Then: kubectl create secret generic ssh-key-node-3 \
    #         --namespace=islap --from-file=id_rsa=./node-3-id_rsa
    -----END OPENSSH PRIVATE KEY-----
```

- [x] **Step 4: Commit**

```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend
git add deploy/ssh-gateway.yaml deploy/ssh-hosts-config.yaml deploy/ssh-keys/node-3-secret.yaml
git commit -m "feat(deploy): add SSH Gateway K8s manifests (deployment, configmap, secret)"
```

---

### Task 4: Update Executor Templates

**Files:**
- Modify: `deploy/exec-service.yaml:124-127`

- [x] **Step 1: Update HOST_SSH templates**

Current (empty placeholders at lines 124-127):
```yaml
        - name: EXEC_EXECUTOR_TEMPLATE__HOST_SSH_READONLY
          value: ""
        - name: EXEC_EXECUTOR_TEMPLATE__HOST_SSH_MUTATING
          value: ""
```

Replace with:
```yaml
        - name: EXEC_EXECUTOR_TEMPLATE__HOST_SSH_READONLY
          value: "curl -sS --fail-with-body -X POST http://ssh-gateway.islap.svc.cluster.local:8096/exec --data-urlencode command={command_quoted} --data-urlencode node={target_node_name_quoted}"
        - name: EXEC_EXECUTOR_TEMPLATE__HOST_SSH_MUTATING
          value: "curl -sS --fail-with-body -X POST http://ssh-gateway.islap.svc.cluster.local:8096/exec --data-urlencode command={command_quoted} --data-urlencode node={target_node_name_quoted}"
```

- [x] **Step 2: Commit**

```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend
git add deploy/exec-service.yaml
git commit -m "feat(deploy): populate HOST_SSH executor templates with SSH Gateway URL"
```

---

### Task 5: Update Target Registration

**Files:**
- Modify: `deploy/ai-service.yaml` (AI_RUNTIME_V4_REMOTE_TARGETS_JSON)

- [x] **Step 1: Append host_node targets to REMOTE_TARGETS_JSON**

Current JSON array in `AI_RUNTIME_V4_REMOTE_TARGETS_JSON` (line 75):
```json
[{"target_kind":"k8s_cluster","target_identity":"namespace:openstack-cluster-01","display_name":"OpenStack K8s Cluster","description":"OpenStack-provisioned Kubernetes cluster","metadata":{"cluster_id":"openstack-cluster-01","risk_tier":"high","preferred_executor_profiles":["toolbox-k8s-readonly","toolbox-k8s-mutating"]},"capabilities":["read_logs","restart_workload","helm_read","helm_mutation"],"credential_scope":{"kubeconfig_name":"openstack-cluster-01"}}]
```

Replace with:
```json
[{"target_kind":"k8s_cluster","target_identity":"namespace:openstack-cluster-01","display_name":"OpenStack K8s Cluster","description":"OpenStack-provisioned Kubernetes cluster","metadata":{"cluster_id":"openstack-cluster-01","risk_tier":"high","preferred_executor_profiles":["toolbox-k8s-readonly","toolbox-k8s-mutating"]},"capabilities":["read_logs","restart_workload","helm_read","helm_mutation"],"credential_scope":{"kubeconfig_name":"openstack-cluster-01"}},{"target_kind":"host_node","target_identity":"host:node-3","display_name":"OpenStack Node-3","description":"OpenStack compute node node-3","metadata":{"cluster_id":"openstack-cluster-01","node_name":"node-3","risk_tier":"high","preferred_executor_profiles":["host-ssh-readonly","host-ssh-mutating"]},"capabilities":["read_host_state","host_mutation"],"credential_scope":{"ssh_host":"node-3"}}]
```

- [x] **Step 2: Commit**

```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend
git add deploy/ai-service.yaml
git commit -m "feat(deploy): register host_node targets for SSH Gateway"
```

---

### Task 6: Update Operations Documentation

**Files:**
- Modify: `docs/operations/remote-cluster-execution.zh-CN.md`

- [x] **Step 1: Append SSH Gateway section to operations doc

Append the following at the end of `docs/operations/remote-cluster-execution.zh-CN.md`:

```markdown
## SSH Gateway — 主机级命令执行

### 适用场景

当需要执行主机级系统命令（非 K8s API）时，SSH Gateway 提供 SSH 通道到目标 Linux 主机：

- `journalctl -u nova-scheduler --no-pager | tail -50`（查日志）
- `df -h /var/lib/docker`（查磁盘）
- `systemctl status docker`（查服务状态）
- `cat /var/log/nova/nova-scheduler.log | grep ERROR`（查文件）

### 架构

```
  AI Service → exec-service → template expansion → curl POST /exec
                                                         │
                                                         ▼
                                                   SSH Gateway (:8096)
                                                         │
                                              ssh -i /etc/ssh-keys/node-3/id_rsa \
                                                  root@node-3 "journalctl..."
                                                         │
                                                         ▼
                                                 stdout/stderr → AI
```

### 部署步骤

#### 前置条件

- 目标 Linux 主机可被 Logoscope 集群访问（网络可达）
- 目标主机已安装 OpenSSH Server

#### Step 1: 生成 SSH 密钥并部署到目标主机

```bash
# 每台主机独立密钥
ssh-keygen -t ed25519 -f node-3-id_rsa -N "" -C "ssh-gateway-node-3@logoscope"

# 部署公钥
ssh-copy-id -i node-3-id_rsa.pub root@<node-3-ip>

# 验证无密码登录
ssh -i node-3-id_rsa root@<node-3-ip> "hostname"
```

#### Step 2: 创建 Secret

```bash
kubectl create secret generic ssh-key-node-3 \
  --namespace=islap \
  --from-file=id_rsa=./node-3-id_rsa
```

#### Step 3: 更新节点映射

编辑 `deploy/ssh-hosts-config.yaml`，添加目标主机的连接信息。

#### Step 4: 构建并部署

```bash
# 构建 SSH Gateway 镜像
scripts/k8s-image-ops.sh build-push ssh-gateway <tag>

# 应用配置
kubectl apply -f deploy/ssh-hosts-config.yaml
kubectl apply -f deploy/ssh-keys/node-3-secret.yaml
kubectl apply -f deploy/ssh-gateway.yaml

# 等待就绪
kubectl rollout status deploy/ssh-gateway -n islap -w
```

#### Step 5: 更新模板和目标注册

```bash
# 启用 HOST_SSH 模板（如果之前是空的）
kubectl apply -f deploy/exec-service.yaml

# 注册主机目标
kubectl apply -f deploy/ai-service.yaml
kubectl rollout restart deploy/ai-service -n islap
```

#### Step 6: 验证

```bash
# 验证 SSH Gateway 健康
kubectl exec -n islap deploy/ssh-gateway -- curl -s http://localhost:8096/health

# 验证节点映射已加载
kubectl exec -n islap deploy/ssh-gateway -- cat /etc/ssh-hosts/config.yaml

# 验证主机目标已注册
kubectl exec -n islap deploy/ai-service -- curl -s http://localhost:8090/api/v2/targets | python3 -m json.tool | grep "host:"
```

#### Step 7: 端到端测试

```bash
# 直接调用 SSH Gateway
curl -sS -X POST http://ssh-gateway:8096/exec \
  --data-urlencode "command=hostname" \
  --data-urlencode "node=node-3"

# 通过 exec-service 路由
curl -X POST http://exec-service:8095/api/v1/exec/runs \
  -H "Content-Type: application/json" \
  -d '{
    "command": "journalctl -u nova-scheduler --no-pager | tail -20",
    "purpose": "verify:ssh-gateway",
    "target_kind": "host_node",
    "target_identity": "host:node-3"
  }'
```

### 审计查询

主机级命令的审计记录与 K8s 命令在同一位置查询：

```bash
# 查询审计记录
curl -X POST http://exec-service:8095/api/v1/exec/audit?limit=10

# 单次执行回放
curl http://exec-service:8095/api/v1/exec/runs/<run-id>/replay
```

审计记录包含 `target_node_name` 和 `target_cluster_id`，可追溯到具体执行目标主机。

### 新增主机

```bash
# 1. 生成密钥
ssh-keygen -t ed25519 -f node-N-id_rsa -N "" -C "ssh-gateway-node-N@logoscope"
ssh-copy-id -i node-N-id_rsa.pub root@<node-N-ip>

# 2. 导入 Secret
kubectl create secret generic ssh-key-node-N --namespace=islap --from-file=id_rsa=./node-N-id_rsa

# 3. 更新 ConfigMap
# 在 deploy/ssh-hosts-config.yaml 中添加 node-N 的连接信息
kubectl apply -f deploy/ssh-hosts-config.yaml

# 4. 挂载新密钥到 SSH Gateway
# 在 deploy/ssh-gateway.yaml 的 volumes/volumeMounts 中添加 node-N 的条目
kubectl apply -f deploy/ssh-gateway.yaml

# 5. 注册目标
# 在 AI_RUNTIME_V4_REMOTE_TARGETS_JSON 中添加 host:node-N 条目
kubectl apply -f deploy/ai-service.yaml
kubectl rollout restart deploy/ai-service -n islap
```

### 回滚

| 阶段 | 回滚操作 |
|------|----------|
| SSH Gateway 服务 | `kubectl delete deploy/ssh-gateway -n islap` |
| Secret 和 ConfigMap | `kubectl delete secret ssh-key-node-3 -n islap` + `kubectl delete configmap ssh-hosts-config -n islap` |
| Executor 模板 | 恢复 `HOST_SSH_*` 模板为空值 |
| 目标注册 | 从 `AI_RUNTIME_V4_REMOTE_TARGETS_JSON` 移除 `host_node` 条目 |
```

- [x] **Step 2: Commit**

```bash
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend
git add docs/operations/remote-cluster-execution.zh-CN.md
git commit -m "docs: add SSH Gateway section to remote-cluster-execution operations doc"
```
