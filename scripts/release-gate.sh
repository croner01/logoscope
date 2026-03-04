#!/usr/bin/env bash
set -euo pipefail

# M4-01 发布门禁脚本
# 1) 执行 trace-e2e-smoke
# 2) 执行 ai-contract-check
# 3) 执行 query-contract-check
# 4) 执行 sql-safety-scan
# 5) 执行 data-retention-check
# 6) 执行 backend-pytest-check
# 7) 执行 p0p1-regression-check
# 8) 执行 perf-baseline-check
# 9) 执行 perf-trend-check
# 10) 任一失败则阻断发布（exit!=0）
# 11) 产出本地报告，并尝试写入 ClickHouse 作为审计记录

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="${NAMESPACE:-islap}"
ARTIFACT_DIR="${ARTIFACT_DIR:-${PROJECT_ROOT}/reports/release-gate}"

CANDIDATE="${CANDIDATE:-manual}"
TAG="${TAG:-unknown}"
TARGET="${TARGET:-all}"
MAX_PENDING="${MAX_PENDING:-0}"
BYPASS_REASON=""
COVERAGE_MIN_FLOOR="${COVERAGE_MIN_FLOOR:-30}"
QUERY_COV_MIN="${QUERY_COV_MIN:-30}"
TOPOLOGY_COV_MIN="${TOPOLOGY_COV_MIN:-30}"
INGEST_COV_MIN="${INGEST_COV_MIN:-30}"
AI_COV_MIN="${AI_COV_MIN:-30}"

usage() {
  cat <<'EOF'
Usage:
  scripts/release-gate.sh [options]

Options:
  --candidate <name>        发布候选名称（默认 manual）
  --tag <tag>               发布镜像 tag（默认 unknown）
  --target <service|all>    发布目标（默认 all）
  --artifact-dir <path>     报告目录（默认 reports/release-gate）
  --max-pending <n>         trace smoke 脚本 MAX_PENDING（默认 0）
  --coverage-min-floor <n>  backend pytest 强制最低覆盖率（默认 30）
  --query-cov-min <n>       query-service 覆盖率阈值（默认 30）
  --topology-cov-min <n>    topology-service 覆盖率阈值（默认 30）
  --ingest-cov-min <n>      ingest-service 覆盖率阈值（默认 30）
  --ai-cov-min <n>          ai-service 覆盖率阈值（默认 30）
  --bypass-reason <reason>  手工绕过门禁（记录审计，不执行 smoke）
  -h, --help                显示帮助

Examples:
  scripts/release-gate.sh --candidate m4-rc1 --tag m4-20260227-001500 --target frontend
  scripts/release-gate.sh --candidate hotfix-1 --bypass-reason "prod rollback in progress"
EOF
}

sanitize_tsv() {
  local value="${1:-}"
  value="${value//$'\t'/ }"
  value="${value//$'\n'/ }"
  printf '%s' "$value"
}

json_escape() {
  printf '%s' "${1:-}" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

is_non_negative_int() {
  [[ "${1:-}" =~ ^[0-9]+$ ]]
}

validate_gate_cov_threshold() {
  local key="$1"
  local value="$2"
  if ! is_non_negative_int "${value}"; then
    echo "[ERROR] ${key} must be a non-negative integer, got: ${value}" >&2
    exit 1
  fi
  if (( value < COVERAGE_MIN_FLOOR )); then
    echo "[ERROR] ${key}=${value} is below mandatory coverage floor ${COVERAGE_MIN_FLOOR}" >&2
    exit 1
  fi
}

ensure_clickhouse_table() {
  local clickhouse_pod="$1"
  kubectl -n "$NAMESPACE" exec "$clickhouse_pod" -- clickhouse-client -q "
    CREATE TABLE IF NOT EXISTS logs.release_gate_reports (
      gate_id String,
      candidate String,
      tag String,
      target String,
      started_at DateTime64(3, 'UTC'),
      finished_at DateTime64(3, 'UTC'),
      duration_ms UInt64,
      status String,
      trace_id String,
      smoke_exit_code Int32,
      trace_smoke_exit_code Int32 DEFAULT smoke_exit_code,
      ai_contract_exit_code Int32 DEFAULT 0,
      query_contract_exit_code Int32 DEFAULT 0,
      sql_safety_exit_code Int32 DEFAULT 0,
      data_retention_exit_code Int32 DEFAULT 0,
      backend_pytest_exit_code Int32 DEFAULT 0,
      p0p1_regression_exit_code Int32 DEFAULT 0,
      perf_baseline_exit_code Int32 DEFAULT 0,
      perf_trend_exit_code Int32 DEFAULT 0,
      report_path String,
      summary String,
      created_at DateTime64(3, 'UTC') DEFAULT now64(3, 'UTC')
    )
    ENGINE = MergeTree()
    ORDER BY (started_at, gate_id)
    SETTINGS index_granularity = 8192
  " >/dev/null

  kubectl -n "$NAMESPACE" exec "$clickhouse_pod" -- clickhouse-client -q "
    ALTER TABLE logs.release_gate_reports
      ADD COLUMN IF NOT EXISTS trace_smoke_exit_code Int32 DEFAULT smoke_exit_code,
      ADD COLUMN IF NOT EXISTS ai_contract_exit_code Int32 DEFAULT 0,
      ADD COLUMN IF NOT EXISTS query_contract_exit_code Int32 DEFAULT 0,
      ADD COLUMN IF NOT EXISTS sql_safety_exit_code Int32 DEFAULT 0,
      ADD COLUMN IF NOT EXISTS data_retention_exit_code Int32 DEFAULT 0,
      ADD COLUMN IF NOT EXISTS backend_pytest_exit_code Int32 DEFAULT 0,
      ADD COLUMN IF NOT EXISTS p0p1_regression_exit_code Int32 DEFAULT 0,
      ADD COLUMN IF NOT EXISTS perf_baseline_exit_code Int32 DEFAULT 0,
      ADD COLUMN IF NOT EXISTS perf_trend_exit_code Int32 DEFAULT 0
  " >/dev/null
}

persist_report_to_clickhouse() {
  local gate_id="$1"
  local started_at="$2"
  local finished_at="$3"
  local duration_ms="$4"
  local status="$5"
  local trace_id="$6"
  local smoke_exit_code="$7"
  local report_path="$8"
  local summary="$9"
  local trace_smoke_exit_code="${10}"
  local ai_contract_exit_code="${11}"
  local query_contract_exit_code="${12}"
  local sql_safety_exit_code="${13}"
  local data_retention_exit_code="${14}"
  local backend_pytest_exit_code="${15}"
  local p0p1_regression_exit_code="${16}"
  local perf_baseline_exit_code="${17}"
  local perf_trend_exit_code="${18}"

  local clickhouse_pod
  clickhouse_pod="$(kubectl -n "$NAMESPACE" get pod -l app=clickhouse -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  if [[ -z "$clickhouse_pod" ]]; then
    echo "[WARN] ClickHouse pod not found, skip gate report DB persistence"
    return 0
  fi

  ensure_clickhouse_table "$clickhouse_pod"

  local started_ch finished_ch json_row
  started_ch="$(date -u -d "$started_at" '+%Y-%m-%d %H:%M:%S.%3N' 2>/dev/null || printf '%s' "$started_at")"
  finished_ch="$(date -u -d "$finished_at" '+%Y-%m-%d %H:%M:%S.%3N' 2>/dev/null || printf '%s' "$finished_at")"

  json_row=$(cat <<EOF
{"gate_id":"$(json_escape "$gate_id")","candidate":"$(json_escape "$CANDIDATE")","tag":"$(json_escape "$TAG")","target":"$(json_escape "$TARGET")","started_at":"$(json_escape "$started_ch")","finished_at":"$(json_escape "$finished_ch")","duration_ms":$duration_ms,"status":"$(json_escape "$status")","trace_id":"$(json_escape "$trace_id")","smoke_exit_code":$smoke_exit_code,"trace_smoke_exit_code":$trace_smoke_exit_code,"ai_contract_exit_code":$ai_contract_exit_code,"query_contract_exit_code":$query_contract_exit_code,"sql_safety_exit_code":$sql_safety_exit_code,"data_retention_exit_code":$data_retention_exit_code,"backend_pytest_exit_code":$backend_pytest_exit_code,"p0p1_regression_exit_code":$p0p1_regression_exit_code,"perf_baseline_exit_code":$perf_baseline_exit_code,"perf_trend_exit_code":$perf_trend_exit_code,"report_path":"$(json_escape "$report_path")","summary":"$(json_escape "$summary")"}
EOF
)

  printf '%s\n' "$json_row" | kubectl -n "$NAMESPACE" exec -i "$clickhouse_pod" -- clickhouse-client -q "
    INSERT INTO logs.release_gate_reports
      (gate_id, candidate, tag, target, started_at, finished_at, duration_ms, status, trace_id, smoke_exit_code, trace_smoke_exit_code, ai_contract_exit_code, query_contract_exit_code, sql_safety_exit_code, data_retention_exit_code, backend_pytest_exit_code, p0p1_regression_exit_code, perf_baseline_exit_code, perf_trend_exit_code, report_path, summary)
    FORMAT JSONEachRow
  " >/dev/null

  local persisted_count
  persisted_count="$(kubectl -n "$NAMESPACE" exec "$clickhouse_pod" -- clickhouse-client -q "
    SELECT count() FROM logs.release_gate_reports WHERE gate_id = '$(sanitize_tsv "$gate_id")'
  " 2>/dev/null | tr -d '\r')"
  if [[ "${persisted_count:-0}" -lt 1 ]]; then
    echo "[WARN] Gate report insert verification failed for gate_id=$gate_id"
    return 1
  fi

  echo "[INFO] Gate report persisted to ClickHouse logs.release_gate_reports (gate_id=$gate_id)"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --candidate)
      CANDIDATE="$2"
      shift 2
      ;;
    --tag)
      TAG="$2"
      shift 2
      ;;
    --target)
      TARGET="$2"
      shift 2
      ;;
    --artifact-dir)
      ARTIFACT_DIR="$2"
      shift 2
      ;;
    --max-pending)
      MAX_PENDING="$2"
      shift 2
      ;;
    --coverage-min-floor)
      COVERAGE_MIN_FLOOR="$2"
      shift 2
      ;;
    --query-cov-min)
      QUERY_COV_MIN="$2"
      shift 2
      ;;
    --topology-cov-min)
      TOPOLOGY_COV_MIN="$2"
      shift 2
      ;;
    --ingest-cov-min)
      INGEST_COV_MIN="$2"
      shift 2
      ;;
    --ai-cov-min)
      AI_COV_MIN="$2"
      shift 2
      ;;
    --bypass-reason)
      BYPASS_REASON="$2"
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

if ! is_non_negative_int "${COVERAGE_MIN_FLOOR}"; then
  echo "[ERROR] COVERAGE_MIN_FLOOR must be a non-negative integer, got: ${COVERAGE_MIN_FLOOR}" >&2
  exit 1
fi

validate_gate_cov_threshold "QUERY_COV_MIN" "${QUERY_COV_MIN}"
validate_gate_cov_threshold "TOPOLOGY_COV_MIN" "${TOPOLOGY_COV_MIN}"
validate_gate_cov_threshold "INGEST_COV_MIN" "${INGEST_COV_MIN}"
validate_gate_cov_threshold "AI_COV_MIN" "${AI_COV_MIN}"

mkdir -p "$ARTIFACT_DIR"

RUN_ID="gate-$(date -u +%Y%m%d-%H%M%S)-$RANDOM"
STARTED_AT="$(date -u +"%Y-%m-%dT%H:%M:%S.%3NZ")"
START_MS="$(date +%s%3N)"
LOG_FILE="${ARTIFACT_DIR}/${RUN_ID}.smoke.log"
AI_LOG_FILE="${ARTIFACT_DIR}/${RUN_ID}.ai.log"
QUERY_LOG_FILE="${ARTIFACT_DIR}/${RUN_ID}.query.log"
SQL_LOG_FILE="${ARTIFACT_DIR}/${RUN_ID}.sql.log"
RETENTION_LOG_FILE="${ARTIFACT_DIR}/${RUN_ID}.retention.log"
BACKEND_PYTEST_LOG_FILE="${ARTIFACT_DIR}/${RUN_ID}.backend-pytest.log"
P0P1_LOG_FILE="${ARTIFACT_DIR}/${RUN_ID}.p0p1.log"
PERF_LOG_FILE="${ARTIFACT_DIR}/${RUN_ID}.perf.log"
PERF_TREND_LOG_FILE="${ARTIFACT_DIR}/${RUN_ID}.perf-trend.log"
REPORT_FILE="${ARTIFACT_DIR}/${RUN_ID}.json"

STATUS="failed"
TRACE_SMOKE_EXIT_CODE=1
AI_CONTRACT_EXIT_CODE=1
QUERY_CONTRACT_EXIT_CODE=1
SQL_SAFETY_EXIT_CODE=1
DATA_RETENTION_EXIT_CODE=1
BACKEND_PYTEST_EXIT_CODE=1
P0P1_REGRESSION_EXIT_CODE=1
PERF_BASELINE_EXIT_CODE=1
PERF_TREND_EXIT_CODE=1
GATE_EXIT_CODE=1
TRACE_ID=""
SUMMARY="release gate failed"

if [[ -n "$BYPASS_REASON" ]]; then
  STATUS="bypassed"
  TRACE_SMOKE_EXIT_CODE=0
  AI_CONTRACT_EXIT_CODE=0
  QUERY_CONTRACT_EXIT_CODE=0
  SQL_SAFETY_EXIT_CODE=0
  DATA_RETENTION_EXIT_CODE=0
  BACKEND_PYTEST_EXIT_CODE=0
  P0P1_REGRESSION_EXIT_CODE=0
  PERF_BASELINE_EXIT_CODE=0
  PERF_TREND_EXIT_CODE=0
  GATE_EXIT_CODE=0
  SUMMARY="manual bypass: ${BYPASS_REASON}"
  printf '[BYPASS] %s\n' "$BYPASS_REASON" >"$LOG_FILE"
  printf '[BYPASS] %s\n' "$BYPASS_REASON" >"$AI_LOG_FILE"
  printf '[BYPASS] %s\n' "$BYPASS_REASON" >"$QUERY_LOG_FILE"
  printf '[BYPASS] %s\n' "$BYPASS_REASON" >"$SQL_LOG_FILE"
  printf '[BYPASS] %s\n' "$BYPASS_REASON" >"$RETENTION_LOG_FILE"
  printf '[BYPASS] %s\n' "$BYPASS_REASON" >"$BACKEND_PYTEST_LOG_FILE"
  printf '[BYPASS] %s\n' "$BYPASS_REASON" >"$P0P1_LOG_FILE"
  printf '[BYPASS] %s\n' "$BYPASS_REASON" >"$PERF_LOG_FILE"
  printf '[BYPASS] %s\n' "$BYPASS_REASON" >"$PERF_TREND_LOG_FILE"
  echo "[WARN] Release gate bypassed with reason: $BYPASS_REASON"
else
  echo "[INFO] Running trace smoke gate..."
  set +e
  NAMESPACE="$NAMESPACE" MAX_PENDING="$MAX_PENDING" "${PROJECT_ROOT}/scripts/trace-e2e-smoke.sh" >"$LOG_FILE" 2>&1
  TRACE_SMOKE_EXIT_CODE=$?
  set -e

  TRACE_ID="$(rg -n "Trace ID:" "$LOG_FILE" | head -n 1 | sed -E 's/.*Trace ID:\s*//g' | tr -d '\r' || true)"

  echo "[INFO] Running AI contract gate..."
  set +e
  NAMESPACE="$NAMESPACE" "${PROJECT_ROOT}/scripts/ai-contract-check.sh" >"$AI_LOG_FILE" 2>&1
  AI_CONTRACT_EXIT_CODE=$?
  set -e

  echo "[INFO] Running query contract gate..."
  set +e
  NAMESPACE="$NAMESPACE" "${PROJECT_ROOT}/scripts/query-contract-check.sh" >"$QUERY_LOG_FILE" 2>&1
  QUERY_CONTRACT_EXIT_CODE=$?
  set -e

  echo "[INFO] Running SQL safety gate..."
  set +e
  ARTIFACT_DIR="${PROJECT_ROOT}/reports/sql-safety" "${PROJECT_ROOT}/scripts/sql-safety-scan.sh" >"$SQL_LOG_FILE" 2>&1
  SQL_SAFETY_EXIT_CODE=$?
  set -e

  echo "[INFO] Running data retention gate..."
  set +e
  NAMESPACE="$NAMESPACE" ARTIFACT_DIR="${PROJECT_ROOT}/reports/data-retention" "${PROJECT_ROOT}/scripts/check-data-retention.sh" >"$RETENTION_LOG_FILE" 2>&1
  DATA_RETENTION_EXIT_CODE=$?
  set -e

  echo "[INFO] Running backend pytest gate..."
  set +e
  NAMESPACE="$NAMESPACE" \
  ARTIFACT_DIR="${PROJECT_ROOT}/reports/backend-pytest" \
  COVERAGE_MIN_FLOOR="$COVERAGE_MIN_FLOOR" \
  QUERY_COV_MIN="$QUERY_COV_MIN" \
  TOPOLOGY_COV_MIN="$TOPOLOGY_COV_MIN" \
  INGEST_COV_MIN="$INGEST_COV_MIN" \
  AI_COV_MIN="$AI_COV_MIN" \
  "${PROJECT_ROOT}/scripts/backend-pytest-check.sh" >"$BACKEND_PYTEST_LOG_FILE" 2>&1
  BACKEND_PYTEST_EXIT_CODE=$?
  set -e

  echo "[INFO] Running P0/P1 regression gate..."
  set +e
  NAMESPACE="$NAMESPACE" "${PROJECT_ROOT}/scripts/p0p1-regression-check.sh" >"$P0P1_LOG_FILE" 2>&1
  P0P1_REGRESSION_EXIT_CODE=$?
  set -e

  echo "[INFO] Running perf baseline gate..."
  set +e
  NAMESPACE="$NAMESPACE" "${PROJECT_ROOT}/scripts/perf-baseline-check.sh" >"$PERF_LOG_FILE" 2>&1
  PERF_BASELINE_EXIT_CODE=$?
  set -e

  echo "[INFO] Running perf trend gate..."
  set +e
  ARTIFACT_DIR="${PROJECT_ROOT}/reports/perf-trend" PERF_REPORT_DIR="${PROJECT_ROOT}/reports/perf-baseline" "${PROJECT_ROOT}/scripts/perf-trend-check.sh" >"$PERF_TREND_LOG_FILE" 2>&1
  PERF_TREND_EXIT_CODE=$?
  set -e

  if [[ "$TRACE_SMOKE_EXIT_CODE" -eq 0 && "$AI_CONTRACT_EXIT_CODE" -eq 0 && "$QUERY_CONTRACT_EXIT_CODE" -eq 0 && "$SQL_SAFETY_EXIT_CODE" -eq 0 && "$DATA_RETENTION_EXIT_CODE" -eq 0 && "$BACKEND_PYTEST_EXIT_CODE" -eq 0 && "$P0P1_REGRESSION_EXIT_CODE" -eq 0 && "$PERF_BASELINE_EXIT_CODE" -eq 0 && "$PERF_TREND_EXIT_CODE" -eq 0 ]]; then
    STATUS="passed"
    GATE_EXIT_CODE=0
    SUMMARY="trace smoke + ai contract + query contract + sql safety + data retention + backend pytest + p0p1 regression + perf baseline + perf trend passed"
    echo "[INFO] Release gate passed"
  else
    STATUS="failed"
    GATE_EXIT_CODE=1
    FAILED_CHECKS=()
    if [[ "$TRACE_SMOKE_EXIT_CODE" -ne 0 ]]; then
      FAILED_CHECKS+=("trace-smoke")
    fi
    if [[ "$AI_CONTRACT_EXIT_CODE" -ne 0 ]]; then
      FAILED_CHECKS+=("ai-contract")
    fi
    if [[ "$QUERY_CONTRACT_EXIT_CODE" -ne 0 ]]; then
      FAILED_CHECKS+=("query-contract")
    fi
    if [[ "$SQL_SAFETY_EXIT_CODE" -ne 0 ]]; then
      FAILED_CHECKS+=("sql-safety")
    fi
    if [[ "$DATA_RETENTION_EXIT_CODE" -ne 0 ]]; then
      FAILED_CHECKS+=("data-retention")
    fi
    if [[ "$BACKEND_PYTEST_EXIT_CODE" -ne 0 ]]; then
      FAILED_CHECKS+=("backend-pytest")
    fi
    if [[ "$P0P1_REGRESSION_EXIT_CODE" -ne 0 ]]; then
      FAILED_CHECKS+=("p0p1-regression")
    fi
    if [[ "$PERF_BASELINE_EXIT_CODE" -ne 0 ]]; then
      FAILED_CHECKS+=("perf-baseline")
    fi
    if [[ "$PERF_TREND_EXIT_CODE" -ne 0 ]]; then
      FAILED_CHECKS+=("perf-trend")
    fi
    SUMMARY="failed checks: ${FAILED_CHECKS[*]}, see artifact logs"
    echo "[ERROR] Release gate failed"
  fi
fi

FINISHED_AT="$(date -u +"%Y-%m-%dT%H:%M:%S.%3NZ")"
END_MS="$(date +%s%3N)"
DURATION_MS="$((END_MS - START_MS))"

REPORT_JSON=$(cat <<EOF
{
  "gate_id": "$(json_escape "$RUN_ID")",
  "candidate": "$(json_escape "$CANDIDATE")",
  "tag": "$(json_escape "$TAG")",
  "target": "$(json_escape "$TARGET")",
  "status": "$(json_escape "$STATUS")",
  "smoke_exit_code": $GATE_EXIT_CODE,
  "trace_smoke_exit_code": $TRACE_SMOKE_EXIT_CODE,
  "ai_contract_exit_code": $AI_CONTRACT_EXIT_CODE,
  "query_contract_exit_code": $QUERY_CONTRACT_EXIT_CODE,
  "sql_safety_exit_code": $SQL_SAFETY_EXIT_CODE,
  "data_retention_exit_code": $DATA_RETENTION_EXIT_CODE,
  "backend_pytest_exit_code": $BACKEND_PYTEST_EXIT_CODE,
  "p0p1_regression_exit_code": $P0P1_REGRESSION_EXIT_CODE,
  "perf_baseline_exit_code": $PERF_BASELINE_EXIT_CODE,
  "perf_trend_exit_code": $PERF_TREND_EXIT_CODE,
  "trace_id": "$(json_escape "$TRACE_ID")",
  "started_at": "$(json_escape "$STARTED_AT")",
  "finished_at": "$(json_escape "$FINISHED_AT")",
  "duration_ms": $DURATION_MS,
  "max_pending": $MAX_PENDING,
  "manual_override": $([[ -n "$BYPASS_REASON" ]] && echo "true" || echo "false"),
  "manual_override_reason": "$(json_escape "$BYPASS_REASON")",
  "coverage_thresholds": {
    "coverage_min_floor": $COVERAGE_MIN_FLOOR,
    "query_cov_min": $QUERY_COV_MIN,
    "topology_cov_min": $TOPOLOGY_COV_MIN,
    "ingest_cov_min": $INGEST_COV_MIN,
    "ai_cov_min": $AI_COV_MIN
  },
  "summary": "$(json_escape "$SUMMARY")",
  "smoke_log_file": "$(json_escape "$LOG_FILE")",
  "ai_contract_log_file": "$(json_escape "$AI_LOG_FILE")",
  "query_contract_log_file": "$(json_escape "$QUERY_LOG_FILE")",
  "sql_safety_log_file": "$(json_escape "$SQL_LOG_FILE")",
  "data_retention_log_file": "$(json_escape "$RETENTION_LOG_FILE")",
  "backend_pytest_log_file": "$(json_escape "$BACKEND_PYTEST_LOG_FILE")",
  "p0p1_regression_log_file": "$(json_escape "$P0P1_LOG_FILE")",
  "perf_baseline_log_file": "$(json_escape "$PERF_LOG_FILE")",
  "perf_trend_log_file": "$(json_escape "$PERF_TREND_LOG_FILE")"
}
EOF
)

printf '%s\n' "$REPORT_JSON" >"$REPORT_FILE"
ln -sfn "$REPORT_FILE" "${ARTIFACT_DIR}/latest.json"

echo "[INFO] Gate report: $REPORT_FILE"
echo "[INFO] Smoke log:   $LOG_FILE"
echo "[INFO] AI log:      $AI_LOG_FILE"
echo "[INFO] Query log:   $QUERY_LOG_FILE"
echo "[INFO] SQL log:     $SQL_LOG_FILE"
echo "[INFO] Retention log: $RETENTION_LOG_FILE"
echo "[INFO] Backend log: $BACKEND_PYTEST_LOG_FILE"
echo "[INFO] P0/P1 log:   $P0P1_LOG_FILE"
echo "[INFO] Perf log:    $PERF_LOG_FILE"
echo "[INFO] Trend log:   $PERF_TREND_LOG_FILE"

if ! persist_report_to_clickhouse "$RUN_ID" "$STARTED_AT" "$FINISHED_AT" "$DURATION_MS" "$STATUS" "$TRACE_ID" "$GATE_EXIT_CODE" "$REPORT_FILE" "$SUMMARY" "$TRACE_SMOKE_EXIT_CODE" "$AI_CONTRACT_EXIT_CODE" "$QUERY_CONTRACT_EXIT_CODE" "$SQL_SAFETY_EXIT_CODE" "$DATA_RETENTION_EXIT_CODE" "$BACKEND_PYTEST_EXIT_CODE" "$P0P1_REGRESSION_EXIT_CODE" "$PERF_BASELINE_EXIT_CODE" "$PERF_TREND_EXIT_CODE"; then
  echo "[WARN] Failed to persist gate report to ClickHouse"
fi

if [[ "$STATUS" == "failed" ]]; then
  if [[ "$TRACE_SMOKE_EXIT_CODE" -ne 0 ]]; then
    echo "========== trace-smoke log =========="
    cat "$LOG_FILE"
  fi
  if [[ "$AI_CONTRACT_EXIT_CODE" -ne 0 ]]; then
    echo "========== ai-contract log =========="
    cat "$AI_LOG_FILE"
  fi
  if [[ "$QUERY_CONTRACT_EXIT_CODE" -ne 0 ]]; then
    echo "========== query-contract log =========="
    cat "$QUERY_LOG_FILE"
  fi
  if [[ "$SQL_SAFETY_EXIT_CODE" -ne 0 ]]; then
    echo "========== sql-safety log =========="
    cat "$SQL_LOG_FILE"
  fi
  if [[ "$DATA_RETENTION_EXIT_CODE" -ne 0 ]]; then
    echo "========== data-retention log =========="
    cat "$RETENTION_LOG_FILE"
  fi
  if [[ "$BACKEND_PYTEST_EXIT_CODE" -ne 0 ]]; then
    echo "========== backend-pytest log =========="
    cat "$BACKEND_PYTEST_LOG_FILE"
  fi
  if [[ "$P0P1_REGRESSION_EXIT_CODE" -ne 0 ]]; then
    echo "========== p0p1-regression log =========="
    cat "$P0P1_LOG_FILE"
  fi
  if [[ "$PERF_BASELINE_EXIT_CODE" -ne 0 ]]; then
    echo "========== perf-baseline log =========="
    cat "$PERF_LOG_FILE"
  fi
  if [[ "$PERF_TREND_EXIT_CODE" -ne 0 ]]; then
    echo "========== perf-trend log =========="
    cat "$PERF_TREND_LOG_FILE"
  fi
  exit 1
fi

exit 0
