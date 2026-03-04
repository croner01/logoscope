#!/usr/bin/env bash
set -euo pipefail

# M4-05/M4-06 价值指标检查脚本
# 1) 拉取 value KPI 告警评估
# 2) 写入一条 KPI 快照（用于周趋势沉淀）
# 3) 落盘本地报告，支持按 active_alerts 失败退出

NAMESPACE="${NAMESPACE:-islap}"
TIME_WINDOW="${TIME_WINDOW:-7 DAY}"
SOURCE="${SOURCE:-manual-kpi-check}"
FAIL_ON_ALERTS="${FAIL_ON_ALERTS:-false}"
ARTIFACT_DIR="${ARTIFACT_DIR:-/root/logoscope/reports/value-kpi}"

mkdir -p "$ARTIFACT_DIR"
RUN_ID="value-kpi-$(date -u +%Y%m%d-%H%M%S)-$RANDOM"
REPORT_FILE="${ARTIFACT_DIR}/${RUN_ID}.json"

QUERY_POD="$(kubectl -n "$NAMESPACE" get pod -l app=query-service -o jsonpath='{.items[0].metadata.name}')"
if [[ -z "$QUERY_POD" ]]; then
  echo "[ERROR] query-service pod not found in namespace $NAMESPACE" >&2
  exit 1
fi

PAYLOAD_JSON="$(
kubectl -n "$NAMESPACE" exec "$QUERY_POD" -c query-service -- /bin/sh -lc "
TIME_WINDOW='${TIME_WINDOW}' SOURCE='${SOURCE}' python - <<'PY'
import json
import os
import requests
from datetime import datetime, timezone

base = 'http://127.0.0.1:8002/api/v1'
time_window = os.environ.get('TIME_WINDOW', '7 DAY')
source = os.environ.get('SOURCE', 'manual-kpi-check')

alerts_resp = requests.get(
    f'{base}/value/kpi/alerts',
    params={'time_window': time_window},
    timeout=30,
)
alerts_resp.raise_for_status()
alerts = alerts_resp.json()

snapshot_resp = requests.post(
    f'{base}/value/kpi/snapshots',
    params={'time_window': time_window, 'source': source},
    timeout=30,
)
snapshot_resp.raise_for_status()
snapshot = snapshot_resp.json()

print(json.dumps({
    'generated_at': datetime.now(timezone.utc).isoformat(),
    'time_window': time_window,
    'alerts': alerts,
    'snapshot': snapshot,
}, ensure_ascii=False))
PY
"
)"

printf '%s\n' "$PAYLOAD_JSON" > "$REPORT_FILE"
ln -sfn "$REPORT_FILE" "${ARTIFACT_DIR}/latest.json"
echo "[INFO] value KPI report: $REPORT_FILE"

ACTIVE_ALERTS="$(
python3 - <<'PY' "$PAYLOAD_JSON"
import json
import sys
payload = json.loads(sys.argv[1])
print(int(payload.get('alerts', {}).get('active_alerts', 0)))
PY
)"

echo "[INFO] active value KPI alerts: $ACTIVE_ALERTS"
if [[ "$FAIL_ON_ALERTS" == "true" && "$ACTIVE_ALERTS" -gt 0 ]]; then
  echo "[ERROR] value KPI alert check failed (active_alerts=$ACTIVE_ALERTS)"
  exit 2
fi

exit 0
