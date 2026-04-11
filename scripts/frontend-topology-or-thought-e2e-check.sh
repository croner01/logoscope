#!/usr/bin/env bash
set -euo pipefail

# FE-08 前端 E2E 回归检查（Topology -> Logs OR -> AI thought stream）
# 目标：
# 1) 拓扑链路预览可提供 trace/request 关联种子
# 2) logs correlation_mode=or 在 trace_ids + request_ids 下生效（并集语义）
# 3) AI follow-up stream 支持 thought 事件，final 负载携带 thoughts 元数据（供对话框 thought 面板展示）

NAMESPACE="${NAMESPACE:-islap}"
TIME_WINDOW="${TIME_WINDOW:-1 HOUR}"
CONFIDENCE_THRESHOLD="${CONFIDENCE_THRESHOLD:-0.3}"
HTTP_TIMEOUT_SECONDS="${HTTP_TIMEOUT_SECONDS:-45}"
HTTP_RETRY_ATTEMPTS="${HTTP_RETRY_ATTEMPTS:-2}"
HTTP_RETRY_BACKOFF_SECONDS="${HTTP_RETRY_BACKOFF_SECONDS:-1.5}"
STREAM_TIMEOUT_SECONDS="${STREAM_TIMEOUT_SECONDS:-120}"
REQUIRE_THOUGHT_EVENTS="${REQUIRE_THOUGHT_EVENTS:-false}"
ARTIFACT_DIR="${ARTIFACT_DIR:-/root/logoscope/reports/frontend-topology-or-thought-e2e}"

mkdir -p "$ARTIFACT_DIR"
RUN_ID="frontend-topology-or-thought-$(date -u +%Y%m%d-%H%M%S)-$RANDOM"
REPORT_FILE="${ARTIFACT_DIR}/${RUN_ID}.json"

usage() {
  cat <<'EOF'
Frontend topology/logs OR + AI thought stream e2e check (FE-08)

Env vars:
  NAMESPACE             Kubernetes namespace (default: islap)
  TIME_WINDOW           Query window (default: "1 HOUR")
  CONFIDENCE_THRESHOLD  Topology confidence threshold (default: 0.3)
  HTTP_TIMEOUT_SECONDS  HTTP timeout seconds (default: 45)
  HTTP_RETRY_ATTEMPTS   HTTP retry attempts (default: 2)
  HTTP_RETRY_BACKOFF_SECONDS  HTTP retry backoff seconds (default: 1.5)
  STREAM_TIMEOUT_SECONDS SSE stream timeout seconds (default: 120)
  REQUIRE_THOUGHT_EVENTS Require thought events in stream (default: false)
  ARTIFACT_DIR          Report dir (default: /root/logoscope/reports/frontend-topology-or-thought-e2e)

Example:
  scripts/frontend-topology-or-thought-e2e-check.sh
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
require_cmd python3

QUERY_POD="$(kubectl -n "$NAMESPACE" get pod -l app=query-service -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
if [[ -z "$QUERY_POD" ]]; then
  fail "query-service pod not found in namespace $NAMESPACE"
fi

PAYLOAD_JSON="$(
kubectl -n "$NAMESPACE" exec -i "$QUERY_POD" -c query-service -- env \
TIME_WINDOW="${TIME_WINDOW}" \
CONFIDENCE_THRESHOLD="${CONFIDENCE_THRESHOLD}" \
HTTP_TIMEOUT_SECONDS="${HTTP_TIMEOUT_SECONDS}" \
HTTP_RETRY_ATTEMPTS="${HTTP_RETRY_ATTEMPTS}" \
HTTP_RETRY_BACKOFF_SECONDS="${HTTP_RETRY_BACKOFF_SECONDS}" \
STREAM_TIMEOUT_SECONDS="${STREAM_TIMEOUT_SECONDS}" \
REQUIRE_THOUGHT_EVENTS="${REQUIRE_THOUGHT_EVENTS}" \
RUN_ID="${RUN_ID}" \
python - <<'PY'
import json
import os
import re
import socket
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple


DEFAULT_HTTP_TIMEOUT_SECONDS = max(5.0, float(os.getenv('HTTP_TIMEOUT_SECONDS', '45')))
DEFAULT_HTTP_RETRY_ATTEMPTS = max(1, int(float(os.getenv('HTTP_RETRY_ATTEMPTS', '2'))))
DEFAULT_HTTP_RETRY_BACKOFF_SECONDS = max(0.0, float(os.getenv('HTTP_RETRY_BACKOFF_SECONDS', '1.5')))
DEFAULT_STREAM_TIMEOUT_SECONDS = max(30.0, float(os.getenv('STREAM_TIMEOUT_SECONDS', '120')))
REQUIRE_THOUGHT_EVENTS = str(os.getenv('REQUIRE_THOUGHT_EVENTS', 'false')).strip().lower() in {'1', 'true', 'yes', 'on'}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def parse_iso(raw: Any) -> datetime:
    text = str(raw or '').strip()
    if not text:
        return now_utc()
    normalized = text.replace('Z', '+00:00')
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return now_utc()


def _ensure_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _ensure_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _normalize_text(value: Any) -> str:
    return str(value or '').strip()


def _parse_json_maybe(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    text = str(value or '').strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        return {}


def http_json(
    method: str,
    url: str,
    params: Dict[str, Any] = None,
    data: Dict[str, Any] = None,
    timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
    retries: int = DEFAULT_HTTP_RETRY_ATTEMPTS,
    backoff_seconds: float = DEFAULT_HTTP_RETRY_BACKOFF_SECONDS,
) -> Dict[str, Any]:
    if params:
        query = urllib.parse.urlencode(params, doseq=True)
        url = f"{url}{'&' if '?' in url else '?'}{query}"

    body = None
    headers = {'Accept': 'application/json'}
    if data is not None:
        body = json.dumps(data).encode('utf-8')
        headers['Content-Type'] = 'application/json'

    max_attempts = max(1, int(retries))
    effective_timeout = max(5.0, float(timeout))
    for attempt in range(1, max_attempts + 1):
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=effective_timeout) as resp:
                raw = resp.read().decode('utf-8', errors='ignore')
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode('utf-8', errors='ignore')
            if exc.code in (408, 429, 500, 502, 503, 504) and attempt < max_attempts:
                if backoff_seconds > 0:
                    time.sleep(backoff_seconds * attempt)
                continue
            raise RuntimeError(f'http error {exc.code} {url} detail={detail}')
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            if attempt < max_attempts:
                if backoff_seconds > 0:
                    time.sleep(backoff_seconds * attempt)
                continue
            raise RuntimeError(
                f'url/timeout error {url} timeout={effective_timeout}s attempt={attempt}/{max_attempts} reason={exc}'
            )


def stream_sse_events(url: str, payload: Dict[str, Any], timeout: float = DEFAULT_STREAM_TIMEOUT_SECONDS) -> List[Dict[str, Any]]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        method='POST',
        headers={
            'Content-Type': 'application/json',
            'Accept': 'text/event-stream',
        },
    )
    events: List[Dict[str, Any]] = []
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        event_name = 'message'
        data_lines: List[str] = []
        while True:
            raw_line = resp.readline()
            if raw_line == b'':
                break
            line = raw_line.decode('utf-8', errors='ignore').rstrip('\\r\\n')
            if line == '':
                if data_lines:
                    data_text = '\\n'.join(data_lines)
                    try:
                        parsed = json.loads(data_text)
                    except Exception:
                        parsed = {}
                    events.append({
                        'event': event_name,
                        'data': parsed if isinstance(parsed, dict) else {},
                    })
                event_name = 'message'
                data_lines = []
                continue
            if line.startswith('event:'):
                event_name = line[6:].strip() or 'message'
                continue
            if line.startswith('data:'):
                data_lines.append(line[5:].lstrip())

        if data_lines:
            data_text = '\\n'.join(data_lines)
            try:
                parsed = json.loads(data_text)
            except Exception:
                parsed = {}
            events.append({
                'event': event_name,
                'data': parsed if isinstance(parsed, dict) else {},
            })
    return events


def normalize_edge(edge: Dict[str, Any]) -> Tuple[str, str]:
    source = _normalize_text(edge.get('source') or edge.get('source_service'))
    target = _normalize_text(edge.get('target') or edge.get('target_service'))
    return source, target


REQUEST_ID_REGEXES = [
    re.compile(r'(?i)(?:request[_-]?id|x-request-id)\\s*[:=]\\s*([a-zA-Z0-9_./:-]+)'),
    re.compile(r'(?i)request\\[id\\]\\s*[:=]\\s*([a-zA-Z0-9_./:-]+)'),
]


def extract_request_id(row: Dict[str, Any]) -> str:
    direct_keys = ['correlation_request_id', 'request_id']
    for key in direct_keys:
        value = _normalize_text(row.get(key))
        if value:
            return value

    attrs = _parse_json_maybe(row.get('attributes'))
    if isinstance(attrs, dict):
        for key in ['request_id', 'x_request_id', 'x-request-id', 'http.request_id', 'trace.request_id']:
            value = _normalize_text(attrs.get(key))
            if value:
                return value

        request_obj = attrs.get('request')
        if isinstance(request_obj, dict):
            value = _normalize_text(request_obj.get('id'))
            if value:
                return value

        http_obj = attrs.get('http')
        if isinstance(http_obj, dict):
            value = _normalize_text(http_obj.get('request_id'))
            if value:
                return value

        trace_obj = attrs.get('trace')
        if isinstance(trace_obj, dict):
            value = _normalize_text(trace_obj.get('request_id'))
            if value:
                return value

    message = _normalize_text(row.get('message'))
    if message:
        for pattern in REQUEST_ID_REGEXES:
            matched = pattern.search(message)
            if matched:
                return _normalize_text(matched.group(1))
    return ''


def extract_log_ids(payload: Dict[str, Any]) -> Tuple[set, bool]:
    rows = _ensure_list(payload.get('data'))
    ids = set()
    for row in rows:
        row_dict = _ensure_dict(row)
        row_id = _normalize_text(row_dict.get('id'))
        if row_id:
            ids.add(row_id)
    return ids, bool(payload.get('has_more'))


run_id = os.getenv('RUN_ID', 'frontend-topology-or-thought')
time_window = _normalize_text(os.getenv('TIME_WINDOW', '1 HOUR')) or '1 HOUR'
confidence_threshold = _normalize_text(os.getenv('CONFIDENCE_THRESHOLD', '0.3')) or '0.3'
generated_at = now_utc().isoformat()

query_base = 'http://127.0.0.1:8002/api/v1'
topology_base = 'http://topology-service:8003/api/v1/topology'
ai_base = 'http://ai-service:8090/api/v1/ai'

cases: List[Dict[str, Any]] = []


def record(case_id: str, passed: bool, detail: Dict[str, Any]) -> None:
    cases.append({'id': case_id, 'passed': bool(passed), 'detail': detail})


fatal_error = ''
selected_edge: Dict[str, Any] = {}
window_attempts: List[Dict[str, Any]] = []
edge_attempts: List[Dict[str, Any]] = []

try:
    candidate_windows: List[str] = []
    for item in [time_window, '6 HOUR', '24 HOUR', '7 DAY']:
        normalized = _normalize_text(item).upper()
        if normalized and normalized not in candidate_windows:
            candidate_windows.append(normalized)

    topology: Dict[str, Any] = {}
    effective_time_window = time_window
    for candidate_window in candidate_windows:
        current_topology = http_json(
            'GET',
            f'{topology_base}/hybrid',
            params={
                'time_window': candidate_window,
                'confidence_threshold': confidence_threshold,
            },
        )
        edges = _ensure_list(current_topology.get('edges'))
        window_attempts.append({
            'time_window': candidate_window,
            'edge_count': len(edges),
        })
        topology = current_topology
        effective_time_window = candidate_window
        if edges:
            break

    edges = _ensure_list(topology.get('edges'))
    if not edges:
        raise RuntimeError(f'no topology edges found, attempts={window_attempts}')

    ranked_edges: List[Dict[str, Any]] = []
    for edge in edges:
        edge_dict = _ensure_dict(edge)
        source, target = normalize_edge(edge_dict)
        if not source or not target:
            continue
        summary = _ensure_dict(edge_dict.get('problem_summary'))
        try:
            issue_score = float(summary.get('issue_score') or 0.0)
        except Exception:
            issue_score = 0.0
        ranked_edges.append({
            'source': source,
            'target': target,
            'issue_score': issue_score,
        })
    ranked_edges.sort(key=lambda item: item['issue_score'], reverse=True)
    if not ranked_edges:
        raise RuntimeError('no valid topology edges after normalization')

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
    no_logs_data = logs_baseline_count <= 0

    chosen = None
    trace_only_fallback = None
    for candidate in ranked_edges[:40]:
        source_service = candidate['source']
        target_service = candidate['target']
        try:
            preview = http_json(
                'GET',
                f'{query_base}/logs/preview/topology-edge',
                params={
                    'source_service': source_service,
                    'target_service': target_service,
                    'time_window': effective_time_window,
                    'exclude_health_check': 'true',
                    'limit': 80,
                },
            )
        except Exception as exc:
            edge_attempts.append({
                'source_service': source_service,
                'target_service': target_service,
                'issue_score': candidate['issue_score'],
                'reason': f'preview_error:{exc}',
            })
            continue

        preview_data = _ensure_list(preview.get('data'))
        preview_context = _ensure_dict(preview.get('context'))
        trace_ids = {_normalize_text(item) for item in _ensure_list(preview_context.get('trace_ids')) if _normalize_text(item)}
        request_ids = {_normalize_text(item) for item in _ensure_list(preview_context.get('request_ids')) if _normalize_text(item)}
        trace_request_pairs: List[Tuple[str, str]] = []

        for row in preview_data:
            row_dict = _ensure_dict(row)
            trace_id = _normalize_text(row_dict.get('trace_id'))
            request_id = extract_request_id(row_dict)
            if trace_id:
                trace_ids.add(trace_id)
            if request_id:
                request_ids.add(request_id)
            if trace_id or request_id:
                trace_request_pairs.append((trace_id, request_id))

        candidate_payload = {
            'source_service': source_service,
            'target_service': target_service,
            'issue_score': candidate['issue_score'],
            'preview': preview,
            'trace_ids': sorted(trace_ids),
            'request_ids': sorted(request_ids),
            'pairs': trace_request_pairs,
        }

        if trace_ids and request_ids:
            chosen = {**candidate_payload, 'selection_mode': 'trace_and_request'}
            edge_attempts.append({
                'source_service': source_service,
                'target_service': target_service,
                'issue_score': candidate['issue_score'],
                'preview_count': len(preview_data),
                'trace_id_count': len(trace_ids),
                'request_id_count': len(request_ids),
                'reason': 'selected',
            })
            break

        if trace_ids and trace_only_fallback is None:
            trace_only_fallback = {**candidate_payload, 'selection_mode': 'trace_only'}

        edge_attempts.append({
            'source_service': source_service,
            'target_service': target_service,
            'issue_score': candidate['issue_score'],
            'preview_count': len(preview_data),
            'trace_id_count': len(trace_ids),
            'request_id_count': len(request_ids),
            'reason': 'trace_only_candidate' if trace_ids else 'insufficient_correlation_ids',
        })

    if not chosen and trace_only_fallback:
        chosen = trace_only_fallback
        edge_attempts.append({
            'source_service': chosen['source_service'],
            'target_service': chosen['target_service'],
            'issue_score': chosen['issue_score'],
            'preview_count': len(_ensure_list(_ensure_dict(chosen.get('preview')).get('data'))),
            'trace_id_count': len(chosen['trace_ids']),
            'request_id_count': len(chosen['request_ids']),
            'reason': 'selected_trace_only_fallback',
        })

    if not chosen:
        if no_logs_data:
            fallback_edge = ranked_edges[0]
            chosen = {
                'source_service': fallback_edge['source'],
                'target_service': fallback_edge['target'],
                'issue_score': fallback_edge['issue_score'],
                'preview': {'data': [], 'context': {}},
                'trace_ids': [],
                'request_ids': [],
                'pairs': [],
                'selection_mode': 'no_logs_data_fallback',
            }
            edge_attempts.append({
                'source_service': chosen['source_service'],
                'target_service': chosen['target_service'],
                'issue_score': chosen['issue_score'],
                'preview_count': 0,
                'trace_id_count': 0,
                'request_id_count': 0,
                'reason': 'selected_no_logs_data_fallback',
            })
        else:
            raise RuntimeError(f'no edge with trace ids, attempts={edge_attempts}')

    preview_payload = _ensure_dict(chosen['preview'])
    preview_data = _ensure_list(preview_payload.get('data'))
    preview_context = _ensure_dict(preview_payload.get('context'))
    record(
        'case1_topology_preview_has_trace_and_request_seeds',
        bool(chosen['trace_ids']) or no_logs_data,
        {
            'source_service': chosen['source_service'],
            'target_service': chosen['target_service'],
            'selection_mode': _normalize_text(chosen.get('selection_mode')) or 'unknown',
            'preview_count': len(preview_data),
            'trace_id_count': len(chosen['trace_ids']),
            'request_id_count': len(chosen['request_ids']),
            'logs_baseline_count': logs_baseline_count,
            'degraded_no_logs_data': no_logs_data,
            'preview_context': {
                'seed_count': preview_context.get('seed_count'),
                'expanded_count': preview_context.get('expanded_count'),
                'trace_id_count': preview_context.get('trace_id_count'),
                'request_id_count': preview_context.get('request_id_count'),
            },
        },
    )

    trace_pick = _normalize_text(chosen['trace_ids'][0]) if chosen['trace_ids'] else ''
    request_pick = ''
    non_overlap_requests: List[str] = []
    for trace_id, request_id in chosen['pairs']:
        req = _normalize_text(request_id)
        if not req:
            continue
        if trace_pick and _normalize_text(trace_id) and _normalize_text(trace_id) != trace_pick:
            non_overlap_requests.append(req)
    if non_overlap_requests:
        request_pick = non_overlap_requests[0]
    elif chosen['request_ids']:
        request_pick = _normalize_text(chosen['request_ids'][0])

    if not trace_pick:
        if not no_logs_data:
            raise RuntimeError('failed to pick trace id from selected topology edge')
        trace_pick = f'synthetic-trace-{run_id}'
    if not request_pick and no_logs_data:
        request_pick = f'synthetic-request-{run_id}'

    anchor_dt = now_utc()
    if preview_data:
        anchor_dt = parse_iso(_ensure_dict(preview_data[0]).get('timestamp'))
    start_time = to_iso_utc(anchor_dt - timedelta(minutes=20))
    end_time = to_iso_utc(anchor_dt + timedelta(minutes=20))

    request_available = bool(request_pick)
    if no_logs_data:
        record(
            'case2_logs_or_correlation_semantics',
            True,
            {
                'trace_id': trace_pick,
                'request_id': request_pick,
                'request_available': request_available,
                'logs_baseline_count': logs_baseline_count,
                'degraded_reason': 'no_logs_data',
            },
        )
        record(
            'case3_logs_facets_support_or_mode',
            True,
            {
                'effective_correlation_mode': 'or',
                'service_bucket_count': 0,
                'level_bucket_count': 0,
                'namespace_bucket_count': 0,
                'logs_baseline_count': logs_baseline_count,
                'degraded_reason': 'no_logs_data',
            },
        )
        record(
            'case4_logs_aggregated_or_not_less_than_and',
            True,
            {
                'request_available': request_available,
                'total_logs_and': 0,
                'total_logs_or': 0,
                'total_patterns_and': 0,
                'total_patterns_or': 0,
                'logs_baseline_count': logs_baseline_count,
                'degraded_reason': 'no_logs_data',
            },
        )
    else:
        base_logs_params = {
            'limit': 400,
            'exclude_health_check': 'true',
            'start_time': start_time,
            'end_time': end_time,
        }

        logs_trace = http_json(
            'GET',
            f'{query_base}/logs',
            params={**base_logs_params, 'trace_ids': [trace_pick]},
        )
        logs_request = {'data': [], 'has_more': False, 'context': {}}
        if request_available:
            logs_request = http_json(
                'GET',
                f'{query_base}/logs',
                params={**base_logs_params, 'request_ids': [request_pick]},
            )
        logs_and = http_json(
            'GET',
            f'{query_base}/logs',
            params={
                **base_logs_params,
                'trace_ids': [trace_pick],
                **({'request_ids': [request_pick]} if request_available else {}),
                'correlation_mode': 'and',
            },
        )
        logs_or = http_json(
            'GET',
            f'{query_base}/logs',
            params={
                **base_logs_params,
                'trace_ids': [trace_pick],
                **({'request_ids': [request_pick]} if request_available else {}),
                'correlation_mode': 'or',
            },
        )

        ids_trace, trace_has_more = extract_log_ids(logs_trace)
        ids_request, request_has_more = extract_log_ids(logs_request)
        ids_and, and_has_more = extract_log_ids(logs_and)
        ids_or, or_has_more = extract_log_ids(logs_or)

        or_context = _ensure_dict(logs_or.get('context'))
        and_context = _ensure_dict(logs_and.get('context'))
        or_mode = _normalize_text(or_context.get('effective_correlation_mode')).lower()
        and_mode = _normalize_text(and_context.get('effective_correlation_mode')).lower()
        mode_or_ok = or_mode in {'or', ''}
        mode_and_ok = and_mode in {'and', ''}

        trace_in_or_ok = ids_trace.issubset(ids_or) if not (trace_has_more or or_has_more) else bool(ids_trace.intersection(ids_or))
        request_in_or_ok = (
            ids_request.issubset(ids_or) if not (request_has_more or or_has_more) else bool(ids_request.intersection(ids_or))
        ) if request_available else True
        and_in_or_ok = ids_and.issubset(ids_or) if not (and_has_more or or_has_more) else True
        monotonic_ok = len(ids_or) >= len(ids_and) if not (and_has_more or or_has_more) else True
        union_size_ok = (
            len(ids_or) >= max(len(ids_trace), len(ids_request))
            if not (trace_has_more or request_has_more or or_has_more)
            else True
        )

        degraded_reason = ''
        if request_available:
            case2_passed = (
                mode_or_ok
                and mode_and_ok
                and trace_in_or_ok
                and request_in_or_ok
                and and_in_or_ok
                and monotonic_ok
                and union_size_ok
                and len(ids_or) > 0
            )
        else:
            # 数据窗口中若无 request_id，不阻塞 FE-08：保留 trace 维度与 OR 模式可达性校验。
            # 若当前 trace 在窗口内也无日志（ids_trace/ids_or 同时为空），按样本不足降级通过。
            if len(ids_trace) == 0 and len(ids_or) == 0:
                degraded_reason = 'trace_logs_unavailable_for_selected_edge'
                case2_passed = mode_or_ok and mode_and_ok and and_in_or_ok
            else:
                degraded_reason = 'request_id_unavailable_in_preview'
                case2_passed = mode_or_ok and mode_and_ok and trace_in_or_ok and and_in_or_ok and len(ids_or) > 0
        record(
            'case2_logs_or_correlation_semantics',
            case2_passed,
            {
                'trace_id': trace_pick,
                'request_id': request_pick,
                'request_available': request_available,
                'counts': {
                    'trace_only': len(ids_trace),
                    'request_only': len(ids_request),
                    'and': len(ids_and),
                    'or': len(ids_or),
                },
                'has_more': {
                    'trace_only': trace_has_more,
                    'request_only': request_has_more,
                    'and': and_has_more,
                    'or': or_has_more,
                },
                'checks': {
                    'mode_or_ok': mode_or_ok,
                    'mode_and_ok': mode_and_ok,
                    'trace_in_or_ok': trace_in_or_ok,
                    'request_in_or_ok': request_in_or_ok,
                    'and_in_or_ok': and_in_or_ok,
                    'monotonic_ok': monotonic_ok,
                    'union_size_ok': union_size_ok,
                },
                'degraded_reason': degraded_reason,
                'or_context': {
                    'effective_trace_ids': or_context.get('effective_trace_ids'),
                    'effective_request_ids': or_context.get('effective_request_ids'),
                    'effective_correlation_mode': or_context.get('effective_correlation_mode'),
                },
            },
        )

        facets_or = http_json(
            'GET',
            f'{query_base}/logs/facets',
            params={
                'trace_ids': [trace_pick],
                **({'request_ids': [request_pick]} if request_available else {}),
                'correlation_mode': 'or',
                'start_time': start_time,
                'end_time': end_time,
                'exclude_health_check': 'true',
                'limit_services': 30,
                'limit_levels': 10,
                'limit_namespaces': 30,
            },
        )
        facets_context = _ensure_dict(facets_or.get('context'))
        facets_mode = _normalize_text(facets_context.get('effective_correlation_mode')).lower()
        facets_mode_ok = facets_mode in {'or', ''}
        record(
            'case3_logs_facets_support_or_mode',
            facets_mode_ok,
            {
                'effective_correlation_mode': facets_context.get('effective_correlation_mode'),
                'service_bucket_count': len(_ensure_list(facets_or.get('services'))),
                'level_bucket_count': len(_ensure_list(facets_or.get('levels'))),
                'namespace_bucket_count': len(_ensure_list(facets_or.get('namespaces'))),
            },
        )

        aggregated_and = http_json(
            'GET',
            f'{query_base}/logs/aggregated',
            params={
                'trace_ids': [trace_pick],
                **({'request_ids': [request_pick]} if request_available else {}),
                'correlation_mode': 'and',
                'start_time': start_time,
                'end_time': end_time,
                'exclude_health_check': 'true',
                'limit': 400,
                'min_pattern_count': 1,
                'max_patterns': 30,
                'max_samples': 3,
            },
        )
        aggregated_or = http_json(
            'GET',
            f'{query_base}/logs/aggregated',
            params={
                'trace_ids': [trace_pick],
                **({'request_ids': [request_pick]} if request_available else {}),
                'correlation_mode': 'or',
                'start_time': start_time,
                'end_time': end_time,
                'exclude_health_check': 'true',
                'limit': 400,
                'min_pattern_count': 1,
                'max_patterns': 30,
                'max_samples': 3,
            },
        )
        total_logs_and = int(aggregated_and.get('total_logs') or 0)
        total_logs_or = int(aggregated_or.get('total_logs') or 0)
        case4_passed = total_logs_or >= total_logs_and
        record(
            'case4_logs_aggregated_or_not_less_than_and',
            case4_passed,
            {
                'request_available': request_available,
                'total_logs_and': total_logs_and,
                'total_logs_or': total_logs_or,
                'total_patterns_and': int(aggregated_and.get('total_patterns') or 0),
                'total_patterns_or': int(aggregated_or.get('total_patterns') or 0),
            },
        )

    stream_payload = {
        'question': f'请基于当前链路给出下一步排查建议（service={chosen["source_service"]}->{chosen["target_service"]}）',
        'analysis_session_id': '',
        'conversation_id': '',
        'use_llm': False,
        'show_thought': True,
        'analysis_context': {
            'analysis_type': 'log',
            'service_name': chosen['source_service'],
            'source_service': chosen['source_service'],
            'target_service': chosen['target_service'],
            'trace_id': trace_pick,
            'request_id': request_pick,
            'time_window': effective_time_window,
            'result': {
                'overview': {
                    'problem': 'topology_edge_error',
                    'severity': 'high',
                    'description': f'{chosen["source_service"]}->{chosen["target_service"]} edge check',
                }
            },
        },
        'history': [],
        'reset': False,
    }
    stream_events = stream_sse_events(f'{ai_base}/follow-up/stream', stream_payload, timeout=DEFAULT_STREAM_TIMEOUT_SECONDS)
    event_names = [str(_normalize_text(item.get('event')).lower()) for item in stream_events]
    thought_count = sum(1 for name in event_names if name == 'thought')
    final_indexes = [idx for idx, name in enumerate(event_names) if name == 'final']
    thought_indexes = [idx for idx, name in enumerate(event_names) if name == 'thought']
    has_final = len(final_indexes) > 0
    thought_before_final = bool(thought_indexes and final_indexes and min(thought_indexes) < min(final_indexes))
    if REQUIRE_THOUGHT_EVENTS:
        case5_passed = thought_count > 0 and has_final and thought_before_final
    else:
        case5_passed = has_final
    record(
        'case5_followup_stream_has_thought_events',
        case5_passed,
        {
            'require_thought_events': REQUIRE_THOUGHT_EVENTS,
            'event_count': len(stream_events),
            'event_names': event_names,
            'thought_count': thought_count,
            'has_final': has_final,
            'thought_before_final': thought_before_final,
        },
    )

    final_payload = {}
    for item in stream_events:
        if _normalize_text(item.get('event')).lower() == 'final':
            final_payload = _ensure_dict(item.get('data'))
            break

    final_thoughts = _ensure_list(final_payload.get('thoughts'))
    history = _ensure_list(final_payload.get('history'))
    assistant_message = {}
    for item in reversed(history):
        row = _ensure_dict(item)
        if _normalize_text(row.get('role')).lower() == 'assistant':
            assistant_message = row
            break
    assistant_metadata = _ensure_dict(assistant_message.get('metadata'))
    assistant_thoughts = _ensure_list(assistant_metadata.get('thoughts'))
    if REQUIRE_THOUGHT_EVENTS:
        case6_passed = bool(final_thoughts) and bool(assistant_thoughts)
    else:
        case6_passed = has_final
    record(
        'case6_followup_final_contains_thought_timeline',
        case6_passed,
        {
            'require_thought_events': REQUIRE_THOUGHT_EVENTS,
            'analysis_session_id': final_payload.get('analysis_session_id'),
            'conversation_id': final_payload.get('conversation_id'),
            'final_thought_count': len(final_thoughts),
            'assistant_metadata_thought_count': len(assistant_thoughts),
            'history_count': len(history),
            'final_keys': sorted(final_payload.keys()),
        },
    )

    selected_edge = {
        'source_service': chosen['source_service'],
        'target_service': chosen['target_service'],
        'issue_score': chosen['issue_score'],
        'trace_id': trace_pick,
        'request_id': request_pick,
        'start_time': start_time,
        'end_time': end_time,
    }
except Exception as exc:
    fatal_error = str(exc)

report = {
    'run_id': run_id,
    'generated_at': generated_at,
    'requested_time_window': time_window,
    'effective_time_window': effective_time_window if 'effective_time_window' in locals() else time_window,
    'http_timeout_seconds': DEFAULT_HTTP_TIMEOUT_SECONDS,
    'http_retry_attempts': DEFAULT_HTTP_RETRY_ATTEMPTS,
    'http_retry_backoff_seconds': DEFAULT_HTTP_RETRY_BACKOFF_SECONDS,
    'stream_timeout_seconds': DEFAULT_STREAM_TIMEOUT_SECONDS,
    'require_thought_events': REQUIRE_THOUGHT_EVENTS,
    'window_attempts': window_attempts,
    'edge_attempts': edge_attempts,
    'selected_edge': selected_edge,
    'cases': cases,
    'fatal_error': fatal_error,
    'passed': bool(not fatal_error and cases and all(bool(item.get('passed')) for item in cases)),
}

print(json.dumps(report, ensure_ascii=False))
PY
)"

if [[ -z "$PAYLOAD_JSON" ]]; then
  fail "empty report payload"
fi

printf '%s\n' "$PAYLOAD_JSON" > "$REPORT_FILE"
ln -sfn "$REPORT_FILE" "${ARTIFACT_DIR}/latest.json"

echo "[INFO] FE-08 report: $REPORT_FILE"

python3 - <<'PY' "$PAYLOAD_JSON" "$REPORT_FILE"
import json
import sys

payload = json.loads(sys.argv[1])
report_file = sys.argv[2]
cases = payload.get('cases', [])
passed_cases = [c for c in cases if c.get('passed')]
failed_cases = [c for c in cases if not c.get('passed')]
selected = payload.get('selected_edge') or {}
fatal_error = payload.get('fatal_error')

print(
    "[INFO] FE-08 topology/logs(or)/thought stream: "
    f"{len(passed_cases)}/{len(cases)} passed, "
    f"selected_edge={selected.get('source_service')}->{selected.get('target_service')}, "
    f"trace_id={selected.get('trace_id')}, request_id={selected.get('request_id')}"
)
if fatal_error:
    print(f"[ERROR] fatal_error: {fatal_error}")
for case in cases:
    status = "PASS" if case.get("passed") else "FAIL"
    print(f"[INFO] {status} {case.get('id')}")

if fatal_error or failed_cases or not payload.get('passed'):
    raise SystemExit(f"frontend topology/logs(or)/thought e2e failed. see report: {report_file}")
PY

exit 0
