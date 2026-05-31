# SSH Gateway 设计文档

## 1. 背景与目标

### 1.1 问题

AI Runtime 当前只能执行 K8s API 级别的命令（通过 toolbox-gateway 的 kubectl）。无法执行主机级命令，如：

- `journalctl -u nova-scheduler --no-pager | tail -50`（查日志）
- `df -h /var/lib/docker`（查磁盘）
- `systemctl status docker`（查服务状态）
- `tcpdump -i eth0 -c 100`（抓包诊断）
- `cat /var/log/nova/nova-scheduler.log | grep ERROR | tail -20`（查文件）

### 1.2 范围

覆盖三种执行场景：

| 场景 | 方式 | 状态 |
|------|------|------|
| 远端 K8s API | kubectl + kubeconfig | ✅ 已完成 |
| **主机级命令** | **SSH 到目标主机** | **⬅️ 本文档** |
| Pod 内诊断 | kubectl exec | 可通过 toolbox-gateway 实现 |

### 1.3 覆盖矩阵

SSH Gateway 解决**传输层**（SSH 到目标主机），应用层命令由目标主机上的 CLI 工具处理：

| 运维场景 | 方式 | 可覆盖？ | 说明 |
|---------|------|---------|------|
| K8s API 查询 | kubectl + kubeconfig | ✅ 不经过 SSH | 已有方案，直接通过 toolbox-gateway |
| 主机系统命令 | journalctl, df, systemctl | ✅ 核心场景 | SSH Gateway 直接覆盖 |
| OpenStack CLI | openstack server list | ✅ 可 | 目标主机安装 openstack-client |
| Cloud CLI | aws s3, gcloud, az | ✅ 可 | 目标主机安装对应 CLI |
| 数据库查询 | mysql, psql, clickhouse-client | ✅ 可 | 通过 SSH 执行原生客户端 |
| HTTP API 调用 | curl 到管理面 | ✅ 可 | 通过 SSH 执行 curl |
| Pod 内诊断 | kubectl exec / logs | ✅ 已有 | 通过 toolbox-gateway（无需 SSH） |
| 文件读取 | cat, tail 日志文件 | ✅ 核心场景 | SSH Gateway 直接覆盖 |
| 文件传输 | scp, rsync | ⚠️ 受限 | 只读场景可用 cat/tar 变通 |
| Windows 主机 | PowerShell, WinRM | ❌ 不支持 | 需单独实现 WinRM Gateway |
| 网络设备 | 交换机 CLI（交互式 SSH） | ⚠️ 有差异 | 交互式 CLI 需 expect 风格处理 |

### 1.4 设计原则

1. **复用现有架构** — 不改变 dispatch 链、模板系统、目标注册表
2. **极致轻量** — 不引入任何第三方 SSH 库，用系统 ssh 命令
3. **与 kubeconfig 模式一致** — 密钥以 Secret 挂载，模板驱动路由
4. **零远端组件** — 不在目标主机安装任何 agent
5. **传输与能力分离** — SSH Gateway 只负责 SSH 传输层；目标主机上已安装的任何 CLI 工具均可执行，不受 Gateway 限制

---

## 2. 架构

### 2.1 完整执行链路

```
AI → exec-service → policy.classify() → target gate → resolve_executor() → template expansion
                                                                                │
                                                                                ▼
                                                                        curl POST /exec
                                                                        command=journalctl...
                                                                        node=node-3
                                                                                │
                                                                                ▼
                                                                        SSH Gateway
                                                                                │
                                                                        ssh -i /etc/ssh-keys/node-3/id_rsa \
                                                                            -o StrictHostKeyChecking=no \
                                                                            root@node-3 "journalctl -u nova-scheduler..."
                                                                                │
                                                                                ▼
                                                                        stdout/stderr/exit_code ──→ AI
```

### 2.2 与现有组件的关系

```
┌──────────────────────────────────────────────────────────────────────┐
│                         OpenHarness 执行架构                           │
├──────────────────────────────────────────────────────────────────────┤
│                                                                       │
│  ┌─────────────┐    ┌───────────────┐    ┌─────────────────────────┐ │
│  │ AI Runtime   │───→│ exec-service  │───→│ resolve_executor()     │ │
│  │ (agent)      │    │ (dispatch)    │    │ template → curl        │ │
│  └─────────────┘    └───────────────┘    └────────┬────────────────┘ │
│                                                    │                  │
│                ┌───────────────────────────────────┼──────────┐      │
│                │             目标网关               │          │      │
│                │          ┌────────────┐           │          │      │
│                │          │ kubeconfig │           │          │      │
│                │          │ 文件挂载   │           │          │      │
│                │   ┌──────▼─────────┐ │           │          │      │
│                │   │ toolbox-gateway│◄├───────────┘          │      │
│                │   │ :8088         │ │                      │      │
│                │   │ kubectl       │ │                      │      │
│                │   │ clickhouse    │ │                      │      │
│                │   └──────▲─────────┘ │                      │      │
│                │          │           │                      │      │
│                │   ┌──────┴─────────┐ │   ┌──────────────────┘      │
│                │   │  ssh-gateway   │◄├───┘                         │
│                │   │ :8096         │ │    SSH 密钥 Secret 挂载      │
│                │   │ ssh user@host │ │                               │
│                │   └──────▲─────────┘ │                               │
│                │          │           │                               │
│                └──────────┼───────────┘                               │
│                           │                                           │
│                ┌──────────┴──────────┐                                │
│                │  目标主机 node-3     │                                │
│                │  journalctl, df,    │                                │
│                │  systemctl, cat     │                                │
│                └─────────────────────┘                                │
└──────────────────────────────────────────────────────────────────────┘
```

### 2.3 数据流

```
Step 1: AI Agent 生成命令
  command: "journalctl -u nova-scheduler --no-pager | tail -50"
  target_kind: "host_node"
  target_identity: "host:node-3"

Step 2: exec-service policy 分类
  → classify_command() → executor_type: "ssh_gateway"
  → executor_profile: "host-ssh-readonly"
  → target_kind: "host_node"

Step 3: target registry gate
  → 查找 host_node / host:node-3
  → 返回 metadata.cluster_id = "openstack-cluster-01"
  → 返回 metadata.node_name = "node-3"
  → 返回 metadata.preferred_executor_profiles = ["host-ssh-readonly", ...]

Step 4: resolve_executor()
  → _select_effective_profile() → "host-ssh-readonly"
  → _template_context() → {command_quoted, target_node_name_quoted, ...}
  → template.format(
      command_quoted=shlex.quote("journalctl -u nova-scheduler..."),
      target_node_name_quoted=shlex.quote("node-3")
    )

Step 5: curl to SSH Gateway
  curl -sS --fail-with-body -X POST http://ssh-gateway:8096/exec \
    --data-urlencode command="journalctl -u nova-scheduler --no-pager | tail -50" \
    --data-urlencode node="node-3"

Step 6: SSH Gateway
  → 读取 /etc/ssh-hosts/config.yaml → node-3 的连接信息
  → ssh -i /etc/ssh-keys/node-3/id_rsa root@10.0.0.1 "journalctl..."
  → 返回 stdout/stderr/exit_code
```

---

## 3. SSH Gateway 服务设计

### 3.1 接口

```
POST /exec
Content-Type: application/x-www-form-urlencoded 或 application/json

请求体:
{
  "command": "journalctl -u nova-scheduler --no-pager | tail -50",
  "node": "node-3",
  "timeout_seconds": 60,
  "response_format": "text"
}

成功响应 200:
  stdout 内容

失败响应:
  504 — 超时
  500 — 命令执行失败（含 stderr）
  400 — 参数缺失
  403 — 命令被策略拒绝

GET /health
{"status": "ok"}
```

### 3.2 核心逻辑（伪代码）

```python
# ssh-gateway/app.py — 关键逻辑

def _resolve_node_config(node_name: str) -> dict | None:
    """从挂载的配置文件解析节点连接信息"""
    config_path = "/etc/ssh-hosts/config.yaml"
    with open(config_path) as f:
        hosts = yaml.safe_load(f) or {}
    return hosts.get(node_name)


def _execute_ssh(command: str, node_cfg: dict, timeout: int) -> ExecResult:
    """通过 SSH 在目标主机上执行命令"""
    key_path = node_cfg.get("key_file", f"/etc/ssh-keys/{node_cfg['name']}/id_rsa")
    user = node_cfg.get("user", "root")
    host = node_cfg["host"]
    port = node_cfg.get("port", 22)

    ssh_cmd = [
        "ssh", "-i", key_path,
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        "-p", str(port),
        f"{user}@{host}",
        command,
    ]

    proc_env = os.environ.copy()
    proc_env.pop("KUBECONFIG", None)  # 避免与 kubectl 混淆
    completed = subprocess.run(ssh_cmd, capture_output=True, text=True,
                               timeout=timeout, env=proc_env)
    return ExecResult(exit_code=completed.returncode,
                      stdout=completed.stdout, stderr=completed.stderr)
```

### 3.3 与 toolbox-gateway 共享的安全策略

- **Shell 语法拦截**：复用 `_SHELL_OPERATOR_TOKENS` 检查，防止管道/重定向绕过
- **输出裁剪**：`_clip_output()` 限制最大输出字节数（默认 256KB）
- **超时控制**：`timeout_seconds` 参数（默认 60s，最大 300s）
- **命令头校验**：可选白名单（默认不限制，但可通过 `SSH_GATEWAY_ALLOWED_PREFIXES` 配置）

### 3.4 节点映射配置

存储在 ConfigMap 中，挂载到 `/etc/ssh-hosts/config.yaml`：

```yaml
# ConfigMap: ssh-hosts-config
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

control-1:
  host: 10.0.0.10
  user: root
  port: 22
  key_file: /etc/ssh-keys/control-1/id_rsa
```

---

## 4. 部署配置

### 4.1 SSH Gateway Deployment

```yaml
# deploy/ssh-gateway.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ssh-gateway
  namespace: islap
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
      containers:
        - name: ssh-gateway
          image: localhost:5000/logoscope/ssh-gateway:latest
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
            # 每个主机一个 Secret 挂载
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
spec:
  selector:
    app: ssh-gateway
  ports:
    - name: http
      port: 8096
      targetPort: 8096
```

### 4.2 SSH Hosts ConfigMap

```yaml
# deploy/ssh-hosts-config.yaml
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

### 4.3 SSH Key Secret

```yaml
# deploy/ssh-keys/node-3-secret.yaml
apiVersion: v1
kind: Secret
metadata:
  name: ssh-key-node-3
  namespace: islap
type: Opaque
stringData:
  id_rsa: |
    -----BEGIN OPENSSH PRIVATE KEY-----
    ...
    -----END OPENSSH PRIVATE KEY-----
```

创建命令：

```bash
# 生成 SSH 密钥对
ssh-keygen -t ed25519 -f node-3-id_rsa -N "" -C "ssh-gateway@logoscope"

# 将公钥部署到目标主机
ssh-copy-id -i node-3-id_rsa.pub root@node-3

# 创建 Secret
kubectl create secret generic ssh-key-node-3 \
  --namespace=islap \
  --from-file=id_rsa=./node-3-id_rsa
```

### 4.4 Executor 模板配置

更新 `deploy/exec-service.yaml` 中的模板：

```yaml
- name: EXEC_EXECUTOR_TEMPLATE__HOST_SSH_READONLY
  value: "curl -sS --fail-with-body -X POST http://ssh-gateway.islap.svc.cluster.local:8096/exec --data-urlencode command={command_quoted} --data-urlencode node={target_node_name_quoted}"

- name: EXEC_EXECUTOR_TEMPLATE__HOST_SSH_MUTATING
  value: "curl -sS --fail-with-body -X POST http://ssh-gateway.islap.svc.cluster.local:8096/exec --data-urlencode command={command_quoted} --data-urlencode node={target_node_name_quoted}"
```

模板变量说明：

| 模板变量 | 来源 | 示例值 |
|---------|------|--------|
| `{command_quoted}` | `_template_context()` | `journalctl -u nova-scheduler` |
| `{target_node_name_quoted}` | `_template_context()` | `node-3` |
| `{target_identity_quoted}` | `_template_context()` | `host:node-3` |

### 4.5 目标注册配置

更新 `deploy/ai-service.yaml` 中的 ConfigMap 的 `AI_RUNTIME_V4_REMOTE_TARGETS_JSON`，追加主机目标：

```json
[
  {
    "target_kind": "k8s_cluster",
    "target_identity": "namespace:openstack-cluster-01",
    ...
  },
  {
    "target_kind": "host_node",
    "target_identity": "host:node-3",
    "display_name": "OpenStack Node-3",
    "description": "OpenStack compute node node-3",
    "metadata": {
      "cluster_id": "openstack-cluster-01",
      "node_name": "node-3",
      "risk_tier": "high",
      "preferred_executor_profiles": ["host-ssh-readonly", "host-ssh-mutating"]
    },
    "capabilities": [
      "read_host_state",
      "host_mutation"
    ],
    "credential_scope": {
      "ssh_host": "node-3"
    }
  }
]
```

---

## 5. SSH 密钥管理

### 5.1 密钥生命周期

| 阶段 | 操作 | 频率 |
|------|------|------|
| **初始化** | 在管理集群生成 SSH 密钥对 | 一次性 |
| **部署** | 公钥部署到目标主机 `~/.ssh/authorized_keys` | 一次性 |
| **导入** | 私钥以 Secret 导入 islap 命名空间 | 一次性 |
| **轮换** | 生成新密钥 → 部署公钥 → 更新 Secret → 重启 gateway | 按安全策略 |
| **回收** | 删除 Secret + 从目标主机移除公钥 | 主机下线时 |

### 5.2 密钥隔离

- 每台主机使用独立的 SSH 密钥对
- 每个密钥对存储在独立的 Secret 中
- Secret 只读挂载（`readOnly: true`, `defaultMode: 0400`）
- Secret 名称与节点名称对应：`ssh-key-{node-name}`

### 5.3 推荐做法

```bash
# 1. 每台主机独立密钥
ssh-keygen -t ed25519 -f ${NODE_NAME}-id_rsa -N "" -C "ssh-gateway-${NODE_NAME}@logoscope"

# 2. 部署公钥
ssh-copy-id -i ${NODE_NAME}-id_rsa.pub root@${NODE_NAME}

# 3. 验证无密码登录
ssh -i ${NODE_NAME}-id_rsa root@${NODE_NAME} "hostname"

# 4. 创建 Secret
kubectl create secret generic ssh-key-${NODE_NAME} \
  --namespace=islap \
  --from-file=id_rsa=./${NODE_NAME}-id_rsa

# 5. 验证挂载
kubectl exec -n islap deploy/ssh-gateway -- ls -la /etc/ssh-keys/${NODE_NAME}/
```

---

## 6. 安全考虑

| 维度 | 措施 |
|------|------|
| **SSH 密钥** | 独立密钥对、只读 Secret 挂载（0400）、定期轮换 |
| **命令校验** | 复用 toolbox-gateway 的 shell 语法拦截（`_SHELL_OPERATOR_TOKENS`） |
| **输出控制** | 最大输出 256KB，裁剪长输出 |
| **超时控制** | 默认 60s，最大 300s，防止命令 hang |
| **StrictHostKeyChecking** | 首次连接跳过（已知主机环境），后续可启用 |
| **审计** | 全程结构化审计 → ClickHouse 持久化 → REST API 可查/回放（详见 6.1） |
| **OPA 策略** | 主机级命令同样经过 OPA 策略评估 |
| **网络隔离** | SSH Gateway 只在 ClusterIP 暴露，不对外暴露 |

### 6.1 审计能力详述

SSH Gateway 通过 exec-service 调度执行，自动继承 exec-service 现有的完整审计系统，无需额外开发。

**审计记录内容**（存储于 ClickHouse `logs.exec_command_runs` / `logs.exec_command_audits` 表）：

| 字段类别 | 包含信息 |
|---------|---------|
| 命令 | `command` 原文、`command_type`、`purpose` |
| 目标 | `target_kind`、`target_identity`、`target_node_name`、`target_cluster_id` |
| 执行方式 | `executor_type`、`executor_profile`、`dispatch_mode` |
| 执行结果 | `stdout`/`stderr`（截断至 12K 字符）、`exit_code`、`status`、`error_code` |
| 用户上下文 | `session_id`、`message_id`、`action_id`、`step_id` |
| 策略评估 | `policy_decision_id`（关联 OPA 评估）、`risk_level`、`approval_policy` |
| 时序 | `created_at`、`started_at`、`ended_at`、`duration_ms` |

**审计 API**：
- `GET /api/v1/exec/audit` — 分页查询审计记录
- `GET /api/v1/exec/runs/{run_id}/replay` — 单次执行的完整回放（含事件、策略决策、输入输出）

**事件级追踪**：`logs.exec_command_events` 表记录 `command_started`、`command_output_delta`、`command_finished` 等细粒度事件，支持流式消费。

**策略决策持久化**：每次执行前经过 OPA 策略评估，决策记录存储于 `logs.exec_policy_decisions` 表，包含 `input_hash`（SHA-256）用于完整性校验。

---

## 7. 实施计划

### Phase 1: SSH Gateway 服务（1-2 天）

- 创建 `ssh-gateway/app.py` (~150 行)
- 创建 `ssh-gateway/Dockerfile`（基于 python:3.11-slim，只需 fastapi + uvicorn）
- 创建 `ssh-gateway/requirements-runtime.txt`

### Phase 2: 部署配置（0.5 天）

- 创建 `deploy/ssh-gateway.yaml`（Deployment + Service + ConfigMap）
- 创建 `deploy/ssh-hosts-config.yaml`
- 创建 `deploy/ssh-keys/node-3-secret.yaml` 示例

### Phase 3: 模板与目标注册（0.5 天）

- 更新 `deploy/exec-service.yaml` 添加 `HOST_SSH_READONLY` 和 `HOST_SSH_MUTATING` 模板
- 更新 `deploy/ai-service.yaml` 的 `AI_RUNTIME_V4_REMOTE_TARGETS_JSON` 追加 `host_node` 目标

### Phase 4: 密钥部署（1 小时）

- 生成 SSH 密钥对
- 公钥部署到目标主机
- Secret 导入管理集群

### Phase 5: 验证（1 小时）

- 本地命令无回归
- 主机命令在新旧节点正确执行
- 超时/错误处理正常

---

## 8. 文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `ssh-gateway/app.py` | 新建 | SSH Gateway 服务（~150 行） |
| `ssh-gateway/Dockerfile` | 新建 | 构建 python:3.11-slim 镜像 |
| `ssh-gateway/requirements-runtime.txt` | 新建 | fastapi, uvicorn |
| `deploy/ssh-gateway.yaml` | 新建 | Deployment + Service + RBAC |
| `deploy/ssh-hosts-config.yaml` | 新建 | 节点连接映射 ConfigMap |
| `deploy/ssh-keys/node-3-secret.yaml` | 新建 | SSH 私钥 Secret 示例 |
| `deploy/exec-service.yaml` | 修改 | 追加 HOST_SSH_* 模板 |
| `deploy/ai-service.yaml` | 修改 | 追加 host_node 目标注册 |
| `docs/operations/remote-cluster-execution.zh-CN.md` | 修改 | 追加 SSH Gateway 章节 |

---

## 9. 验证方案

```bash
# 9a. 直接调用 SSH Gateway
curl -sS -X POST http://ssh-gateway:8096/exec \
  --data-urlencode "command=hostname" \
  --data-urlencode "node=node-3"
# 预期: node-3

# 9b. 主机级诊断命令
curl -sS -X POST http://ssh-gateway:8096/exec \
  --data-urlencode "command=df -h / | tail -1" \
  --data-urlencode "node=node-3"

# 9c. 通过 exec-service 路由
curl -X POST http://exec-service:8095/api/v1/exec/runs \
  -H "Content-Type: application/json" \
  -d '{
    "command": "journalctl -u nova-scheduler --no-pager | tail -20",
    "purpose": "verify:ssh-gateway",
    "target_kind": "host_node",
    "target_identity": "host:node-3"
  }'

# 9d. 本地集群命令无回归
curl -X POST http://exec-service:8095/api/v1/exec/runs \
  -H "Content-Type: application/json" \
  -d '{
    "command": "kubectl get pods -n islap | head -5",
    "purpose": "verify:regression",
    "target_kind": "k8s_cluster",
    "target_identity": "namespace:islap"
  }'

# 9e. 远端 K8s 命令（验证已有功能未受影响）
curl -X POST http://exec-service:8095/api/v1/exec/runs \
  -H "Content-Type: application/json" \
  -d '{
    "command": "kubectl get pods -A | head -10",
    "purpose": "verify:remote-k8s",
    "target_kind": "k8s_cluster",
    "target_identity": "namespace:openstack-cluster-01"
  }'
```

---

## 10. 不回滚方案

| 阶段 | 回滚操作 |
|------|----------|
| Phase 1: SSH Gateway | `kubectl delete deploy/ssh-gateway -n islap` |
| Phase 3: 模板 | 恢复 `HOST_SSH_*` 模板为空值 |
| Phase 3: 目标注册 | 从 `AI_RUNTIME_V4_REMOTE_TARGETS_JSON` 移除 `host_node` 条目 |

---

## 11. 未来扩展

### 11.1 扩展模式

SSH Gateway 遵循与 toolbox-gateway（kubeconfig 模式）一致的架构模式：

```
新 Gateway 服务 + 新 executor profile + 新 target kind = 新传输协议
```

添加一个新的远程执行协议只需：
1. 创建新的 Gateway 服务（~150 行代码 + Dockerfile）
2. 定义 executor profile 模板（在 exec-service 环境变量中添加）
3. 注册对应类型的 target（在 AI_RUNTIME_V4_REMOTE_TARGETS_JSON 中添加）

### 11.2 WinRM Gateway（Windows 主机）

当需要管理 Windows 主机时，可新增 `winrm-gateway` 服务：

```python
# winrm-gateway/app.py（示例结构）
def _execute_winrm(command, host, creds):
    # 使用 pywinrm 或 PowerShell Remoting
    # 与 SSH Gateway 相同的 API 契约
    pass
```

- 创建 `deploy/winrm-gateway.yaml`，挂载凭据 Secret
- 新增 executor profile `WINRM_READONLY` / `WINRM_MUTATING`
- 新增 target kind `windows_host`

### 11.3 Network Device Gateway（网络设备）

对于交换机/路由器等交互式 CLI，可新增 `net-gateway` 服务：

```python
# net-gateway/app.py（示例结构）
def _execute_network(command, device, creds):
    # SSH 到设备 + expect 风格交互
    # 处理不同厂商的 prompt 和分页
    pass
```

- 分页处理（`--more--` 等）
- 超时控制（某些命令执行时间长）
- 厂商适配层（Cisco、Huawei、Juniper 等不同 prompt 格式）

### 11.4 扩展原则

- **统一 API 契约**：所有 Gateway 使用 `POST /exec`（command + target 标识）
- **统一调度入口**：exec-service 的 policy 分类 + 模板展开 + dispatch 链路不变
- **统一审计**：所有 Gateway 都通过 exec-service 调度，自动获得完整审计能力
- **统一 Secret 管理**：凭据统一以 K8s Secret 挂载，readOnly + 独立密钥
