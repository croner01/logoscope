#!/usr/bin/env bash
set -euo pipefail

# SQL 安全静态扫描
# 目标：
# 1) 禁止 INTERVAL {time_window}/{time_range} 直拼
# 2) 禁止 API 层将高风险用户参数直接拼接为 SQL 字面量

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARTIFACT_DIR="${ARTIFACT_DIR:-${PROJECT_ROOT}/reports/sql-safety}"

TARGET_DIRS=(
  "${PROJECT_ROOT}/query-service/api"
  "${PROJECT_ROOT}/topology-service/api"
  "${PROJECT_ROOT}/ingest-service/api"
)

RUN_ID="sql-safety-$(date -u +%Y%m%d-%H%M%S)-$RANDOM"
GENERATED_AT="$(date -u +"%Y-%m-%dT%H:%M:%S.%3NZ")"
FINDINGS_FILE="${ARTIFACT_DIR}/${RUN_ID}.findings.log"
REPORT_FILE="${ARTIFACT_DIR}/${RUN_ID}.json"

json_escape() {
  printf '%s' "${1:-}" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

scan_pattern() {
  local pattern="$1"
  local title="$2"
  local matched=1

  local out
  out="$(
    rg -n --no-heading --pcre2 "$pattern" "${TARGET_DIRS[@]}" \
      --glob '!**/__pycache__/**' \
      --glob '*.py' || true
  )"

  if [[ -n "${out}" ]]; then
    matched=0
    {
      echo "[$title]"
      printf '%s\n' "${out}"
      echo
    } >>"${FINDINGS_FILE}"
  fi

  return "${matched}"
}

mkdir -p "${ARTIFACT_DIR}"
: >"${FINDINGS_FILE}"

UNSAFE_COUNT=0

# 规则 1: INTERVAL 原始 time_window/time_range 直拼
if scan_pattern 'INTERVAL\s+\{(?:time_window|time_range)\}' "UNSAFE_INTERVAL_RAW_PARAM"; then
  UNSAFE_COUNT=$((UNSAFE_COUNT + 1))
fi

# 规则 2: 高风险参数 SQL 字面量直拼（例如 service_name = '{service_name}'）
if scan_pattern "'\{(?:service_name|metric_name|trace_id|pod_name|namespace|start_time|end_time|sort_by|sort_order|order_by|time_window|time_range)\}'" "UNSAFE_LITERAL_INTERPOLATION"; then
  UNSAFE_COUNT=$((UNSAFE_COUNT + 1))
fi

# 规则 3: LIMIT 直接拼接 limit 变量（应改为占位符）
if scan_pattern 'LIMIT\s+\{limit\}' "UNSAFE_LIMIT_INTERPOLATION"; then
  UNSAFE_COUNT=$((UNSAFE_COUNT + 1))
fi

PASSED=true
SUMMARY="sql safety scan passed"
EXIT_CODE=0

if [[ "${UNSAFE_COUNT}" -gt 0 ]]; then
  PASSED=false
  SUMMARY="sql safety scan failed: ${UNSAFE_COUNT} rule(s) matched"
  EXIT_CODE=1
fi

cat >"${REPORT_FILE}" <<EOF
{
  "run_id": "$(json_escape "${RUN_ID}")",
  "generated_at": "$(json_escape "${GENERATED_AT}")",
  "passed": ${PASSED},
  "exit_code": ${EXIT_CODE},
  "matched_rule_count": ${UNSAFE_COUNT},
  "summary": "$(json_escape "${SUMMARY}")",
  "findings_file": "$(json_escape "${FINDINGS_FILE}")"
}
EOF

ln -sfn "${REPORT_FILE}" "${ARTIFACT_DIR}/latest.json"

echo "[INFO] SQL safety report: ${REPORT_FILE}"
echo "[INFO] SQL safety latest: ${ARTIFACT_DIR}/latest.json"

if [[ "${EXIT_CODE}" -ne 0 ]]; then
  echo "========== sql safety findings =========="
  cat "${FINDINGS_FILE}"
  exit "${EXIT_CODE}"
fi

exit 0
