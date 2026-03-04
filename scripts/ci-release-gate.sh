#!/usr/bin/env bash
set -euo pipefail

# CI 标准入口：
# 1) 统一封装 release-gate 必要参数（含覆盖率硬门槛）
# 2) 支持在同一流水线可选触发每周提阈检查

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="${NAMESPACE:-islap}"

CANDIDATE="${CANDIDATE:-ci-${CI_PIPELINE_ID:-manual}-${CI_COMMIT_SHORT_SHA:-local}}"
TAG="${TAG:-${CI_COMMIT_TAG:-${CI_COMMIT_SHORT_SHA:-unknown}}}"
TARGET="${TARGET:-all}"
MAX_PENDING="${MAX_PENDING:-0}"
ARTIFACT_DIR="${ARTIFACT_DIR:-${PROJECT_ROOT}/reports/release-gate-ci}"

COVERAGE_MIN_FLOOR="${COVERAGE_MIN_FLOOR:-30}"
QUERY_COV_MIN="${QUERY_COV_MIN:-30}"
TOPOLOGY_COV_MIN="${TOPOLOGY_COV_MIN:-30}"
INGEST_COV_MIN="${INGEST_COV_MIN:-30}"
AI_COV_MIN="${AI_COV_MIN:-30}"

RUN_WEEKLY_THRESHOLD_CHECK="${RUN_WEEKLY_THRESHOLD_CHECK:-false}"
WEEKLY_FAIL_ON_RAISE_GAP="${WEEKLY_FAIL_ON_RAISE_GAP:-true}"
WEEKLY_ARTIFACT_DIR="${WEEKLY_ARTIFACT_DIR:-${PROJECT_ROOT}/reports/coverage-threshold-weekly}"

as_bool() {
  case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

mkdir -p "${ARTIFACT_DIR}"

echo "[INFO] Running CI release gate..."
echo "[INFO] candidate=${CANDIDATE} tag=${TAG} target=${TARGET} namespace=${NAMESPACE}"
echo "[INFO] coverage floor=${COVERAGE_MIN_FLOOR}, query=${QUERY_COV_MIN}, topology=${TOPOLOGY_COV_MIN}, ingest=${INGEST_COV_MIN}, ai=${AI_COV_MIN}"

NAMESPACE="${NAMESPACE}" \
"${PROJECT_ROOT}/scripts/release-gate.sh" \
  --candidate "${CANDIDATE}" \
  --tag "${TAG}" \
  --target "${TARGET}" \
  --artifact-dir "${ARTIFACT_DIR}" \
  --max-pending "${MAX_PENDING}" \
  --coverage-min-floor "${COVERAGE_MIN_FLOOR}" \
  --query-cov-min "${QUERY_COV_MIN}" \
  --topology-cov-min "${TOPOLOGY_COV_MIN}" \
  --ingest-cov-min "${INGEST_COV_MIN}" \
  --ai-cov-min "${AI_COV_MIN}"

if as_bool "${RUN_WEEKLY_THRESHOLD_CHECK}"; then
  echo "[INFO] Running weekly coverage threshold check..."
  FAIL_ON_RAISE_GAP="${WEEKLY_FAIL_ON_RAISE_GAP}" \
  ARTIFACT_DIR="${WEEKLY_ARTIFACT_DIR}" \
  "${PROJECT_ROOT}/scripts/coverage-threshold-weekly-check.sh"
fi

echo "[INFO] CI release gate completed"

