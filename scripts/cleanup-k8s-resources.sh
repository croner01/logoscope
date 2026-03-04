#!/bin/bash
# Kubernetes 资源清理脚本
# 清理未使用的 Pod、ConfigMap、Secret 等

set -e

echo "=== Kubernetes 资源清理脚本 ==="
echo "开始时间: $(date)"
echo ""

# ============================================================================
# 1. 清理已完成的 Pod
# ============================================================================

echo "1. 清理已完成的 Pod..."
echo "查找状态为 Completed 或 Succeeded 的 Pod..."

# 查找所有命名空间中的已完成 Pod
kubectl get pods -A -o json | jq -r '.items[] | select(.status.phase // "" | IN("", "") == "Completed" or .status.phase // "" == "Succeeded") | "\(.metadata.namespace)/\(.metadata.name)"' | while IFS=/ read -r pod; do
    namespace=$(echo "$pod" | cut -d/ -f1)
    podname=$(echo "$pod" | cut -d/ -f2)
    
    echo "删除 Pod: $pod"
    kubectl delete pod "$podname" -n "$namespace" --ignore-not-found=true &
done

wait
echo "✅ 已完成的 Pod 清理完成"
echo ""

# ============================================================================
# 2. 清理孤立的服务
# ============================================================================

echo "2. 清理没有选择器的服务..."
kubectl get svc --all-namespaces -o json | jq -r '.items[] | select(.spec.selector == null) | "\(.metadata.namespace)/\(.metadata.name)"' | while IFS=/ read -r svc; do
    namespace=$(echo "$svc" | cut -d/ -f1)
    svcname=$(echo "$svc" | cut -d/ -f2)
    
    echo "删除无选择器的服务: $svc"
    kubectl delete svc "$svcname" -n "$namespace" --ignore-not-found=true &
done

wait
echo "✅ 孤立服务清理完成"
echo ""

# ============================================================================
# 3. 清理未使用的 PersistentVolumeClaim
# ============================================================================

echo "3. 清理未使用的 PVC..."
kubectl get pvc --all-namespaces -o json | jq -r '.items[] | select(.status.phase == "Released") | "\(.metadata.namespace)/\(.metadata.name)"' | while IFS=/ read -r pvc; do
    namespace=$(echo "$pvc" | cut -d/ -f1)
    pvcname=$(echo "$pvc" | cut -d/ -f2)
    
    echo "删除已释放的 PVC: $pvc"
    kubectl delete pvc "$pvcname" -n "$namespace" --ignore-not-found=true &
done

wait
echo "✅ PVC 清理完成"
echo ""

# ============================================================================
# 4. 清理旧的 ReplicaSet
# ============================================================================

echo "4. 清理旧的 ReplicaSet..."
kubectl get replicasets --all-namespaces -o json | jq -r '.items[] | select(.status.replicas == 0) | "\(.metadata.namespace)/\(.metadata.name)"' | while IFS=/ read -r rs; do
    namespace=$(echo "$rs" | cut -d/ -f1)
    rsname=$(echo "$rs" | cut -d/ -f2)
    
    echo "删除零副本的 ReplicaSet: $rs"
    kubectl delete replicaset "$rsname" -n "$namespace" --ignore-not-found=true &
done

wait
echo "✅ ReplicaSet 清理完成"
echo ""

# ============================================================================
# 5. 总结
# ============================================================================

echo ""
echo "=== 资源清理总结 ==="
echo "已清理:"
echo "  - 已完成的 Pod"
echo "  - 无选择器的服务"
echo "  - 未使用的 PVC"
echo "  - 旧的 ReplicaSet"
echo ""
echo "完成时间: $(date)"

exit 0
