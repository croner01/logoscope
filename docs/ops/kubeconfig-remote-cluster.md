# 远程集群配置说明

## 概述

Logoscope 通过 **toolbox-gateway** 访问远程 Kubernetes 集群（如 OpenStack 部署的 K8s 集群）。
远程集群的访问凭据以 Kubernetes Secret 方式存储，挂载到 toolbox-gateway Pod 中。

## 架构

```
用户/LLM → ai-service → exec-service → toolbox-gateway → kubectl --kubeconfig=<文件> → 远程集群 API Server
```

关键组件：

| 组件 | 作用 |
|------|------|
| `ai-service` | 持有远程目标注册表 (`AI_RUNTIME_V4_REMOTE_TARGETS_JSON`) |
| `exec-service` | 根据 `target_identity` 路由到对应执行器 |
| `toolbox-gateway` | 持有 kubeconfig 文件，执行 kubectl 命令 |

---

## 一、新增集群

要将一个新的远程 K8s 集群添加到 Logoscope，需要完成以下 4 个步骤：

### 步骤 1：创建 kubeconfig 文件

在远程集群的控制节点（或已配好 kubectl 的机器）上，准备 kubeconfig 文件。

#### 1.1 获取集群信息

```bash
# 获取 CA 证书（base64 编码）
cat /etc/kubernetes/pki/ca.crt | base64 -w0

# 获取 ServiceAccount Token（在 Logoscope 所在的集群上已创建好）
# 如果还没有 token，需要先在远程集群上创建:
kubectl create sa logoscope-remote-executor -n kube-system
kubectl create clusterrolebinding logoscope-remote-executor \
  --clusterrole=view \
  --serviceaccount=kube-system:logoscope-remote-executor

# 获取 token
kubectl get secret -n kube-system \
  $(kubectl get sa logoscope-remote-executor -n kube-system -o jsonpath='{.secrets[0].name}') \
  -o jsonpath='{.data.token}' | base64 -d
```

#### 1.2 组装 kubeconfig

```yaml
apiVersion: v1
kind: Config
current-context: <集群名称>
clusters:
- cluster:
    certificate-authority-data: <base64编码的CA证书>
    server: https://<API_SERVER_IP>:6443
  name: <集群名称>
contexts:
- context:
    cluster: <集群名称>
    user: logoscope
  name: <集群名称>
users:
- name: logoscope
  user:
    token: <service-account-token>
```

> **注意：** 确保 ServiceAccount 有足够的 RBAC 权限来执行诊断命令（至少需要 `view` ClusterRole）。

### 步骤 2：创建 Kubernetes Secret

将 kubeconfig 保存为文件，然后创建 Secret：

```bash
# 方式一：从文件创建
kubectl create secret generic -n islap kubeconfig-<集群ID> \
  --from-file=<集群ID>=<kubeconfig文件路径>

# 方式二：手动写入（将上述 yaml 内容 base64 编码）
echo '<base64编码的完整kubeconfig>' > /tmp/kubeconfig.b64
kubectl create secret -n islap generic kubeconfig-<集群ID> \
  --from-file=<集群ID>=/dev/stdin < /tmp/kubeconfig.b64
```

示例（参考现有配置）：

```bash
kubectl create secret generic -n islap kubeconfig-my-cluster \
  --from-file=my-cluster=./my-cluster-kubeconfig.yaml
```

### 步骤 3：更新 toolbox-gateway Deployment

新增的 Secret 需要挂载到 toolbox-gateway Pod 中。

#### 方法 A：手动编辑 Deployment

```bash
kubectl edit deployment -n islap toolbox-gateway
```

在 `spec.template.spec.volumes` 中添加新卷：

```yaml
spec:
  template:
    spec:
      volumes:
      - name: kubeconfig-<集群ID>
        secret:
          defaultMode: 420
          secretName: kubeconfig-<集群ID>
```

在 `spec.template.spec.containers[0].volumeMounts` 中添加挂载：

```yaml
        volumeMounts:
        - mountPath: /etc/kubeconfigs/<集群ID>
          name: kubeconfig-<集群ID>
          readOnly: true
          subPath: <集群ID>
```

> **注意：** `subPath` 必须与 Secret 中的 key 名称一致，`mountPath` 中的文件名也保持一致。

#### 方法 B：使用 kubectl patch

```bash
# 添加 volume
kubectl patch deployment -n islap toolbox-gateway --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/volumes/-","value":{"name":"kubeconfig-<集群ID>","secret":{"secretName":"kubeconfig-<集群ID>","defaultMode":420}}}]'

# 添加 volumeMount
kubectl patch deployment -n islap toolbox-gateway --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/volumeMounts/-","value":{"name":"kubeconfig-<集群ID>","mountPath":"/etc/kubeconfigs/<集群ID>","readOnly":true,"subPath":"<集群ID>"}}]'
```

### 步骤 4：注册远程目标到 ai-service

修改 `ai-service-config` ConfigMap，在 `AI_RUNTIME_V4_REMOTE_TARGETS_JSON` 中添加新集群的注册信息：

```bash
kubectl edit cm -n islap ai-service-config
```

找到 `AI_RUNTIME_V4_REMOTE_TARGETS_JSON` 字段，在 JSON 数组末尾添加新条目。

每个集群目标的格式：

```json
{
  "target_kind": "k8s_cluster",
  "target_identity": "namespace:<集群ID>",
  "display_name": "<显示名称>",
  "description": "<描述>",
  "metadata": {
    "cluster_id": "<集群ID>",
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
    "kubeconfig_name": "<集群ID>"
  }
}
```

**字段说明：**

| 字段 | 说明 | 示例 |
|------|------|------|
| `target_kind` | 目标类型，固定 `k8s_cluster` | `k8s_cluster` |
| `target_identity` | 唯一标识，前缀 `namespace:` 后接集群ID | `namespace:my-cluster` |
| `display_name` | 前端显示名称 | `MyCluster K8s` |
| `metadata.cluster_id` | 集群ID，与 credential_scope 对应 | `my-cluster` |
| `metadata.risk_tier` | 风险等级 | `high` |
| `metadata.preferred_executor_profiles` | 执行器配置 | `toolbox-k8s-readonly` |
| `capabilities` | 允许的操作 | `read_logs`, `restart_workload` |
| `credential_scope.kubeconfig_name` | kubeconfig 文件名，与 Secret key 一致 | `my-cluster` |

### 步骤 5：重启服务

```bash
# 重启 toolbox-gateway（加载新 kubeconfig）
kubectl rollout restart -n islap deployment/toolbox-gateway

# 重启 ai-service（加载新目标注册表）
kubectl rollout restart -n islap deployment/ai-service

# 等待就绪
kubectl rollout status -n islap deployment/toolbox-gateway --timeout=120s
kubectl rollout status -n islap deployment/ai-service --timeout=120s
```

### 步骤 6：验证

```bash
# 验证 kubeconfig 已挂载
kubectl exec -n islap deploy/toolbox-gateway -- cat /etc/kubeconfigs/<集群ID>

# 验证远程集群可访问
kubectl exec -n islap deploy/ai-service -- python3 -c "
import requests, json

pre = requests.post('http://exec-service:8095/api/v1/exec/precheck', json={
    'session_id': 'verify',
    'message_id': 'verify',
    'action_id': 'verify',
    'command': 'kubectl get nodes',
    'purpose': 'verify new cluster',
    'target_kind': 'k8s_cluster',
    'target_identity': 'namespace:<集群ID>',
}, timeout=10).json()
print('Precheck:', pre.get('status'))

exec_resp = requests.post('http://exec-service:8095/api/v1/exec/execute', json={
    'session_id': 'verify',
    'message_id': 'verify', 
    'action_id': 'verify',
    'command': 'kubectl get nodes',
    'purpose': 'verify new cluster',
    'confirmed': True,
    'target_kind': 'k8s_cluster',
    'target_identity': 'namespace:<集群ID>',
    'timeout_seconds': 15,
}, timeout=20).json()
r = exec_resp.get('run', exec_resp)
print(f'Exit: {r.get(\"exit_code\")}')
print(f'Nodes: {r.get(\"stdout\",\"\").strip()[:500]}')
"
```

---

## 二、更新已有集群

### 1. 准备 CA 证书

登录到远程 K8s 集群的控制节点（或任何已配置了 kubectl 的机器），执行：

```bash
# 获取 CA 证书并 base64 编码
cat /etc/kubernetes/pki/ca.crt | base64 -w0
```

会输出一行 base64 字符串，保存下来备用。

也可以从已有的 kubeconfig 中提取：

```bash
kubectl config view --raw --minify --flatten -o jsonpath='{.clusters[0].cluster.certificate-authority-data}'
```

### 2. 更新 Kubernetes Secret

```bash
# 读取当前 kubeconfig，替换证书和地址，然后更新 Secret
kubectl get secret -n islap kubeconfig-<集群ID> -o jsonpath="{.data.<集群ID>}" | \
  base64 -d | \
  sed 's|certificate-authority-data: .*|certificate-authority-data: <base64证书>|' | \
  sed 's|server: https://.*:6443|server: https://<新IP>:6443|' | \
  base64 -w0 > /tmp/kubeconfig-new.b64

# 更新 Secret
kubectl patch secret -n islap kubeconfig-<集群ID> \
  -p "{\"data\":{\"<集群ID>\":\"$(cat /tmp/kubeconfig-new.b64)\"}}"
```

### 3. 重启 toolbox-gateway

**重要：** Secret 使用了 `subPath` 挂载，Kubernetes **不会自动同步**更新后的内容到运行中的 Pod。
更新 Secret 后必须重启 toolbox-gateway：

```bash
kubectl rollout restart -n islap deployment/toolbox-gateway
kubectl rollout status -n islap deployment/toolbox-gateway --timeout=120s
```

### 4. 验证

```bash
# 检查 kubeconfig
kubectl exec -n islap deploy/toolbox-gateway -- cat /etc/kubeconfigs/<集群ID>

# 测试远程集群访问
kubectl exec -n islap deploy/ai-service -- python3 -c "
import requests, json
pre = requests.post('http://exec-service:8095/api/v1/exec/precheck', json={
    'session_id': 'verify',
    'message_id': 'verify',
    'action_id': 'verify',
    'command': 'kubectl get nodes',
    'purpose': 'verify',
    'target_kind': 'k8s_cluster',
    'target_identity': 'namespace:<集群ID>',
}, timeout=10).json()
print('Precheck:', pre.get('status'))
exec_resp = requests.post('http://exec-service:8095/api/v1/exec/execute', json={
    'session_id': 'verify', 'message_id': 'verify', 'action_id': 'verify',
    'command': 'kubectl get nodes', 'purpose': 'verify', 'confirmed': True,
    'target_kind': 'k8s_cluster', 'target_identity': 'namespace:<集群ID>',
    'timeout_seconds': 15,
}, timeout=20).json()
r = exec_resp.get('run', exec_resp)
print(f'Exit: {r.get(\"exit_code\")} | Output: {r.get(\"stdout\",\"\").strip()[:300]}')
"
```

---

## 三、当前配置参考

### 现有集群信息

| 项目 | 值 |
|------|------|
| Secret 名称 | `kubeconfig-remote-cluster-01` |
| Secret Key | `openstack-cluster-01` |
| 挂载路径 | `/etc/kubeconfigs/openstack-cluster-01` |
| 目标 identity | `namespace:openstack-cluster-01` |
| 集群地址 | `https://192.168.3.204:6443` |

### 现有目标注册（ConfigMap 中 AI_RUNTIME_V4_REMOTE_TARGETS_JSON）

```json
[
  {
    "target_kind": "k8s_cluster",
    "target_identity": "namespace:openstack-cluster-01",
    "display_name": "OpenStack K8s Cluster",
    "description": "OpenStack-provisioned Kubernetes cluster",
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
  }
]
```

---

## 四、常见问题

### Q: 更新 Secret 后执行命令还是旧的证书/地址？

A: 因为使用了 `subPath` 挂载，更新 Secret 后必须重启 toolbox-gateway：
```bash
kubectl rollout restart -n islap deployment/toolbox-gateway
```

### Q: 如何查看当前 kubeconfig 内容？

```bash
kubectl get secret -n islap kubeconfig-<集群ID> -o jsonpath="{.data.<集群ID>}" | base64 -d
```

### Q: 执行命令返回 `unable to load root certificates`

A: CA 证书不正确或格式错误。重新获取证书 base64 并更新 Secret。

### Q: 执行命令返回 `Connection timed out`

A: 网络不通，检查 API Server IP 是否正确，以及防火墙/网络路由是否可达。

### Q: 新增集群后需重启哪些服务？

A: 需要重启 `toolbox-gateway`（加载新 kubeconfig）和 `ai-service`（加载新目标注册表）。

### Q: `toolbox-gateway` 挂载成功但执行时报 `no such file or directory`

A: 检查 `subPath` 值与 Secret key 名称是否一致。`mountPath` 的文件名应与 `subPath` 相同。

### Q: 是否可以通过 ConfigMap 而不是 Secret 存储 kubeconfig？

A: 理论上可以，但 kubeconfig 包含 ServiceAccount Token，属于敏感信息，必须使用 Secret 存储。
