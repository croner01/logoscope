# 远程 K8s 集群 kubeconfig 配置说明

## 概述

Logoscope 通过 **toolbox-gateway** 访问远程 Kubernetes 集群（如 OpenStack 部署的 K8s 集群）。
远程集群的访问凭据以 Kubernetes Secret 方式存储，挂载到 toolbox-gateway Pod 中。

## 架构

```
exec-service → toolbox-gateway → kubectl --kubeconfig=<文件> → 远程集群 API Server
```

## 配置信息

| 项目 | 值 |
|------|------|
| Secret 名称 | `kubeconfig-remote-cluster-01` |
| Secret Key | `openstack-cluster-01` |
| 挂载路径 | `/etc/kubeconfigs/openstack-cluster-01` |
| 目标集群 | 以 `namespace:openstack-cluster-01` 引用 |
| 当前集群地址 | `https://192.168.3.204:6443` |

## 更新步骤

### 1. 准备 CA 证书

登录到远程 K8s 集群的控制节点（或任何已配置了 kubectl 的机器），执行：

```bash
# 获取 CA 证书并 base64 编码
cat /etc/kubernetes/pki/ca.crt | base64 -w0
```

会输出一行 base64 字符串，保存下来备用。

也可以从已有的 kubeconfig 中提取：

```bash
# 如果本地已有该集群的 kubeconfig
kubectl config view --raw --minify --flatten -o jsonpath='{.clusters[0].cluster.certificate-authority-data}'
```

### 2. 准备新的 kubeconfig 内容

用以下模板，替换 `<>` 中的占位符：

```yaml
apiVersion: v1
kind: Config
current-context: openstack
clusters:
- cluster:
    certificate-authority-data: <base64编码的CA证书>
    server: https://<API_SERVER_IP>:6443
  name: openstack
contexts:
- context:
    cluster: openstack
    user: logoscope
  name: openstack
users:
- name: logoscope
  user:
    token: <service-account-token>
```

> **注意：** 令牌（token）保持不变即可，通常不需要更新。只需要更新 `certificate-authority-data` 和 `server`。

### 3. 更新 Kubernetes Secret

```bash
# 读取当前 kubeconfig，替换证书和地址，然后更新 Secret
kubectl get secret -n islap kubeconfig-remote-cluster-01 -o jsonpath='{.data.openstack-cluster-01}' | \
  base64 -d | \
  sed 's|certificate-authority-data: .*|certificate-authority-data: <base64证书>|' | \
  sed 's|server: https://.*:6443|server: https://<新IP>:6443|' | \
  base64 -w0 > /tmp/kubeconfig-new.b64

# 更新 Secret
kubectl patch secret -n islap kubeconfig-remote-cluster-01 \
  -p "{\"data\":{\"openstack-cluster-01\":\"$(cat /tmp/kubeconfig-new.b64)\"}}"
```

### 4. 重启 toolbox-gateway

**重要：** Secret 使用了 `subPath` 挂载方式，Kubernetes **不会自动同步**更新后的内容到运行中的 Pod。
更新 Secret 后必须重启 toolbox-gateway Deployment：

```bash
kubectl rollout restart -n islap deployment/toolbox-gateway

# 等待重启完成
kubectl rollout status -n islap deployment/toolbox-gateway --timeout=120s
```

### 5. 验证

```bash
# 检查 kubeconfig 内容是否正确
kubectl exec -n islap deploy/toolbox-gateway -- cat /etc/kubeconfigs/openstack-cluster-01

# 测试远程集群访问
kubectl exec -n islap deploy/ai-service -- python3 -c "
import requests, json

# Step 1: precheck
pre = requests.post('http://exec-service:8095/api/v1/exec/precheck', json={
    'session_id': 'verify',
    'message_id': 'verify',
    'action_id': 'verify',
    'command': 'kubectl get nodes',
    'purpose': 'verify remote cluster access',
    'target_kind': 'k8s_cluster',
    'target_identity': 'namespace:openstack-cluster-01',
}, timeout=10).json()
print('Precheck:', pre.get('status'))

# Step 2: execute
exec_resp = requests.post('http://exec-service:8095/api/v1/exec/execute', json={
    'session_id': 'verify',
    'message_id': 'verify',
    'action_id': 'verify',
    'command': 'kubectl get nodes',
    'purpose': 'verify remote cluster access',
    'confirmed': True,
    'target_kind': 'k8s_cluster',
    'target_identity': 'namespace:openstack-cluster-01',
    'timeout_seconds': 15,
}, timeout=20).json()
r = exec_resp.get('run', exec_resp)
print(f'Exit: {r.get(\"exit_code\")}')
print(f'Output: {r.get(\"stdout\",\"\").strip()[:500]}')
"
```

## 常见问题

### Q: 更新 Secret 后执行命令还是旧的证书/地址？

A: 因为使用了 `subPath` 挂载，更新 Secret 后必须重启 toolbox-gateway：
```bash
kubectl rollout restart -n islap deployment/toolbox-gateway
```

### Q: 如何查看当前 kubeconfig 内容？

```bash
kubectl get secret -n islap kubeconfig-remote-cluster-01 -o jsonpath='{.data.openstack-cluster-01}' | base64 -d
```

### Q: 执行命令返回 `unable to load root certificates`

A: CA 证书不正确或格式错误。重新获取证书 base64 并更新。

### Q: 执行命令返回 `Connection timed out`

A: 网络不通，检查 IP 地址是否正确，以及防火墙/网络路由是否可达。
