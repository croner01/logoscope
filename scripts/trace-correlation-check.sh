#!/usr/bin/env bash
set -euo pipefail

# 链路追踪质量校验脚本
# 输出最近窗口内 logs/traces 关键指标：
# 1) logs trace/span 覆盖率（含 otlp/synthetic/missing 来源分布）
# 2) traces id 编码健康度（hex32/hex16）
# 3) logs->traces 关联命中率（trace 维度与 trace+span 维度）

NAMESPACE="${NAMESPACE:-islap}"
CLICKHOUSE_POD="${CLICKHOUSE_POD:-}"
WINDOW_HOURS="${WINDOW_HOURS:-1}"
JOIN_SAMPLE_LIMIT="${JOIN_SAMPLE_LIMIT:-50000}"
ARTIFACT_DIR="${ARTIFACT_DIR:-reports/trace-correlation-check}"

usage() {
  cat <<'EOF'
Usage:
  scripts/trace-correlation-check.sh [options]

Options:
  --namespace <ns>           Kubernetes namespace (default: islap)
  --clickhouse-pod <name>    ClickHouse pod 名称（默认自动发现 app=clickhouse）
  --window-hours <n>         统计窗口（小时，默认 1）
  --join-sample-limit <n>    logs-traces 关联采样上限（默认 50000）
  --artifact-dir <path>      报告输出目录（默认 reports/trace-correlation-check）
  -h, --help                 显示帮助
EOF
}

json_escape() {
  printf '%s' "${1:-}" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

ensure_positive_int() {
  local raw="${1:-}"
  local fallback="${2:-1}"
  if [[ "$raw" =~ ^[0-9]+$ ]] && (( raw > 0 )); then
    printf '%s' "$raw"
    return 0
  fi
  printf '%s' "$fallback"
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
    --window-hours)
      WINDOW_HOURS="$2"
      shift 2
      ;;
    --join-sample-limit)
      JOIN_SAMPLE_LIMIT="$2"
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

WINDOW_HOURS="$(ensure_positive_int "$WINDOW_HOURS" "1")"
JOIN_SAMPLE_LIMIT="$(ensure_positive_int "$JOIN_SAMPLE_LIMIT" "50000")"

mkdir -p "$ARTIFACT_DIR"
RUN_ID="trace-correlation-$(date -u +%Y%m%d-%H%M%S)-$RANDOM"
REPORT_FILE="${ARTIFACT_DIR}/${RUN_ID}.json"

if [[ -z "$CLICKHOUSE_POD" ]]; then
  CLICKHOUSE_POD="$(kubectl -n "$NAMESPACE" get pod -l app=clickhouse -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
fi
if [[ -z "$CLICKHOUSE_POD" ]]; then
  echo "[ERROR] clickhouse pod not found in namespace=${NAMESPACE}" >&2
  exit 1
fi

run_ch_query() {
  local sql="$1"
  kubectl -n "$NAMESPACE" exec "$CLICKHOUSE_POD" -- clickhouse-client -q "$sql"
}

logs_stats_raw="$(
  run_ch_query "
    SELECT
      count() AS total_logs,
      countIf(notEmpty(trace_id)) AS logs_with_trace_id,
      countIf(notEmpty(span_id)) AS logs_with_span_id,
      countIf(notEmpty(trace_id) AND notEmpty(span_id)) AS logs_with_trace_and_span,
      countIf(
        notEmpty(trace_id)
        AND notEmpty(span_id)
        AND lowerUTF8(ifNull(JSONExtractString(attributes_json, 'trace_id_source'), '')) = 'otlp'
      ) AS logs_with_otel_trace_and_span,
      countIf(
        lowerUTF8(ifNull(JSONExtractString(attributes_json, 'trace_id_source'), '')) = 'synthetic'
      ) AS logs_source_synthetic,
      countIf(
        lowerUTF8(ifNull(JSONExtractString(attributes_json, 'trace_id_source'), '')) = 'missing'
      ) AS logs_source_missing,
      countIf(
        lowerUTF8(ifNull(JSONExtractString(attributes_json, 'trace_id_source'), '')) = 'otlp'
      ) AS logs_source_otlp,
      countIf(
        ifNull(JSONExtractString(attributes_json, 'trace_id_source'), '') = ''
      ) AS logs_source_empty,
      countIf(
        notEmpty(trace_id)
        AND notEmpty(span_id)
        AND lowerUTF8(ifNull(JSONExtractString(attributes_json, 'trace_id_source'), '')) != 'synthetic'
      ) AS kpi_correlated_logs
    FROM logs.logs
    WHERE timestamp >= now() - INTERVAL ${WINDOW_HOURS} HOUR
    FORMAT TSVRaw
  " 2>/dev/null || true
)"
if [[ -z "$logs_stats_raw" ]]; then
  echo "[ERROR] failed to query logs stats from ClickHouse" >&2
  exit 1
fi
IFS=$'\t' read -r \
  total_logs \
  logs_with_trace_id \
  logs_with_span_id \
  logs_with_trace_and_span \
  logs_with_otel_trace_and_span \
  logs_source_synthetic \
  logs_source_missing \
  logs_source_otlp \
  logs_source_empty \
  kpi_correlated_logs <<< "$logs_stats_raw"

traces_stats_raw="$(
  run_ch_query "
    SELECT
      count() AS total_traces,
      countIf(match(trace_id, '^[0-9a-f]{32}$')) AS traces_trace_hex32,
      countIf(match(span_id, '^[0-9a-f]{16}$')) AS traces_span_hex16,
      countIf(parent_span_id = '' OR match(parent_span_id, '^[0-9a-f]{16}$')) AS traces_parent_span_valid
    FROM logs.traces
    WHERE timestamp >= now() - INTERVAL ${WINDOW_HOURS} HOUR
    FORMAT TSVRaw
  " 2>/dev/null || true
)"
if [[ -z "$traces_stats_raw" ]]; then
  echo "[ERROR] failed to query traces stats from ClickHouse" >&2
  exit 1
fi
IFS=$'\t' read -r total_traces traces_trace_hex32 traces_span_hex16 traces_parent_span_valid <<< "$traces_stats_raw"

join_stats_raw="$(
  run_ch_query "
    WITH candidates AS (
      SELECT trace_id, span_id
      FROM logs.logs
      WHERE timestamp >= now() - INTERVAL ${WINDOW_HOURS} HOUR
        AND notEmpty(trace_id)
        AND notEmpty(span_id)
        AND lowerUTF8(ifNull(JSONExtractString(attributes_json, 'trace_id_source'), '')) != 'synthetic'
      LIMIT ${JOIN_SAMPLE_LIMIT}
    )
    SELECT
      count() AS candidate_logs,
      countIf(trace_hit = 1) AS joined_by_trace,
      countIf(span_hit = 1) AS joined_by_trace_span
    FROM (
      SELECT
        c.trace_id,
        c.span_id,
        max(if(t.trace_id != '', 1, 0)) AS trace_hit,
        max(if(t.trace_id != '' AND t.span_id = c.span_id, 1, 0)) AS span_hit
      FROM candidates c
      LEFT JOIN logs.traces t
        ON c.trace_id = t.trace_id
      GROUP BY c.trace_id, c.span_id
    )
    FORMAT TSVRaw
  " 2>/dev/null || true
)"
if [[ -z "$join_stats_raw" ]]; then
  echo "[ERROR] failed to query logs-traces join stats from ClickHouse" >&2
  exit 1
fi
IFS=$'\t' read -r candidate_logs joined_by_trace joined_by_trace_span <<< "$join_stats_raw"

safe_ratio() {
  local numerator="${1:-0}"
  local denominator="${2:-0}"
  if [[ -z "$denominator" || "$denominator" == "0" ]]; then
    printf '0'
    return 0
  fi
  awk -v n="$numerator" -v d="$denominator" 'BEGIN { printf "%.6f", n / d }'
}

kpi_correlation_rate="$(safe_ratio "$kpi_correlated_logs" "$total_logs")"
join_rate_trace="$(safe_ratio "$joined_by_trace" "$candidate_logs")"
join_rate_trace_span="$(safe_ratio "$joined_by_trace_span" "$candidate_logs")"

{
  echo "{"
  echo "  \"run_id\": \"$(json_escape "$RUN_ID")\","
  echo "  \"generated_at\": \"$(date -u +"%Y-%m-%dT%H:%M:%S.%3NZ")\","
  echo "  \"namespace\": \"$(json_escape "$NAMESPACE")\","
  echo "  \"clickhouse_pod\": \"$(json_escape "$CLICKHOUSE_POD")\","
  echo "  \"window_hours\": ${WINDOW_HOURS},"
  echo "  \"join_sample_limit\": ${JOIN_SAMPLE_LIMIT},"
  echo "  \"logs\": {"
  echo "    \"total\": ${total_logs:-0},"
  echo "    \"with_trace_id\": ${logs_with_trace_id:-0},"
  echo "    \"with_span_id\": ${logs_with_span_id:-0},"
  echo "    \"with_trace_and_span\": ${logs_with_trace_and_span:-0},"
  echo "    \"with_otel_trace_and_span\": ${logs_with_otel_trace_and_span:-0},"
  echo "    \"kpi_correlated_logs\": ${kpi_correlated_logs:-0},"
  echo "    \"kpi_correlation_rate\": ${kpi_correlation_rate},"
  echo "    \"trace_id_source\": {"
  echo "      \"otlp\": ${logs_source_otlp:-0},"
  echo "      \"synthetic\": ${logs_source_synthetic:-0},"
  echo "      \"missing\": ${logs_source_missing:-0},"
  echo "      \"empty\": ${logs_source_empty:-0}"
  echo "    }"
  echo "  },"
  echo "  \"traces\": {"
  echo "    \"total\": ${total_traces:-0},"
  echo "    \"trace_id_hex32\": ${traces_trace_hex32:-0},"
  echo "    \"span_id_hex16\": ${traces_span_hex16:-0},"
  echo "    \"parent_span_id_valid\": ${traces_parent_span_valid:-0}"
  echo "  },"
  echo "  \"join\": {"
  echo "    \"candidate_logs\": ${candidate_logs:-0},"
  echo "    \"joined_by_trace\": ${joined_by_trace:-0},"
  echo "    \"joined_by_trace_span\": ${joined_by_trace_span:-0},"
  echo "    \"join_rate_trace\": ${join_rate_trace},"
  echo "    \"join_rate_trace_span\": ${join_rate_trace_span}"
  echo "  }"
  echo "}"
} > "$REPORT_FILE"

ln -sfn "$(basename "$REPORT_FILE")" "${ARTIFACT_DIR}/latest.json"

echo "[INFO] Trace correlation report: $REPORT_FILE"
echo "[INFO] Trace correlation latest: ${ARTIFACT_DIR}/latest.json"
cat "$REPORT_FILE"
