# 远端 K8s 集群命令执行

## 背景

AI Runtime 在执行 `kubectl get pods -l app=cinder-api` 等命令时，实际调用的是**本地**集群 (namespace islap) 的 K8s API。

当目标服务（如 OpenStack 的 nova、cinder、glance）运行在远端 Kubernetes 集群时，command 需要路由到远端集群执行。

## 架构

```
AI Service                     exec-service                     toolbox-gateway
  |                                |                                |
  | POST /api/v1/exec/runs         | resolve_executor()             |
  | target_identity:               | → template expansion           |
  |   "namespace:openstack-..."    | → {target_cluster_id}          |
  |                                | → "openstack-cluster-01"       |
  |                                |                                |
  |                                | curl POST /exec               |
  |                                | command=...                    |
  |                                | kubeconfig=openstack-cluster-01|
  |                                |                                |
  |                                |    KUBECONFIG=/etc/kubeconfigs/openstack-cluster-01
  |                                |    kubectl --kubeconfig=... get pods
```

### 核心流程

1. AI 发送命令，指定 `target_identity: "namespace:openstack-cluster-01"`
2. Target Registry 解析目标身份，返回 metadata 中包含 `cluster_id: "openstack-cluster-01"`
3. `resolve_executor()` 展开 executor 模板，将 `{target_cluster_id}` 替换为 `openstack-cluster-01`
4. exec-service 执行 curl 请求 toolbox-gateway，附带 `kubeconfig=openstack-cluster-01`
5. toolbox-gateway 设置 `KUBECONFIG=/etc/kubeconfigs/openstack-cluster-01`，运行 kubectl
6. kubectl 使用远端集群的 kubeconfig 连接到目标集群 API Server

### 方案选型

| 方案 | 代表产品 | 说明 |
|------|----------|------|
| **Kubeconfig Secrets** | Crossplane, ArgoCD | kubeconfig 以 Secret 挂载到 toolbox-gateway（**选用**） |
| SSH Bastion | Rundeck, Ansible | 通过跳板机 SSH 执行（密钥管理复杂） |
| Agent Tunnel | Rancher, Anthos | 远端部署 Agent（组件多，维护成本高） |

选用理由：零远端组件、兼容现有 executor 模板系统、安全边界清晰。

## 修改文件清单

| 文件 | 改动类型 | 说明 |
|------|----------|------|
| `toolbox-gateway/app.py` | ✅ 已修改 | 添加 kubeconfig 参数解析、路径解析、子进程 env 注入 |
| `deploy/toolbox-gateway.yaml` | ✅ 已修改 | 挂载 kubeconfig Secret 到 `/etc/kubeconfigs/` |
| `deploy/exec-service.yaml` | ✅ 已修改 | 模板追加 `kubeconfig={target_cluster_id}` |
| `deploy/ai-service.yaml` | ✅ 已修改 | 配置抽为 ConfigMap + `AI_RUNTIME_V4_REMOTE_TARGETS_JSON` |
| `ai-service/.../targets/service.py` | ✅ 已修改 | 新增远端目标自动注册逻辑 |
| `deploy/kubeconfigs/remote-cluster-01-secret.yaml` | ✅ 已创建 | Secret 模板 + 远端 RBAC 参考 |

### 无需修改

- `exec-service/core/executor_registry.py` — `_template_context()` 已提供 `{target_cluster_id}`
- `exec-service/core/dispatch.py` — 通用调度管道
- `exec-service/api/execute.py` — 前置检查不变
- `exec-service/core/runner.py` — 子进程执行不变

## 实现细节

### 1. toolbox-gateway — kubeconfig 切换

**文件**: `toolbox-gateway/app.py`

关键函数：

- `_resolve_kubeconfig_path(name: str) -> str | None`
  - `name` 为空或 `"default"` → 返回 `None`（使用 Pod 默认 ServiceAccount）
  - 对应文件存在 → 返回 `/etc/kubeconfigs/{name}` 绝对路径
  - 文件不存在 → 日志 WARNING，返回 `None`（不中断）

- `_execute_command()` 新增 `kubeconfig_path` 参数
  - 有值时，在 `proc_env` 中设置 `KUBECONFIG` 环境变量
  - 子进程继承该环境变量，kubectl 自动读取对应 kubeconfig

### 2. executor 模板路由

**文件**: `deploy/exec-service.yaml`

```yaml
# 修改前
EXEC_EXECUTOR_TEMPLATE__TOOLBOX_K8S_READONLY:
  curl ... --data-urlencode command={command_quoted}

# 修改后
EXEC_EXECUTOR_TEMPLATE__TOOLBOX_K8S_READONLY:
  curl ... --data-urlencode command={command_quoted}
          --data-urlencode kubeconfig={target_cluster_id}
```

- `target_cluster_id` 为空 → 发送 `kubeconfig=` → toolbox-gateway 视为 default
- `target_cluster_id` 为具体值 → 路由到对应 kubeconfig

### 3. 远端目标自动注册

**文件**: `ai-service/ai/runtime_v4/targets/service.py`

新增函数：

- `_parse_json_target_specs(env_key)` — 从环境变量解析 JSON 数组
- `_remote_target_specs_for_bootstrap()` — 读取 `AI_RUNTIME_V4_REMOTE_TARGETS_JSON`

在 `ensure_runtime_v4_default_targets()` 中，处理完本地目标后，再处理远端目标：
- 按 `target_kind` + `target_identity` 查找是否已存在
- 不存在 → 创建新目标
- 已存在但缺少能力/metadata → 自动修复
- 已存在且完整 → 跳过

## 部署步骤

### 前置条件

- Logoscope 集群可访问远端集群的 API Server（网络通）
- 远端集群已创建 ServiceAccount 并绑定所需 RBAC

### Step 1: 在远端集群创建 RBAC

在 OpenStack K8s 集群执行：

```bash
# 创建 ServiceAccount
kubectl create sa logoscope-remote-executor -n kube-system

# 创建只读 ClusterRole
kubectl apply -f - <<'EOF'
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: logoscope-remote-readonly
rules:
- apiGroups: ["", "apps", "batch", "networking.k8s.io"]
  resources: ["pods", "pods/log", "deployments", "statefulsets", "services",
              "namespaces", "nodes", "events", "configmaps"]
  verbs: ["get", "list", "watch"]
EOF

# 绑定
kubectl create clusterrolebinding logoscope-remote-readonly \
  --clusterrole=logoscope-remote-readonly \
  --serviceaccount=kube-system:logoscope-remote-executor
```

### Step 2: 提取 kubeconfig

在远端集群执行：

```bash
TOKEN=$(kubectl get secret $(kubectl get sa logoscope-remote-executor -n kube-system -o jsonpath='{.secrets[0].name}') -n kube-system -o jsonpath='{.data.token}' | base64 -d)
APISERVER=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')
CA=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')

cat > openstack-cluster-kubeconfig <<EOF
apiVersion: v1
kind: Config
current-context: openstack
clusters:
- cluster:
    certificate-authority-data: ${CA}
    server: ${APISERVER}
  name: openstack
contexts:
- context:
    cluster: openstack
    user: logoscope
  name: openstack
users:
- name: logoscope
  user:
    token: ${TOKEN}
EOF
```

### Step 3: 导入 Secret 到 Logoscope 集群

```bash
kubectl create secret generic kubeconfig-remote-cluster-01 \
  --namespace=islap \
  --from-file=openstack-cluster-01=./openstack-cluster-kubeconfig
```

**重要**: Secret 的 key 名称（`openstack-cluster-01`）必须与 `cluster_id` 一致。

### Step 4: 构建并部署

```bash
# 构建 ai-service 镜像（包含远端目标注册代码）
cd /root/logoscope/.worktrees/openhands-runtime-v4-backend
scripts/k8s-image-ops.sh build-push ai-service <tag>

# 构建 toolbox-gateway 镜像（包含 kubeconfig 切换代码）
scripts/k8s-image-ops.sh build-push toolbox-gateway <tag>

# 更新部署
kubectl apply -f deploy/toolbox-gateway.yaml
kubectl apply -f deploy/ai-service.yaml  # 包含 ConfigMap + Deployment
kubectl apply -f deploy/exec-service.yaml

# 等待就绪
kubectl rollout status deploy/toolbox-gateway -n islap -w
kubectl rollout status deploy/ai-service -n islap -w
kubectl rollout status deploy/exec-service -n islap -w
```

### Step 5: 验证

```bash
# 5a. 验证远端目标已注册
kubectl exec -n islap deploy/ai-service -- curl -s http://localhost:8090/api/v2/targets | python3 -m json.tool | grep target_identity
# 应输出:
# "target_identity": "namespace:openstack-cluster-01",
# "target_identity": "namespace:islap",
# "target_identity": "database:logs",

# 5b. 验证 kubeconfig 已挂载
kubectl exec -n islap deploy/toolbox-gateway -- ls -la /etc/kubeconfigs/
# 应输出: openstack-cluster-01

# 5c. 验证模板已更新
kubectl exec -n islap deploy/exec-service -- env | grep TOOLBOX_K8S_READONLY
# 应包含: --data-urlencode kubeconfig={target_cluster_id}
```

### Step 6: 端到端测试

```bash
# 测试本地集群命令（无回归）
curl -X POST http://exec-service:8095/api/v1/exec/runs \
  -H "Content-Type: application/json" \
  -d '{
    "command": "kubectl get pods -n islap --no-headers | head -3",
    "purpose": "verify:local",
    "target_kind": "k8s_cluster",
    "target_identity": "namespace:islap"
  }'

# 测试远端集群命令
curl -X POST http://exec-service:8095/api/v1/exec/runs \
  -H "Content-Type: application/json" \
  -d '{
    "command": "kubectl get pods -A --no-headers | head -10",
    "purpose": "verify:remote",
    "target_kind": "k8s_cluster",
    "target_identity": "namespace:openstack-cluster-01"
  }'
```

## 新增另一个远端集群

如需接入第二个远端集群（如 `openstack-cluster-02`），执行以下步骤：

### 1. 创建 kubeconfig Secret

```bash
kubectl create secret generic kubeconfig-remote-cluster-02 \
  --namespace=islap \
  --from-file=openstack-cluster-02=./cluster-02-kubeconfig
```

### 2. 挂载到 toolbox-gateway

编辑 `deploy/toolbox-gateway.yaml`，在 `volumes` 和 `volumeMounts` 中追加：

```yaml
# volumeMounts 追加:
- name: kubeconfig-remote-cluster-02
  mountPath: /etc/kubeconfigs/openstack-cluster-02
  subPath: openstack-cluster-02
  readOnly: true

# volumes 追加:
- name: kubeconfig-remote-cluster-02
  secret:
    secretName: kubeconfig-remote-cluster-02
```

### 3. 注册远端目标

在 ConfigMap `ai-service-config` 的 `AI_RUNTIME_V4_REMOTE_TARGETS_JSON` 中追加条目。如果已有 JSON，直接在数组中添加新元素：

```json
[
  {"target_kind":"k8s_cluster","target_identity":"namespace:openstack-cluster-01",...},
  {"target_kind":"k8s_cluster","target_identity":"namespace:openstack-cluster-02",...}
]
```

更新 ConfigMap：

```bash
kubectl apply -f deploy/ai-service.yaml
kubectl rollout restart deploy/ai-service -n islap
```

### 4. 部署

```bash
kubectl apply -f deploy/toolbox-gateway.yaml
kubectl rollout status deploy/toolbox-gateway -n islap -w
```

## 配置参考

### `AI_RUNTIME_V4_REMOTE_TARGETS_JSON` 格式

```json
[{
  "target_kind": "k8s_cluster",
  "target_identity": "namespace:<cluster-id>",
  "display_name": "Human readable name",
  "description": "Description of this cluster",
  "metadata": {
    "cluster_id": "openstack-cluster-01",
    "risk_tier": "high",
    "preferred_executor_profiles": [
      "toolbox-k8s-readonly",
      "toolbox-k8s-mutating"
    ]
  },
  "capabilities": [
    "read_logs",
    "restart_workload",
    "helm_read",
    "helm_mutation"
  ],
  "credential_scope": {
    "kubeconfig_name": "openstack-cluster-01"
  }
}]
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `target_kind` | 是 | 固定 `k8s_cluster` |
| `target_identity` | 是 | 唯一标识，格式 `namespace:<name>` |
| `metadata.cluster_id` | 是 | 对应 Secret key 名称、kubeconfig 文件名 |
| `metadata.risk_tier` | 是 | 风险等级 `low`/`medium`/`high` |
| `metadata.preferred_executor_profiles` | 是 | 路由到 toolbox-k8s 配置 |
| `capabilities` | 是 | 能力声明 |
| `credential_scope.kubeconfig_name` | 否 | 记录用途，不影响功能 |

### 模板配置 (`deploy/exec-service.yaml`)

```yaml
- name: EXEC_EXECUTOR_TEMPLATE__TOOLBOX_K8S_READONLY
  value: "curl -sS --fail-with-body -X POST
    http://toolbox-gateway.islap.svc.cluster.local:8088/exec
    --data-urlencode command={command_quoted}
    --data-urlencode kubeconfig={target_cluster_id}"

- name: EXEC_EXECUTOR_TEMPLATE__TOOLBOX_K8S_MUTATING
  value: "curl -sS --fail-with-body -X POST
    http://toolbox-gateway.islap.svc.cluster.local:8088/exec
    --data-urlencode command={command_quoted}
    --data-urlencode kubeconfig={target_cluster_id}"
```

模板变量由 `_template_context()` 提供（`executor_registry.py:311`）：

| 变量 | 说明 |
|------|------|
| `{command_quoted}` | shlex.quote 转义后的命令 |
| `{target_cluster_id}` | 目标集群 ID（空=本地） |
| `{target_identity}` | 目标身份 |
| `{target_namespace}` | 目标命名空间 |

## 运维指南

### 验证命令

```bash
# 查看所有注册目标
kubectl exec -n islap deploy/ai-service -- curl -s http://localhost:8090/api/v2/targets | python3 -m json.tool

# 查看目标详情
kubectl exec -n islap deploy/ai-service -- curl -s http://localhost:8090/api/v2/targets/<target-id>

# 查看 executor 状态
curl -X POST http://exec-service:8095/api/v1/exec/executor-statuses

# 查看 toolbox-gateway 挂载的 kubeconfig
kubectl exec -n islap deploy/toolbox-gateway -- ls -la /etc/kubeconfigs/
```

### 常见问题

**Q: 远端目标未注册**
- 确认 `AI_RUNTIME_V4_REMOTE_TARGETS_JSON` 环境变量已注入：`kubectl exec deploy/ai-service -- env | grep AI_RUNTIME_V4_REMOTE`
- 确认 ai-service 镜像包含新代码：`kubectl exec deploy/ai-service -- grep _remote_target_specs_for_bootstrap /app/ai/runtime_v4/targets/service.py`
- 确认未启用 `AI_RUNTIME_V4_TARGET_AUTO_SEED_ENABLED=false`

**Q: 远端命令返回空或连接错误**
- 检查网络连通性：从 Logoscope 集群能否访问远端 API Server
- 检查 kubeconfig 是否过期：token 可能已过期
- 检查 Secret 是否正确挂载：`kubectl exec deploy/toolbox-gateway -- cat /etc/kubeconfigs/openstack-cluster-01 | head -5`

**Q: toolbox-gateway 返回 403 command head not allowed**
- 确认 `TOOLBOX_GATEWAY_ALLOWED_HEADS` 包含 `kubectl`
- 默认值包含 `kubectl`，除非自定义覆盖

**Q: 本地集群命令行为异常（回归）**
- 确认 `target_cluster_id` 为空时，`kubeconfig=` 空值被 toolbox-gateway 视为 default
- 回退方式：移除 `--data-urlencode kubeconfig=` 部分，恢复到旧模板

### 回滚

按阶段回滚，各阶段可独立执行：

| 阶段 | 回滚操作 |
|------|----------|
| Phase 1: toolbox-gateway | 恢复旧版 `app.py`，移除 kubeconfig 逻辑 |
| Phase 2: Secret 挂载 | 移除 toolbox-gateway 的 volumeMount/volume 配置 |
| Phase 3: 模板更新 | 恢复 `deploy/exec-service.yaml` 旧模板（去掉 kubeconfig 参数） |
| Phase 4: 目标注册 | 移除 `AI_RUNTIME_V4_REMOTE_TARGETS_JSON`，重启 ai-service |

## 安全注意事项

1. **所有命令都经过 toolbox-gateway 校验**，包括头命令白名单和 shell 语法拦截，远端命令也不例外
2. **OPA 策略对本地和远端命令一视同仁**，无需额外配置
3. **Kubeconfig Secret 受 k8s RBAC 保护**，仅 islap 命名空间的授权用户可访问
4. **Secrets 只读挂载**：`readOnly: true`
5. **审计可追溯**：exec-service 审计记录的 `resolved_target_context` 中包含 `cluster_id`
6. **远端 token 应设置 TTL**，定期轮换

---

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
        ┌────────────────────────────────────────────────┼──────────────────┐
        │ 前端管理 (在 Logoscope UI 中)                    │                  │
        │ ┌─────────────────────┐                        │                  │
        │ │ SSHHostsPage        │                        ▼                  │
        │ │ 注册 / 列 表 / 删除  │                 SSH Gateway               │
        │ │ 粘贴私钥注册新主机    │                    (:8096)                │
        │ └─────────┬───────────┘                        │                  │
        │           │ API (POST/GET/DELETE)              │                  │
        │           ▼                                    ▼                  │
        │  Vite Proxy → /ssh-gateway/           ssh -i <key_file> \         │
        │  → localhost:8096                      root@node-3 "cmd"          │
        │                                                    │              │
        │  ClickHouse 动态注册表                               │              │
        │  (ssh_host_registry)                               ▼              │
        └─────────────────────────────────────────→ stdout/stderr → AI      │
                                                                           │
  主机注册方式:
    ┌── 静态 YAML (deploy/ssh-hosts-config.yaml)     ← Secret 挂载密钥
    └── 动态 API (ssh-gateway/hosts API)             ← 粘贴私钥注册（Base64 存储）
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

新增主机有两种方式：**静态 ConfigMap 方式**（传统）和 **动态 API 注册方式**（推荐，支持前端管理）。

#### 方式 A：静态 ConfigMap（传统，适合固定节点）

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

#### 方式 B：动态 API 注册（推荐，支持前端粘贴私钥）

基于 ClickHouse 的动态主机注册表，可通过前端页面或 API 直接注册主机。

**方式 B1 — 通过前端页面管理**

在 Logoscope UI 中打开 **系统 → SSH 主机管理** (`/ssh-hosts`)：

1. 点击「注册主机」按钮
2. 填写主机信息：名称、IP/域名、SSH 端口、用户
3. 在「SSH 私钥内容」文本框中粘贴私钥文件内容（`-----BEGIN OPENSSH PRIVATE KEY-----` ...）
4. 可选添加标签（如 `env=prod`）
5. 点击「注册」提交

注册完成后主机出现在列表中，支持一键删除。

**方式 B2 — 通过 API 注册**

```bash
# 注册主机（含私钥内容）
curl -sS -X POST http://ssh-gateway:8096/hosts \
  -H "Content-Type: application/json" \
  -d '{
    "name": "node-N",
    "host": "<node-N-ip>",
    "port": 22,
    "user": "root",
    "private_key": "-----BEGIN OPENSSH PRIVATE KEY-----\n...\n-----END OPENSSH PRIVATE KEY-----",
    "labels": {"env": "prod"}
  }'

# 注册主机（使用已有的密钥文件路径）
curl -sS -X POST http://ssh-gateway:8096/hosts \
  -H "Content-Type: application/json" \
  -d '{
    "name": "node-N",
    "host": "<node-N-ip>",
    "port": 22,
    "user": "root",
    "key_file": "/etc/ssh-keys/node-N/id_rsa"
  }'

# 列出所有已注册主机
curl -sS http://ssh-gateway:8096/hosts | python3 -m json.tool

# 删除主机
curl -sS -X DELETE http://ssh-gateway:8096/hosts/node-N
```

**私钥存储说明：** 通过 API/前端注册时填写的私钥内容会被 Base64 编码后存储在 ClickHouse `logs.ssh_host_registry.private_key` 列。执行 SSH 命令时，SSH Gateway 将私钥解码写入临时文件（`/tmp/ssh-key-*.tmp`），连接建立后自动清理。API 响应中**不会**返回私钥字段。

#### 静态 vs 动态对比

| 特性 | 静态 ConfigMap | 动态 API 注册 |
|------|---------------|-------------|
| 密钥存储 | K8s Secret 挂载 | ClickHouse（Base64 编码） |
| 新增节点 | 需要修改 YAML + kubectl apply | API 调用即可 |
| 即时生效 | 需要滚动重启 | 立即生效 |
| 持久化 | 无 | ClickHouse 表 `ssh_host_registry` |
| 前端管理 | 不支持 | SSH 主机管理页面 |
| 密钥轮换 | 更新 Secret + rollout | 重新注册覆盖即可 |

### Host Registry API 参考

动态注册表通过 ClickHouse 实现，SSH Gateway 提供 RESTful API：

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/hosts` | 列出所有已注册主机（未删除） |
| `GET` | `/hosts/{name}` | 查询单个主机详情 |
| `POST` | `/hosts` | 注册新主机或更新已有主机 |
| `DELETE` | `/hosts/{name}` | 软删除主机 |

**注册请求字段：**

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `name` | string | — | 唯一主机名（必填） |
| `host` | string | — | IP 地址或主机名（必填） |
| `port` | int | 22 | SSH 端口（1-65535） |
| `user` | string | `root` | SSH 登录用户 |
| `key_file` | string | ""（空字符串） | 密钥文件路径（与 `private_key` 二选一） |
| `private_key` | string | null | 粘贴私钥内容（API 不返回此字段） |
| `labels` | object | null | 自定义标签（K/V） |

**环境变量配置：**

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SSH_GATEWAY_HOST_REGISTRY_CH_URL` | `http://clickhouse:8123` | ClickHouse HTTP 地址 |
| `SSH_GATEWAY_HOST_REGISTRY_CH_DATABASE` | `logs` | 数据库名 |
| `SSH_GATEWAY_HOST_REGISTRY_CH_TABLE` | `ssh_host_registry` | 主机注册表名 |
| `SSH_GATEWAY_HOST_REGISTRY_CH_USER` | `default` | ClickHouse 用户 |
| `SSH_GATEWAY_HOST_REGISTRY_CH_PASSWORD` | `""` | ClickHouse 密码 |
| `SSH_GATEWAY_HOST_REGISTRY_CH_FAIL_OPEN` | `true` | CH 不可用时是否降级 |

### 前端管理页面

SSH 主机管理页面已集成到 Logoscope 前端，路径为 `/ssh-hosts`（侧边栏 → 系统 → SSH 主机管理）：

- **列表查看：** 展示所有已注册主机的名称、IP、端口、用户、标签、创建时间
- **注册主机：** 弹窗表单，支持填写连接信息 + 粘贴 SSH 私钥内容
- **删除主机：** 确认后软删除（ClickHouse `is_deleted = 1`）
- **加载/空状态：** 优雅处理初始化加载和空列表场景

开发环境通过 Vite 代理 `/ssh-gateway/*` → `http://localhost:8096`，生产环境通过 Nginx 反向代理。

### 回滚

| 阶段 | 回滚操作 |
|------|----------|
| SSH Gateway 服务 | `kubectl delete deploy/ssh-gateway -n islap` |
| Secret 和 ConfigMap | `kubectl delete secret ssh-key-node-3 -n islap` + `kubectl delete configmap ssh-hosts-config -n islap` |
| Executor 模板 | 恢复 `HOST_SSH_*` 模板为空值 |
| 目标注册 | 从 `AI_RUNTIME_V4_REMOTE_TARGETS_JSON` 移除 `host_node` 条目 |
