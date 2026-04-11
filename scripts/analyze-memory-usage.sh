#!/bin/bash
# 内存使用分析脚本
# 识别高内存消耗的进程和服务

echo "=== 内存使用分析 ==="
echo "分析时间: $(date)"
echo ""

# 按 PID 排序的前 10 个进程
echo "=== Top 10 内存消耗进程 ==="
ps aux --sort=-%mem | head -11

echo ""

# Kubernetes Pod 内存使用
echo "=== Kubernetes Pod 内存使用 ==="
kubectl get pods -A -o json | jq -r '.items[] | {
    namespace: .metadata.namespace,
    name: .metadata.name,
    memory: .spec.containers[0].resources.requests.memory // "N/A"
  }' | while IFS= read -r line; do
    echo "$line"
done | jq -r '@tsv' | column -t

echo ""

# 系统内存摘要
echo "=== 系统内存摘要 ==="
free -h

echo ""

# 建议优化措施
echo "=== 优化建议 ==="
echo "1. 检查上述高内存进程是否必要"
echo "2. 对于高内存的 Pod，考虑设置资源限制"
echo "3. 重启高内存占用的服务"
echo "4. 调整 ClickHouse 内存限制"
echo "5. 启用 Kubernetes 资源配额"

exit 0
