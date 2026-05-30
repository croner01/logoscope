#!/usr/bin/env bash
set -euo pipefail

# P0/P1 关键修复回归检查脚本
# 覆盖：
# 1) 注入输入防护（INTERVAL/sort 白名单）
# 2) 统一错误模型（code/message/request_id）
# 3) 异常分支与降级路径（worker stop / health degrade）
# 4) 慢查询采样日志开关行为

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
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
  local exit_code=0

  run_service_tests_in_pod "${pod}" "${container}" "${log_file}" "${tests[@]}" || exit_code=$?
  if [[ "${exit_code}" -eq 0 ]]; then
    return 0
  fi

  if grep -qiE "pytest: not found|No module named pytest|pytest unavailable in container" "${log_file}"; then
    {
      echo
      echo "[WARN] pytest unavailable in ${container} pod, fallback to in-pod runtime checks"
    } >>"${log_file}"
    run_service_runtime_fallback_in_pod "${pod}" "${container}" "${log_file}"
    return $?
  fi

  return "${exit_code}"
}

run_service_tests_in_pod() {
  local pod="$1"
  local container="$2"
  local log_file="$3"
  shift 3
  local tests=("$@")
  local joined_tests
  joined_tests="$(printf "%s " "${tests[@]}")"

  kubectl -n "${NAMESPACE}" exec "${pod}" -c "${container}" -- /bin/sh -lc \
    "cd /app && if command -v pytest >/dev/null 2>&1; then pytest -q ${P0P1_PYTEST_EXTRA_ARGS} ${joined_tests}; elif python -c 'import pytest' >/dev/null 2>&1; then python -m pytest -q ${P0P1_PYTEST_EXTRA_ARGS} ${joined_tests}; else echo 'pytest unavailable in container'; exit 127; fi" >"${log_file}" 2>&1
}

run_service_runtime_fallback_in_pod() {
  local pod="$1"
  local container="$2"
  local log_file="$3"

  case "${container}" in
    query-service)
      kubectl -n "${NAMESPACE}" exec -i "${pod}" -c "${container}" -- python - <<'PY' >>"${log_file}" 2>&1
import asyncio
import json
import sys

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

sys.path.insert(0, '/app')

import main as query_main
from storage import adapter as query_storage_adapter


def _build_request(path='/test', headers=None, request_id=None):
    raw_headers = []
    for key, value in (headers or {}).items():
        raw_headers.append((str(key).lower().encode('utf-8'), str(value).encode('utf-8')))
    scope = {
        'type': 'http',
        'http_version': '1.1',
        'method': 'GET',
        'scheme': 'http',
        'path': path,
        'raw_path': path.encode('utf-8'),
        'query_string': b'',
        'headers': raw_headers,
        'client': ('127.0.0.1', 12345),
        'server': ('testserver', 80),
    }
    request = Request(scope)
    if request_id is not None:
        request.state.request_id = request_id
    return request


async def _run():
    request = _build_request(headers={'X-Request-ID': 'rid-query-inline'})

    async def _call_next(_request):
        return Response(content='ok', media_type='text/plain')

    response = await query_main.request_id_middleware(request, _call_next)
    assert request.state.request_id == 'rid-query-inline'
    assert response.headers.get('X-Request-ID') == 'rid-query-inline'

    err_req = _build_request(request_id='rid-query-500')
    err_resp = await query_main.http_exception_handler(
        err_req,
        HTTPException(status_code=500, detail='credential leaked'),
    )
    payload = json.loads(err_resp.body.decode('utf-8'))
    assert payload.get('code') == 'INTERNAL_SERVER_ERROR'
    assert payload.get('detail') == 'Internal server error'
    assert payload.get('request_id') == 'rid-query-500'

    safe = query_storage_adapter._sanitize_interval('1 HOUR; DROP TABLE logs.logs --', default_value='7 DAY')
    assert safe == '7 DAY'


asyncio.run(_run())
print('query-service runtime fallback checks passed')
PY
      ;;
    topology-service)
      kubectl -n "${NAMESPACE}" exec -i "${pod}" -c "${container}" -- python - <<'PY' >>"${log_file}" 2>&1
import asyncio
import json
import sys

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

sys.path.insert(0, '/app')

import main as topology_main
from storage import adapter as topology_storage_adapter


def _build_request(path='/test', headers=None, request_id=None):
    raw_headers = []
    for key, value in (headers or {}).items():
        raw_headers.append((str(key).lower().encode('utf-8'), str(value).encode('utf-8')))
    scope = {
        'type': 'http',
        'http_version': '1.1',
        'method': 'GET',
        'scheme': 'http',
        'path': path,
        'raw_path': path.encode('utf-8'),
        'query_string': b'',
        'headers': raw_headers,
        'client': ('127.0.0.1', 12345),
        'server': ('testserver', 80),
    }
    request = Request(scope)
    if request_id is not None:
        request.state.request_id = request_id
    return request


async def _run():
    request = _build_request(headers={'X-Request-ID': 'rid-topology-inline'})

    async def _call_next(_request):
        return Response(content='ok', media_type='text/plain')

    response = await topology_main.request_id_middleware(request, _call_next)
    assert request.state.request_id == 'rid-topology-inline'
    assert response.headers.get('X-Request-ID') == 'rid-topology-inline'

    err_req = _build_request(request_id='rid-topology-500')
    err_resp = await topology_main.http_exception_handler(
        err_req,
        HTTPException(status_code=500, detail='internal detail leaked'),
    )
    payload = json.loads(err_resp.body.decode('utf-8'))
    assert payload.get('code') == 'INTERNAL_SERVER_ERROR'
    assert payload.get('detail') == 'Internal server error'
    assert payload.get('request_id') == 'rid-topology-500'

    safe = topology_storage_adapter._sanitize_interval('1 HOUR; DROP TABLE logs.traces --', default_value='1 HOUR')
    assert safe == '1 HOUR'


asyncio.run(_run())
print('topology-service runtime fallback checks passed')
PY
      ;;
    semantic-engine)
      kubectl -n "${NAMESPACE}" exec -i "${pod}" -c "${container}" -- python - <<'PY' >>"${log_file}" 2>&1
import asyncio
import json
import sys
from unittest.mock import AsyncMock, Mock

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

sys.path.insert(0, '/app')

import main as semantic_main
from msgqueue.worker import LogWorker
from storage import adapter as semantic_storage_adapter


def _build_request(path='/test', headers=None, request_id=None):
    raw_headers = []
    for key, value in (headers or {}).items():
        raw_headers.append((str(key).lower().encode('utf-8'), str(value).encode('utf-8')))
    scope = {
        'type': 'http',
        'http_version': '1.1',
        'method': 'GET',
        'scheme': 'http',
        'path': path,
        'raw_path': path.encode('utf-8'),
        'query_string': b'',
        'headers': raw_headers,
        'client': ('127.0.0.1', 12345),
        'server': ('testserver', 80),
    }
    request = Request(scope)
    if request_id is not None:
        request.state.request_id = request_id
    return request


async def _run():
    request = _build_request(headers={'X-Request-ID': 'rid-semantic-inline'})

    async def _call_next(_request):
        return Response(content='ok', media_type='text/plain')

    response = await semantic_main.request_id_middleware(request, _call_next)
    assert request.state.request_id == 'rid-semantic-inline'
    assert response.headers.get('X-Request-ID') == 'rid-semantic-inline'

    err_req = _build_request(request_id='rid-semantic-500')
    err_resp = await semantic_main.http_exception_handler(
        err_req,
        HTTPException(status_code=500, detail='stack leaked'),
    )
    payload = json.loads(err_resp.body.decode('utf-8'))
    assert payload.get('code') == 'INTERNAL_SERVER_ERROR'
    assert payload.get('detail') == 'Internal server error'
    assert payload.get('request_id') == 'rid-semantic-500'

    safe = semantic_storage_adapter._sanitize_interval('1 HOUR; DROP TABLE logs.logs --', default_value='1 HOUR')
    assert safe == '1 HOUR'

    worker = LogWorker()
    worker.running = True
    worker.queue = Mock()
    worker.queue.close = AsyncMock()
    worker.storage = Mock()
    worker.storage.close = Mock()
    worker.log_writer = Mock()
    worker.log_writer.stop = Mock()
    worker.log_writer.get_stats = Mock(return_value={'buffer_size': 0, 'total_rows': 0})

    await worker.stop()
    await worker.stop()

    worker.queue.close.assert_awaited_once()
    worker.storage.close.assert_called_once()
    worker.log_writer.stop.assert_called_once()
    assert worker.running is False


asyncio.run(_run())
print('semantic-engine runtime fallback checks passed')
PY
      ;;
    ai-service)
      kubectl -n "${NAMESPACE}" exec -i "${pod}" -c "${container}" -- python - <<'PY' >>"${log_file}" 2>&1
import sys

sys.path.insert(0, '/app')

from ai.session_history import AISessionStore


class _FakeCHClient:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return []


class _FakeStorage:
    def __init__(self):
        self.ch_client = _FakeCHClient()
        self.ch_database = 'logs'
        self.config = {'clickhouse': {'database': 'logs'}}


storage = _FakeStorage()
store = AISessionStore(storage_adapter=storage)
store.list_sessions(
    limit=5,
    offset=0,
    sort_by='updated_at DESC; DROP TABLE logs.ai_analysis_sessions --',
    sort_order='asc; DROP TABLE logs.logs --',
    pinned_first=True,
)
sql = ' '.join(str(storage.ch_client.calls[-1][0]).split())
assert 'DROP TABLE' not in sql.upper()
assert 'ORDER BY is_pinned DESC, updated_at DESC, session_id DESC' in sql
print('ai-service runtime fallback checks passed')
PY
      ;;
    *)
      echo "[ERROR] unsupported container fallback: ${container}" >>"${log_file}"
      return 2
      ;;
  esac
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
FRONTEND_E2E_LOG_FILE="${ARTIFACT_DIR}/${RUN_ID}.frontend-e2e.log"

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

echo "[INFO] Running frontend topology/logs(or)/thought e2e gate..."
set +e
NAMESPACE="${NAMESPACE}" "${PROJECT_ROOT}/scripts/frontend-topology-or-thought-e2e-check.sh" >"${FRONTEND_E2E_LOG_FILE}" 2>&1
FRONTEND_E2E_EXIT_CODE=$?
set -e

OVERALL_EXIT_CODE=0
if [[ "${QUERY_EXIT_CODE}" -ne 0 || "${TOPOLOGY_EXIT_CODE}" -ne 0 || "${SEMANTIC_EXIT_CODE}" -ne 0 || "${AI_EXIT_CODE}" -ne 0 || "${FRONTEND_E2E_EXIT_CODE}" -ne 0 ]]; then
  OVERALL_EXIT_CODE=1
fi

SUMMARY="p0p1 regression passed"
if [[ "${OVERALL_EXIT_CODE}" -ne 0 ]]; then
  FAILED=()
  [[ "${QUERY_EXIT_CODE}" -ne 0 ]] && FAILED+=("query-service")
  [[ "${TOPOLOGY_EXIT_CODE}" -ne 0 ]] && FAILED+=("topology-service")
  [[ "${SEMANTIC_EXIT_CODE}" -ne 0 ]] && FAILED+=("semantic-engine")
  [[ "${AI_EXIT_CODE}" -ne 0 ]] && FAILED+=("ai-service")
  [[ "${FRONTEND_E2E_EXIT_CODE}" -ne 0 ]] && FAILED+=("frontend-topology-or-thought-e2e")
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
  "frontend_e2e_exit_code": ${FRONTEND_E2E_EXIT_CODE},
  "query_log_file": "$(json_escape "${QUERY_LOG_FILE}")",
  "topology_log_file": "$(json_escape "${TOPOLOGY_LOG_FILE}")",
  "semantic_log_file": "$(json_escape "${SEMANTIC_LOG_FILE}")",
  "ai_log_file": "$(json_escape "${AI_LOG_FILE}")",
  "frontend_e2e_log_file": "$(json_escape "${FRONTEND_E2E_LOG_FILE}")"
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
  if [[ "${FRONTEND_E2E_EXIT_CODE}" -ne 0 ]]; then
    echo "========== p0p1 frontend e2e log =========="
    cat "${FRONTEND_E2E_LOG_FILE}"
  fi
  exit 1
fi

exit 0
