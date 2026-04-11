#!/usr/bin/env bash
set -euo pipefail

# ClickHouse 历史日志/观测数据一次性清理脚本
#
# 设计目标：
# 1. 优先删除完整旧分区，避免大范围 DELETE WHERE 带来的额外放大。
# 2. 对边界日残留数据补做条件删除，确保严格收敛到最近 N 天。
# 3. 支持 dry-run / kubectl / local 三种常见运维方式。
#
# 默认清理表：
# - logs
# - traces
# - events
# - metrics
#
# 注意：
# - 这是“一次性清理脚本”，不会修改表上的 TTL 保留策略。
# - 表必须包含 timestamp 列，且适用于按时间窗口清理。

CLICKHOUSE_CLIENT="${CLICKHOUSE_CLIENT:-clickhouse-client}"
CLICKHOUSE_HOST="${CLICKHOUSE_HOST:-clickhouse}"
CLICKHOUSE_PORT="${CLICKHOUSE_PORT:-9000}"
CLICKHOUSE_USER="${CLICKHOUSE_USER:-default}"
CLICKHOUSE_PASSWORD="${CLICKHOUSE_PASSWORD:-}"
CLICKHOUSE_DATABASE="${CLICKHOUSE_DATABASE:-logs}"

DEFAULT_KUBECTL_BIN="$(command -v kubectl 2>/dev/null || true)"
if [[ -z "$DEFAULT_KUBECTL_BIN" && -x /usr/local/bin/kubectl ]]; then
  DEFAULT_KUBECTL_BIN="/usr/local/bin/kubectl"
fi
KUBECTL_BIN="${KUBECTL_BIN:-${DEFAULT_KUBECTL_BIN:-kubectl}}"

MODE="${MODE:-auto}" # auto|local|kubectl
NAMESPACE="${NAMESPACE:-islap}"
CLICKHOUSE_POD="${CLICKHOUSE_POD:-}"

RETENTION_DAYS="${RETENTION_DAYS:-3}"
TABLES="${TABLES:-logs traces events metrics}"
WAIT_MUTATIONS="${WAIT_MUTATIONS:-true}"
WAIT_TIMEOUT_SECONDS="${WAIT_TIMEOUT_SECONDS:-300}"
POLL_INTERVAL_SECONDS="${POLL_INTERVAL_SECONDS:-2}"
DRY_RUN="${DRY_RUN:-false}"

usage() {
  cat <<'EOF'
Usage:
  scripts/cleanup-historical-log-data.sh [options]

Options:
  --mode <auto|local|kubectl>      执行模式（默认 auto）
  --namespace <ns>                 kubectl 模式命名空间（默认 islap）
  --clickhouse-pod <name>          kubectl 模式指定 pod
  --database <name>                数据库名（默认 logs）
  --retention-days <n>             仅保留最近 n 天数据（默认 3）
  --tables "<list>"                清理表列表（默认 "logs traces events metrics"）
  --no-wait                        不等待 DELETE mutation 完成
  --wait-timeout-seconds <n>       等待 mutation 完成超时（默认 300）
  --poll-interval-seconds <n>      轮询间隔秒数（默认 2）
  --dry-run                        仅输出计划，不实际执行
  -h, --help                       显示帮助

Examples:
  scripts/cleanup-historical-log-data.sh --retention-days 3 --dry-run

  scripts/cleanup-historical-log-data.sh \
    --mode kubectl \
    --namespace islap \
    --tables "logs traces events" \
    --retention-days 7
EOF
}

to_bool() {
  local raw="${1:-false}"
  case "${raw,,}" in
    1|true|yes|on) echo "true" ;;
    *) echo "false" ;;
  esac
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

trim_spaces() {
  printf '%s' "${1:-}" | xargs
}

validate_table_name() {
  local table_name="${1:-}"
  [[ "$table_name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --mode)
        MODE="${2:-}"; shift 2 ;;
      --namespace)
        NAMESPACE="${2:-}"; shift 2 ;;
      --clickhouse-pod)
        CLICKHOUSE_POD="${2:-}"; shift 2 ;;
      --database)
        CLICKHOUSE_DATABASE="${2:-}"; shift 2 ;;
      --retention-days)
        RETENTION_DAYS="${2:-}"; shift 2 ;;
      --tables)
        TABLES="${2:-}"; shift 2 ;;
      --no-wait)
        WAIT_MUTATIONS="false"; shift 1 ;;
      --wait-timeout-seconds)
        WAIT_TIMEOUT_SECONDS="${2:-}"; shift 2 ;;
      --poll-interval-seconds)
        POLL_INTERVAL_SECONDS="${2:-}"; shift 2 ;;
      --dry-run)
        DRY_RUN="true"; shift 1 ;;
      -h|--help)
        usage; exit 0 ;;
      *)
        echo "[ERROR] Unknown option: $1" >&2
        usage
        exit 1 ;;
    esac
  done
}

resolve_mode() {
  if [[ "$MODE" == "auto" ]]; then
    if command -v "$CLICKHOUSE_CLIENT" >/dev/null 2>&1 && run_query_local_capture "SELECT 1" >/dev/null 2>&1; then
      MODE="local"
    else
      MODE="kubectl"
    fi
  fi
}

resolve_clickhouse_pod() {
  if [[ -n "$CLICKHOUSE_POD" ]]; then
    return 0
  fi
  CLICKHOUSE_POD="$("$KUBECTL_BIN" -n "$NAMESPACE" get pod -l app=clickhouse -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
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

run_query_kubectl_capture() {
  local sql="$1"
  resolve_clickhouse_pod
  if [[ -z "$CLICKHOUSE_POD" ]]; then
    echo "[ERROR] clickhouse pod not found in namespace=${NAMESPACE}" >&2
    exit 1
  fi
  "$KUBECTL_BIN" -n "$NAMESPACE" exec "$CLICKHOUSE_POD" -- clickhouse-client -q "$sql"
}

run_query_capture() {
  local sql="$1"
  if [[ "$MODE" == "local" ]]; then
    run_query_local_capture "$sql"
  else
    run_query_kubectl_capture "$sql"
  fi
}

run_query() {
  local sql="$1"
  if [[ "$(to_bool "$DRY_RUN")" == "true" ]]; then
    echo "[DRY_RUN] $sql"
    return 0
  fi
  run_query_capture "$sql" >/dev/null
}

capture_query_result() {
  local sql="$1"
  local raw=""
  if [[ "$MODE" == "local" ]]; then
    raw="$(run_query_local_capture "$sql" 2>/dev/null || true)"
  else
    resolve_clickhouse_pod
    if [[ -z "$CLICKHOUSE_POD" ]]; then
      echo "[ERROR] clickhouse pod not found in namespace=${NAMESPACE}" >&2
      exit 1
    fi
    raw="$("$KUBECTL_BIN" -n "$NAMESPACE" exec "$CLICKHOUSE_POD" -- clickhouse-client -q "$sql" 2>/dev/null || true)"
  fi
  printf '%s' "$raw"
}

query_scalar() {
  local sql="$1"
  local raw
  raw="$(capture_query_result "$sql")"
  raw="${raw//$'\r'/}"
  raw="${raw//$'\n'/}"
  printf '%s' "$raw"
}

table_has_timestamp_column() {
  local table_name="$1"
  local count
  count="$(query_scalar "SELECT count() FROM system.columns WHERE database = '${CLICKHOUSE_DATABASE}' AND table = '${table_name}' AND name = 'timestamp'")"
  [[ "$count" =~ ^[0-9]+$ ]] && [[ "$count" -gt 0 ]]
}

collect_old_partitions() {
  local table_name="$1"
  capture_query_result "
    WITH toDate(now() - INTERVAL ${RETENTION_DAYS} DAY) AS cutoff
    SELECT partition, partition_id, sum(rows) AS rows
    FROM system.parts
    WHERE database = '${CLICKHOUSE_DATABASE}'
      AND table = '${table_name}'
      AND active = 1
      AND parseDateTimeBestEffortOrNull(partition) < cutoff
    GROUP BY partition, partition_id
    ORDER BY partition
    FORMAT TSVRaw
  "
}

count_old_rows() {
  local table_name="$1"
  query_scalar "SELECT count() FROM ${CLICKHOUSE_DATABASE}.${table_name} WHERE timestamp < now() - INTERVAL ${RETENTION_DAYS} DAY"
}

drop_old_partitions() {
  local table_name="$1"
  local partition_rows
  partition_rows="$(collect_old_partitions "$table_name")"
  if [[ -z "$partition_rows" ]]; then
    echo "[INFO] ${table_name}: no full partitions older than ${RETENTION_DAYS} days"
    return 0
  fi

  echo "[INFO] ${table_name}: dropping old partition_id(s)"
  while IFS=$'\t' read -r partition partition_id rows; do
    [[ -z "${partition_id:-}" ]] && continue
    echo "  - partition=${partition} partition_id=${partition_id} rows=${rows}"
    run_query "ALTER TABLE ${CLICKHOUSE_DATABASE}.${table_name} DROP PARTITION ID '${partition_id}'"
  done <<< "$partition_rows"
}

submit_boundary_delete() {
  local table_name="$1"
  local old_rows
  old_rows="$(count_old_rows "$table_name" || true)"
  old_rows="${old_rows:-0}"
  if [[ ! "$old_rows" =~ ^[0-9]+$ ]]; then
    old_rows="0"
  fi
  if [[ "$old_rows" -eq 0 ]]; then
    echo "[INFO] ${table_name}: no boundary rows older than ${RETENTION_DAYS} days"
    return 0
  fi

  echo "[INFO] ${table_name}: submit DELETE WHERE for remaining ${old_rows} old rows"
  run_query "ALTER TABLE ${CLICKHOUSE_DATABASE}.${table_name} DELETE WHERE timestamp < now() - INTERVAL ${RETENTION_DAYS} DAY"
}

wait_for_cleanup() {
  local table_list=("$@")
  local timeout_seconds
  local poll_interval
  local deadline

  timeout_seconds="$(normalize_positive_int "$WAIT_TIMEOUT_SECONDS" 300)"
  poll_interval="$(normalize_positive_int "$POLL_INTERVAL_SECONDS" 2)"
  deadline=$((SECONDS + timeout_seconds))

  while (( SECONDS <= deadline )); do
    local pending_tables=()
    for table_name in "${table_list[@]}"; do
      local remaining
      remaining="$(count_old_rows "$table_name" || true)"
      remaining="${remaining:-0}"
      if [[ ! "$remaining" =~ ^[0-9]+$ ]]; then
        remaining="0"
      fi
      if [[ "$remaining" -gt 0 ]]; then
        pending_tables+=("${table_name}:${remaining}")
      fi
    done

    if (( ${#pending_tables[@]} == 0 )); then
      echo "[INFO] cleanup completed: no rows older than ${RETENTION_DAYS} days remain"
      return 0
    fi

    echo "[INFO] waiting for mutation(s): ${pending_tables[*]}"
    sleep "$poll_interval"
  done

  echo "[WARN] wait timeout reached after ${timeout_seconds}s; some old rows may still be being mutated" >&2
  return 1
}

print_plan() {
  local table_list=("$@")
  echo "[INFO] database=${CLICKHOUSE_DATABASE} mode=${MODE} retention_days=${RETENTION_DAYS} dry_run=${DRY_RUN} wait=${WAIT_MUTATIONS}"
  if [[ "$MODE" == "kubectl" ]]; then
    resolve_clickhouse_pod
    echo "[INFO] namespace=${NAMESPACE} clickhouse_pod=${CLICKHOUSE_POD:-<not-found>}"
  else
    echo "[INFO] clickhouse_host=${CLICKHOUSE_HOST} clickhouse_port=${CLICKHOUSE_PORT}"
  fi

  for table_name in "${table_list[@]}"; do
    local partition_rows old_rows
    partition_rows="$(collect_old_partitions "$table_name")"
    old_rows="$(count_old_rows "$table_name" || true)"
    old_rows="${old_rows:-0}"
    echo "[INFO] table=${table_name} rows_older_than_cutoff=${old_rows}"
    if [[ -n "$partition_rows" ]]; then
      while IFS=$'\t' read -r partition partition_id rows; do
        [[ -z "${partition_id:-}" ]] && continue
        echo "  partition=${partition} partition_id=${partition_id} rows=${rows}"
      done <<< "$partition_rows"
    else
      echo "  partition=<none>"
    fi
  done
}

main() {
  parse_args "$@"
  resolve_mode

  RETENTION_DAYS="$(normalize_positive_int "$RETENTION_DAYS" 3)"
  WAIT_TIMEOUT_SECONDS="$(normalize_positive_int "$WAIT_TIMEOUT_SECONDS" 300)"
  POLL_INTERVAL_SECONDS="$(normalize_positive_int "$POLL_INTERVAL_SECONDS" 2)"
  WAIT_MUTATIONS="$(to_bool "$WAIT_MUTATIONS")"
  DRY_RUN="$(to_bool "$DRY_RUN")"

  if [[ "$MODE" == "kubectl" ]]; then
    resolve_clickhouse_pod
    if [[ -z "$CLICKHOUSE_POD" ]]; then
      echo "[ERROR] clickhouse pod not found in namespace=${NAMESPACE}" >&2
      exit 1
    fi
  fi

  local table_list=()
  local raw_table
  for raw_table in $TABLES; do
    local table_name
    table_name="$(trim_spaces "$raw_table")"
    [[ -z "$table_name" ]] && continue
    if ! validate_table_name "$table_name"; then
      echo "[ERROR] invalid table name: ${table_name}" >&2
      exit 1
    fi
    if ! table_has_timestamp_column "$table_name"; then
      echo "[ERROR] table ${CLICKHOUSE_DATABASE}.${table_name} does not have timestamp column; refusing cleanup" >&2
      exit 1
    fi
    table_list+=("$table_name")
  done

  if (( ${#table_list[@]} == 0 )); then
    echo "[ERROR] no tables selected" >&2
    exit 1
  fi

  print_plan "${table_list[@]}"

  for table_name in "${table_list[@]}"; do
    drop_old_partitions "$table_name"
  done

  for table_name in "${table_list[@]}"; do
    submit_boundary_delete "$table_name"
  done

  if [[ "$DRY_RUN" == "true" ]]; then
    echo "[INFO] dry-run completed"
    exit 0
  fi

  if [[ "$WAIT_MUTATIONS" == "true" ]]; then
    wait_for_cleanup "${table_list[@]}" || true
  fi

  echo "[INFO] final remaining rows older than ${RETENTION_DAYS} days:"
  for table_name in "${table_list[@]}"; do
    echo "  ${table_name}=$(count_old_rows "$table_name" || true)"
  done
}

main "$@"
