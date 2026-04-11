#!/usr/bin/env bash
set -euo pipefail

# Query-service contract check runner (QS-04)
# Pod-native mode:
# - run HTTP contract checks inside query-service pod (python stdlib only)

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="${NAMESPACE:-islap}"
ARTIFACT_DIR="${ARTIFACT_DIR:-${PROJECT_ROOT}/reports/query-contract}"
TIME_WINDOW="${TIME_WINDOW:-1 HOUR}"
CONFIDENCE_THRESHOLD="${CONFIDENCE_THRESHOLD:-0.3}"

json_escape() {
  printf '%s' "${1:-}" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

run_in_pod() {
  echo "[INFO] Running query contract checks in query-service pod..."
  local pod
  pod="$(
    kubectl -n "${NAMESPACE}" get pod -l app=query-service --sort-by=.metadata.creationTimestamp -o name \
      | tail -n 1 \
      | cut -d/ -f2
  )"
  if [[ -z "${pod}" ]]; then
    echo "[ERROR] query-service pod not found in namespace ${NAMESPACE}" >&2
    exit 1
  fi

  kubectl -n "${NAMESPACE}" exec "${pod}" -c query-service -- /bin/sh -lc "
TIME_WINDOW='${TIME_WINDOW}' \
CONFIDENCE_THRESHOLD='${CONFIDENCE_THRESHOLD}' \
python - <<'PY'
import json
import os
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone


def http_json(method, url, params=None, data=None, timeout=30):
    if params:
        query = urllib.parse.urlencode(params)
        url = f\"{url}{'&' if '?' in url else '?'}{query}\"

    body = None
    headers = {}
    if data is not None:
        body = json.dumps(data).encode('utf-8')
        headers['Content-Type'] = 'application/json'

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode('utf-8', errors='ignore')
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='ignore')
        raise RuntimeError(f\"http error: {exc.code} {url} detail={detail}\")
    except urllib.error.URLError as exc:
        raise RuntimeError(f\"url error: {url} reason={exc}\")


def to_float(value, default=0.0):
    try:
        if value is None or value == '':
            return float(default)
        return float(value)
    except Exception:
        return float(default)


query_base = 'http://127.0.0.1:8002/api/v1'
topology_base = 'http://topology-service:8003/api/v1/topology'
time_window = os.getenv('TIME_WINDOW', '1 HOUR')
confidence_threshold = os.getenv('CONFIDENCE_THRESHOLD', '0.3')

cases = []


def record_case(case_id, passed, detail):
    cases.append({
        'id': case_id,
        'passed': bool(passed),
        'detail': detail,
    })
    if passed:
        print(f'[INFO] PASS {case_id}')
    else:
        print(f'[ERROR] FAIL {case_id}: {detail}')


candidate_windows = []
for item in [time_window, '6 HOUR', '24 HOUR', '7 DAY']:
    normalized = str(item or '').strip().upper()
    if normalized and normalized not in candidate_windows:
        candidate_windows.append(normalized)

window_attempts = []
selected_edge = None
selected_preview = None
selected_logs = None
selected_window = time_window

for candidate_window in candidate_windows:
    topology = http_json(
        'GET',
        f'{topology_base}/hybrid',
        params={'time_window': candidate_window, 'confidence_threshold': confidence_threshold},
    )
    edges = topology.get('edges') or []
    window_attempts.append({'time_window': candidate_window, 'edge_count': len(edges)})
    if not edges:
        continue

    ranked = []
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        metrics = edge.get('metrics') if isinstance(edge.get('metrics'), dict) else {}
        source = str(edge.get('source_service') or metrics.get('source_service') or edge.get('source') or '').strip()
        target = str(edge.get('target_service') or metrics.get('target_service') or edge.get('target') or '').strip()
        if not source or not target:
            continue
        source_namespace = str(edge.get('source_namespace') or metrics.get('source_namespace') or '').strip()
        target_namespace = str(edge.get('target_namespace') or metrics.get('target_namespace') or '').strip()
        issue_score = to_float(
            ((edge.get('problem_summary') or {}).get('issue_score')
             if isinstance(edge.get('problem_summary'), dict) else None),
            0.0,
        )
        ranked.append({
            'edge': edge,
            'source_service': source,
            'target_service': target,
            'source_namespace': source_namespace,
            'target_namespace': target_namespace,
            'issue_score': issue_score,
        })

    ranked.sort(key=lambda item: item['issue_score'], reverse=True)
    for candidate in ranked:
        params = {
            'source_service': candidate['source_service'],
            'target_service': candidate['target_service'],
            'time_window': candidate_window,
            'exclude_health_check': 'true',
            'limit': 20,
        }
        preview = http_json('GET', f'{query_base}/logs/preview/topology-edge', params=params)
        preview_data = preview.get('data') or []
        if isinstance(preview_data, list) and preview_data:
            selected_edge = candidate
            selected_preview = preview
            selected_window = candidate_window
            logs_params = {
                'source_service': candidate['source_service'],
                'target_service': candidate['target_service'],
                'time_window': candidate_window,
                'exclude_health_check': 'true',
                'limit': 50,
            }
            selected_logs = http_json('GET', f'{query_base}/logs', params=logs_params)
            break
    if selected_edge is not None:
        break

if selected_edge is None:
    record_case(
        'case1_topology_edge_preview_contract',
        False,
        {'window_attempts': window_attempts, 'reason': 'no_edge_with_preview_logs'},
    )
else:
    preview_data = selected_preview.get('data') or []
    preview_context = selected_preview.get('context') or {}
    first_preview = preview_data[0] if preview_data else {}
    case1_passed = (
        isinstance(preview_data, list)
        and len(preview_data) > 0
        and preview_context.get('source_service') == selected_edge['source_service']
        and preview_context.get('target_service') == selected_edge['target_service']
        and str(preview_context.get('time_window') or '').strip().upper() == selected_window
        and all(key in first_preview for key in ('id', 'timestamp', 'service_name', 'message', 'edge_match_score'))
    )
    record_case(
        'case1_topology_edge_preview_contract',
        case1_passed,
        {
            'selected_edge': {
                'source_service': selected_edge['source_service'],
                'target_service': selected_edge['target_service'],
                'source_namespace': selected_edge['source_namespace'],
                'target_namespace': selected_edge['target_namespace'],
                'issue_score': selected_edge['issue_score'],
            },
            'window': selected_window,
            'preview_count': len(preview_data),
            'preview_context': preview_context,
        },
    )

if selected_edge is None:
    record_case(
        'case2_logs_context_contract',
        False,
        {'reason': 'edge_not_selected_from_case1'},
    )
else:
    logs_data = selected_logs.get('data') or []
    logs_context = selected_logs.get('context') or {}
    case2_passed = (
        isinstance(logs_data, list)
        and isinstance(logs_context, dict)
        and logs_context.get('source_service') == selected_edge['source_service']
        and logs_context.get('target_service') == selected_edge['target_service']
        and str(logs_context.get('time_window') or '').strip().upper() == selected_window
        and 'effective_levels' in logs_context
        and 'effective_trace_ids' in logs_context
        and 'effective_request_ids' in logs_context
    )
    record_case(
        'case2_logs_context_contract',
        case2_passed,
        {
            'logs_count': len(logs_data),
            'context': logs_context,
        },
    )

if selected_edge is None:
    record_case(
        'case3_logs_aggregated_contract',
        False,
        {'reason': 'edge_not_selected_from_case1'},
    )
else:
    try:
        aggregated_params = {
            'source_service': selected_edge['source_service'],
            'target_service': selected_edge['target_service'],
            'time_window': selected_window,
            'exclude_health_check': 'true',
            'limit': 200,
            'max_patterns': 20,
            'min_pattern_count': 1,
            'max_samples': 2,
        }
        aggregated = http_json('GET', f'{query_base}/logs/aggregated', params=aggregated_params)
        patterns = aggregated.get('patterns')
        case3_passed = (
            isinstance(patterns, list)
            and isinstance(aggregated.get('total_logs'), int)
            and isinstance(aggregated.get('total_patterns'), int)
        )
        detail = {
            'total_logs': aggregated.get('total_logs'),
            'total_patterns': aggregated.get('total_patterns'),
            'pattern_count': len(patterns) if isinstance(patterns, list) else -1,
            'has_context': isinstance(aggregated.get('context'), dict),
        }
    except Exception as exc:
        case3_passed = False
        detail = {'error': str(exc)}
    record_case(
        'case3_logs_aggregated_contract',
        case3_passed,
        detail,
    )

try:
    trace_params = {
        'time_window': selected_window if selected_edge is not None else candidate_windows[0],
        'limit': 50,
    }
    if selected_edge is not None:
        trace_params['source_service'] = selected_edge['source_service']
        trace_params['target_service'] = selected_edge['target_service']
    trace_lite = http_json('GET', f'{query_base}/trace-lite/inferred', params=trace_params)
    trace_data = trace_lite.get('data')
    trace_stats = trace_lite.get('stats')
    case4_passed = (
        isinstance(trace_data, list)
        and isinstance(trace_lite.get('count'), int)
        and isinstance(trace_stats, dict)
        and str(trace_lite.get('time_window') or '').strip()
    )
    case4_detail = {
        'count': trace_lite.get('count'),
        'time_window': trace_lite.get('time_window'),
        'has_stats': isinstance(trace_stats, dict),
    }
except Exception as exc:
    case4_passed = False
    case4_detail = {'error': str(exc)}
record_case(
    'case4_trace_lite_contract',
    case4_passed,
    case4_detail,
)

try:
    alerts = http_json(
        'GET',
        f'{query_base}/value/kpi/alerts',
        params={
            'time_window': '7 DAY',
            'max_mttd_minutes': '120',
            'max_mttr_minutes': '240',
            'min_trace_log_correlation_rate': '0.0',
            'min_topology_coverage_rate': '0.0',
            'min_release_regression_pass_rate': '0.0',
        },
    )
    case5_passed = (
        alerts.get('status') == 'ok'
        and isinstance(alerts.get('metrics'), dict)
        and isinstance(alerts.get('alerts'), list)
        and isinstance(alerts.get('active_alerts'), int)
    )
    case5_detail = {
        'active_alerts': alerts.get('active_alerts'),
        'metric_keys': sorted((alerts.get('metrics') or {}).keys()),
    }
except Exception as exc:
    case5_passed = False
    case5_detail = {'error': str(exc)}
record_case(
    'case5_value_kpi_alerts_contract',
    case5_passed,
    case5_detail,
)

try:
    snapshot_source = f\"query-contract-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}\"
    snapshot_created = http_json(
        'POST',
        f'{query_base}/value/kpi/snapshots',
        params={'time_window': '7 DAY', 'source': snapshot_source},
    )
    snapshot_list = http_json(
        'GET',
        f'{query_base}/value/kpi/snapshots',
        params={'limit': 5, 'source': snapshot_source},
    )
    snapshot_rows = snapshot_list.get('data') or []
    case6_passed = (
        snapshot_created.get('status') == 'ok'
        and str(snapshot_created.get('snapshot_id') or '').startswith('vkpi-')
        and snapshot_list.get('status') == 'ok'
        and isinstance(snapshot_list.get('count'), int)
        and isinstance(snapshot_rows, list)
        and len(snapshot_rows) >= 1
        and str((snapshot_rows[0] or {}).get('source') or '') == snapshot_source
    )
    case6_detail = {
        'created_snapshot_id': snapshot_created.get('snapshot_id'),
        'listed_count': snapshot_list.get('count'),
        'source': snapshot_source,
    }
except Exception as exc:
    case6_passed = False
    case6_detail = {'error': str(exc)}
record_case(
    'case6_value_kpi_snapshot_contract',
    case6_passed,
    case6_detail,
)

passed_count = sum(1 for case in cases if case.get('passed'))
failed_count = len(cases) - passed_count
payload = {
    'status': 'ok' if failed_count == 0 else 'failed',
    'generated_at': datetime.now(timezone.utc).isoformat(),
    'time_window': time_window,
    'selected_window': selected_window,
    'selected_edge': (
        {
            'source_service': selected_edge['source_service'],
            'target_service': selected_edge['target_service'],
            'source_namespace': selected_edge['source_namespace'],
            'target_namespace': selected_edge['target_namespace'],
            'issue_score': selected_edge['issue_score'],
        }
        if selected_edge is not None else None
    ),
    'window_attempts': window_attempts,
    'summary': {
        'total': len(cases),
        'passed': passed_count,
        'failed': failed_count,
    },
    'cases': cases,
}

print('__QUERY_CONTRACT_JSON_START__')
print(json.dumps(payload, ensure_ascii=False))
print('__QUERY_CONTRACT_JSON_END__')
print(f'{passed_count} passed, {failed_count} failed')

if failed_count > 0:
    raise SystemExit(1)
PY
"
}

mkdir -p "${ARTIFACT_DIR}"

RUN_ID="query-contract-$(date -u +%Y%m%d-%H%M%S)-$RANDOM"
LOG_FILE="${ARTIFACT_DIR}/${RUN_ID}.log"
REPORT_FILE="${ARTIFACT_DIR}/${RUN_ID}.json"
GENERATED_AT="$(date -u +"%Y-%m-%dT%H:%M:%S.%3NZ")"
MODE="unknown"
if command -v kubectl >/dev/null 2>&1; then
  MODE="pod"
else
  echo "[ERROR] kubectl is required for pod-native query contract checks." >&2
  exit 1
fi

set +e
run_in_pod 2>&1 | tee "${LOG_FILE}"
EXIT_CODE=${PIPESTATUS[0]}
set -e

SUMMARY_LINE="$(rg -n "=+ .* in .* =+" "${LOG_FILE}" | tail -n 1 | sed -E 's/^[0-9]+://g' | tr -d '\r' || true)"
if [[ -z "${SUMMARY_LINE}" ]]; then
  SUMMARY_LINE="$(rg -n "passed in|failed in|errors? in|error in" "${LOG_FILE}" | tail -n 1 | sed -E 's/^[0-9]+://g' | tr -d '\r' || true)"
fi
if [[ -z "${SUMMARY_LINE}" ]]; then
  SUMMARY_LINE="$(rg -n "[0-9]+ passed, [0-9]+ failed" "${LOG_FILE}" | tail -n 1 | sed -E 's/^[0-9]+://g' | tr -d '\r' || true)"
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
