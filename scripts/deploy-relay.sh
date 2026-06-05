#!/usr/bin/env bash
# ==============================================================================
# deploy-relay.sh — 部署 Fluent Bit Relay（每个远端集群一个实例）
#
# 用法:
#   ./scripts/deploy-relay.sh prod-beijing --parsers "multiline_openstack_traceback,multiline_openstack_ts_boundary"
#   ./scripts/deploy-relay.sh prod-shanghai --parsers "multiline_java_stack,multiline_trace_stack" --apply
#
# 必填参数:
#   <cluster-name>          集群唯一标识（DNS-1123 规范: 小写字母/数字/连字符）
#
# 选填参数:
#   -p, --parsers TEXT       multiline parser 列表（逗号分隔，必填）
#   -n, --namespace TEXT     命名空间（默认: islap）
#   -i, --image TEXT         Fluent Bit 镜像（默认: localhost:5000/logoscope/fluent-bit:3.1.3）
#   -o, --otel-host TEXT     OTel Collector 地址（默认: otel-collector.islap.svc.cluster.local）
#       --cpu-req TEXT        CPU 请求（默认: 100m）
#       --mem-req TEXT        内存请求（默认: 256Mi）
#       --cpu-limit TEXT       CPU 上限（默认: 1000m）
#       --mem-limit TEXT       内存上限（默认: 1Gi）
#       --replicas NUM        副本数（默认: 1）
#       --output-dir PATH     输出目录（默认: deploy/relays）
#       --apply              生成后执行 kubectl apply
#       --dry-run            仅输出到 stdout，不写文件
#   -h, --help               显示帮助
#
# 示例:
#   ./scripts/deploy-relay.sh prod-beijing -p "multiline_openstack_traceback"
#   ./scripts/deploy-relay.sh prod-beijing -p "multiline_openstack_traceback" --apply
#   ./scripts/deploy-relay.sh staging-shanghai -p "multiline_java_stack,multiline_trace_stack" --dry-run
# ==============================================================================
set -euo pipefail

# ── 默认值 ──────────────────────────────────────────────────────────────────
NAMESPACE="islap"
FLUENT_BIT_IMAGE="fluent/fluent-bit:3.1.3"
OTEL_COLLECTOR_HOST="otel-collector.islap.svc.cluster.local"
CPU_REQ="100m"
MEM_REQ="256Mi"
CPU_LIMIT="1000m"
MEM_LIMIT="1Gi"
REPLICAS="1"
OUTPUT_DIR="deploy/relays"
APPLY=false
DRY_RUN=false
PARSERS=""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
TEMPLATE_FILE="${REPO_DIR}/deploy/templates/fluent-bit-relay-template.yaml"

# ── 颜色 ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# ── 帮助 ────────────────────────────────────────────────────────────────────
usage() {
    sed -n 's/^#//p' "${BASH_SOURCE[0]}" | sed 's/^ //' | sed -n '3,/^$/p'
    exit 0
}

# ── 参数解析 ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help) usage ;;
        -p|--parsers) PARSERS="$2"; shift 2 ;;
        -n|--namespace) NAMESPACE="$2"; shift 2 ;;
        -i|--image) FLUENT_BIT_IMAGE="$2"; shift 2 ;;
        -o|--otel-host) OTEL_COLLECTOR_HOST="$2"; shift 2 ;;
        --cpu-req) CPU_REQ="$2"; shift 2 ;;
        --mem-req) MEM_REQ="$2"; shift 2 ;;
        --cpu-limit) CPU_LIMIT="$2"; shift 2 ;;
        --mem-limit) MEM_LIMIT="$2"; shift 2 ;;
        --replicas) REPLICAS="$2"; shift 2 ;;
        --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
        --apply) APPLY=true; shift ;;
        --dry-run) DRY_RUN=true; shift ;;
        -*)
            echo -e "${RED}未知参数: $1${NC}"
            usage
            ;;
        *)
            if [[ -z "${CLUSTER_NAME:-}" ]]; then
                CLUSTER_NAME="$1"
            else
                echo -e "${RED}多余参数: $1${NC}"
                usage
            fi
            shift
            ;;
    esac
done

# ── 校验 ────────────────────────────────────────────────────────────────────
if [[ -z "${CLUSTER_NAME:-}" ]]; then
    echo -e "${RED}错误: 缺少 <cluster-name>${NC}"
    usage
fi

if [[ -z "${PARSERS}" ]]; then
    echo -e "${RED}错误: 必须指定 --parsers${NC}"
    usage
fi

# 校验 cluster-name：DNS-1123 规范
if ! [[ "${CLUSTER_NAME}" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]]; then
    echo -e "${RED}错误: cluster-name 不符合 DNS-1123 规范（小写字母/数字/连字符，最长 63 字符）${NC}"
    exit 1
fi

if [[ ! -f "${TEMPLATE_FILE}" ]]; then
    echo -e "${RED}错误: 模板文件不存在: ${TEMPLATE_FILE}${NC}"
    exit 1
fi

# ── 生成 YAML ───────────────────────────────────────────────────────────────
echo -e "${CYAN}正在生成 relay 配置:${NC}"
echo -e "  集群名称:    ${CLUSTER_NAME}"
echo -e "  parser:      ${PARSERS}"
echo -e "  命名空间:    ${NAMESPACE}"
echo -e "  镜像:        ${FLUENT_BIT_IMAGE}"
echo -e "  OTel 地址:   ${OTEL_COLLECTOR_HOST}"
echo -e "  资源:        ${CPU_REQ}/${MEM_REQ} (request)  ${CPU_LIMIT}/${MEM_LIMIT} (limit)"
echo -e "  副本数:      ${REPLICAS}"

# sed 转义：镜像地址中的 / 需要替换为 \/
FLUENT_BIT_IMAGE_ESCAPED=$(echo "${FLUENT_BIT_IMAGE}" | sed 's/\//\\\//g')
OTEL_COLLECTOR_HOST_ESCAPED=$(echo "${OTEL_COLLECTOR_HOST}" | sed 's/\//\\\//g')

_generate() {
    sed \
        -e "s/\${CLUSTER_NAME}/${CLUSTER_NAME}/g" \
        -e "s/\${MULTILINE_PARSERS}/${PARSERS}/g" \
        -e "s/\${FLUENT_BIT_IMAGE}/${FLUENT_BIT_IMAGE_ESCAPED}/g" \
        -e "s/\${OTEL_COLLECTOR_HOST}/${OTEL_COLLECTOR_HOST_ESCAPED}/g" \
        -e "s/\${RELAY_NAMESPACE}/${NAMESPACE}/g" \
        -e "s/\${RELAY_CPU_REQ}/${CPU_REQ}/g" \
        -e "s/\${RELAY_MEM_REQ}/${MEM_REQ}/g" \
        -e "s/\${RELAY_CPU_LIMIT}/${CPU_LIMIT}/g" \
        -e "s/\${RELAY_MEM_LIMIT}/${MEM_LIMIT}/g" \
        -e "s/\${RELAY_REPLICAS}/${REPLICAS}/g" \
        "${TEMPLATE_FILE}"
}

if [[ "${DRY_RUN}" == true ]]; then
    echo -e "\n${YELLOW}--- dry-run: 输出到 stdout ---${NC}"
    _generate
    echo -e "\n${YELLOW}--- dry-run 结束 ---${NC}"
    exit 0
fi

# 写入文件
mkdir -p "${REPO_DIR}/${OUTPUT_DIR}"
OUTPUT_FILE="${REPO_DIR}/${OUTPUT_DIR}/relay-${CLUSTER_NAME}.yaml"
_generate > "${OUTPUT_FILE}"
echo -e "${GREEN}✓ 已生成: ${OUTPUT_FILE}${NC}"

# ── 可选: kubectl apply ────────────────────────────────────────────────────
if [[ "${APPLY}" == true ]]; then
    echo -e "${CYAN}正在部署到 Kubernetes...${NC}"
    kubectl apply -f "${OUTPUT_FILE}"

    echo -e "${CYAN}等待 relay 启动...${NC}"
    kubectl -n "${NAMESPACE}" wait --for=condition=available \
        "deployment/fluent-bit-relay-${CLUSTER_NAME}" --timeout=60s 2>/dev/null || \
    kubectl -n "${NAMESPACE}" rollout status \
        "deployment/fluent-bit-relay-${CLUSTER_NAME}" --timeout=60s

    echo -e "${GREEN}✓ relay 已部署: ${CLUSTER_NAME}${NC}"
    echo -e "  验证: kubectl -n ${NAMESPACE} get pods -l relay=${CLUSTER_NAME}"
fi

echo -e "\n${GREEN}完成。${NC}"
