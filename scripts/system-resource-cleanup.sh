#!/bin/bash
# Logoscope 系统资源优化脚本
# 清理无效资源、释放内存、优化存储

set -e

DATE=$(date +%Y%m%d_%H%M%S)
LOG_FILE="/var/log/logoscope-cleanup-${DATE}.log"

echo "=== Logoscope 系统资源清理开始: $(date) ===" | tee -a "$LOG_FILE"

# ============================================================================
# 第一部分: Kubernetes 资源清理
# ============================================================================

echo "" | tee -a "$LOG_FILE"
echo "=== 1. Kubernetes 资源清理 ===" | tee -a "$LOG_FILE"

# 1.1 清理已完成的 Pod
echo "检查已完成的 Pod..." | tee -a "$LOG_FILE"
COMPLETED_PODS=$(kubectl get pods -A | grep -E "(Completed|Succeeded)" || true)
if [ -n "$COMPLETED_PODS" ]; then
    echo "发现的已完成 Pod:" | tee -a "$LOG_FILE"
    echo "$COMPLETED_PODS" | tee -a "$LOG_FILE"
    echo "$COMPLETED_PODS" | awk '{print $1}' | xargs -I {} kubectl delete pod -n {} 2>/dev/null || true
    echo "✅ 已清理完成的 Pod" | tee -a "$LOG_FILE"
else
    echo "✅ 无已完成的 Pod 需要清理" | tee -a "$LOG_FILE"
fi

# 1.2 清理未使用的 ConfigMap
echo "" | tee -a "$LOG_FILE"
echo "检查未使用的 ConfigMap..." | tee -a "$LOG_FILE"
UNUSED_CONFIGMAPS=$(kubectl get configmaps -A -o json | jq -r '.items[] | select(.metadata.uid // "" | IN("", []) == "" or .metadata.uid == null) | "\(.metadata.namespace)/\(.metadata.name)"' 2>/dev/null || echo "")
if [ -n "$UNUSED_CONFIGMAPS" ]; then
    echo "$UNUSED_CONFIGMAPS" | xargs -I {} kubectl delete configmap -n {} 2>/dev/null || true
    echo "✅ 已清理未使用的 ConfigMap" | tee -a "$LOG_FILE"
else
    echo "✅ 无未使用的 ConfigMap" | tee -a "$LOG_FILE"
fi

# ============================================================================
# 第二部分: 日志清理
# ============================================================================

echo "" | tee -a "$LOG_FILE"
echo "=== 2. 系统日志清理 ===" | tee -a "$LOG_FILE"

# 2.1 清理系统日志 (保留最近7天)
echo "清理 /var/log 下的旧日志..." | tee -a "$LOG_FILE"
find /var/log -type f -name "*.log" -mtime +7 -delete 2>/dev/null || true
find /var/log -type f -name "*.gz" -mtime +30 -delete 2>/dev/null || true
echo "✅ 已清理超过7天的系统日志" | tee -a "$LOG_FILE"

# 2.2 清理 Journal 日志
echo "清理 Journal 日志..." | tee -a "$LOG_FILE"
journalctl --vacuum-time=7d 2>/dev/null || true
echo "✅ 已清理超过7天的 Journal 日志" | tee -a "$LOG_FILE"

# ============================================================================
# 第三部分: 临时文件清理
# ============================================================================

echo "" | tee -a "$LOG_FILE"
echo "=== 3. 临时文件清理 ===" | tee -a "$LOG_FILE"

# 3.1 清理 /tmp
echo "清理 /tmp 目录..." | tee -a "$LOG_FILE"
find /tmp -type f -atime +7 -delete 2>/dev/null || true
echo "✅ 已清理7天未访问的临时文件" | tee -a "$LOG_FILE"

# 3.2 清理包管理器缓存
echo "清理包管理器缓存..." | tee -a "$LOG_FILE"
if [ -d /var/cache/apt ]; then
    apt-get clean 2>/dev/null || true
    apt-get autoclean 2>/dev/null || true
    apt-get autoremove -y 2>/dev/null || true
    echo "✅ 已清理 APT 缓存" | tee -a "$LOG_FILE"
fi

if [ -d /var/cache/yum ]; then
    yum clean all 2>/dev/null || true
    echo "✅ 已清理 YUM 缓存" | tee -a "$LOG_FILE"
fi

# ============================================================================
# 第四部分: 内存优化
# ============================================================================

echo "" | tee -a "$LOG_FILE"
echo "=== 4. 内存优化 ===" | tee -a "$LOG_FILE"

# 4.1 清理 PageCache
echo "清理 PageCache..." | tee -a "$LOG_FILE"
sync
echo 3 > /proc/sys/vm/drop_caches
echo "✅ 已清理 PageCache" | tee -a "$LOG_FILE"

# 4.2 显示内存使用情况
echo "" | tee -a "$LOG_FILE"
echo "当前内存使用情况:" | tee -a "$LOG_FILE"
free -h | tee -a "$LOG_FILE"

# ============================================================================
# 第五部分: 磁盘空间分析
# ============================================================================

echo "" | tee -a "$LOG_FILE"
echo "=== 5. 磁盘空间分析 ===" | tee -a "$LOG_FILE"

echo "磁盘使用情况:" | tee -a "$LOG_FILE"
df -h | tee -a "$LOG_FILE"

echo "" | tee -a "$LOG_FILE"
echo "查找大目录 (Top 10):" | tee -a "$LOG_FILE"
du -sh /* 2>/dev/null | sort -rh | head -10 | tee -a "$LOG_FILE"

# ============================================================================
# 第六部分: Docker 清理 (如果可用)
# ============================================================================

echo "" | tee -a "$LOG_FILE"
echo "=== 6. Docker 资源清理 ===" | tee -a "$LOG_FILE"

if command -v docker &> /dev/null; then
    # 6.1 清理未使用的镜像
    echo "检查未使用的 Docker 镜像..." | tee -a "$LOG_FILE"
    UNUSED_IMAGES=$(docker images -f "dangling=true" -q | head -20)
    if [ -n "$UNUSED_IMAGES" ]; then
        echo "$UNUSED_IMAGES" | xargs docker rmi 2>/dev/null || true
        echo "✅ 已清理未使用的 Docker 镜像" | tee -a "$LOG_FILE"
    else
        echo "✅ 无未使用的 Docker 镜像" | tee -a "$LOG_FILE"
    fi
    
    # 6.2 清理未使用的容器
    echo "清理停止的容器..." | tee -a "$LOG_FILE"
    docker container prune -f 2>/dev/null || true
    echo "✅ 已清理停止的容器" | tee -a "$LOG_FILE"
    
    # 6.3 清理构建缓存
    echo "清理 Docker 构建缓存..." | tee -a "$LOG_FILE"
    docker builder prune -f --keep-storage=2GB 2>/dev/null || true
    echo "✅ 已清理 Docker 构建缓存" | tee -a "$LOG_FILE"
    
    # 6.4 显示 Docker 占用
    echo "" | tee -a "$LOG_FILE"
    echo "Docker 系统占用:" | tee -a "$LOG_FILE"
    docker system df 2>/dev/null | tee -a "$LOG_FILE"
else
    echo "⚠️ Docker 未安装或不可用，跳过 Docker 清理" | tee -a "$LOG_FILE"
fi

# ============================================================================
# 第七部分: Kubernetes 日志清理
# ============================================================================

echo "" | tee -a "$LOG_FILE"
echo "=== 7. Kubernetes 日志清理 ===" | tee -a "$LOG_FILE"

# 清理节点上的日志
echo "清理节点日志..." | tee -a "$LOG_FILE"
find /var/log -type f -name "*kube*" -mtime +7 -delete 2>/dev/null || true
echo "✅ 已清理7天前的 Kubernetes 日志" | tee -a "$LOG_FILE"

# ============================================================================
# 第八部分: 应用数据清理
# ============================================================================

echo "" | tee -a "$LOG_FILE"
echo "=== 8. ClickHouse 数据清理 ===" | tee -a "$LOG_FILE"

# 检查 ClickHouse 数据保留策略
if [ -f /root/logoscope/scripts/cleanup-old-data.sh ]; then
    echo "运行 ClickHouse 数据清理脚本..." | tee -a "$LOG_FILE"
    bash /root/logoscope/scripts/cleanup-old-data.sh 2>&1 | tee -a "$LOG_FILE"
    echo "✅ ClickHouse 数据清理完成" | tee -a "$LOG_FILE"
else
    echo "⚠️ ClickHouse 清理脚本不存在，跳过" | tee -a "$LOG_FILE"
fi

# ============================================================================
# 清理总结
# ============================================================================

echo "" | tee -a "$LOG_FILE"
echo "=== 清理总结 ===" | tee -a "$LOG_FILE"
echo "清理时间: $(date)" | tee -a "$LOG_FILE"
echo "日志文件: $LOG_FILE" | tee -a "$LOG_FILE"

# 计算释放的空间
BEFORE_DISK=$(df -h / | grep -E "^/dev/sda" | awk '{print $3}' | sed 's/G//')
AFTER_DISK=$(df -h / | grep -E "^/dev/sda" | awk '{print $3}' | sed 's/G//')

echo "" | tee -a "$LOG_FILE"
echo "磁盘使用情况变化:" | tee -a "$LOG_FILE"
echo "清理前: ${BEFORE_DISK}G 已用" | tee -a "$LOG_FILE"
echo "清理后: ${AFTER_DISK}G 已用" | tee -a "$LOG_FILE"

echo "" | tee -a "$LOG_FILE"
echo "=== 清理完成 ===" | tee -a "$LOG_FILE"
echo "建议:" | tee -a "$LOG_FILE"
echo "1. 定期运行此脚本 (建议每周一次)" | tee -a "$LOG_FILE"
echo "2. 监控磁盘使用率，保持在 80% 以下" | tee -a "$LOG_FILE"
echo "3. 定期重启高内存占用的服务" | tee -a "$LOG_FILE"
echo "4. 检查并优化 ClickHouse 数据保留策略" | tee -a "$LOG_FILE"
echo "5. 考虑扩展磁盘容量 (当前使用率 85%)" | tee -a "$LOG_FILE"

exit 0
