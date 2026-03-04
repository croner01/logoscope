#!/usr/bin/env bash
set -euo pipefail

# Trace 端到端冒烟脚本
# 流程:
# 1) 向 ingest-service 注入一条 OTLP JSON traces
# 2) 等待 trace 落入 ClickHouse logs.traces
# 3) 调用 query-service 校验 /api/v1/traces /spans /stats
# 4) 输出 Redis traces.raw 消费组 pending 状态

NAMESPACE="${NAMESPACE:-islap}"
TRACE_ID="${TRACE_ID:-$(cat /proc/sys/kernel/random/uuid | tr 'A-Z' 'a-z' | tr -d '-')}"
SERVICE_NAME="${SERVICE_NAME:-smoke-trace-service}"
SPAN_ID="${SPAN_ID:-$(cat /proc/sys/kernel/random/uuid | tr 'A-Z' 'a-z' | tr -d '-' | cut -c1-16)}"
ATTEMPTS="${ATTEMPTS:-20}"
SLEEP_SECONDS="${SLEEP_SECONDS:-2}"
MAX_PENDING="${MAX_PENDING:-0}"
TRACE_PENDING_GROUP="${TRACE_PENDING_GROUP:-log-workers}"

# 默认使用当前时间生成纳秒时间戳；允许通过环境变量覆盖
NOW_NS="$(date +%s%N)"
START_NS="${START_NS:-$NOW_NS}"
END_NS="${END_NS:-$((START_NS + 123456789))}"

usage() {
  cat <<'EOF'
Trace E2E smoke script

Env vars:
  NAMESPACE       K8s namespace (default: islap)
  TRACE_ID        Trace id to inject (default: smoke-<ts>-<rand>)
  SERVICE_NAME    service.name in injected span
  SPAN_ID         span id in injected span
  ATTEMPTS        Poll attempts for ClickHouse query (default: 20)
  SLEEP_SECONDS   Sleep seconds between polls (default: 2)
  START_NS        Span start time in unix nano (default: now)
  END_NS          Span end time in unix nano (default: START_NS+123456789)
  MAX_PENDING     Max allowed pending in traces.raw group.
                  default 0 means release-gate strict check.
                  set -1 to disable pending assertion.
  TRACE_PENDING_GROUP
                  Which consumer group to validate pending for (default: log-workers).

Examples:
  scripts/trace-e2e-smoke.sh
  NAMESPACE=islap ATTEMPTS=30 scripts/trace-e2e-smoke.sh
  MAX_PENDING=-1 scripts/trace-e2e-smoke.sh
  TRACE_PENDING_GROUP=trace-processors scripts/trace-e2e-smoke.sh
EOF
}

log() {
  printf '[INFO] %s\n' "$1"
}

warn() {
  printf '[WARN] %s\n' "$1"
}

fail() {
  printf '[ERROR] %s\n' "$1" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "missing command: $1"
}

get_pod() {
  local label="$1"
  kubectl -n "$NAMESPACE" get pod -l "$label" -o jsonpath='{.items[0].metadata.name}'
}

inject_trace() {
  local ingest_pod="$1"
  log "Injecting trace via ingest pod: ${ingest_pod}, trace_id=${TRACE_ID}"

  kubectl -n "$NAMESPACE" exec "$ingest_pod" -c ingest-service -- /bin/sh -lc "
TRACE_ID='${TRACE_ID}' SERVICE_NAME='${SERVICE_NAME}' SPAN_ID='${SPAN_ID}' START_NS='${START_NS}' END_NS='${END_NS}' python - <<'PY'
import json
import os
import urllib.request

trace_id = os.environ['TRACE_ID']
service_name = os.environ['SERVICE_NAME']
span_id = os.environ['SPAN_ID']
start_ns = os.environ['START_NS']
end_ns = os.environ['END_NS']

payload = {
    'resourceSpans': [{
        'resource': {
            'attributes': [
                {'key': 'service.name', 'value': {'stringValue': service_name}}
            ]
        },
        'scopeSpans': [{
            'spans': [{
                'traceId': trace_id,
                'spanId': span_id,
                'parentSpanId': '',
                'name': 'GET /smoke',
                'kind': 2,
                'startTimeUnixNano': start_ns,
                'endTimeUnixNano': end_ns,
                'status': {'code': 2, 'message': 'smoke error'},
                'attributes': [
                    {'key': 'http.method', 'value': {'stringValue': 'GET'}},
                    {'key': 'duration_ns', 'value': {'intValue': str(int(end_ns) - int(start_ns))}}
                ]
            }]
        }]
    }]
}

data = json.dumps(payload).encode('utf-8')
req = urllib.request.Request(
    'http://localhost:8080/v1/traces',
    data=data,
    headers={'Content-Type': 'application/json'},
    method='POST'
)
with urllib.request.urlopen(req, timeout=10) as resp:
    body = resp.read().decode('utf-8', errors='ignore')
    print(resp.status)
    print(body)
PY"
}

wait_clickhouse_trace() {
  local clickhouse_pod="$1"
  local attempt

  log "Waiting trace persisted in ClickHouse: trace_id=${TRACE_ID}"
  for attempt in $(seq 1 "$ATTEMPTS"); do
    local output
    output="$(
      kubectl -n "$NAMESPACE" exec "$clickhouse_pod" -- clickhouse-client -q \
        "SELECT trace_id, span_id, service_name, status, operation_name FROM logs.traces WHERE trace_id='${TRACE_ID}' ORDER BY timestamp DESC LIMIT 5 FORMAT TSVRaw" || true
    )"

    if [[ -n "${output}" ]]; then
      log "Trace found in ClickHouse on attempt ${attempt}:"
      printf '%s\n' "$output"
      return 0
    fi

    sleep "$SLEEP_SECONDS"
  done

  fail "Trace not found in ClickHouse after ${ATTEMPTS} attempts (trace_id=${TRACE_ID})"
}

check_query_apis() {
  local query_pod="$1"
  log "Checking query-service APIs in pod: ${query_pod}"

  kubectl -n "$NAMESPACE" exec "$query_pod" -c query-service -- /bin/sh -lc "
TRACE_ID='${TRACE_ID}' python - <<'PY'
import json
import os
import urllib.parse
import urllib.request

trace_id = os.environ['TRACE_ID']
base = 'http://localhost:8002'

traces_url = base + '/api/v1/traces?' + urllib.parse.urlencode({'trace_id': trace_id, 'limit': 10})
with urllib.request.urlopen(traces_url, timeout=10) as resp:
    traces = json.loads(resp.read().decode('utf-8'))

if traces.get('count', 0) < 1:
    raise SystemExit('traces api returned empty data')

first_trace = traces.get('data', [])[0]
print('traces_count', traces.get('count'))
print('first_trace', json.dumps(first_trace, ensure_ascii=False))

spans_url = base + '/api/v1/traces/' + urllib.parse.quote(trace_id) + '/spans?limit=50'
with urllib.request.urlopen(spans_url, timeout=10) as resp:
    spans = json.loads(resp.read().decode('utf-8'))
if len(spans) < 1:
    raise SystemExit('spans api returned empty data')

first_span = spans[0]
print('spans_count', len(spans))
print('first_span', json.dumps(first_span, ensure_ascii=False))

stats_url = base + '/api/v1/traces/stats'
with urllib.request.urlopen(stats_url, timeout=10) as resp:
    stats = json.loads(resp.read().decode('utf-8'))

required = ('total', 'avg_duration', 'p99_duration', 'error_rate', 'spanCount')
missing = [k for k in required if k not in stats]
if missing:
    raise SystemExit('stats api missing keys: ' + ','.join(missing))

print('stats_sample', {k: stats.get(k) for k in required})
PY"
}

show_redis_group_status() {
  local redis_pod="$1"
  local group_info
  local pending

  log "Redis traces.raw group status (target group: ${TRACE_PENDING_GROUP}):"
  group_info="$(kubectl -n "$NAMESPACE" exec "$redis_pod" -- redis-cli --raw XINFO GROUPS traces.raw || true)"
  printf '%s\n' "$group_info"

  if [[ -z "$group_info" ]]; then
    warn "No group info returned from redis"
    return 0
  fi

  pending="$(printf '%s\n' "$group_info" | awk -v target_group="$TRACE_PENDING_GROUP" '
    $0 == "name" {
      getline
      current_group = $0
      next
    }
    $0 == "pending" {
      getline
      if (current_group == target_group) {
        print $0
        found = 1
        exit
      }
    }
    END {
      if (!found) exit 1
    }
  ' || true)"

  if [[ -z "$pending" ]]; then
    warn "Pending field not found for group=${TRACE_PENDING_GROUP}"
    return 0
  fi

  if [[ "$MAX_PENDING" != "-1" ]] && (( pending > MAX_PENDING )); then
    fail "Pending trace messages too high: pending=${pending}, max_allowed=${MAX_PENDING}"
  fi
}

main() {
  if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
  fi

  require_cmd kubectl

  [[ "$TRACE_ID" =~ ^[0-9a-f]{32}$ ]] || fail "TRACE_ID must be 32 lowercase hex chars"
  [[ "$SPAN_ID" =~ ^[0-9a-f]{16}$ ]] || fail "SPAN_ID must be 16 lowercase hex chars"

  log "Namespace: ${NAMESPACE}"
  log "Trace ID: ${TRACE_ID}"

  local ingest_pod
  local query_pod
  local clickhouse_pod
  local redis_pod

  ingest_pod="$(get_pod "app=ingest-service")"
  query_pod="$(get_pod "app=query-service")"
  clickhouse_pod="$(get_pod "app=clickhouse")"
  redis_pod="$(get_pod "app=redis")"

  [[ -n "${ingest_pod}" ]] || fail "ingest-service pod not found"
  [[ -n "${query_pod}" ]] || fail "query-service pod not found"
  [[ -n "${clickhouse_pod}" ]] || fail "clickhouse pod not found"
  [[ -n "${redis_pod}" ]] || fail "redis pod not found"

  inject_trace "$ingest_pod"
  wait_clickhouse_trace "$clickhouse_pod"
  check_query_apis "$query_pod"
  show_redis_group_status "$redis_pod"

  log "Trace E2E smoke passed: trace_id=${TRACE_ID}"
}

main "$@"
