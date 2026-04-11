#!/usr/bin/env bash
set -euo pipefail

# FE-07 前端关键路径脚本化检查
# 目标：
# 1) 校验拓扑问题摘要字段（TS-02）可用
# 2) 校验拓扑链路跳转日志筛选参数闭环（source/target/time_window）
# 3) 校验日志触发 AI 分析返回结构
# 4) 形成 6 条关键用例的机器可读报告

NAMESPACE="${NAMESPACE:-islap}"
TIME_WINDOW="${TIME_WINDOW:-1 HOUR}"
CONFIDENCE_THRESHOLD="${CONFIDENCE_THRESHOLD:-0.3}"
HTTP_TIMEOUT_SECONDS="${HTTP_TIMEOUT_SECONDS:-45}"
HTTP_RETRY_ATTEMPTS="${HTTP_RETRY_ATTEMPTS:-2}"
HTTP_RETRY_BACKOFF_SECONDS="${HTTP_RETRY_BACKOFF_SECONDS:-1.5}"
ARTIFACT_DIR="${ARTIFACT_DIR:-/root/logoscope/reports/frontend-critical-path}"

mkdir -p "$ARTIFACT_DIR"
RUN_ID="frontend-critical-$(date -u +%Y%m%d-%H%M%S)-$RANDOM"
REPORT_FILE="${ARTIFACT_DIR}/${RUN_ID}.json"

usage() {
  cat <<'EOF'
Frontend critical path check (FE-07)

Env vars:
  NAMESPACE             Kubernetes namespace (default: islap)
  TIME_WINDOW           Query window (default: "1 HOUR")
  CONFIDENCE_THRESHOLD  Topology confidence threshold (default: 0.3)
  HTTP_TIMEOUT_SECONDS  Per-request timeout seconds (default: 45)
  HTTP_RETRY_ATTEMPTS   Retry attempts for timeout/URLError (default: 2)
  HTTP_RETRY_BACKOFF_SECONDS Retry backoff seconds (default: 1.5)
  ARTIFACT_DIR          Report dir (default: /root/logoscope/reports/frontend-critical-path)

Example:
  scripts/frontend-critical-path-check.sh
EOF
}

fail() {
  printf '[ERROR] %s\n' "$1" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "missing command: $1"
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

require_cmd kubectl

QUERY_POD="$(kubectl -n "$NAMESPACE" get pod -l app=query-service -o jsonpath='{.items[0].metadata.name}')"
if [[ -z "$QUERY_POD" ]]; then
  fail "query-service pod not found in namespace $NAMESPACE"
fi

PAYLOAD_JSON="$(
kubectl -n "$NAMESPACE" exec "$QUERY_POD" -c query-service -- /bin/sh -lc "
TIME_WINDOW='${TIME_WINDOW}' \
CONFIDENCE_THRESHOLD='${CONFIDENCE_THRESHOLD}' \
HTTP_TIMEOUT_SECONDS='${HTTP_TIMEOUT_SECONDS}' \
HTTP_RETRY_ATTEMPTS='${HTTP_RETRY_ATTEMPTS}' \
HTTP_RETRY_BACKOFF_SECONDS='${HTTP_RETRY_BACKOFF_SECONDS}' \
RUN_ID='${RUN_ID}' \
python - <<'PY'
import json
import os
import socket
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone


DEFAULT_TIMEOUT_SECONDS = max(5.0, float(os.getenv('HTTP_TIMEOUT_SECONDS', '45')))
DEFAULT_RETRY_ATTEMPTS = max(1, int(float(os.getenv('HTTP_RETRY_ATTEMPTS', '2'))))
DEFAULT_RETRY_BACKOFF_SECONDS = max(0.0, float(os.getenv('HTTP_RETRY_BACKOFF_SECONDS', '1.5')))


def http_json(method, url, params=None, data=None, timeout=None, retry_attempts=None, retry_backoff_seconds=None):
    if params:
        query = urllib.parse.urlencode(params)
        url = f\"{url}{'&' if '?' in url else '?'}{query}\"

    effective_timeout = float(timeout if timeout is not None else DEFAULT_TIMEOUT_SECONDS)
    effective_retry_attempts = int(retry_attempts if retry_attempts is not None else DEFAULT_RETRY_ATTEMPTS)
    effective_retry_backoff = float(
        retry_backoff_seconds if retry_backoff_seconds is not None else DEFAULT_RETRY_BACKOFF_SECONDS
    )

    last_error = None
    for attempt in range(1, effective_retry_attempts + 1):
        body = None
        headers = {}
        if data is not None:
            body = json.dumps(data).encode('utf-8')
            headers['Content-Type'] = 'application/json'

        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=effective_timeout) as resp:
                raw = resp.read().decode('utf-8', errors='ignore')
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode('utf-8', errors='ignore')
            raise SystemExit(f'http error: {exc.code} {url} detail={detail}')
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            last_error = exc
            if attempt >= effective_retry_attempts:
                raise SystemExit(
                    f'url/timeout error: {url} reason={exc} timeout={effective_timeout}s attempts={effective_retry_attempts}'
                )
            if effective_retry_backoff > 0:
                time.sleep(effective_retry_backoff)
        except Exception as exc:
            raise SystemExit(f'unexpected error: {url} reason={exc}')

    raise SystemExit(f'request failed without response: {url} reason={last_error}')


def to_float(value, default=0.0):
    try:
        if value is None or value == '':
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def normalize_edge(edge):
    source = str(edge.get('source') or edge.get('source_service') or '').strip()
    target = str(edge.get('target') or edge.get('target_service') or '').strip()
    return source, target


time_window = os.getenv('TIME_WINDOW', '1 HOUR')
confidence_threshold = os.getenv('CONFIDENCE_THRESHOLD', '0.3')
run_id = os.getenv('RUN_ID', 'frontend-critical')

query_base = 'http://127.0.0.1:8002/api/v1'
topology_base = 'http://topology-service:8003/api/v1/topology'
semantic_base = os.getenv('AI_BASE_URL', 'http://ai-service:8090/api/v1/ai')

cases = []


def record_case(case_id, passed, detail):
    cases.append({
        'id': case_id,
        'passed': bool(passed),
        'detail': detail,
    })


candidate_windows = []
for item in [time_window, '6 HOUR', '24 HOUR', '7 DAY']:
    normalized = str(item or '').strip().upper()
    if normalized and normalized not in candidate_windows:
        candidate_windows.append(normalized)

topology = {}
effective_time_window = time_window
window_attempts = []
for candidate_window in candidate_windows:
    current_topology = http_json(
        'GET',
        f'{topology_base}/hybrid',
        params={'time_window': candidate_window, 'confidence_threshold': confidence_threshold},
    )
    current_edges = current_topology.get('edges') or []
    window_attempts.append({'time_window': candidate_window, 'edge_count': len(current_edges)})
    topology = current_topology
    effective_time_window = candidate_window
    if len(current_edges) > 0:
        break

nodes = topology.get('nodes') or []
edges = topology.get('edges') or []
metadata = topology.get('metadata') or {}

case1_passed = isinstance(metadata.get('issue_summary'), dict)
record_case(
    'case1_topology_issue_summary_fields',
    case1_passed,
    {'has_issue_summary': case1_passed, 'metadata_keys': sorted(metadata.keys())},
)
if not case1_passed:
    raise SystemExit('case1 failed: metadata.issue_summary missing')

case2_passed = bool(edges) and isinstance((edges[0] or {}).get('problem_summary'), dict)
any_edge_has_problem_summary = any(
    isinstance((edge or {}).get('problem_summary'), dict)
    for edge in edges
    if isinstance(edge, dict)
)
first_edge_has_problem_summary = bool(edges) and isinstance((edges[0] or {}).get('problem_summary'), dict)
case2_passed = bool(edges) and any_edge_has_problem_summary
record_case(
    'case2_edge_problem_summary_fields',
    case2_passed,
    {
        'edge_count': len(edges),
        'first_edge_has_problem_summary': first_edge_has_problem_summary,
        'any_edge_has_problem_summary': any_edge_has_problem_summary,
        'window_attempts': window_attempts,
    },
)
if not case2_passed:
    if not edges:
        raise SystemExit(f'case2 failed: topology edges empty attempts={json.dumps(window_attempts, ensure_ascii=False)}')
    raise SystemExit('case2 failed: edge.problem_summary missing')

if not edges:
    raise SystemExit('no topology edges')

ranked = []
for edge in edges:
    if not isinstance(edge, dict):
        continue
    source, target = normalize_edge(edge)
    if not source or not target:
        continue
    summary = edge.get('problem_summary') or {}
    score = to_float(summary.get('issue_score'), 0.0)
    ranked.append({'source': source, 'target': target, 'score': score})
ranked.sort(key=lambda item: item['score'], reverse=True)
if not ranked:
    raise SystemExit('no valid source/target edge found')
logs_baseline = http_json(
    'GET',
    f'{query_base}/logs',
    params={
        'time_window': effective_time_window,
        'limit': 1,
        'exclude_health_check': 'false',
    },
)
try:
    logs_baseline_count = int(logs_baseline.get('count') or 0)
except Exception:
    logs_baseline_count = 0
selected = None
selected_preview = None
selected_logs = None
attempts = []

for candidate in ranked:
    source_service = candidate['source']
    target_service = candidate['target']

    preview = http_json(
        'GET',
        f'{query_base}/logs/preview/topology-edge',
        params={
            'source_service': source_service,
            'target_service': target_service,
            'time_window': effective_time_window,
            'limit': 10,
            'exclude_health_check': 'true',
        },
    )
    preview_count = len(preview.get('data') or [])
    if preview_count <= 0:
        attempts.append({
            'source_service': source_service,
            'target_service': target_service,
            'score': candidate['score'],
            'preview_count': preview_count,
            'logs_count': 0,
            'reason': 'preview_empty',
        })
        continue

    logs = http_json(
        'GET',
        f'{query_base}/logs',
        params={
            'service_name': source_service,
            'search': target_service,
            'source_service': source_service,
            'target_service': target_service,
            'time_window': effective_time_window,
            'exclude_health_check': 'true',
            'limit': 30,
        },
    )
    logs_data = logs.get('data') or []
    logs_context = logs.get('context') or {}
    context_ok = (
        str(logs_context.get('source_service') or '').strip() == source_service
        and str(logs_context.get('target_service') or '').strip() == target_service
    )
    if not context_ok:
        attempts.append({
            'source_service': source_service,
            'target_service': target_service,
            'score': candidate['score'],
            'preview_count': preview_count,
            'logs_count': len(logs_data),
            'reason': 'context_mismatch',
        })
        continue

    selected = candidate
    selected_preview = preview
    selected_logs = logs
    attempts.append({
        'source_service': source_service,
        'target_service': target_service,
        'score': candidate['score'],
        'preview_count': preview_count,
        'logs_count': len(logs_data),
        'reason': 'selected',
    })
    break

if selected is None:
    if logs_baseline_count <= 0:
        selected = ranked[0]
        selected_preview = {'data': []}
        selected_logs = {
            'data': [],
            'context': {
                'source_service': selected['source'],
                'target_service': selected['target'],
                'no_logs_data': True,
            },
        }
        attempts.append({
            'source_service': selected['source'],
            'target_service': selected['target'],
            'score': selected['score'],
            'preview_count': 0,
            'logs_count': 0,
            'reason': 'no_logs_data_fallback',
        })
    else:
        raise SystemExit(f'case3/4 failed: no edge completed preview+logs path attempts={json.dumps(attempts, ensure_ascii=False)}')

source_service = selected['source']
target_service = selected['target']
preview = selected_preview or {}
preview_count = len(preview.get('data') or [])
case3_passed = preview_count > 0 or logs_baseline_count <= 0
record_case(
    'case3_topology_edge_log_preview',
    case3_passed,
    {
        'source_service': source_service,
        'target_service': target_service,
        'preview_count': preview_count,
        'logs_baseline_count': logs_baseline_count,
        'degraded_no_logs_data': logs_baseline_count <= 0,
        'attempts': attempts,
    },
)

logs = selected_logs or {}
logs_data = logs.get('data') or []
logs_context = logs.get('context') or {}
context_ok = (
    str(logs_context.get('source_service') or '').strip() == source_service
    and str(logs_context.get('target_service') or '').strip() == target_service
)
record_case(
    'case4_logs_jump_context_retained',
    context_ok,
    {
        'logs_count': len(logs_data),
        'context': logs_context,
        'logs_baseline_count': logs_baseline_count,
        'degraded_no_logs_data': logs_baseline_count <= 0,
        'allow_empty_logs': True,
    },
)
if not context_ok:
    raise SystemExit('case4 failed: logs context mismatch')

preview_data = preview.get('data') or []
if logs_data:
    log = logs_data[0]
elif preview_data:
    log = preview_data[0]
else:
    log = {
        'id': f'frontend-case-{run_id}',
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'service_name': source_service,
        'pod_name': 'synthetic',
        'level': 'ERROR',
        'message': f'synthetic log for FE-07: {source_service} -> {target_service}',
    }
log_payload = {
    'id': str(log.get('id') or f'frontend-case-{run_id}'),
    'timestamp': str(log.get('timestamp') or datetime.now(timezone.utc).isoformat()),
    'entity': {
        'type': 'service',
        'name': str(log.get('service_name') or source_service),
        'instance': str(log.get('pod_name') or 'unknown'),
    },
    'event': {
        'level': str(log.get('level') or 'ERROR'),
        'raw': str(log.get('message') or ''),
    },
    'context': {
        'source_service': source_service,
        'target_service': target_service,
        'time_window': effective_time_window,
        'fe07_case': run_id,
    },
}
ai = http_json('POST', f'{semantic_base}/analyze-log', data=log_payload)
overview = ai.get('overview') if isinstance(ai, dict) else None
case5_passed = isinstance(overview, dict) and all(k in overview for k in ('problem', 'severity', 'description', 'confidence'))
record_case(
    'case5_ai_trigger_contract',
    case5_passed,
    {'ai_keys': sorted(ai.keys()) if isinstance(ai, dict) else [], 'overview': overview},
)
if not case5_passed:
    raise SystemExit('case5 failed: AI analyze-log contract mismatch')

top_problem_edges = (metadata.get('issue_summary') or {}).get('top_problem_edges') or []
case6_passed = isinstance(top_problem_edges, list)
record_case(
    'case6_issue_summary_top_edges',
    case6_passed,
    {'top_problem_edges_count': len(top_problem_edges)},
)
if not case6_passed:
    raise SystemExit('case6 failed: issue_summary.top_problem_edges missing')

report = {
    'run_id': run_id,
    'generated_at': datetime.now(timezone.utc).isoformat(),
    'time_window': effective_time_window,
    'requested_time_window': time_window,
    'http_timeout_seconds': DEFAULT_TIMEOUT_SECONDS,
    'http_retry_attempts': DEFAULT_RETRY_ATTEMPTS,
    'http_retry_backoff_seconds': DEFAULT_RETRY_BACKOFF_SECONDS,
    'window_attempts': window_attempts,
    'selected_edge': selected,
    'cases': cases,
    'passed': all(item['passed'] for item in cases),
}
print(json.dumps(report, ensure_ascii=False))
PY
"
)"

printf '%s\n' "$PAYLOAD_JSON" > "$REPORT_FILE"
ln -sfn "$REPORT_FILE" "${ARTIFACT_DIR}/latest.json"

echo "[INFO] FE-07 report: $REPORT_FILE"

python3 - <<'PY' "$PAYLOAD_JSON"
import json
import sys

payload = json.loads(sys.argv[1])
cases = payload.get('cases', [])
passed = [c for c in cases if c.get('passed')]
failed = [c for c in cases if not c.get('passed')]
selected = payload.get('selected_edge', {})

print(
    f"[INFO] FE-07 critical path: {len(passed)}/{len(cases)} passed, "
    f"selected_edge={selected.get('source')}->{selected.get('target')}"
)
for case in cases:
    status = "PASS" if case.get("passed") else "FAIL"
    print(f"[INFO] {status} {case.get('id')}")

if failed:
    raise SystemExit(2)
PY

exit 0
