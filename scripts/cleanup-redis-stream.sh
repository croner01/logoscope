#!/bin/bash
################################################################################
# Redis Stream 自动清理脚本
#
# 功能：
#   1. 清理 logs.raw Stream，保留最近 10,000 条消息
#   2. 记录清理日志
#   3. 统计 Redis 内存使用
#
# 使用方法：
#   ./cleanup-redis-stream.sh
#
# Crontab 配置（每小时执行）：
#   0 * * * * /root/logoscope/scripts/cleanup-redis-stream.sh
#
# 作者：AI Assistant (Claude)
# 创建时间：2026-02-13
################################################################################

set -euo pipefail

# 配置
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="/var/log/redis-cleanup.log"
REDIS_POD_NAME="redis"
REDIS_NAMESPACE="islap"
STREAM_NAME="logs.raw"
MAX_LENGTH=10000

# 日志函数
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

# 获取 Redis Pod 名称
get_redis_pod() {
    kubectl get pods -n "$REDIS_NAMESPACE" -l app=redis -o jsonpath='{.items[0].metadata.name}' 2>/dev/null
}

# 执行 Redis 命令
redis_exec() {
    local pod_name="$1"
    shift
    kubectl exec -n "$REDIS_NAMESPACE" "$pod_name" -- redis-cli "$@"
}

# 清理 Redis Stream
cleanup_stream() {
    local pod_name="$1"
    local stream_name="$2"
    local max_len="$3"

    log "开始清理 Stream: $stream_name"

    # 获取清理前的信息
    local before_len
    before_len=$(redis_exec "$pod_name" XLEN "$stream_name" | tr -d '\r')

    # 执行清理（使用波浪号 ~ 近似值，性能更好）
    local result
    result=$(redis_exec "$pod_name" XTRIM "$stream_name" MAXLEN ~ "$max_len" 2>&1)

    if [ $? -eq 0 ]; then
        # 获取清理后的信息
        local after_len
        after_len=$(redis_exec "$pod_name" XLEN "$stream_name" | tr -d '\r')

        local deleted=$((before_len - after_len))
        log "✅ 清理完成: $stream_name | 删除: $deleted 条 | 保留: $after_len 条 | 限制: $max_len"

        # 记录到 syslog
        logger -t "redis-cleanup" "Cleaned stream $stream_name: deleted=$deleted, remaining=$after_len, max=$max_len"

        return 0
    else
        log "❌ 清理失败: $stream_name | 错误: $result"
        return 1
    fi
}

# 获取 Redis 内存使用
get_memory_info() {
    local pod_name="$1"

    log "查询 Redis 内存使用情况"

    # 获取内存信息
    local used_memory
    local max_memory

    used_memory=$(redis_exec "$pod_name" INFO memory | grep used_memory_human | awk '{print $2}')
    max_memory=$(redis_exec "$pod_name" CONFIG GET maxmemory 2>/dev/null | tail -1 | tr -d '\r')

    log "Redis 内存: 使用=$used_memory, 最大限制=$max_memory"
}

# 检查 Redis Stream 状态
check_stream_status() {
    local pod_name="$1"
    local stream_name="$2"

    log "检查 Stream 状态: $stream_name"

    # 获取 Stream 长度
    local stream_len
    stream_len=$(redis_exec "$pod_name" XLEN "$stream_name" | tr -d '\r')

    # 获取 Consumer Group 信息
    local group_info
    group_info=$(redis_exec "$pod_name" XINFO GROUPS "$stream_name" 2>&1 || echo "")

    # 解析 lag（未确认消息数）
    local lag="N/A"
    if echo "$group_info" | grep -q "lag"; then
        lag=$(echo "$group_info" | grep "lag" | awk '{print $2}')
    fi

    log "Stream 状态: 长度=$stream_len, lag=$lag"
}

# 主函数
main() {
    log "========================================"
    log "Redis Stream 清理开始"
    log "========================================"

    # 获取 Redis Pod 名称
    REDIS_POD=$(get_redis_pod)
    if [ -z "$REDIS_POD" ]; then
        log "❌ 错误：无法找到 Redis Pod"
        exit 1
    fi

    log "Redis Pod: $REDIS_POD"
    log "Namespace: $REDIS_NAMESPACE"

    # 检查 Stream 状态
    check_stream_status "$REDIS_POD" "$STREAM_NAME"

    # 执行清理
    if cleanup_stream "$REDIS_POD" "$STREAM_NAME" "$MAX_LENGTH"; then
        # 获取内存信息
        get_memory_info "$REDIS_POD"

        log "========================================"
        log "✅ 清理完成"
        log "========================================"
        exit 0
    else
        log "========================================"
        log "❌ 清理失败"
        log "========================================"
        exit 1
    fi
}

# 执行主函数
main "$@"
