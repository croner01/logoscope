#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${NAMESPACE:-islap}"
ARTIFACT_DIR="${ARTIFACT_DIR:-/root/logoscope/reports/exec-runtime-replay-check}"
mkdir -p "$ARTIFACT_DIR"

RUN_LABEL="exec-runtime-replay-check-$(date -u +%Y%m%d-%H%M%S)-$RANDOM"
REPORT_FILE="${ARTIFACT_DIR}/${RUN_LABEL}.json"

fail() {
  printf '[ERROR] %s\n' "$1" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "missing command: $1"
}

require_cmd kubectl

EXEC_POD="$(kubectl -n "$NAMESPACE" get pod -l app=exec-service -o jsonpath='{.items[0].metadata.name}')"
if [[ -z "$EXEC_POD" ]]; then
  fail "exec-service pod not found in namespace $NAMESPACE"
fi

PAYLOAD_JSON="$(
kubectl -n "$NAMESPACE" exec "$EXEC_POD" -c exec-service -- /bin/sh -lc "
python - <<'PY'
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone


BASE = 'http://127.0.0.1:8095/api/v1/exec'
SESSION_ID = 'sess-replay-check'
MESSAGE_ID = 'msg-replay-check'
ACTION_ID = 'act-replay-check'
COMMAND = 'kubectl -n islap get pods -o name'
PURPOSE = '验证 run/event/audit/policy 回放闭环（含 ticket 确认路径）'


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def request_json(method, url, payload=None, timeout=20):
    body = None
    headers = {'Accept': 'application/json'}
    if payload is not None:
        body = json.dumps(payload).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode('utf-8', errors='ignore')
            return int(resp.status), (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode('utf-8', errors='ignore')
        data = {}
        if raw:
            try:
                data = json.loads(raw)
            except Exception:
                data = {'raw': raw}
        return int(exc.code), data
    except Exception as exc:
        return 599, {'error': str(exc)}


precheck_status, precheck_data = request_json(
    'POST',
    f'{BASE}/precheck',
    {
        'session_id': SESSION_ID,
        'message_id': MESSAGE_ID,
        'action_id': ACTION_ID,
        'command': COMMAND,
        'purpose': PURPOSE,
    },
)

precheck_payload = precheck_data if isinstance(precheck_data, dict) else {}
precheck_result = str(precheck_payload.get('status') or '').strip().lower()
requires_confirmation = precheck_result in {'confirmation_required', 'elevation_required'}
confirmation_ticket = str(precheck_payload.get('confirmation_ticket') or '').strip()
requires_elevation = bool(precheck_payload.get('requires_elevation'))

create_payload = {
    'session_id': SESSION_ID,
    'message_id': MESSAGE_ID,
    'action_id': ACTION_ID,
    'command': COMMAND,
    'purpose': PURPOSE,
    'timeout_seconds': 10,
    'confirmed': requires_confirmation,
    'elevated': bool(requires_confirmation and requires_elevation),
    'confirmation_ticket': confirmation_ticket,
}

if precheck_result in {'permission_required', 'denied'} and not confirmation_ticket:
    create_status = precheck_status
    create_data = precheck_payload
else:
    create_status, create_data = request_json(
        'POST',
        f'{BASE}/runs',
        create_payload,
    )
run = create_data.get('run', {}) if isinstance(create_data, dict) else {}
run_id = str(run.get('run_id') or '')
decision_id = str(run.get('policy_decision_id') or '')

terminal_status = ''
poll_payload = {}
for _ in range(20):
    if not run_id:
        break
    poll_status, poll_data = request_json(
        'GET',
        f'{BASE}/runs/{urllib.parse.quote(run_id)}',
    )
    poll_payload = poll_data if isinstance(poll_data, dict) else {}
    current = poll_payload.get('run', {}) if isinstance(poll_payload, dict) else {}
    terminal_status = str(current.get('status') or '')
    if terminal_status in {'completed', 'failed', 'cancelled'}:
        break
    time.sleep(0.5)

replay_status, replay_data = request_json(
    'GET',
    f'{BASE}/runs/{urllib.parse.quote(run_id)}/replay?events_limit=500&decisions_limit=100&audit_limit=100',
)
replay = replay_data if isinstance(replay_data, dict) else {}
events = replay.get('events') if isinstance(replay.get('events'), list) else []
decisions = replay.get('policy_decisions') if isinstance(replay.get('policy_decisions'), list) else []
audits = replay.get('audit_rows') if isinstance(replay.get('audit_rows'), list) else []

event_types = [str(item.get('event_type') or '') for item in events if isinstance(item, dict)]
decision_ids = [str(item.get('decision_id') or '') for item in decisions if isinstance(item, dict)]
audit_run_ids = [str(item.get('run_id') or '') for item in audits if isinstance(item, dict)]

ok = (
    precheck_status == 200
    and precheck_result in {'ok', 'confirmation_required', 'elevation_required'}
    and (not requires_confirmation or bool(confirmation_ticket))
    and create_status == 200
    and bool(run_id)
    and bool(decision_id)
    and replay_status == 200
    and str((replay.get('run') or {}).get('run_id') or '') == run_id
    and 'command_started' in event_types
    and ('command_finished' in event_types or 'command_cancelled' in event_types)
    and decision_id in decision_ids
    and run_id in audit_run_ids
)

print(
    json.dumps(
        {
            'ok': ok,
            'created_at': now_iso(),
            'precheck_status': precheck_status,
            'precheck_result': precheck_result,
            'precheck_requires_confirmation': requires_confirmation,
            'precheck_requires_elevation': requires_elevation,
            'precheck_confirmation_ticket': confirmation_ticket,
            'precheck_response': precheck_data,
            'create_status': create_status,
            'replay_status': replay_status,
            'run_id': run_id,
            'decision_id': decision_id,
            'terminal_status': terminal_status,
            'event_types': event_types,
            'decision_ids': decision_ids,
            'audit_run_ids': audit_run_ids,
            'create_response': create_data,
            'poll_response': poll_payload,
            'replay_response': replay_data,
        },
        ensure_ascii=False,
    )
)
PY
"
)"

printf '%s\n' "$PAYLOAD_JSON" > "$REPORT_FILE"

python3 - "$REPORT_FILE" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as f:
    data = json.load(f)

if not data.get("ok"):
    print(json.dumps(data, ensure_ascii=False, indent=2))
    sys.exit(1)

print(json.dumps(
    {
        "ok": True,
        "report_file": path,
        "run_id": data.get("run_id"),
        "decision_id": data.get("decision_id"),
        "terminal_status": data.get("terminal_status"),
    },
    ensure_ascii=False,
))
PY
