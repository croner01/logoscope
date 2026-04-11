#!/usr/bin/env bash
set -euo pipefail

# INT-01 端到端验收脚本
# 流程：
# 1) 从 topology-service 获取链路，优先选择“问题边”
# 2) 调用 query-service /logs/preview/topology-edge 校验问题日志预览
# 3) 调用 query-service /logs（携带拓扑上下文）校验日志详情上下文
# 4) 取一条日志调用 semantic-engine /ai/analyze-log 校验 AI 建议返回

NAMESPACE="${NAMESPACE:-islap}"
TIME_WINDOW="${TIME_WINDOW:-1 HOUR}"
CONFIDENCE_THRESHOLD="${CONFIDENCE_THRESHOLD:-0.3}"
PREVIEW_LIMIT="${PREVIEW_LIMIT:-20}"
LOG_LIMIT="${LOG_LIMIT:-50}"
ARTIFACT_DIR="${ARTIFACT_DIR:-/root/logoscope/reports/int01}"
REQUIRE_PROBLEM_EDGE="${REQUIRE_PROBLEM_EDGE:-false}"

mkdir -p "$ARTIFACT_DIR"
RUN_ID="int01-$(date -u +%Y%m%d-%H%M%S)-$RANDOM"
REPORT_FILE="${ARTIFACT_DIR}/${RUN_ID}.json"

usage() {
  cat <<'EOF'
INT-01 topology -> logs -> AI acceptance script

Env vars:
  NAMESPACE             Kubernetes namespace (default: islap)
  TIME_WINDOW           Query window (default: "1 HOUR")
  CONFIDENCE_THRESHOLD  Topology confidence threshold (default: 0.3)
  PREVIEW_LIMIT         Preview logs limit (default: 20)
  LOG_LIMIT             Logs detail limit (default: 50)
  ARTIFACT_DIR          Report output dir (default: /root/logoscope/reports/int01)
  REQUIRE_PROBLEM_EDGE  Require issue_score > 0 edge (default: false)

Examples:
  scripts/int01-topology-log-ai-check.sh
  NAMESPACE=islap TIME_WINDOW='2 HOUR' scripts/int01-topology-log-ai-check.sh
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
NAMESPACE='${NAMESPACE}' \
TIME_WINDOW='${TIME_WINDOW}' \
CONFIDENCE_THRESHOLD='${CONFIDENCE_THRESHOLD}' \
PREVIEW_LIMIT='${PREVIEW_LIMIT}' \
LOG_LIMIT='${LOG_LIMIT}' \
REQUIRE_PROBLEM_EDGE='${REQUIRE_PROBLEM_EDGE}' \
RUN_ID='${RUN_ID}' \
python - <<'PY'
import json
import os
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone


def to_float(value, default=0.0):
    try:
        if value is None or value == '':
            return float(default)
        return float(value)
    except Exception:
        return float(default)


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
        raise SystemExit(f\"http error: {exc.code} {url} detail={detail}\")
    except urllib.error.URLError as exc:
        raise SystemExit(f\"url error: {url} reason={exc}\")


def normalize_edge(edge):
    source = str(edge.get('source') or edge.get('from') or edge.get('source_service') or '').strip()
    target = str(edge.get('target') or edge.get('to') or edge.get('target_service') or '').strip()
    return source, target


namespace = os.getenv('NAMESPACE', 'islap')
time_window = os.getenv('TIME_WINDOW', '1 HOUR')
confidence_threshold = os.getenv('CONFIDENCE_THRESHOLD', '0.3')
preview_limit = int(os.getenv('PREVIEW_LIMIT', '20'))
log_limit = int(os.getenv('LOG_LIMIT', '50'))
require_problem_edge = str(os.getenv('REQUIRE_PROBLEM_EDGE', 'false')).lower() == 'true'
run_id = os.getenv('RUN_ID', 'int01')

query_base = 'http://127.0.0.1:8002/api/v1'
topology_base = 'http://topology-service:8003/api/v1/topology'
semantic_base = os.getenv('AI_BASE_URL', 'http://ai-service:8090/api/v1/ai')

topology_payload = http_json(
    'GET',
    f'{topology_base}/hybrid',
    params={
        'time_window': time_window,
        'confidence_threshold': confidence_threshold,
    },
)

edges = topology_payload.get('edges') or []
if not isinstance(edges, list) or not edges:
    raise SystemExit('topology api returned no edges')

ranked_edges = []
for edge in edges:
    if not isinstance(edge, dict):
        continue
    source, target = normalize_edge(edge)
    if not source or not target:
        continue
    metrics = edge.get('metrics') if isinstance(edge.get('metrics'), dict) else {}
    error_rate = to_float(metrics.get('error_rate', edge.get('error_rate', 0.0)))
    timeout_rate = to_float(metrics.get('timeout_rate', edge.get('timeout_rate', 0.0)))
    quality_score = to_float(metrics.get('quality_score', edge.get('quality_score', 100.0)), 100.0)
    coverage = to_float(metrics.get('coverage', edge.get('coverage', 0.0)))
    evidence_type = str(metrics.get('evidence_type', edge.get('evidence_type', 'unknown')))

    issue_score = error_rate * 100.0 + timeout_rate * 100.0 + max(0.0, 100.0 - quality_score)
    ranked_edges.append({
        'edge': edge,
        'source': source,
        'target': target,
        'issue_score': round(issue_score, 4),
        'error_rate': round(error_rate, 6),
        'timeout_rate': round(timeout_rate, 6),
        'quality_score': round(quality_score, 4),
        'coverage': round(coverage, 4),
        'evidence_type': evidence_type,
    })

if not ranked_edges:
    raise SystemExit('topology edges exist but no valid source/target pair found')

ranked_edges.sort(key=lambda item: item['issue_score'], reverse=True)
problem_edge_detected = any(item['issue_score'] > 0 for item in ranked_edges)

if require_problem_edge and not problem_edge_detected:
    raise SystemExit('no problem edge detected (all issue_score <= 0), but REQUIRE_PROBLEM_EDGE=true')

selected = None
preview_data = []
logs_data = []
context = {}
preview_first = {}
attempts = []

for candidate in ranked_edges:
    source_service = candidate['source']
    target_service = candidate['target']

    preview_payload = http_json(
        'GET',
        f'{query_base}/logs/preview/topology-edge',
        params={
            'source_service': source_service,
            'target_service': target_service,
            'time_window': time_window,
            'limit': preview_limit,
            'exclude_health_check': 'true',
        },
    )

    candidate_preview = preview_payload.get('data') or []
    if not isinstance(candidate_preview, list) or not candidate_preview:
        attempts.append({
            'source_service': source_service,
            'target_service': target_service,
            'issue_score': candidate['issue_score'],
            'preview_count': 0,
            'logs_count': 0,
            'reason': 'preview_empty',
        })
        continue

    logs_payload = http_json(
        'GET',
        f'{query_base}/logs',
        params={
            'service_name': source_service,
            'search': target_service,
            'source_service': source_service,
            'target_service': target_service,
            'time_window': time_window,
            'exclude_health_check': 'true',
            'limit': log_limit,
        },
    )

    candidate_logs = logs_payload.get('data') or []
    candidate_context = logs_payload.get('context') or {}
    if not isinstance(candidate_logs, list):
        attempts.append({
            'source_service': source_service,
            'target_service': target_service,
            'issue_score': candidate['issue_score'],
            'preview_count': len(candidate_preview),
            'logs_count': 0 if not isinstance(candidate_logs, list) else len(candidate_logs),
            'reason': 'logs_payload_invalid',
        })
        continue

    if str(candidate_context.get('source_service', '')).strip() != source_service:
        attempts.append({
            'source_service': source_service,
            'target_service': target_service,
            'issue_score': candidate['issue_score'],
            'preview_count': len(candidate_preview),
            'logs_count': len(candidate_logs),
            'reason': 'context_source_mismatch',
        })
        continue
    if str(candidate_context.get('target_service', '')).strip() != target_service:
        attempts.append({
            'source_service': source_service,
            'target_service': target_service,
            'issue_score': candidate['issue_score'],
            'preview_count': len(candidate_preview),
            'logs_count': len(candidate_logs),
            'reason': 'context_target_mismatch',
        })
        continue

    selected = candidate
    preview_data = candidate_preview
    logs_data = candidate_logs
    context = candidate_context
    preview_first = preview_data[0] if preview_data else {}
    attempts.append({
        'source_service': source_service,
        'target_service': target_service,
        'issue_score': candidate['issue_score'],
        'preview_count': len(candidate_preview),
        'logs_count': len(candidate_logs),
        'reason': 'selected',
    })
    break

if selected is None:
    raise SystemExit(
        'no topology edge can complete preview+logs context validation, attempts=' + json.dumps(attempts, ensure_ascii=False)
    )

source_service = selected['source']
target_service = selected['target']
preview_first_id = str(preview_first.get('id', '')).strip()
selected_log = None
if preview_first_id:
    for row in logs_data:
        if str(row.get('id', '')).strip() == preview_first_id:
            selected_log = row
            break
if selected_log is None:
    if logs_data:
        selected_log = logs_data[0]
    elif preview_data:
        selected_log = preview_data[0]
    else:
        raise SystemExit('no log candidate available from logs/preview data')

selected_log_source = 'logs' if logs_data else 'preview'

selected_log_id = str(selected_log.get('id') or '').strip() or f'int01-log-{run_id}'
selected_ts = str(selected_log.get('timestamp') or datetime.now(timezone.utc).isoformat())
selected_service = str(selected_log.get('service_name') or source_service)
selected_level = str(selected_log.get('level') or 'ERROR')
selected_message = str(selected_log.get('message') or '')

ai_payload = {
    'id': selected_log_id,
    'timestamp': selected_ts,
    'entity': {
        'type': 'service',
        'name': selected_service,
        'instance': str(selected_log.get('pod_name') or 'unknown'),
    },
    'event': {
        'level': selected_level,
        'raw': selected_message,
    },
    'context': {
        'source_service': source_service,
        'target_service': target_service,
        'time_window': time_window,
        'trace_id': str(selected_log.get('trace_id') or ''),
        'edge_match_score': preview_first.get('edge_match_score'),
        'edge_side': preview_first.get('edge_side'),
        'int01_run_id': run_id,
    },
}

ai_payload_result = http_json(
    'POST',
    f'{semantic_base}/analyze-log',
    data=ai_payload,
)

if not isinstance(ai_payload_result, dict):
    raise SystemExit('ai analyze-log result is not a dict')

overview = ai_payload_result.get('overview')
root_causes = ai_payload_result.get('rootCauses')
solutions = ai_payload_result.get('solutions')
if not isinstance(overview, dict):
    raise SystemExit('ai analyze-log missing overview')
if not isinstance(root_causes, list):
    raise SystemExit('ai analyze-log missing rootCauses list')
if not isinstance(solutions, list):
    raise SystemExit('ai analyze-log missing solutions list')

for required_key in ('problem', 'severity', 'description', 'confidence'):
    if required_key not in overview:
        raise SystemExit(f'ai analyze-log overview missing key: {required_key}')

report = {
    'run_id': run_id,
    'generated_at': datetime.now(timezone.utc).isoformat(),
    'namespace': namespace,
    'time_window': time_window,
    'selected_edge': {
        'source_service': source_service,
        'target_service': target_service,
        'issue_score': selected['issue_score'],
        'error_rate': selected['error_rate'],
        'timeout_rate': selected['timeout_rate'],
        'quality_score': selected['quality_score'],
        'coverage': selected['coverage'],
        'evidence_type': selected['evidence_type'],
    },
    'problem_edge_detected': problem_edge_detected,
    'edge_attempts': attempts,
    'preview': {
        'count': len(preview_data),
        'first_log_id': preview_first_id,
    },
    'logs': {
        'count': len(logs_data),
        'selected_log_id': selected_log_id,
        'selected_log_source': selected_log_source,
        'selected_log_level': selected_level,
        'context': context,
    },
    'ai': {
        'analysis_method': ai_payload_result.get('analysis_method', 'rule-based'),
        'overview': overview,
        'root_causes_count': len(root_causes),
        'solutions_count': len(solutions),
    },
    'status': 'passed',
}

print(json.dumps(report, ensure_ascii=False))
PY
"
)"

printf '%s\n' "$PAYLOAD_JSON" > "$REPORT_FILE"
ln -sfn "$REPORT_FILE" "${ARTIFACT_DIR}/latest.json"

echo "[INFO] INT-01 report: $REPORT_FILE"

python3 - <<'PY' "$PAYLOAD_JSON"
import json
import sys

payload = json.loads(sys.argv[1])
edge = payload.get("selected_edge", {})
preview = payload.get("preview", {})
logs = payload.get("logs", {})
ai = payload.get("ai", {})
overview = ai.get("overview", {})

print("[INFO] INT-01 acceptance passed")
print(
    f"[INFO] Edge: {edge.get('source_service')} -> {edge.get('target_service')} "
    f"(issue_score={edge.get('issue_score')}, error_rate={edge.get('error_rate')}, "
    f"timeout_rate={edge.get('timeout_rate')}, quality={edge.get('quality_score')})"
)
print(
    f"[INFO] Preview count={preview.get('count')}, logs count={logs.get('count')}, "
    f"selected_log_id={logs.get('selected_log_id')}"
)
print(
    f"[INFO] AI overview: problem={overview.get('problem')}, "
    f"severity={overview.get('severity')}, confidence={overview.get('confidence')}"
)
print(f"[INFO] problem_edge_detected={payload.get('problem_edge_detected')}")
PY

exit 0
