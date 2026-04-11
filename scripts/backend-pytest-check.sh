#!/usr/bin/env bash
set -euo pipefail

# 后端统一 pytest + 覆盖率门槛检查
# 覆盖服务：
# 1) query-service
# 2) topology-service
# 3) ingest-service
# 4) ai-service

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="${NAMESPACE:-islap}"
ARTIFACT_DIR="${ARTIFACT_DIR:-${PROJECT_ROOT}/reports/backend-pytest}"

QUERY_COV_MIN="${QUERY_COV_MIN:-30}"
TOPOLOGY_COV_MIN="${TOPOLOGY_COV_MIN:-30}"
INGEST_COV_MIN="${INGEST_COV_MIN:-30}"
AI_COV_MIN="${AI_COV_MIN:-30}"
COVERAGE_MIN_FLOOR="${COVERAGE_MIN_FLOOR:-30}"

QUERY_TESTS="${QUERY_TESTS:-tests/test_topology_log_preview_routes.py tests/test_query_contract_routes.py tests/test_trace_lite_routes.py tests/test_trace_lite_inference.py tests/test_query_inference_service.py tests/test_data_quality_routes.py tests/test_value_kpi_routes.py tests/test_value_kpi_service.py tests/test_query_observability_service.py tests/test_query_logs_service.py tests/test_p0_p1_regression.py tests/test_query_params_helpers.py}"
TOPOLOGY_TESTS="${TOPOLOGY_TESTS:-tests/test_topology_core_routes_contract.py tests/test_topology_problem_summary_routes.py tests/test_realtime_topology_cache.py tests/test_p0_p1_regression.py tests/test_hybrid_topology_utils.py}"
INGEST_TESTS="${INGEST_TESTS:-tests/test_active_path_ingest.py tests/test_trace_processor.py}"
AI_TESTS="${AI_TESTS:-tests/test_ai_api.py tests/test_ai_analyzer.py tests/test_llm_service.py tests/test_session_history_sort_whitelist.py tests/test_similar_cases.py}"

json_escape() {
  printf '%s' "${1:-}" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

is_non_negative_int() {
  [[ "${1:-}" =~ ^[0-9]+$ ]]
}

normalize_cov_numeric() {
  local raw="${1:-}"
  if [[ "${raw}" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    printf '%s' "${raw}"
  else
    printf '0'
  fi
}

validate_cov_threshold() {
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

extract_total_coverage() {
  local log_file="$1"
  local parsed
  parsed="$(sed -n 's/.*Total coverage: \([0-9][0-9]*\(\.[0-9][0-9]*\)\?\)%.*/\1/p' "${log_file}" | tail -n 1)"
  if [[ -z "${parsed}" ]]; then
    parsed="$(awk '/TOTAL/ && /%/ {gsub("%", "", $NF); cov=$NF} END {if (cov == "") cov="0"; print cov}' "${log_file}")"
  fi
  normalize_cov_numeric "${parsed}"
}

run_local_service() {
  local service="$1"
  local cov_min="$2"
  local log_file="$3"
  local tests="$4"
  local python_bin
  python_bin="$(resolve_python_bin "${service}")"
  (
    cd "${PROJECT_ROOT}/${service}"
    "${python_bin}" -m pytest -q ${tests} --cov-fail-under="${cov_min}" >"${log_file}" 2>&1
  )
}

resolve_python_bin() {
  local service="$1"
  local service_venv_python="${PROJECT_ROOT}/${service}/venv/bin/python"
  if [[ -x "${service_venv_python}" ]]; then
    echo "${service_venv_python}"
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    echo "python3"
    return 0
  fi
  if command -v python >/dev/null 2>&1; then
    echo "python"
    return 0
  fi
  echo "[ERROR] No python interpreter available for ${service}" >&2
  return 1
}

run_pod_service() {
  local service="$1"
  local container="$2"
  local cov_min="$3"
  local log_file="$4"
  local tests="$5"
  local pod

  pod="$(kubectl -n "${NAMESPACE}" get pod -l app="${service}" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  if [[ -z "${pod}" ]]; then
    echo "[ERROR] ${service} pod not found in namespace ${NAMESPACE}" >"${log_file}"
    return 1
  fi

  kubectl -n "${NAMESPACE}" exec "${pod}" -c "${container}" -- /bin/sh -lc \
    "cd /app && pytest -q ${tests} --cov-fail-under=${cov_min}" >"${log_file}" 2>&1
}

local_env_ready() {
  local service="$1"
  local python_bin
  python_bin="$(resolve_python_bin "${service}")" || return 1
  "${python_bin}" - <<'PY' >/dev/null 2>&1
import importlib.util
import sys
required = ("pytest", "fastapi")
missing = [name for name in required if importlib.util.find_spec(name) is None]
sys.exit(0 if not missing else 1)
PY
}

if ! is_non_negative_int "${COVERAGE_MIN_FLOOR}"; then
  echo "[ERROR] COVERAGE_MIN_FLOOR must be a non-negative integer, got: ${COVERAGE_MIN_FLOOR}" >&2
  exit 1
fi

validate_cov_threshold "QUERY_COV_MIN" "${QUERY_COV_MIN}"
validate_cov_threshold "TOPOLOGY_COV_MIN" "${TOPOLOGY_COV_MIN}"
validate_cov_threshold "INGEST_COV_MIN" "${INGEST_COV_MIN}"
validate_cov_threshold "AI_COV_MIN" "${AI_COV_MIN}"

mkdir -p "${ARTIFACT_DIR}"

RUN_ID="backend-pytest-$(date -u +%Y%m%d-%H%M%S)-$RANDOM"
GENERATED_AT="$(date -u +"%Y-%m-%dT%H:%M:%S.%3NZ")"
REPORT_FILE="${ARTIFACT_DIR}/${RUN_ID}.json"
QUERY_LOG_FILE="${ARTIFACT_DIR}/${RUN_ID}.query.log"
TOPOLOGY_LOG_FILE="${ARTIFACT_DIR}/${RUN_ID}.topology.log"
INGEST_LOG_FILE="${ARTIFACT_DIR}/${RUN_ID}.ingest.log"
AI_LOG_FILE="${ARTIFACT_DIR}/${RUN_ID}.ai.log"

MODE="unknown"
if local_env_ready "query-service" \
  && local_env_ready "topology-service" \
  && local_env_ready "ingest-service" \
  && local_env_ready "ai-service"; then
  MODE="local"
elif command -v kubectl >/dev/null 2>&1; then
  MODE="pod"
else
  echo "[ERROR] Neither local python test environment nor kubectl is available." >&2
  exit 1
fi

set +e
if [[ "${MODE}" == "pod" ]]; then
  run_pod_service "query-service" "query-service" "${QUERY_COV_MIN}" "${QUERY_LOG_FILE}" "${QUERY_TESTS}"
  QUERY_EXIT_CODE=$?
  run_pod_service "topology-service" "topology-service" "${TOPOLOGY_COV_MIN}" "${TOPOLOGY_LOG_FILE}" "${TOPOLOGY_TESTS}"
  TOPOLOGY_EXIT_CODE=$?
  run_pod_service "ingest-service" "ingest-service" "${INGEST_COV_MIN}" "${INGEST_LOG_FILE}" "${INGEST_TESTS}"
  INGEST_EXIT_CODE=$?
  run_pod_service "ai-service" "ai-service" "${AI_COV_MIN}" "${AI_LOG_FILE}" "${AI_TESTS}"
  AI_EXIT_CODE=$?
else
  run_local_service "query-service" "${QUERY_COV_MIN}" "${QUERY_LOG_FILE}" "${QUERY_TESTS}"
  QUERY_EXIT_CODE=$?
  run_local_service "topology-service" "${TOPOLOGY_COV_MIN}" "${TOPOLOGY_LOG_FILE}" "${TOPOLOGY_TESTS}"
  TOPOLOGY_EXIT_CODE=$?
  run_local_service "ingest-service" "${INGEST_COV_MIN}" "${INGEST_LOG_FILE}" "${INGEST_TESTS}"
  INGEST_EXIT_CODE=$?
  run_local_service "ai-service" "${AI_COV_MIN}" "${AI_LOG_FILE}" "${AI_TESTS}"
  AI_EXIT_CODE=$?
fi
set -e

QUERY_COV_ACTUAL="$(extract_total_coverage "${QUERY_LOG_FILE}")"
TOPOLOGY_COV_ACTUAL="$(extract_total_coverage "${TOPOLOGY_LOG_FILE}")"
INGEST_COV_ACTUAL="$(extract_total_coverage "${INGEST_LOG_FILE}")"
AI_COV_ACTUAL="$(extract_total_coverage "${AI_LOG_FILE}")"

OVERALL_EXIT_CODE=0
if [[ "${QUERY_EXIT_CODE}" -ne 0 || "${TOPOLOGY_EXIT_CODE}" -ne 0 || "${INGEST_EXIT_CODE}" -ne 0 || "${AI_EXIT_CODE}" -ne 0 ]]; then
  OVERALL_EXIT_CODE=1
fi

SUMMARY="backend pytest gate passed"
if [[ "${OVERALL_EXIT_CODE}" -ne 0 ]]; then
  FAILED=()
  [[ "${QUERY_EXIT_CODE}" -ne 0 ]] && FAILED+=("query-service")
  [[ "${TOPOLOGY_EXIT_CODE}" -ne 0 ]] && FAILED+=("topology-service")
  [[ "${INGEST_EXIT_CODE}" -ne 0 ]] && FAILED+=("ingest-service")
  [[ "${AI_EXIT_CODE}" -ne 0 ]] && FAILED+=("ai-service")
  SUMMARY="failed services: ${FAILED[*]}"
fi

cat >"${REPORT_FILE}" <<EOF
{
  "run_id": "$(json_escape "${RUN_ID}")",
  "generated_at": "$(json_escape "${GENERATED_AT}")",
  "mode": "$(json_escape "${MODE}")",
  "passed": $([[ "${OVERALL_EXIT_CODE}" -eq 0 ]] && echo "true" || echo "false"),
  "summary": "$(json_escape "${SUMMARY}")",
  "thresholds": {
    "coverage_min_floor": ${COVERAGE_MIN_FLOOR},
    "query_cov_min": ${QUERY_COV_MIN},
    "topology_cov_min": ${TOPOLOGY_COV_MIN},
    "ingest_cov_min": ${INGEST_COV_MIN},
    "ai_cov_min": ${AI_COV_MIN}
  },
  "actuals": {
    "query_cov_percent": ${QUERY_COV_ACTUAL},
    "topology_cov_percent": ${TOPOLOGY_COV_ACTUAL},
    "ingest_cov_percent": ${INGEST_COV_ACTUAL},
    "ai_cov_percent": ${AI_COV_ACTUAL}
  },
  "query_exit_code": ${QUERY_EXIT_CODE},
  "topology_exit_code": ${TOPOLOGY_EXIT_CODE},
  "ingest_exit_code": ${INGEST_EXIT_CODE},
  "ai_exit_code": ${AI_EXIT_CODE},
  "query_log_file": "$(json_escape "${QUERY_LOG_FILE}")",
  "topology_log_file": "$(json_escape "${TOPOLOGY_LOG_FILE}")",
  "ingest_log_file": "$(json_escape "${INGEST_LOG_FILE}")",
  "ai_log_file": "$(json_escape "${AI_LOG_FILE}")"
}
EOF

ln -sfn "${REPORT_FILE}" "${ARTIFACT_DIR}/latest.json"

echo "[INFO] Backend pytest report: ${REPORT_FILE}"
echo "[INFO] Backend pytest latest: ${ARTIFACT_DIR}/latest.json"

if [[ "${OVERALL_EXIT_CODE}" -ne 0 ]]; then
  if [[ "${QUERY_EXIT_CODE}" -ne 0 ]]; then
    echo "========== backend pytest query-service log =========="
    cat "${QUERY_LOG_FILE}"
  fi
  if [[ "${TOPOLOGY_EXIT_CODE}" -ne 0 ]]; then
    echo "========== backend pytest topology-service log =========="
    cat "${TOPOLOGY_LOG_FILE}"
  fi
  if [[ "${INGEST_EXIT_CODE}" -ne 0 ]]; then
    echo "========== backend pytest ingest-service log =========="
    cat "${INGEST_LOG_FILE}"
  fi
  if [[ "${AI_EXIT_CODE}" -ne 0 ]]; then
    echo "========== backend pytest ai-service log =========="
    cat "${AI_LOG_FILE}"
  fi
  exit 1
fi

exit 0
