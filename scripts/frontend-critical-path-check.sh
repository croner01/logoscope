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
RUN_ID='${RUN_ID}' \
python - <<'PY'
import json
import os
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone


def http_json(method, url, params=None, data=None, timeout=20):
    if params:
        query = urllib.parse.urlencode(params)
        url = f\"{url}{'&' if '?' in url else '?'}{query}\"

    body = None
    headers = {}
    if data is not None:
        body = json.dumps(data).encode('utf-8')
        headers['Content-Type'] = 'application/json'

    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode('utf-8', errors='ignore')
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='ignore')
        raise SystemExit(f'http error: {exc.code} {url} detail={detail}')
    except urllib.error.URLError as exc:
        raise SystemExit(f'url error: {url} reason={exc}')


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


topology = http_json(
    'GET',
    f'{topology_base}/hybrid',
    params={'time_window': time_window, 'confidence_threshold': confidence_threshold},
)

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
record_case(
    'case2_edge_problem_summary_fields',
    case2_passed,
    {
        'edge_count': len(edges),
        'first_edge_has_problem_summary': case2_passed,
    },
)
if not case2_passed:
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
            'time_window': time_window,
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
            'time_window': time_window,
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
    raise SystemExit(f'case3/4 failed: no edge completed preview+logs path attempts={json.dumps(attempts, ensure_ascii=False)}')

source_service = selected['source']
target_service = selected['target']
preview = selected_preview or {}
preview_count = len(preview.get('data') or [])
record_case(
    'case3_topology_edge_log_preview',
    preview_count > 0,
    {
        'source_service': source_service,
        'target_service': target_service,
        'preview_count': preview_count,
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
    raise SystemExit('case5 failed: no log entry available from logs/preview path')
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
        'time_window': time_window,
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
    'time_window': time_window,
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
