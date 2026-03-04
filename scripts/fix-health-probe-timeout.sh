#!/bin/bash
# 修复 Semantic Engine 健康检查超时问题

echo "=== Semantic Engine 健康检查超时修复 ==="
echo "开始时间: $(date)"
echo ""

# 备份当前 Deployment
kubectl get deployment -n logoscope semantic-engine -o yaml > /tmp/semantic-engine-backup.yaml

echo "1. 增加健康检查超时配置..."
kubectl patch deployment -n logoscope semantic-engine --type=json -p='[
  {
    "op": "replace",
    "path": "/spec/template/spec/containers/0",
    "value": {
      "name": "semantic-engine",
      "livenessProbe": {
        "httpGet": {
          "path": "/health",
          "port": 8080,
          "initialDelaySeconds": 30,
          "timeoutSeconds": 30
        }
      },
      "readinessProbe": {
        "httpGet": {
          "path": "/health",
          "port": 8080,
          "initialDelaySeconds": 20,
          "timeoutSeconds": 10
        }
      }
    }
  }
}]'

echo ""
echo "2. 等待 Pod 重启..."
sleep 5

echo "3. 检查新 Pod 启动状态..."
kubectl get pods -n logoscope -l app=semantic-engine

echo ""
echo "✅ 健康检查超时配置已更新"
echo ""
echo "预期效果:"
echo "  - Liveness probe 超时: 1-5秒 → 30秒"
echo "  - Readiness probe 超时: 1-5秒 → 10秒"
echo "  - Initial delay: 给应用更多启动时间"
echo ""
echo "监控命令:"
echo "  kubectl get pods -n logoscope -l app=semantic-engine"
echo "  kubectl logs -n logoscope -l app=semantic-engine --tail=50"

echo ""
echo "如果仍然重启，请检查:"
echo "  1. /health 端点响应时间"
echo "  2. 数据库连接是否慢"
echo "  3. 是否有阻塞操作"

echo ""
echo "如需恢复原配置:"
echo "  kubectl apply -f /tmp/semantic-engine-backup.yaml"

exit 0
