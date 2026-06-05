#!/usr/bin/env bash
# ==============================================================================
# verify-relay-connection.sh — 验证远端 Fluent Bit → Relay → ClickHouse 链路
#
# 用法:
#   ./scripts/verify-relay-connection.sh prod-beijing
#   ./scripts/verify-relay-connection.sh prod-beijing --verbose
#
# 参数:
#   <cluster-name>          集群唯一标识
#   -n, --namespace TEXT     命名空间（默认: islap）
#   -c, --clickhouse TEXT    ClickHouse pod 名称（默认: clickhouse-0）
#   -t, --timeout SEC        总超时秒数（默认: 60）
#       --verbose            详细输出
#   -h, --help               显示帮助
# ==============================================================================
set -euo pipefail

# ── 默认值 ──────────────────────────────────────────────────────────────────
NAMESPACE="islap"
CLICKHOUSE_POD="clickhouse-0"
TIMEOUT=60
VERBOSE=false
CLUSTER_NAME=""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── 颜色 ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# ── 状态跟踪 ────────────────────────────────────────────────────────────────
PASS_COUNT=0
FAIL_COUNT=0

pass() {
    echo -e "  ${GREEN}✓ PASS${NC} $1"
    ((PASS_COUNT++))
}

fail() {
    echo -e "  ${RED}✗ FAIL${NC} $1"
    ((FAIL_COUNT++))
}

info() {
    if [[ "${VERBOSE}" == true ]]; then
        echo -e "  ${CYAN}ℹ${NC} $1"
    fi
}

warn() {
    echo -e "  ${YELLOW}⚠${NC} $1"
}

# ── 帮助 ────────────────────────────────────────────────────────────────────
usage() {
    sed -n 's/^#//p' "${BASH_SOURCE[0]}" | sed 's/^ //' | sed -n '3,/^$/p'
    exit 0
}

# ── 参数解析 ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help) usage ;;
        -n|--namespace) NAMESPACE="$2"; shift 2 ;;
        -c|--clickhouse) CLICKHOUSE_POD="$2"; shift 2 ;;
        -t|--timeout) TIMEOUT="$2"; shift 2 ;;
        --verbose) VERBOSE=true; shift ;;
        -*)
            echo -e "${RED}未知参数: $1${NC}"
            usage
            ;;
        *)
            if [[ -z "${CLUSTER_NAME}" ]]; then
                CLUSTER_NAME="$1"
            else
                echo -e "${RED}多余参数: $1${NC}"
                usage
            fi
            shift
            ;;
    esac
done

if [[ -z "${CLUSTER_NAME}" ]]; then
    echo -e "${RED}错误: 缺少 <cluster-name>${NC}"
    usage
fi

RELAY_DEPLOYMENT="fluent-bit-relay-${CLUSTER_NAME}"
RELAY_SERVICE="fluent-bit-relay-${CLUSTER_NAME}"

# ══════════════════════════════════════════════════════════════════════════════
echo -e "${CYAN}══════════════════════════════════════════════════════════════${NC}"
echo -e "${CYAN}   Relay 连接验证: ${CLUSTER_NAME}${NC}"
echo -e "${CYAN}══════════════════════════════════════════════════════════════${NC}"
echo ""

# ── 检查 1: Pod 状态 ────────────────────────────────────────────────────────
echo -e "${YELLOW}[1/6] 检查 relay Pod 状态...${NC}"
POD_STATUS=$(kubectl -n "${NAMESPACE}" get pod -l "relay=${CLUSTER_NAME}" \
    -o jsonpath='{.items[0].status.phase}' 2>/dev/null || echo "")
if [[ "${POD_STATUS}" == "Running" ]]; then
    POD_NAME=$(kubectl -n "${NAMESPACE}" get pod -l "relay=${CLUSTER_NAME}" \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
    pass "Pod ${POD_NAME} 状态为 Running"
    info "Pod: ${POD_NAME}"
else
    fail "Pod 未运行（当前: ${POD_STATUS:-未找到}）"
    warn "检查: kubectl -n ${NAMESPACE} get pods -l relay=${CLUSTER_NAME}"
fi

# ── 检查 2: Health 端点 ─────────────────────────────────────────────────────
echo -e "${YELLOW}[2/6] 检查 relay health 端点...${NC}"
if kubectl -n "${NAMESPACE}" port-forward "svc/${RELAY_SERVICE}" 20210:2020 &>/dev/null &
then
    PF_PID=$!
    sleep 2
    HEALTH=$(curl -s --max-time 5 http://localhost:20210/api/v1/health 2>/dev/null || echo "")
    if echo "${HEALTH}" | grep -q '"ok"'; then
        pass "Health 端点正常"
    else
        fail "Health 端点异常（返回: ${HEALTH}）"
    fi
    kill "${PF_PID}" 2>/dev/null || true
else
    warn "无法 port-forward，跳过 health 检查"
fi

# ── 检查 3: 输入流量指标 ─────────────────────────────────────────────────────
echo -e "${YELLOW}[3/6] 检查 relay 输入流量...${NC}"
if kubectl -n "${NAMESPACE}" port-forward "svc/${RELAY_SERVICE}" 20211:2020 &>/dev/null &
then
    PF_PID=$!
    sleep 2
    METRICS=$(curl -s --max-time 5 http://localhost:20211/metrics 2>/dev/null || echo "")
    if echo "${METRICS}" | grep -q "forward_bytes"; then
        BYTES=$(echo "${METRICS}" | grep "forward_bytes" | awk '{sum+=$2} END {print sum}')
        if [[ "${BYTES:-0}" -gt 0 ]]; then
            pass "已接收流量: ${BYTES} bytes"
        else
            warn "forward_bytes 为 0，可能尚无数据流入"
            warn "确认远端 Fluent Bit output 已指向 ${RELAY_SERVICE}:24224"
        fi
    else
        warn "未找到 forward_bytes 指标，可能尚无连接"
        warn "检查: kubectl -n ${NAMESPACE} logs deployment/${RELAY_DEPLOYMENT} | tail -20"
    fi
    kill "${PF_PID}" 2>/dev/null || true
else
    warn "无法 port-forward，跳过 metrics 检查"
fi

# ── 检查 4: relay 日志无报错 ────────────────────────────────────────────────
echo -e "${YELLOW}[4/6] 检查 relay 日志错误...${NC}"
ERROR_LOG=$(kubectl -n "${NAMESPACE}" logs "deployment/${RELAY_DEPLOYMENT}" --tail=50 2>/dev/null \
    | grep -i "error\|warn\|fail" || true)
if [[ -z "${ERROR_LOG}" ]]; then
    pass "relay 日志无错误"
else
    warn "relay 日志中有 warning/error"
    if [[ "${VERBOSE}" == true ]]; then
        echo "${ERROR_LOG}" | head -10
    fi
fi

# ── 检查 5: ClickHouse source_cluster 数据 ──────────────────────────────────
echo -e "${YELLOW}[5/6] 检查 ClickHouse source_cluster 数据...${NC}"

if kubectl get pod "${CLICKHOUSE_POD}" -n "${NAMESPACE}" &>/dev/null; then
    CLICKHOUSE_QUERY="SELECT count() FROM logs.logs WHERE source_cluster = '${CLUSTER_NAME}' AND timestamp > now() - INTERVAL 5 MINUTE"
    COUNT=$(kubectl -n "${NAMESPACE}" exec "${CLICKHOUSE_POD}" -- clickhouse-client --query "${CLICKHOUSE_QUERY}" 2>/dev/null || echo "")
    if [[ -n "${COUNT}" && "${COUNT}" -gt 0 ]]; then
        pass "最近 5 分钟 ${COUNT} 条日志已写入 ClickHouse，source_cluster='${CLUSTER_NAME}'"
    else
        CLICKHOUSE_TOTAL="SELECT count() FROM logs.logs WHERE source_cluster = '${CLUSTER_NAME}'"
        TOTAL=$(kubectl -n "${NAMESPACE}" exec "${CLICKHOUSE_POD}" -- clickhouse-client --query "${CLICKHOUSE_TOTAL}" 2>/dev/null || echo "")
        if [[ -n "${TOTAL}" && "${TOTAL}" -gt 0 ]]; then
            pass "ClickHouse 中共 ${TOTAL} 条日志（source_cluster='${CLUSTER_NAME}'），但最近 5 分钟无新数据"
            warn "可能是数据频率较低，或 relay 到 ClickHouse 存在延迟"
        else
            if [[ "${COUNT:-0}" == "0" ]]; then
                fail "ClickHouse 未找到 source_cluster='${CLUSTER_NAME}' 的数据"
                warn "可能原因:"
                warn "  - relay 尚未收到远端数据"
                warn "  - source_cluster 列尚未在 Ingest/Semantic Engine 中实现"
                warn "  - 检查: kubectl -n ${NAMESPACE} logs deployment/${RELAY_DEPLOYMENT} --tail=30"
            fi
        fi
    fi
else
    warn "ClickHouse pod ${CLICKHOUSE_POD} 不可达，跳过"
    warn "可用: kubectl -n ${NAMESPACE} get pods | grep clickhouse"
fi

# ── 检查 6: 堆栈合并效果 ────────────────────────────────────────────────────
echo -e "${YELLOW}[6/6] 检查 multiline 堆栈合并效果...${NC}"
if kubectl get pod "${CLICKHOUSE_POD}" -n "${NAMESPACE}" &>/dev/null; then
    MERGE_QUERY="SELECT count() FROM logs.logs WHERE source_cluster = '${CLUSTER_NAME}' AND level = 'ERROR' AND message LIKE '%Traceback%'"
    TRACE_COUNT=$(kubectl -n "${NAMESPACE}" exec "${CLICKHOUSE_POD}" -- clickhouse-client --query "${MERGE_QUERY}" 2>/dev/null || echo "")
    if [[ -n "${TRACE_COUNT}" && "${TRACE_COUNT}" -gt 0 ]]; then
        pass "发现 ${TRACE_COUNT} 条含 Traceback 的 ERROR 日志"
        SAMPLE_QUERY="SELECT substring(message, 1, 500) FROM logs.logs WHERE source_cluster = '${CLUSTER_NAME}' AND level = 'ERROR' AND message LIKE '%Traceback%' LIMIT 1"
        SAMPLE=$(kubectl -n "${NAMESPACE}" exec "${CLICKHOUSE_POD}" -- clickhouse-client --query "${SAMPLE_QUERY}" 2>/dev/null || echo "")
        if echo "${SAMPLE}" | grep -q $'\n'; then
            pass "Traceback 包含多行（合并生效）"
        else
            warn "Traceback 可能只合并了一行，检查 multiline parser 规则是否匹配"
        fi
        if [[ "${VERBOSE}" == true ]]; then
            echo -e "${CYAN}  样本:${NC}"
            echo "${SAMPLE}" | head -10
        fi
    else
        warn "未发现含 Traceback 的 ERROR 日志（或集群没有此类错误）"
        warn "可手动检查: kubectl -n ${NAMESPACE} exec ${CLICKHOUSE_POD} -- clickhouse-client --query"
        warn "  \"SELECT level, count() FROM logs.logs WHERE source_cluster='${CLUSTER_NAME}' AND timestamp > now() - INTERVAL 1 HOUR GROUP BY level\""
    fi
else
    warn "ClickHouse 不可达，跳过堆栈合并检查"
fi

# ══════════════════════════════════════════════════════════════════════════════
echo ""
echo -e "${CYAN}──────────────────────────────────────────────────────────${NC}"
echo -e "${CYAN}   结果: ${PASS_COUNT}/${PASS_COUNT} 通过, ${FAIL_COUNT} 失败${NC}"
echo -e "${CYAN}──────────────────────────────────────────────────────────${NC}"

if [[ "${FAIL_COUNT}" -gt 0 ]]; then
    exit 1
fi
