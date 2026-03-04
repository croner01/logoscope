#!/usr/bin/env bash
set -euo pipefail

# ClickHouse 数据保留策略应用脚本
# 默认策略：
# - logs/traces/events: 30 天
# - metrics: 7 天
# - 可选冷热分层：先迁移到 cold volume，再按总保留期删除
#
# 运行模式：
# - local: 直连 clickhouse-client
# - kubectl: 通过 kubectl exec 到 clickhouse pod 执行

CLICKHOUSE_CLIENT="${CLICKHOUSE_CLIENT:-clickhouse-client}"
CLICKHOUSE_HOST="${CLICKHOUSE_HOST:-clickhouse}"
CLICKHOUSE_PORT="${CLICKHOUSE_PORT:-9000}"
CLICKHOUSE_USER="${CLICKHOUSE_USER:-default}"
CLICKHOUSE_PASSWORD="${CLICKHOUSE_PASSWORD:-}"
CLICKHOUSE_DATABASE="${CLICKHOUSE_DATABASE:-logs}"

LOGS_TTL_DAYS="${LOGS_TTL_DAYS:-30}"
TRACES_TTL_DAYS="${TRACES_TTL_DAYS:-30}"
EVENTS_TTL_DAYS="${EVENTS_TTL_DAYS:-30}"
METRICS_TTL_DAYS="${METRICS_TTL_DAYS:-7}"
LOGS_HOT_DAYS="${LOGS_HOT_DAYS:-7}"
TRACES_HOT_DAYS="${TRACES_HOT_DAYS:-7}"
EVENTS_HOT_DAYS="${EVENTS_HOT_DAYS:-7}"
METRICS_HOT_DAYS="${METRICS_HOT_DAYS:-1}"
ENABLE_COLD_MOVE="${ENABLE_COLD_MOVE:-false}"
COLD_VOLUME_NAME="${COLD_VOLUME_NAME:-cold}"

DRY_RUN="${DRY_RUN:-false}"

MODE="${MODE:-auto}" # auto|local|kubectl
NAMESPACE="${NAMESPACE:-islap}"
CLICKHOUSE_POD="${CLICKHOUSE_POD:-}"

usage() {
  cat <<'EOF'
Usage:
  scripts/apply-data-retention.sh [options]

Options:
  --mode <auto|local|kubectl>    执行模式（默认 auto）
  --namespace <ns>               kubectl 模式命名空间（默认 islap）
  --clickhouse-pod <name>        kubectl 模式指定 pod
  --database <name>              数据库名（默认 logs）
  --logs-ttl-days <n>            logs TTL 天数（默认 30）
  --traces-ttl-days <n>          traces TTL 天数（默认 30）
  --events-ttl-days <n>          events TTL 天数（默认 30）
  --metrics-ttl-days <n>         metrics TTL 天数（默认 7）
  --logs-hot-days <n>            logs 热数据窗口天数（默认 7，仅冷热分层时生效）
  --traces-hot-days <n>          traces 热数据窗口天数（默认 7，仅冷热分层时生效）
  --events-hot-days <n>          events 热数据窗口天数（默认 7，仅冷热分层时生效）
  --metrics-hot-days <n>         metrics 热数据窗口天数（默认 1，仅冷热分层时生效）
  --enable-cold-move             启用 TTL TO VOLUME 冷层迁移（要求 cold volume 存在）
  --cold-volume <name>           冷层 volume 名称（默认 cold）
  --dry-run                      仅打印 SQL，不执行
  -h, --help                     显示帮助
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      MODE="$2"
      shift 2
      ;;
    --namespace)
      NAMESPACE="$2"
      shift 2
      ;;
    --clickhouse-pod)
      CLICKHOUSE_POD="$2"
      shift 2
      ;;
    --database)
      CLICKHOUSE_DATABASE="$2"
      shift 2
      ;;
    --logs-ttl-days)
      LOGS_TTL_DAYS="$2"
      shift 2
      ;;
    --traces-ttl-days)
      TRACES_TTL_DAYS="$2"
      shift 2
      ;;
    --events-ttl-days)
      EVENTS_TTL_DAYS="$2"
      shift 2
      ;;
    --metrics-ttl-days)
      METRICS_TTL_DAYS="$2"
      shift 2
      ;;
    --logs-hot-days)
      LOGS_HOT_DAYS="$2"
      shift 2
      ;;
    --traces-hot-days)
      TRACES_HOT_DAYS="$2"
      shift 2
      ;;
    --events-hot-days)
      EVENTS_HOT_DAYS="$2"
      shift 2
      ;;
    --metrics-hot-days)
      METRICS_HOT_DAYS="$2"
      shift 2
      ;;
    --enable-cold-move)
      ENABLE_COLD_MOVE="true"
      shift
      ;;
    --cold-volume)
      COLD_VOLUME_NAME="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[ERROR] Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ "$MODE" == "auto" ]]; then
  if command -v "$CLICKHOUSE_CLIENT" >/dev/null 2>&1; then
    MODE="local"
  else
    MODE="kubectl"
  fi
fi

resolve_clickhouse_pod() {
  if [[ -n "$CLICKHOUSE_POD" ]]; then
    return 0
  fi
  CLICKHOUSE_POD="$(kubectl -n "$NAMESPACE" get pod -l app=clickhouse -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
}

run_query_local_capture() {
  local sql="$1"
  if [[ -n "$CLICKHOUSE_PASSWORD" ]]; then
    "$CLICKHOUSE_CLIENT" \
      --host "$CLICKHOUSE_HOST" \
      --port "$CLICKHOUSE_PORT" \
      --user "$CLICKHOUSE_USER" \
      --password "$CLICKHOUSE_PASSWORD" \
      --query "$sql"
  else
    "$CLICKHOUSE_CLIENT" \
      --host "$CLICKHOUSE_HOST" \
      --port "$CLICKHOUSE_PORT" \
      --user "$CLICKHOUSE_USER" \
      --query "$sql"
  fi
}

run_query_local() {
  local sql="$1"
  run_query_local_capture "$sql" >/dev/null
}

run_query_kubectl() {
  local sql="$1"
  resolve_clickhouse_pod
  if [[ -z "$CLICKHOUSE_POD" ]]; then
    echo "[ERROR] clickhouse pod not found in namespace=$NAMESPACE" >&2
    exit 1
  fi
  kubectl -n "$NAMESPACE" exec "$CLICKHOUSE_POD" -- clickhouse-client -q "$sql"
}

run_query_kubectl_capture() {
  local sql="$1"
  resolve_clickhouse_pod
  if [[ -z "$CLICKHOUSE_POD" ]]; then
    echo "[ERROR] clickhouse pod not found in namespace=$NAMESPACE" >&2
    exit 1
  fi
  kubectl -n "$NAMESPACE" exec "$CLICKHOUSE_POD" -- clickhouse-client -q "$sql"
}

run_query() {
  local sql="$1"
  if [[ "$DRY_RUN" == "true" ]]; then
    echo "[DRY_RUN] $sql"
    return 0
  fi

  if [[ "$MODE" == "local" ]]; then
    run_query_local "$sql"
  else
    run_query_kubectl "$sql"
  fi
}

query_scalar() {
  local sql="$1"
  if [[ "$MODE" == "local" ]]; then
    run_query_local_capture "$sql" 2>/dev/null | tr -d '\r\n'
  else
    run_query_kubectl_capture "$sql" 2>/dev/null | tr -d '\r\n'
  fi
}

normalize_positive_int() {
  local raw="${1:-}"
  local fallback="${2:-1}"
  if [[ "$raw" =~ ^[0-9]+$ ]] && [[ "$raw" -gt 0 ]]; then
    echo "$raw"
  else
    echo "$fallback"
  fi
}

build_ttl_expression() {
  local delete_days="$1"
  local hot_days="$2"
  local cold_move_enabled="$3"
  local normalized_delete_days
  local normalized_hot_days

  normalized_delete_days="$(normalize_positive_int "$delete_days" 1)"
  normalized_hot_days="$(normalize_positive_int "$hot_days" 1)"

  if [[ "$cold_move_enabled" != "true" ]]; then
    echo "toDateTime(timestamp) + INTERVAL ${normalized_delete_days} DAY DELETE"
    return 0
  fi

  if [[ "$normalized_hot_days" -ge "$normalized_delete_days" ]]; then
    normalized_hot_days=$((normalized_delete_days - 1))
  fi
  if [[ "$normalized_hot_days" -lt 1 ]]; then
    normalized_hot_days=1
  fi

  echo "toDateTime(timestamp) + INTERVAL ${normalized_hot_days} DAY TO VOLUME '${COLD_VOLUME_NAME}', toDateTime(timestamp) + INTERVAL ${normalized_delete_days} DAY DELETE"
}

echo "[INFO] Applying retention policy on database: $CLICKHOUSE_DATABASE"
echo "[INFO] mode=${MODE} logs=${LOGS_TTL_DAYS}d traces=${TRACES_TTL_DAYS}d events=${EVENTS_TTL_DAYS}d metrics=${METRICS_TTL_DAYS}d dry_run=${DRY_RUN} enable_cold_move=${ENABLE_COLD_MOVE} cold_volume=${COLD_VOLUME_NAME}"

cold_move_effective="false"
if [[ "$ENABLE_COLD_MOVE" == "true" ]]; then
  if [[ "$DRY_RUN" == "true" ]]; then
    cold_move_effective="true"
  else
    cold_volume_exists="$(query_scalar "SELECT count() FROM system.disks WHERE name = '${COLD_VOLUME_NAME}'" || true)"
    if [[ "$cold_volume_exists" =~ ^[0-9]+$ ]] && [[ "$cold_volume_exists" -ge 1 ]]; then
      cold_move_effective="true"
    else
      echo "[WARN] cold volume '${COLD_VOLUME_NAME}' not found, fallback to DELETE-only TTL"
    fi
  fi
fi

logs_ttl_expr="$(build_ttl_expression "$LOGS_TTL_DAYS" "$LOGS_HOT_DAYS" "$cold_move_effective")"
traces_ttl_expr="$(build_ttl_expression "$TRACES_TTL_DAYS" "$TRACES_HOT_DAYS" "$cold_move_effective")"
events_ttl_expr="$(build_ttl_expression "$EVENTS_TTL_DAYS" "$EVENTS_HOT_DAYS" "$cold_move_effective")"
metrics_ttl_expr="$(build_ttl_expression "$METRICS_TTL_DAYS" "$METRICS_HOT_DAYS" "$cold_move_effective")"

run_query "ALTER TABLE ${CLICKHOUSE_DATABASE}.logs MODIFY TTL ${logs_ttl_expr}"
run_query "ALTER TABLE ${CLICKHOUSE_DATABASE}.traces MODIFY TTL ${traces_ttl_expr}"
run_query "ALTER TABLE ${CLICKHOUSE_DATABASE}.events MODIFY TTL ${events_ttl_expr}"
run_query "ALTER TABLE ${CLICKHOUSE_DATABASE}.metrics MODIFY TTL ${metrics_ttl_expr}"

echo "[INFO] Retention policy applied successfully."
