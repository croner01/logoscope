# 集群感知的命令路由 — 设计文档

## 概述

LLM 分析会话中，AI agent 生成 `kubectl exec` 命令来检查远端 Pod 状态，
但当前 exec-service 无法将命令路由到正确的 Kubernetes 集群。

**根因：** 日志记录缺少 `source_cluster` 标识，导致命令回退到本地集群执行，
目标 namespace 在本地集群不存在，返回 NotFound 错误。

本文档描述端到端解决方案：从日志采集时打标，到 AI 上下文传递集群标识，
再到 exec-service 精确路由命令到目标集群的 kubeconfig。

---

## 问题画像

### 当前数据流

```
用户日志 (openstack 集群)
  → Fluent Bit (openstack 集群)
    → OTel Collector
      → ClickHouse (logs.logs)
        → source_cluster 为空 ❌ (0/52M 行有值)
          → AI 分析启动，source_target 只含 namespace，不含 cluster_id
            → LLM 生成: kubectl exec thanos-ruler-ecms-0 -n openstack -- ls
              → compiler: target_identity = "namespace:openstack"
                → exec-service: target_cluster_id = "cluster-local" (默认)
                  → toolbox-gateway: 用本地集群 kubeconfig
                    → kubectl: 本地集群没有 namespace "openstack"
                      → exit=22, 500 ❌
```

### 核心差距

| 差距 | 说明 |
|------|------|
| `source_cluster` 字段存在 | `logs.logs` 表第 16 列但全部为空 |
| 多集群日志混存 | islap, openstack, thor, ems, ceph, eks 等不同集群的日志共存在同一个 ClickHouse 实例 |
| register 的 target 不匹配 | `namespace:openstack-cluster-01` 而非真正的 k8s namespace `openstack` |
| 无 cluster_id 端到端传递 | 从日志 → 分析上下文 → command spec → exec dispatch，链条上没有任何一环传递集群标识 |

---

## 设计原则

1. **声明式，非编程式** — 新增集群只改配置（Helm values + kubeconfig），不改代码
2. **向上兼容** — 单集群场景零改动；多集群场景只需配 Fluent Bit cluster_id
3. **安全分层的信任** — 自动接入不引入额外安全风险：OPA 策略 + approval policy + 命令白名单已兜底
4. **正交维度不合并** — `namespace` 和 `cluster` 是不同维度，在 CommandSpec 中作为独立字段存在

---

## 架构层次

```
┌──────────────────────────────────────────────────────────────────┐
│   Layer 1: 数据源头 — source_cluster 打标                        │
│   Fluent Bit DaemonSet 通过 Downward API / Helm values 注入     │
│   cluster_id → OTel Collector 映射到 source_cluster              │
└────────────────────────────────┬─────────────────────────────────┘
                                 ↓
┌──────────────────────────────────────────────────────────────────┐
│   Layer 2: 存储层 — ClickHouse 记录带 cluster_id                  │
│   logs.logs.source_cluster 被 populate（52M 行不再为空）         │
└────────────────────────────────┬─────────────────────────────────┘
                                 ↓
└──────────────────────────────────────────────────────────────────┐
│   Layer 3: 分析上下文传递                                        │
│   AI 分析启动时，从日志记录读取 source_cluster                     │
│   → context_json.source_target.cluster_id = "openstack-cluster-01"│
└────────────────────────────────┬─────────────────────────────────┘
                                 ↓
┌──────────────────────────────────────────────────────────────────┐
│   Layer 4: 命令编译                                              │
│   normalizer.py 从 source_target.cluster_id 写入                  │
│   → CommandSpec.target_cluster_id                                │
└────────────────────────────────┬─────────────────────────────────┘
                                 ↓
┌──────────────────────────────────────────────────────────────────┐
│   Layer 5: 目标注册 & 路由                                        │
│   新 source_cluster 出现 → auto-seed k8s_cluster target           │
│   exec-service 路由优先级:                                        │
│     1. target_cluster_id 精确路由                                 │
│     2. target_identity + cluster_id 二元匹配                      │
│     3. namespace 单一匹配（兼容旧数据）                            │
│   toolbox-gateway → /etc/kubeconfigs/{cluster_id}                │
└──────────────────────────────────────────────────────────────────┘
```

---

## 层 1：数据源头 — source_cluster 打标

### 设计

Fluent Bit DaemonSet 通过 Helm values 注入 `CLUSTER_ID` 环境变量，
然后在 record_modifier 中追加到日志记录。

### 配置变更

**Fluent Bit Helm values（每个集群独立）：**

```yaml
# clusters/openstack/values.yaml
env:
  - name: CLUSTER_ID
    value: "openstack-cluster-01"

# clusters/local/values.yaml
env:
  - name: CLUSTER_ID
    value: "cluster-local"
```

**Fluent Bit ConfigMap 追加 cluster_id 标签：**

```
# 在 outputs/forward 之前
[FILTER]
    Name    record_modifier
    Match   *
    Record  cluster_id ${CLUSTER_ID}
```

### OTel Collector 映射

**已有 processors 配置追加一条规则：**

```yaml
processors:
  attributes:
    actions:
      - key: source_cluster
        from_attribute: cluster_id
        action: insert
      - key: cluster_id          # 清理临时标签
        action: delete
```

### 优劣

| 方面 | 评价 |
|------|------|
| 实现成本 | ✅ 低：总共 3 行配置 + 1 个 env var |
| 改动面 | ✅ Only Fluent Bit + OTel Collector config，不涉及代码发布 |
| 每集群成本 | ⚠️ 每个集群的 Fluent Bit Helm values 要设 CLUSTER_ID（本质是声明式，没代码成本） |
| 灰度风险 | ✅ 存量日志不受影响，新日志从生效时刻开始带 tag，可逐步验证 |

### 兜底考虑

**Q: 如果某个集群忘记设 CLUSTER_ID 会怎样？**

旧行为保持不变：`source_cluster = ''`。该集群的日志 AI 分析仍然可用，只是 `kubectl exec` 命令会被路由到本地集群（fallback）。
这和今天的行为完全一致，不会比现在更差。

**Q: 如果集群迁移需要改 CLUSTER_ID？**

改 Helm values → 滚动更新 Fluent Bit DaemonSet → 新日志用新 cluster_id。
旧日志保留原值，不影响历史分析。

---

## 层 2：存储层

ClickHouse 侧无改动。

`logs.logs.source_cluster` 字段已存在（`String DEFAULT ''`），OTel Collector 写入后自动填充。

---

## 层 3：分析上下文传递

### 当前状态

`analysis_context.source_target` 目前只含：
```python
source_target = {
    "pod_name": "thanos-ruler-ecms-0",
    "namespace": "openstack"
}
```

### 改动

AI Service 在构建 `source_target` 时，从当前分析的日志记录中读取 `source_cluster`：

```python
# api/ai.py 或其他初始化位置
source_target = {
    "pod_name": log_record.get("pod_name", ""),
    "namespace": log_record.get("namespace", ""),
    "cluster_id": log_record.get("source_cluster", ""),   # ← 新增
}
```

### 影响范围

| 文件 | 改动 |
|------|------|
| `api/ai.py` | `source_target` 构建位置加 `cluster_id` |
| `request_flow_agent.py`（如有） | 同样加透传 |
| `bridge.py` | 已从 `analysis_context.get("source_target")` 读，无需改 |

### 降级行为

`source_cluster` 为空 → `cluster_id = ""` → 下游走回退逻辑（同今日行为）。

---

## 层 4：命令编译

### 改动清单

#### `ai/command/spec.py` — 加 target_cluster_id 字段

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

#### `ai/command/normalizer.py` — 从 source_target 填入

```python
def normalize_command_spec(raw, *, source_target=None):
    ...
    target_cluster_id = _as_str(safe.get("target_cluster_id")).strip()
    if not target_cluster_id and source_target:
        target_cluster_id = _as_str(source_target.get("cluster_id")).strip()
    ...
    return CommandSpec(
        ...
        target_cluster_id=target_cluster_id,
    )
```

#### `ai/runtime/state.py` — source_target 透传 cluster_id

`RuntimeState` 的 `source_target` 属性已经在 bridge.py 中赋值：
```python
# bridge.py line 198-204
source_target = analysis_context.get("source_target")
state = RuntimeState(
    source_target=source_target if isinstance(source_target, dict) else None,
    ...
)
```

若 `source_target` 已含 `cluster_id`，则 `state.source_target` 自然携带。

#### `ai/runtime/bridge.py` — 已就绪

`_on_iteration` 中调 `normalize_command_spec(raw, source_target=st.source_target)`，
只要 `st.source_target` 含 `cluster_id`，自动流入 CommandSpec。

### 数据流验证

```
source_target.cluster_id = "openstack-cluster-01"
  → normalize_command_spec()
    → CommandSpec(target_cluster_id="openstack-cluster-01")
      → compile_command()  → CompiledCommand (集群无关，compile 只处理命令文本)
        → run_diagnosis → Action 含 spec.target_cluster_id
          → tools.execute() → exec-service 含 target_cluster_id
```

---

## 层 5：目标注册 & 路由

### Auto-seed 新 target

当 ClickHouse 中出现新的 `source_cluster` 值时，自动注册 `k8s_cluster` target。

**触发条件：** AI Service target registry 后台检测新 cluster_id：

```
轮询或事件驱动 → 发现 logs.logs 中有新 source_cluster
  → 检查 target 注册表是否已存在
  → 不存在 → 注册:
    {
      "target_kind": "k8s_cluster",
      "target_identity": "namespace:{default_ns}/cluster:{cluster_id}",
      "cluster_id": "{cluster_id}",
      "preferred_executor_profiles": ["toolbox-k8s-readonly", "toolbox-k8s-mutating"]
    }
```

**namespace 发现：** 从 logs.logs 中该 cluster 对应的 namespace
（查询 `SELECT DISTINCT namespace FROM logs.logs WHERE source_cluster = '{cluster_id}'`）。

**注册粒度：** 每个 cluster_id + namespace 组合生成一个 target。

```
cluster_id = "openstack-cluster-01"
  → namespace: openstack    → k8s_cluster / namespace:openstack/cluster:openstack-cluster-01
  → namespace: kube-system  → k8s_cluster / namespace:kube-system/cluster:openstack-cluster-01
```

### Exec-service 路由逻辑改造

#### `executor_registry.py` — 路由优先级

```python
def resolve_executor(
    *,
    target_cluster_id: str = "",      # ← 从 CommandSpec 传入
    target_kind: str,
    target_identity: str,
    ...
):
    scope = _extract_execution_scope(...)

    # 优先级 1: target_cluster_id 精确路由
    if target_cluster_id:
        cluster_id = target_cluster_id
    # 优先级 2: 从 resolved_target_context 取
    elif scope.get("cluster_id"):
        cluster_id = scope["cluster_id"]
    else:
        cluster_id = "cluster-local"  # 兼容旧数据
```

#### `toolbox-gateway/app.py` — _resolve_kubeconfig_path

当前逻辑：

```python
def _resolve_kubeconfig_path(kubeconfig_name: str):
    if not kubeconfig_name:
        return None
    base = '/etc/kubeconfigs'
    candidate = os.path.join(base, kubeconfig_name)
    if os.path.isfile(candidate):
        return candidate
    for ext in ['', '.yaml', '.yml', '.kubeconfig', '.conf', '.json']:
        candidate = os.path.join(base, kubeconfig_name + ext)
        if os.path.isfile(candidate):
            return candidate
    if os.path.isdir(base):
        for f in os.listdir(base):
            if kubeconfig_name in f:
                return os.path.join(base, f)
    return None
```

**当前已够用**。`openstack-cluster-01` 作为 `kubeconfig_name` → 精确匹配 `/etc/kubeconfigs/openstack-cluster-01` ✅

### 路由验证示例

```
target_cluster_id = "openstack-cluster-01"
target_kind       = "k8s_cluster"
target_identity   = "namespace:openstack/cluster:openstack-cluster-01"

1. dispatch → curl toolbox-gateway:8088/exec
   --data-urlencode command="kubectl exec thanos-ruler-ecms-0 ..."
   --data-urlencode kubeconfig="openstack-cluster-01"

2. toolbox-gateway:
   _resolve_kubeconfig_path("openstack-cluster-01")
   → /etc/kubeconfigs/openstack-cluster-01 ✅

3. kubectl exec thanos-ruler-ecms-0 -n openstack -- ls ...
   → 成功! ✅
```

---

## 向后兼容

| 场景 | 行为 | 回归 |
|------|------|------|
| 单集群，无 CLUSTER_ID 配置 | source_cluster="" → target_cluster_id="" → fallback 到 cluster-local | 同今日行为 |
| 多集群，新集群未放 kubeconfig | target_cluster_id 有值但 toolbox-gateway 找不到 kubeconfig → exit=126（command not found），不会发到错误集群 | 可检测，日志清晰 |
| 存量历史日志（source_cluster=""） | 分析时不携带 cluster_id → 同今日 fallback 行为 | 不会更差 |
| target_cluster_id 为空但 target_identity 匹配 | 走现有逻辑（同 v1 行为） | 无 |

---

## 上线计划

| 阶段 | 内容 | 依赖 |
|------|------|------|
| **Phase 1** | Fluent Bit CLUSTER_ID 注入 + OTel 映射 | Helm values 调整 |
| **Phase 2** | spec.py + normalizer.py + state.py 字段扩展 | Phase 1 上线验证通过 |
| **Phase 3** | target auto-seed 后台服务 | Phase 2 上线后 |
| **Phase 4** | exec-service 路由优化（二元匹配） | Phase 3 上线后 |

每个 phase 独立上线、独立验证。

---

## 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| Fluent Bit CLUSTER_ID 配置遗漏 | 低 | 该集群日志不带 cluster_id，fallback 到本地 | Fallback 行为同今天，不漏报 |
| OTel Collector 映射配置遗漏 | 低 | source_cluster 持续为空 | 可加监控告警：检测 source_cluster 占比 |
| 新集群 kubeconfig 未部署 | 低 | 精确路由失败，exit=126 | toolbox-gateway 返回明确的 "kubeconfig not found" 错误信息，可排查 |
| 旧分析 session 引用旧日志 | 中 | 旧 session 的 source_target 无 cluster_id | 兼容处理：cluster_id 为空时降级到旧路由逻辑 |
