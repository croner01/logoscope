#!/usr/bin/env bash
set -euo pipefail

# M4-04 灰度复盘辅助脚本
# 生成两业务域灰度摘要并输出 Markdown 报告。

NAMESPACE="${NAMESPACE:-islap}"
DOMAIN_A_SERVICES="${DOMAIN_A_SERVICES:-frontend,query-service}"
DOMAIN_B_SERVICES="${DOMAIN_B_SERVICES:-ingest-service,topology-service}"
REPORT_DIR="${REPORT_DIR:-/root/logoscope/reports/canary}"
TIME_WINDOW="${TIME_WINDOW:-24 HOUR}"

mkdir -p "$REPORT_DIR"
REPORT_FILE="${REPORT_DIR}/canary-retro-$(date -u +%Y%m%d-%H%M%S).md"

QUERY_POD="$(kubectl -n "$NAMESPACE" get pod -l app=query-service -o jsonpath='{.items[0].metadata.name}')"
if [[ -z "$QUERY_POD" ]]; then
  echo "[ERROR] query-service pod not found in namespace $NAMESPACE" >&2
  exit 1
fi

DATA_JSON="$(
kubectl -n "$NAMESPACE" exec "$QUERY_POD" -c query-service -- /bin/sh -lc "
DOMAIN_A_SERVICES='${DOMAIN_A_SERVICES}' DOMAIN_B_SERVICES='${DOMAIN_B_SERVICES}' TIME_WINDOW='${TIME_WINDOW}' python - <<'PY'
import os
import json
import time
from datetime import datetime, timezone
import requests

base = 'http://127.0.0.1:8002/api/v1'
domain_a = [s.strip() for s in os.environ['DOMAIN_A_SERVICES'].split(',') if s.strip()]
domain_b = [s.strip() for s in os.environ['DOMAIN_B_SERVICES'].split(',') if s.strip()]
time_window = os.environ['TIME_WINDOW']
api_timeout = int(os.environ.get('API_TIMEOUT', '20'))
api_retries = int(os.environ.get('API_RETRIES', '3'))
retry_sleep = float(os.environ.get('RETRY_SLEEP_SECONDS', '2'))

def request_json(method, path, params=None):
    last_exc = None
    for attempt in range(1, api_retries + 1):
        try:
            if method == 'GET':
                resp = requests.get(f'{base}{path}', params=params or {}, timeout=api_timeout)
            else:
                resp = requests.post(f'{base}{path}', params=params or {}, timeout=api_timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as exc:
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            # 4xx(除429)通常是参数错误，直接失败；timeout/5xx/429做重试。
            if status is not None and 400 <= status < 500 and status != 429:
                raise
            last_exc = exc
        except ValueError as exc:
            last_exc = exc

        if attempt < api_retries:
            time.sleep(retry_sleep * attempt)

    raise SystemExit(
        f'request failed after {api_retries} attempts: method={method} path={path} error={last_exc}'
    )

def domain_trace_lite_count(services):
    total = 0
    for svc in services:
        data = request_json('GET', '/trace-lite/inferred', {
            'time_window': time_window,
            'source_service': svc,
            'limit': 200,
        })
        total += int(data.get('count', 0))
    return total

kpi = request_json('GET', '/value/kpi', {'time_window': '7 DAY'})
alerts = request_json('GET', '/value/kpi/alerts', {'time_window': '7 DAY'})
snapshot = request_json('POST', '/value/kpi/snapshots', {'time_window': '7 DAY', 'source': 'canary-retro'})
domain_a_edges = domain_trace_lite_count(domain_a)
domain_b_edges = domain_trace_lite_count(domain_b)

print(json.dumps({
    'generated_at': datetime.now(timezone.utc).isoformat(),
    'time_window': time_window,
    'domain_a': domain_a,
    'domain_b': domain_b,
    'domain_a_edges': domain_a_edges,
    'domain_b_edges': domain_b_edges,
    'metrics': kpi.get('metrics', {}),
    'gate': kpi.get('release_gate_summary', {}),
    'active_alerts': alerts.get('active_alerts', 0),
    'alert_count': len(alerts.get('alerts', [])),
    'snapshot_id': snapshot.get('snapshot_id'),
}, ensure_ascii=False))
PY
"
)"

python3 - <<'PY' "$DATA_JSON" "$REPORT_FILE"
import json
import sys

payload = json.loads(sys.argv[1])
report_file = sys.argv[2]

content = f"""# Canary Retrospective Snapshot

generated_at: {payload.get('generated_at')}
time_window: {payload.get('time_window')}

## Domains

- Domain-A: {', '.join(payload.get('domain_a', []))}
- Domain-B: {', '.join(payload.get('domain_b', []))}

## KPI Snapshot

- MTTD(min): {payload.get('metrics', {}).get('mttd_minutes', 0)}
- MTTR(min): {payload.get('metrics', {}).get('mttr_minutes', 0)}
- trace-log correlation: {payload.get('metrics', {}).get('trace_log_correlation_rate', 0)}
- topology coverage: {payload.get('metrics', {}).get('topology_coverage_rate', 0)}
- release pass rate: {payload.get('metrics', {}).get('release_regression_pass_rate', 0)}

## Canary Data

- Domain-A inferred edge count: {payload.get('domain_a_edges', 0)}
- Domain-B inferred edge count: {payload.get('domain_b_edges', 0)}

## Release Gate Summary

- total: {payload.get('gate', {}).get('total', 0)}
- passed: {payload.get('gate', {}).get('passed', 0)}
- failed: {payload.get('gate', {}).get('failed', 0)}
- bypassed: {payload.get('gate', {}).get('bypassed', 0)}

## Value KPI Alert Snapshot

- active alerts: {payload.get('active_alerts', 0)}
- alert count: {payload.get('alert_count', 0)}
- snapshot id: {payload.get('snapshot_id', 'unknown')}
"""

with open(report_file, "w", encoding="utf-8") as f:
    f.write(content)
PY

echo "[INFO] Canary retrospective report generated: $REPORT_FILE"
