# 集群感知的命令路由 — 实现方案

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 使 exec-service 能根据日志来源集群，将 `kubectl exec` 命令精确路由到目标集群的 kubeconfig，而不是错误回退到本地集群。

**Architecture:** 4 个独立 phase：
1. Fluent Bit 注入 CLUSTER_ID → OTel Collector 映射到 ClickHouse `source_cluster`
2. AI Service 端到端传递 cluster_id（`CommandSpec.target_cluster_id`）
3. 新集群出现时自动注册 k8s_cluster target
4. exec-service 路由优先级改造（target_cluster_id 优先）

每个 phase 独立上线、独立验证、不阻塞下游。

**Tech Stack:** Python (ai-service, exec-service), clickhouse-connect, Fluent Bit, OTel Collector, YAML

---

## 文件清单

| 文件 | 改动类型 | 说明 |
|------|----------|------|
| `fluent-bit/values.yaml` (每个集群) | Modify | 加 `CLUSTER_ID` env |
| `fluent-bit/configmap.yaml` | Modify | record_modifier 注入 cluster_id |
| `otel-collector/config.yaml` | Modify | attributes processor 映射 source_cluster |
| `ai-service/ai/command/spec.py` | Modify | `CommandSpec` 加 `target_cluster_id` |
| `ai-service/ai/command/normalizer.py` | Modify | 从 `source_target.cluster_id` 填入 |
| `ai-service/api/ai.py` | Modify | `source_target` 构建时加 `cluster_id` |
| `ai-service/tests/test_command_nomalizer.py` | New | 测试 cluster_id 传递 |
| `ai-service/tests/test_command_spec.py` | New | 测试 target_cluster_id 序列化 |
| `exec-service/core/executor_registry.py` | Modify | 路由优先级：target_cluster_id 优先 |
| `exec-service/core/dispatch.py` | Modify | dispatch_command 传 target_cluster_id |
| `exec-service/core/target_registry_client.py` | Modify | target 判定加 cluster_id 条件 |
| `exec-service/tests/test_executor_registry.py` | Modify | 测试路由优先级 |
| `ai-service/ai/target_auto_seed.py` | Create | 自动检测新 source_cluster 并注册 target |

---

## Phase 1: Fluent Bit CLUSTER_ID 注入 + OTel 映射

### Task 1-1: Fluent Bit Helm values 加 CLUSTER_ID

- [ ] **Step 1: 为 openstack 集群的 Fluent Bit values 加 env**

```yaml
# clusters/openstack/fluent-bit-values.yaml 或对应位置
fluent-bit:
  env:
    - name: CLUSTER_ID
      value: "openstack-cluster-01"
```

- [ ] **Step 2: 为本地集群的 Fluent Bit values 加 env**

```yaml
# clusters/local/fluent-bit-values.yaml 或对应位置
fluent-bit:
  env:
    - name: CLUSTER_ID
      value: "cluster-local"
```

```bash
# 滚动更新 Fluent Bit DaemonSet（只举一例）
helm upgrade --install fluent-bit fluent/fluent-bit -n islap -f clusters/openstack/fluent-bit-values.yaml
```

### Task 1-2: Fluent Bit ConfigMap 追加 record_modifier

- [ ] **Step 1: 在 Fluent Bit ConfigMap 中加 filter**

```
# fluent-bit-configmap.yaml → [FILTER] 段
[FILTER]
    Name    record_modifier
    Match   *
    Record  cluster_id ${CLUSTER_ID}
```

- [ ] **Step 2: 重启 Fluent Bit 使配置生效**

```bash
kubectl rollout restart -n islap daemonset/fluent-bit
```

- [ ] **Step 3: 验证 cluster_id 出现在日志 attributes 中**

```bash
# 取一条最新日志看 attributes 里有没有 cluster_id
kubectl exec -n islap deploy/clickhouse -- clickhouse-client --query "
SELECT attributes_json FROM logs.logs
WHERE source_cluster != ''
LIMIT 1
FORMAT PrettyCompact
"
```

预期：属性中无变化（因为 OTel 侧还没映射到 source_cluster）。

### Task 1-3: OTel Collector 映射 source_cluster

- [ ] **Step 1: 在 OTel Collector config 的 processors.attributes 追加**

```yaml
# otel-collector-config.yaml → processors.attributes.actions
processors:
  attributes:
    actions:
      - key: source_cluster
        from_attribute: cluster_id
        action: insert
      - key: cluster_id
        action: delete
```

- [ ] **Step 2: 重启 OTel Collector**

```bash
kubectl rollout restart -n islap deployment/otel-collector
```

- [ ] **Step 3: 验证 source_cluster 写入 ClickHouse**

```bash
kubectl exec -n islap deploy/clickhouse -- clickhouse-client --query "
SELECT source_cluster, count()
FROM logs.logs
WHERE source_cluster != ''
GROUP BY source_cluster
FORMAT PrettyCompact
"
```

预期: 出现 "openstack-cluster-01" 和 "cluster-local" 行，且 count > 0。

---

## Phase 2: AI Service — target_cluster_id 端到端传递

### Task 2-1: spec.py — CommandSpec 加 target_cluster_id 字段

**Files:**
- Modify: `ai-service/ai/command/spec.py`

- [ ] **Step 1: 修改 CommandSpec 增加 target_cluster_id**

在 `ai-service/ai/command/spec.py` 中找到 `@dataclass class CommandSpec`，在 `target_identity` 后加一行：

```python
@dataclass
class CommandSpec:
    tool: ToolType
    command: str
    target_kind: str = ""
    target_identity: str = ""
    target_cluster_id: str = ""   # ← 新增
    purpose: str = ""
    command_type: CommandType = CommandType.QUERY
    timeout_seconds: int = 20
```

- [ ] **Step 2: 确认 CompiledCommand 无需改**

`CompiledCommand` 在 compile 阶段只处理命令文本，不涉及集群路由，不改。

- [ ] **Step 3: 运行已有测试确认无回归**

```bash
cd ai-service && python -m pytest tests/test_command_compiler.py -v --tb=short 2>&1 | tail -10
```

预期：所有测试通过。

- [ ] **Step 4: Commit**

```bash
git add ai-service/ai/command/spec.py
git commit -m "feat(spec): add target_cluster_id field to CommandSpec"
```

### Task 2-2: normalizer.py — 从 source_target 填入 target_cluster_id

**Files:**
- Modify: `ai-service/ai/command/normalizer.py`
- Test: `ai-service/tests/test_command_normalizer.py` (create)

- [ ] **Step 1: 写测试**

```python
# ai-service/tests/test_command_normalizer.py
"""Tests for normalizer cluster_id propagation."""
import pytest

from ai.command.normalizer import normalize_command_spec


class TestNormalizeCommandSpecClusterId:
    """target_cluster_id propagation from source_target."""

    def test_cluster_id_from_source_target(self):
        """cluster_id flows from source_target into CommandSpec."""
        raw = {
            "tool": "clickhouse_query",
            "command": "SELECT 1",
            "purpose": "test",
        }
        source_target = {"pod_name": "pod-0", "namespace": "test-ns", "cluster_id": "my-cluster"}
        spec = normalize_command_spec(raw, source_target=source_target)
        assert spec.target_cluster_id == "my-cluster"

    def test_cluster_id_empty_when_not_in_source_target(self):
        """When source_target has no cluster_id, target_cluster_id stays empty."""
        raw = {
            "tool": "clickhouse_query",
            "command": "SELECT 1",
            "purpose": "test",
        }
        source_target = {"pod_name": "pod-0", "namespace": "test-ns"}
        spec = normalize_command_spec(raw, source_target=source_target)
        assert spec.target_cluster_id == ""

    def test_cluster_id_empty_when_no_source_target(self):
        """When source_target is None, target_cluster_id stays empty."""
        raw = {
            "tool": "clickhouse_query",
            "command": "SELECT 1",
            "purpose": "test",
        }
        spec = normalize_command_spec(raw, source_target=None)
        assert spec.target_cluster_id == ""

    def test_raw_dict_overrides_source_target(self):
        """When raw dict has target_cluster_id, it takes priority."""
        raw = {
            "tool": "clickhouse_query",
            "command": "SELECT 1",
            "purpose": "test",
            "target_cluster_id": "from-llm",
        }
        source_target = {"pod_name": "pod-0", "namespace": "test-ns", "cluster_id": "from-source"}
        spec = normalize_command_spec(raw, source_target=source_target)
        assert spec.target_cluster_id == "from-llm"
```

- [ ] **Step 2: 运行测试验证失败**

```bash
cd ai-service && python -m pytest tests/test_command_normalizer.py -v --tb=short 2>&1 | tail -10
```

预期：FAIL，`CommandSpec` 没有 `target_cluster_id` 属性（Task 2-1 尚未合并时），
或 `normalize_command_spec` 未设置该字段。

- [ ] **Step 3: 修改 normalizer.py**

在 `normalize_command_spec()` 函数中，找到 `target_identity` 构建代码之后，
在 `return CommandSpec(...)` 之前，加 cluster_id 解析逻辑：

```python
# ai/command/normalizer.py 在构建 target_identity 之后

    # Cluster ID from raw dict or source_target
    target_cluster_id = _as_str(safe.get("target_cluster_id")).strip()
    if not target_cluster_id and source_target:
        target_cluster_id = _as_str(source_target.get("cluster_id")).strip()
```

然后在 `return CommandSpec(...)` 中加参数：

```python
    return CommandSpec(
        tool=tool,
        command=command,
        target_kind=target_kind,
        target_identity=target_identity,
        target_cluster_id=target_cluster_id,    # ← 新增
        purpose=_as_str(safe.get("purpose")).strip(),
        command_type=cmd_type,
        timeout_seconds=int(safe.get("timeout_seconds", 20)),
    )
```

- [ ] **Step 4: 运行测试验证通过**

```bash
cd ai-service && python -m pytest tests/test_command_normalizer.py -v --tb=short 2>&1 | tail -10
```

预期：PASS（4 tests）

- [ ] **Step 5: 运行全部相关测试验证无回归**

```bash
cd ai-service && python -m pytest tests/test_command_compiler.py tests/test_runtime_engine.py tests/test_runtime_core.py -v --tb=short 2>&1 | tail -15
```

预期：全部通过。

- [ ] **Step 6: Commit**

```bash
git add ai-service/ai/command/normalizer.py ai-service/tests/test_command_normalizer.py
git commit -m "feat(normalizer): propagate cluster_id from source_target to CommandSpec"
```

### Task 2-3: api/ai.py — source_target 构建时加 cluster_id

**Files:**
- Modify: `ai-service/api/ai.py`

- [ ] **Step 1: 找到 source_target 构建位置**

搜索 `api/ai.py` 中构造 `source_target` 字典的代码。有以下两处需改：

**位置 A：followup_analysis_runtime 模式初始化**

找到类似 `source_target = {"pod_name": ..., "namespace": ...}` 的代码段，
在 if 分支中读取 log 记录后，加 `cluster_id`：

```python
source_target = {
    "pod_name": log_id_info.get("pod_name", ""),
    "namespace": log_id_info.get("namespace", ""),
    "cluster_id": log_record.get("source_cluster", "") if log_record else "",  # ← 新增
}
```

**位置 B：followup 初始化（非 runtime 模式回调）**

搜索第二个 `source_target` 构造位置，同样加 cluster_id。

> 注意：具体行号因代码可能变化，以实际搜索 `source_target = {` 位置为准。

- [ ] **Step 2: 验证 bridge.py 已就绪**

确认 `bridge.py` 中 `source_target` 已经传入 `RuntimeState`：

```bash
grep -n "source_target" ai-service/ai/runtime/bridge.py
```

预期：
```
198:    source_target = analysis_context.get("source_target")
205:        source_target=source_target if isinstance(source_target, dict) else None,
392:                                spec = normalize_command_spec(raw, source_target=st.source_target)
```

这些行已经存在，无需修改。

- [ ] **Step 3: 运行测试验证**

```bash
cd ai-service && python -m pytest tests/test_runtime_engine.py tests/test_runtime_core.py -v --tb=short 2>&1 | tail -15
```

预期：全部通过。

- [ ] **Step 4: Commit**

```bash
git add ai-service/api/ai.py
git commit -m "feat(api): include cluster_id in source_target from log record"
```

---

## Phase 3: Target 注册

Phase 3 分为两步：立刻能用的手动注册方案（Task 3-1）+ 长期维护的全自动方案（Task 3-2）。

### Task 3-1: 立即注册 openstack namespace target

**Files:** 无代码改动，直接调 API。

- [ ] **Step 1: 注册 namespace:openstack target**

```bash
# 从 toolbox-gateway 查 openstack kubeconfig 的集群名
# kubeconfig 文件名是 openstack-cluster-01，也是 cluster_id

kubectl exec -n islap deploy/ai-service -- python3 -c "
import requests, json
payload = {
    'target_id': 'auto-k8s-cluster-namespace-openstack',
    'target_kind': 'k8s_cluster',
    'target_identity': 'namespace:openstack',
    'display_name': 'OpenStack Namespace',
    'description': 'auto-seeded openstack kubernetes diagnosis target',
    'capabilities': ['read_logs', 'restart_workload', 'helm_read', 'helm_mutation'],
    'credential_scope': {'namespace': 'openstack'},
    'metadata': {
        'cluster_id': 'openstack-cluster-01',
        'namespace': 'openstack',
        'risk_tier': 'high',
        'preferred_executor_profiles': ['toolbox-k8s-readonly', 'toolbox-k8s-mutating'],
    },
    'status': 'active',
}
r = requests.post('http://localhost:8090/api/v2/targets', json=payload)
print(r.status_code, r.text[:200])
"
```

- [ ] **Step 2: 验证 register 成功**

```bash
kubectl exec -n islap clickhouse-6c857f7b96-98wp8 -- clickhouse-client --query "
SELECT target_id, target_kind, target_identity FROM logs.ai_runtime_v4_targets
WHERE target_identity = 'namespace:openstack'
FORMAT PrettyCompact
"
```

预期：返回一行记录。

- [ ] **Step 3: 测试命令路由**

重新触发一次针对 openstack namespace 的 `kubectl exec` 命令（通过新的 AI 分析会话），
观察 exec_command_runs 中 `target_cluster_id` 是否为 `openstack-cluster-01`。

预期：`kubectl exec thanos-ruler-ecms-0 -n openstack` 路由到 openstack kubeconfig → 成功。

### Task 3-2: 自动 target 注册服务

**Files:**
- Create: `ai-service/ai/target_auto_seed.py`
- Test: `ai-service/tests/test_target_auto_seed.py`

- [ ] **Step 1: 写 auto-seed 核心逻辑**

```python
# ai-service/ai/target_auto_seed.py
"""Auto-seed k8s_cluster targets from new source_cluster values in ClickHouse."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

import clickhouse_connect
import requests

logger = logging.getLogger(__name__)


def _as_str(value: Any, default: str = "") -> str:
    return str(value) if isinstance(value, str) else default


def discover_new_clusters(ch_client) -> List[Dict[str, Any]]:
    """Find source_cluster values not yet registered as targets."""
    # Get all distinct source_cluster values seen in logs
    rows = ch_client.query("""
        SELECT source_cluster, namespace, count() as cnt
        FROM logs.logs
        WHERE source_cluster != ''
        GROUP BY source_cluster, namespace
        ORDER BY cnt DESC
    """).result_rows

    # Get existing target identities from ClickHouse target table
    existing = ch_client.query("""
        SELECT target_identity FROM logs.ai_runtime_v4_targets
        WHERE target_kind = 'k8s_cluster'
    """).result_rows
    existing_set = {row[0] for row in existing}

    new_targets = []
    for cluster_id, namespace, _cnt in rows:
        target_identity = f"namespace:{namespace}/cluster:{cluster_id}"
        if target_identity in existing_set:
            continue
        # Also check legacy format
        legacy_identity = f"namespace:{namespace}"
        if legacy_identity in existing_set:
            continue
        new_targets.append({
            "target_id": f"auto-k8s-cluster-namespace-{namespace}",
            "target_kind": "k8s_cluster",
            "target_identity": target_identity,
            "cluster_id": cluster_id,
            "namespace": namespace,
            "display_name": f"{namespace} namespace ({cluster_id})",
            "capabilities": ["read_logs", "restart_workload", "helm_read", "helm_mutation"],
            "credential_scope": {"namespace": namespace},
            "metadata": {
                "cluster_id": cluster_id,
                "namespace": namespace,
                "risk_tier": "high",
                "preferred_executor_profiles": ["toolbox-k8s-readonly", "toolbox-k8s-mutating"],
            },
        })
    return new_targets


def register_target(ai_service_url: str, target: Dict[str, Any]) -> bool:
    """Register a new target via AI Service API."""
    url = f"{ai_service_url.rstrip('/')}/api/v2/targets"
    try:
        r = requests.post(url, json=target, timeout=10)
        if r.status_code in (200, 201):
            logger.info("registered target: %s (%s)", target["target_identity"], r.status_code)
            return True
        logger.warning("failed to register target %s: %s %s", target["target_identity"], r.status_code, r.text[:200])
        return False
    except requests.RequestException as exc:
        logger.error("request error registering target %s: %s", target["target_identity"], exc)
        return False


def run_auto_seed(ch_host: str = "localhost", ch_port: int = 8123,
                  ai_service_url: str = "http://localhost:8090") -> int:
    """Run auto-seed once. Returns number of new targets registered."""
    client = clickhouse_connect.get_client(host=ch_host, port=ch_port)
    new_targets = discover_new_clusters(client)
    registered = 0
    for target in new_targets:
        if register_target(ai_service_url, target):
            registered += 1
    logger.info("auto-seed: %d new targets registered", registered)
    return registered
```

- [ ] **Step 2: 写测试**

```python
# ai-service/tests/test_target_auto_seed.py
"""Tests for target auto-seed logic."""
from unittest.mock import MagicMock, patch

from ai.target_auto_seed import discover_new_clusters, register_target


class TestDiscoverNewClusters:
    def test_discovers_new_cluster_namespace_pairs(self):
        """New source_cluster values produce target candidates."""
        mock_client = MagicMock()
        mock_client.query.return_value.result_rows = [
            ("openstack-cluster-01", "openstack", 5000),
            ("openstack-cluster-01", "kube-system", 100),
            ("cluster-local", "islap", 20000),
        ]

        targets = discover_new_clusters(mock_client)

        assert len(targets) == 3
        ids = [t["target_identity"] for t in targets]
        assert "namespace:openstack/cluster:openstack-cluster-01" in ids
        assert "namespace:kube-system/cluster:openstack-cluster-01" in ids
        assert "namespace:islap/cluster:cluster-local" in ids

    def test_skips_existing_targets(self):
        """Already-registered target identities are not duplicated."""
        mock_client = MagicMock()

        # First call: get new clusters
        mock_client.query.side_effect = [
            # discover_new_clusters first query
            MagicMock(result_rows=[
                ("openstack-cluster-01", "openstack", 5000),
                ("cluster-local", "islap", 20000),
            ]),
            # discover_new_clusters second query (existing targets)
            MagicMock(result_rows=[
                ("namespace:islap/cluster:cluster-local",),
            ]),
        ]

        targets = discover_new_clusters(mock_client)

        identities = [t["target_identity"] for t in targets]
        assert "namespace:openstack/cluster:openstack-cluster-01" in identities
        assert "namespace:islap/cluster:cluster-local" not in identities  # already exists

    def test_skips_legacy_format_targets(self):
        """Existing targets in legacy format (namespace only) also prevent duplicates."""
        mock_client = MagicMock()
        mock_client.query.side_effect = [
            MagicMock(result_rows=[
                ("cluster-local", "islap", 20000),
            ]),
            MagicMock(result_rows=[
                ("namespace:islap",),  # legacy format
            ]),
        ]

        targets = discover_new_clusters(mock_client)
        assert len(targets) == 0


class TestRegisterTarget:
    def test_register_success(self):
        target = {"target_identity": "namespace:test/cluster:test-cluster"}
        with patch("ai.target_auto_seed.requests.post") as mock_post:
            mock_post.return_value.status_code = 201
            result = register_target("http://ai:8090", target)
            assert result is True
            mock_post.assert_called_once()

    def test_register_failure_logged(self):
        target = {"target_identity": "namespace:test/cluster:test-cluster"}
        with patch("ai.target_auto_seed.requests.post") as mock_post:
            mock_post.return_value.status_code = 409
            result = register_target("http://ai:8090", target)
            assert result is False
```

- [ ] **Step 3: 运行测试**

```bash
cd ai-service && python -m pytest tests/test_target_auto_seed.py -v --tb=short 2>&1 | tail -15
```

预期：PASS（4 tests）

- [ ] **Step 4: 集成到 ai-service 启动脚本（可选）**

如果希望 auto-seed 作为 ai-service 的 sidecar 或定时任务运行：

```python
# ai-service/main.py 或单独的 cronjob 脚本
# 建议作为独立的定时任务（每 5 分钟运行一次），不阻塞主服务
```

- [ ] **Step 5: Commit**

```bash
git add ai-service/ai/target_auto_seed.py ai-service/tests/test_target_auto_seed.py
git commit -m "feat(target): auto-seed k8s_cluster targets from ClickHouse source_cluster"
```

---

## Phase 4: exec-service 路由优先使用 target_cluster_id

### Task 4-1: dispatch.py — 透传 target_cluster_id

**Files:**
- Modify: `exec-service/core/dispatch.py`

- [ ] **Step 1: dispatch_command 签名加 target_cluster_id 参数**

```python
# exec-service/core/dispatch.py 找到 dispatch_command 函数签名
async def dispatch_command(
    *,
    command: str,
    executor_type: str,
    executor_profile: str,
    target_kind: str,
    target_identity: str,
    target_cluster_id: str = "",               # ← 新增
    resolved_target_context: Optional[Dict[str, Any]] = None,
    timeout_seconds: int = 20,
    ...
)
```

- [ ] **Step 2: 传给 resolve_executor**

在 `dispatch_command` 函数体中调用 `resolve_executor` 的地方加参数：

```python
    dispatch = resolve_executor(
        command=command,
        executor_type=executor_type,
        executor_profile=executor_profile,
        target_kind=target_kind,
        target_identity=target_identity,
        target_cluster_id=target_cluster_id,    # ← 新增
        resolved_target_context=resolved_target_context,
    )
```

### Task 4-2: executor_registry.py — 路由优先级

**Files:**
- Modify: `exec-service/core/executor_registry.py`

- [ ] **Step 1: resolve_executor 签名加 target_cluster_id**

```python
# exec-service/core/executor_registry.py → resolve_executor 签名
def resolve_executor(
    *,
    command: str,
    executor_type: str,
    executor_profile: str,
    target_kind: str,
    target_identity: str,
    target_cluster_id: str = "",               # ← 新增
    resolved_target_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
```

- [ ] **Step 2: 修改路由优先级逻辑**

在函数体中，`_extract_execution_scope` 调用之后，增加优先逻辑：

```python
    scope = _extract_execution_scope(
        resolved_target_context=safe_context,
        target_identity=safe_target_identity,
    )

    # ── 路由优先级：target_cluster_id > resolved_target_context > fallback ──
    if target_cluster_id:
        # 优先级 1: 从 CommandSpec 传来的精确 cluster_id
        target_cluster_id = target_cluster_id
    elif as_str(scope.get("cluster_id")):
        # 优先级 2: 从 resolved_target_context 中取
        target_cluster_id = as_str(scope.get("cluster_id"))
    else:
        # 优先级 3: 默认本地集群
        target_cluster_id = "cluster-local"

    # 覆盖 scope 中的 cluster_id
    scope["cluster_id"] = target_cluster_id
```

这段代码放在现有 `scope` 提取之后、`_template_context` 调用之前。

- [ ] **Step 3: 写测试**

```python
# exec-service/tests/test_executor_registry.py 追加
class TestResolveExecutorClusterId:
    """Cluster routing priority test."""

    def test_target_cluster_id_priority(self):
        """Explicit target_cluster_id takes priority over resolved_target_context."""
        import os
        template_env = "EXEC_EXECUTOR_TEMPLATE__TOOLBOX_K8S_READONLY"
        original = os.environ.get(template_env)
        os.environ[template_env] = "kubectl --kubeconfig={target_cluster_id} {command}"
        try:
            from core.executor_registry import resolve_executor
            result = resolve_executor(
                command="kubectl get pods -n test",
                executor_type="sandbox_pod",
                executor_profile="toolbox-k8s-readonly",
                target_kind="k8s_cluster",
                target_identity="namespace:test",
                target_cluster_id="openstack-cluster-01",
                resolved_target_context={
                    "execution_scope": {"cluster_id": "cluster-local"},
                    "metadata": {},
                },
            )
            assert result["dispatch_ready"] is True
            assert "openstack-cluster-01" in result.get("resolved_command", "")
        finally:
            if original is None:
                os.environ.pop(template_env, None)
            else:
                os.environ[template_env] = original

    def test_fallback_to_context_when_no_target_cluster_id(self):
        """When target_cluster_id is empty, use resolved_target_context's cluster_id."""
        import os
        template_env = "EXEC_EXECUTOR_TEMPLATE__TOOLBOX_K8S_READONLY"
        original = os.environ.get(template_env)
        os.environ[template_env] = "kubectl --kubeconfig={target_cluster_id} {command}"
        try:
            from core.executor_registry import resolve_executor
            result = resolve_executor(
                command="kubectl get pods -n test",
                executor_type="sandbox_pod",
                executor_profile="toolbox-k8s-readonly",
                target_kind="k8s_cluster",
                target_identity="namespace:test",
                target_cluster_id="",
                resolved_target_context={
                    "execution_scope": {"cluster_id": "openstack-cluster-01"},
                    "metadata": {},
                },
            )
            assert result["dispatch_ready"] is True
            assert "openstack-cluster-01" in result.get("resolved_command", "")
        finally:
            if original is None:
                os.environ.pop(template_env, None)
            else:
                os.environ[template_env] = original
```

- [ ] **Step 4: 运行全部 exec-service 测试**

```bash
cd exec-service && python -m pytest tests/test_executor_registry.py -v --tb=short 2>&1 | tail -20
```

预期：旧测试 + 新测试全部通过。

- [ ] **Step 5: Commit**

```bash
git add exec-service/core/dispatch.py exec-service/core/executor_registry.py exec-service/tests/test_executor_registry.py
git commit -m "feat(exec): route commands by target_cluster_id priority"
```

---

## 向后兼容验证

所有 Phase 完成后，运行全量测试确认无回归：

```bash
echo "=== AI Service ==="
cd ai-service && python -m pytest tests/test_command_compiler.py tests/test_runtime_engine.py tests/test_runtime_core.py tests/test_command_normalizer.py -v --tb=short 2>&1 | tail -5

echo "=== Exec Service ==="
cd exec-service && python -m pytest tests/test_executor_registry.py tests/test_execute_api_streaming.py -v --tb=short 2>&1 | tail -5
```

---

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| Fluent Bit CLUSTER_ID 未配置 | Fallback 到 cluster-local，同旧行为 |
| OTel 属性映射遗漏 | source_cluster 为空，不影响旧数据 |
| auto-seed 注册了不存在对应 kubeconfig 的 target | toolbox-gateway 返回 126（command not found），不会发到错误集群 |
| exec-service 更新中未传 target_cluster_id | 默认空字符串 → 走 `resolved_target_context` fallback |
