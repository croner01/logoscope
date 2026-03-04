#!/usr/bin/env bash
set -euo pipefail

# P0/P1 关键修复回归检查脚本
# 覆盖：
# 1) 注入输入防护（INTERVAL/sort 白名单）
# 2) 统一错误模型（code/message/request_id）
# 3) 异常分支与降级路径（worker stop / health degrade）
# 4) 慢查询采样日志开关行为

NAMESPACE="${NAMESPACE:-islap}"
ARTIFACT_DIR="${ARTIFACT_DIR:-/root/logoscope/reports/p0p1-regression}"
P0P1_PYTEST_EXTRA_ARGS="${P0P1_PYTEST_EXTRA_ARGS:---no-cov}"

QUERY_TESTS=(
  "tests/test_p0_p1_regression.py"
)
TOPOLOGY_TESTS=(
  "tests/test_p0_p1_regression.py"
)
SEMANTIC_TESTS=(
  "tests/test_p0_p1_regression.py"
)
AI_TESTS=(
  "tests/test_session_history_sort_whitelist.py"
)

json_escape() {
  printf '%s' "${1:-}" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

fail() {
  printf '[ERROR] %s\n' "$1" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "missing command: $1"
}

run_service_tests() {
  local pod="$1"
  local container="$2"
  local log_file="$3"
  shift 3
  local tests=("$@")
  local joined_tests
  joined_tests="$(printf "%s " "${tests[@]}")"

  kubectl -n "${NAMESPACE}" exec "${pod}" -c "${container}" -- /bin/sh -lc \
    "cd /app && pytest -q ${P0P1_PYTEST_EXTRA_ARGS} ${joined_tests}" >"${log_file}" 2>&1
}

mkdir -p "${ARTIFACT_DIR}"
require_cmd kubectl

RUN_ID="p0p1-regression-$(date -u +%Y%m%d-%H%M%S)-$RANDOM"
GENERATED_AT="$(date -u +"%Y-%m-%dT%H:%M:%S.%3NZ")"
REPORT_FILE="${ARTIFACT_DIR}/${RUN_ID}.json"
QUERY_LOG_FILE="${ARTIFACT_DIR}/${RUN_ID}.query.log"
TOPOLOGY_LOG_FILE="${ARTIFACT_DIR}/${RUN_ID}.topology.log"
SEMANTIC_LOG_FILE="${ARTIFACT_DIR}/${RUN_ID}.semantic.log"
AI_LOG_FILE="${ARTIFACT_DIR}/${RUN_ID}.ai.log"

QUERY_POD="$(kubectl -n "${NAMESPACE}" get pod -l app=query-service -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
TOPOLOGY_POD="$(kubectl -n "${NAMESPACE}" get pod -l app=topology-service -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
SEMANTIC_POD="$(kubectl -n "${NAMESPACE}" get pod -l app=semantic-engine -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
AI_POD="$(kubectl -n "${NAMESPACE}" get pod -l app=ai-service -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"

[[ -n "${QUERY_POD}" ]] || fail "query-service pod not found in namespace ${NAMESPACE}"
[[ -n "${TOPOLOGY_POD}" ]] || fail "topology-service pod not found in namespace ${NAMESPACE}"
[[ -n "${SEMANTIC_POD}" ]] || fail "semantic-engine pod not found in namespace ${NAMESPACE}"
[[ -n "${AI_POD}" ]] || fail "ai-service pod not found in namespace ${NAMESPACE}"

echo "[INFO] Running P0/P1 regression tests in query-service pod: ${QUERY_POD}"
set +e
run_service_tests "${QUERY_POD}" "query-service" "${QUERY_LOG_FILE}" "${QUERY_TESTS[@]}"
QUERY_EXIT_CODE=$?
set -e

echo "[INFO] Running P0/P1 regression tests in topology-service pod: ${TOPOLOGY_POD}"
set +e
run_service_tests "${TOPOLOGY_POD}" "topology-service" "${TOPOLOGY_LOG_FILE}" "${TOPOLOGY_TESTS[@]}"
TOPOLOGY_EXIT_CODE=$?
set -e

echo "[INFO] Running P0/P1 regression tests in semantic-engine pod: ${SEMANTIC_POD}"
set +e
run_service_tests "${SEMANTIC_POD}" "semantic-engine" "${SEMANTIC_LOG_FILE}" "${SEMANTIC_TESTS[@]}"
SEMANTIC_EXIT_CODE=$?
set -e

echo "[INFO] Running P0/P1 regression tests in ai-service pod: ${AI_POD}"
set +e
run_service_tests "${AI_POD}" "ai-service" "${AI_LOG_FILE}" "${AI_TESTS[@]}"
AI_EXIT_CODE=$?
set -e

OVERALL_EXIT_CODE=0
if [[ "${QUERY_EXIT_CODE}" -ne 0 || "${TOPOLOGY_EXIT_CODE}" -ne 0 || "${SEMANTIC_EXIT_CODE}" -ne 0 || "${AI_EXIT_CODE}" -ne 0 ]]; then
  OVERALL_EXIT_CODE=1
fi

SUMMARY="p0p1 regression passed"
if [[ "${OVERALL_EXIT_CODE}" -ne 0 ]]; then
  FAILED=()
  [[ "${QUERY_EXIT_CODE}" -ne 0 ]] && FAILED+=("query-service")
  [[ "${TOPOLOGY_EXIT_CODE}" -ne 0 ]] && FAILED+=("topology-service")
  [[ "${SEMANTIC_EXIT_CODE}" -ne 0 ]] && FAILED+=("semantic-engine")
  [[ "${AI_EXIT_CODE}" -ne 0 ]] && FAILED+=("ai-service")
  SUMMARY="failed services: ${FAILED[*]}"
fi

cat >"${REPORT_FILE}" <<EOF
{
  "run_id": "$(json_escape "${RUN_ID}")",
  "generated_at": "$(json_escape "${GENERATED_AT}")",
  "passed": $([[ "${OVERALL_EXIT_CODE}" -eq 0 ]] && echo "true" || echo "false"),
  "summary": "$(json_escape "${SUMMARY}")",
  "query_exit_code": ${QUERY_EXIT_CODE},
  "topology_exit_code": ${TOPOLOGY_EXIT_CODE},
  "semantic_exit_code": ${SEMANTIC_EXIT_CODE},
  "ai_exit_code": ${AI_EXIT_CODE},
  "query_log_file": "$(json_escape "${QUERY_LOG_FILE}")",
  "topology_log_file": "$(json_escape "${TOPOLOGY_LOG_FILE}")",
  "semantic_log_file": "$(json_escape "${SEMANTIC_LOG_FILE}")",
  "ai_log_file": "$(json_escape "${AI_LOG_FILE}")"
}
EOF

ln -sfn "${REPORT_FILE}" "${ARTIFACT_DIR}/latest.json"

echo "[INFO] P0/P1 regression report: ${REPORT_FILE}"
echo "[INFO] P0/P1 regression latest: ${ARTIFACT_DIR}/latest.json"

if [[ "${OVERALL_EXIT_CODE}" -ne 0 ]]; then
  if [[ "${QUERY_EXIT_CODE}" -ne 0 ]]; then
    echo "========== p0p1 query-service log =========="
    cat "${QUERY_LOG_FILE}"
  fi
  if [[ "${TOPOLOGY_EXIT_CODE}" -ne 0 ]]; then
    echo "========== p0p1 topology-service log =========="
    cat "${TOPOLOGY_LOG_FILE}"
  fi
  if [[ "${SEMANTIC_EXIT_CODE}" -ne 0 ]]; then
    echo "========== p0p1 semantic-engine log =========="
    cat "${SEMANTIC_LOG_FILE}"
  fi
  if [[ "${AI_EXIT_CODE}" -ne 0 ]]; then
    echo "========== p0p1 ai-service log =========="
    cat "${AI_LOG_FILE}"
  fi
  exit 1
fi

exit 0
