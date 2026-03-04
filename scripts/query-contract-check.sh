#!/usr/bin/env bash
set -euo pipefail

# Query-service contract test runner (QS-04)
# Priority:
# 1) local python -m pytest (when dependencies are ready)
# 2) kubectl exec query-service pod pytest

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="${NAMESPACE:-islap}"
ARTIFACT_DIR="${ARTIFACT_DIR:-${PROJECT_ROOT}/reports/query-contract}"
TARGET_TESTS=(
  "tests/test_topology_log_preview_routes.py"
  "tests/test_query_contract_routes.py"
  "tests/test_trace_lite_routes.py"
  "tests/test_value_kpi_routes.py"
)

json_escape() {
  printf '%s' "${1:-}" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

run_local() {
  echo "[INFO] Running query contract tests locally..."
  (
    cd "${PROJECT_ROOT}/query-service"
    python3 -m pytest -q "${TARGET_TESTS[@]}"
  )
}

run_in_pod() {
  echo "[INFO] Running query contract tests in query-service pod..."
  local pod
  pod="$(kubectl -n "${NAMESPACE}" get pod -l app=query-service -o jsonpath='{.items[0].metadata.name}')"
  if [[ -z "${pod}" ]]; then
    echo "[ERROR] query-service pod not found in namespace ${NAMESPACE}" >&2
    exit 1
  fi

  local joined_tests
  joined_tests="$(printf "%s " "${TARGET_TESTS[@]}")"
  kubectl -n "${NAMESPACE}" exec "${pod}" -c query-service -- /bin/sh -lc "cd /app && pytest -q ${joined_tests}"
}

mkdir -p "${ARTIFACT_DIR}"

RUN_ID="query-contract-$(date -u +%Y%m%d-%H%M%S)-$RANDOM"
LOG_FILE="${ARTIFACT_DIR}/${RUN_ID}.log"
REPORT_FILE="${ARTIFACT_DIR}/${RUN_ID}.json"
GENERATED_AT="$(date -u +"%Y-%m-%dT%H:%M:%S.%3NZ")"
MODE="unknown"

local_ready=false
if command -v python3 >/dev/null 2>&1; then
  if python3 - <<'PY' >/dev/null 2>&1
import importlib.util
import sys
required = ("pytest", "fastapi")
missing = [name for name in required if importlib.util.find_spec(name) is None]
sys.exit(0 if not missing else 1)
PY
  then
    local_ready=true
  fi
fi

if [[ "${local_ready}" == "true" ]]; then
  MODE="local"
elif command -v kubectl >/dev/null 2>&1; then
  MODE="pod"
else
  echo "[ERROR] Neither local python test environment nor kubectl is available." >&2
  exit 1
fi

set +e
if [[ "${MODE}" == "local" ]]; then
  run_local 2>&1 | tee "${LOG_FILE}"
  EXIT_CODE=${PIPESTATUS[0]}
else
  run_in_pod 2>&1 | tee "${LOG_FILE}"
  EXIT_CODE=${PIPESTATUS[0]}
fi
set -e

SUMMARY_LINE="$(rg -n "=+ .* in .* =+" "${LOG_FILE}" | tail -n 1 | sed -E 's/^[0-9]+://g' | tr -d '\r' || true)"
if [[ -z "${SUMMARY_LINE}" ]]; then
  SUMMARY_LINE="$(rg -n "passed in|failed in|errors? in|error in" "${LOG_FILE}" | tail -n 1 | sed -E 's/^[0-9]+://g' | tr -d '\r' || true)"
fi
PASSED_COUNT="$(rg -o "[0-9]+ passed" "${LOG_FILE}" | tail -n 1 | awk '{print $1}' || true)"
FAILED_COUNT="$(rg -o "[0-9]+ failed" "${LOG_FILE}" | tail -n 1 | awk '{print $1}' || true)"
ERROR_COUNT="$(rg -o "[0-9]+ error" "${LOG_FILE}" | tail -n 1 | awk '{print $1}' || true)"
SKIPPED_COUNT="$(rg -o "[0-9]+ skipped" "${LOG_FILE}" | tail -n 1 | awk '{print $1}' || true)"

PASSED_COUNT="${PASSED_COUNT:-0}"
FAILED_COUNT="${FAILED_COUNT:-0}"
ERROR_COUNT="${ERROR_COUNT:-0}"
SKIPPED_COUNT="${SKIPPED_COUNT:-0}"

cat >"${REPORT_FILE}" <<EOF
{
  "run_id": "$(json_escape "${RUN_ID}")",
  "generated_at": "$(json_escape "${GENERATED_AT}")",
  "mode": "$(json_escape "${MODE}")",
  "passed": $([[ "${EXIT_CODE}" -eq 0 ]] && echo "true" || echo "false"),
  "exit_code": ${EXIT_CODE},
  "passed_count": ${PASSED_COUNT},
  "failed_count": ${FAILED_COUNT},
  "error_count": ${ERROR_COUNT},
  "skipped_count": ${SKIPPED_COUNT},
  "summary": "$(json_escape "${SUMMARY_LINE}")",
  "log_file": "$(json_escape "${LOG_FILE}")"
}
EOF

ln -sfn "${REPORT_FILE}" "${ARTIFACT_DIR}/latest.json"

echo "[INFO] Query contract report: ${REPORT_FILE}"
echo "[INFO] Query contract latest: ${ARTIFACT_DIR}/latest.json"

if [[ "${EXIT_CODE}" -ne 0 ]]; then
  exit "${EXIT_CODE}"
fi
