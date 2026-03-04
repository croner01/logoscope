#!/usr/bin/env bash
set -euo pipefail

# ClickHouse 数据保留策略校验脚本
# 校验目标：
# 1) 核心表是否都包含 TTL 规则
# 2) 指定核心表是否使用按日分区（toDate/toYYYYMMDD）

NAMESPACE="${NAMESPACE:-islap}"
CLICKHOUSE_POD="${CLICKHOUSE_POD:-}"
CLICKHOUSE_DATABASE="${CLICKHOUSE_DATABASE:-logs}"
REQUIRED_TABLES="${REQUIRED_TABLES:-logs traces events metrics}"
REQUIRED_DAILY_PARTITION_TABLES="${REQUIRED_DAILY_PARTITION_TABLES:-logs traces events metrics}"
ARTIFACT_DIR="${ARTIFACT_DIR:-reports/data-retention}"

usage() {
  cat <<'EOF'
Usage:
  scripts/check-data-retention.sh [options]

Options:
  --namespace <ns>             Kubernetes namespace (default: islap)
  --clickhouse-pod <name>      指定 ClickHouse pod
  --database <name>            数据库名 (default: logs)
  --required-tables "<list>"   要求包含 TTL 的表列表 (default: "logs traces events metrics")
  --required-daily-partitions "<list>"  要求按日分区的表列表 (default: "logs traces events metrics")
  --artifact-dir <path>        报告输出目录 (default: reports/data-retention)
  -h, --help                   显示帮助
EOF
}

json_escape() {
  printf '%s' "${1:-}" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
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
    --required-tables)
      REQUIRED_TABLES="$2"
      shift 2
      ;;
    --required-daily-partitions)
      REQUIRED_DAILY_PARTITION_TABLES="$2"
      shift 2
      ;;
    --artifact-dir)
      ARTIFACT_DIR="$2"
      shift 2
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

mkdir -p "$ARTIFACT_DIR"
RUN_ID="data-retention-$(date -u +%Y%m%d-%H%M%S)-$RANDOM"
REPORT_FILE="${ARTIFACT_DIR}/${RUN_ID}.json"

if [[ -z "$CLICKHOUSE_POD" ]]; then
  CLICKHOUSE_POD="$(kubectl -n "$NAMESPACE" get pod -l app=clickhouse -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
fi
if [[ -z "$CLICKHOUSE_POD" ]]; then
  echo "[ERROR] clickhouse pod not found in namespace=${NAMESPACE}" >&2
  exit 1
fi

all_required_tables="$REQUIRED_TABLES $REQUIRED_DAILY_PARTITION_TABLES"
required_csv="$(printf '%s' "$all_required_tables" | tr ' ' '\n' | sed '/^$/d' | sort -u | tr '\n' ',' | sed 's/,$//')"
required_sql_list="$(printf "'%s'" "$(printf '%s' "$required_csv" | sed "s/,/','/g")")"

result_rows="$(
  kubectl -n "$NAMESPACE" exec "$CLICKHOUSE_POD" -- clickhouse-client -q "
    SELECT
      name,
      if(positionCaseInsensitive(create_table_query, 'TTL') > 0, 1, 0) AS has_ttl,
      partition_key
    FROM system.tables
    WHERE database = '${CLICKHOUSE_DATABASE}'
      AND name IN (${required_sql_list})
    ORDER BY name
    FORMAT TSVRaw
  " 2>/dev/null || true
)"

if [[ -z "$result_rows" ]]; then
  echo "[ERROR] failed to query retention metadata" >&2
  exit 1
fi

declare -A table_ttl=()
declare -A table_partition_key=()
while IFS=$'\t' read -r table_name has_ttl partition_key; do
  [[ -z "${table_name:-}" ]] && continue
  table_ttl["$table_name"]="$has_ttl"
  table_partition_key["$table_name"]="${partition_key:-}"
done <<< "$result_rows"

missing=()
for table_name in $REQUIRED_TABLES; do
  value="${table_ttl[$table_name]:-0}"
  if [[ "$value" != "1" ]]; then
    missing+=("$table_name")
  fi
done

missing_daily_partition=()
for table_name in $REQUIRED_DAILY_PARTITION_TABLES; do
  partition_expr="$(printf '%s' "${table_partition_key[$table_name]:-}" | tr '[:upper:]' '[:lower:]')"
  if [[ -z "$partition_expr" ]]; then
    missing_daily_partition+=("$table_name")
    continue
  fi
  if [[ "$partition_expr" != *"todate("* && "$partition_expr" != *"toyyyymmdd("* ]]; then
    missing_daily_partition+=("$table_name")
  fi
done

passed=true
summary="retention policy check passed"
if (( ${#missing[@]} > 0 )); then
  passed=false
  summary="missing TTL on tables: ${missing[*]}"
fi
if (( ${#missing_daily_partition[@]} > 0 )); then
  passed=false
  if [[ "$summary" == "retention policy check passed" ]]; then
    summary="missing daily partition on tables: ${missing_daily_partition[*]}"
  else
    summary="${summary}; missing daily partition on tables: ${missing_daily_partition[*]}"
  fi
fi

{
  echo "{"
  echo "  \"run_id\": \"$(json_escape "$RUN_ID")\","
  echo "  \"generated_at\": \"$(date -u +"%Y-%m-%dT%H:%M:%S.%3NZ")\","
  echo "  \"namespace\": \"$(json_escape "$NAMESPACE")\","
  echo "  \"database\": \"$(json_escape "$CLICKHOUSE_DATABASE")\","
  echo "  \"clickhouse_pod\": \"$(json_escape "$CLICKHOUSE_POD")\","
  echo "  \"required_tables\": [$(printf '"%s",' $REQUIRED_TABLES | sed 's/,$//')],"
  echo "  \"required_daily_partition_tables\": [$(printf '"%s",' $REQUIRED_DAILY_PARTITION_TABLES | sed 's/,$//')],"
  echo "  \"passed\": ${passed},"
  echo "  \"summary\": \"$(json_escape "$summary")\","
  echo "  \"tables\": ["
  first=true
  for table_name in $REQUIRED_TABLES; do
    if [[ "$first" != true ]]; then
      echo ","
    fi
    first=false
    has_ttl="${table_ttl[$table_name]:-0}"
    partition_expr="${table_partition_key[$table_name]:-}"
    has_ttl_bool=false
    if [[ "$has_ttl" == "1" ]]; then
      has_ttl_bool=true
    fi
    has_daily_partition=false
    partition_expr_lower="$(printf '%s' "$partition_expr" | tr '[:upper:]' '[:lower:]')"
    if [[ "$partition_expr_lower" == *"todate("* || "$partition_expr_lower" == *"toyyyymmdd("* ]]; then
      has_daily_partition=true
    fi
    printf '    {"name":"%s","has_ttl":%s,"has_daily_partition":%s,"partition_key":"%s"}' \
      "$(json_escape "$table_name")" "$has_ttl_bool" "$has_daily_partition" "$(json_escape "$partition_expr")"
  done
  echo
  echo "  ]"
  echo "}"
} > "$REPORT_FILE"

ln -sfn "$REPORT_FILE" "${ARTIFACT_DIR}/latest.json"
echo "[INFO] Data retention report: $REPORT_FILE"
echo "[INFO] Data retention latest: ${ARTIFACT_DIR}/latest.json"

if [[ "$passed" != true ]]; then
  echo "[ERROR] $summary" >&2
  exit 1
fi

echo "[INFO] $summary"
