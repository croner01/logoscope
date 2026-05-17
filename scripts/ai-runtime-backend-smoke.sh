#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${NAMESPACE:-islap}"
ARTIFACT_DIR="${ARTIFACT_DIR:-/root/logoscope/reports/ai-runtime-backend-smoke}"

mkdir -p "$ARTIFACT_DIR"
RUN_ID="ai-runtime-backend-smoke-$(date -u +%Y%m%d-%H%M%S)-$RANDOM"
REPORT_FILE="${ARTIFACT_DIR}/${RUN_ID}.json"

usage() {
  cat <<'EOF'
AI Runtime backend smoke check

Coverage:
  0) runtime v1 API is disabled (410)
  1) v2 create thread/run contract
  2) initial events contract
  3) SSE stream emits canonical runtime events
  4) mutating command either enters approval or is denied by execution-plane gate
  5) reject approval follows configured strategy (replan or terminate)
  6) exec precheck returns explicit max-chars rejection (no local fallback)
  7) optional OpenHands create run contract
  8) optional OpenHands preview actions contract
  9) optional OpenHands preview action execution path

Env vars:
  NAMESPACE    Kubernetes namespace (default: islap)
  ARTIFACT_DIR Report dir (default: /root/logoscope/reports/ai-runtime-backend-smoke)
  SMOKE_OPENHANDS Run optional OpenHands backend checks (default: false)

Example:
  scripts/ai-runtime-backend-smoke.sh

OpenHands example:
  SMOKE_OPENHANDS=true scripts/ai-runtime-backend-smoke.sh
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

AI_POD="$(
  kubectl -n "$NAMESPACE" get pod \
    -l app=ai-service \
    --field-selector=status.phase=Running \
    -o jsonpath='{.items[0].metadata.name}'
)"
if [[ -z "$AI_POD" ]]; then
  fail "running ai-service pod not found in namespace $NAMESPACE"
fi

PAYLOAD_JSON="$(
kubectl -n "$NAMESPACE" exec "$AI_POD" -c ai-service -- /bin/sh -lc "
export SMOKE_RUN_ID='${RUN_ID}'
export SMOKE_NAMESPACE='${NAMESPACE}'
export SMOKE_OPENHANDS='${SMOKE_OPENHANDS:-false}'
python - <<'PY'
import json
import os
import shlex
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone


BASE_API = 'http://127.0.0.1:8090'
BASE_AI_V1 = f'{BASE_API}/api/v1/ai'
BASE_AI_V2 = f'{BASE_API}/api/v2'
BASE_EXEC = 'http://exec-service:8095/api/v1/exec'
SMOKE_RUN_ID = os.getenv('SMOKE_RUN_ID', 'ai-runtime-backend-smoke')
NAMESPACE = os.getenv('SMOKE_NAMESPACE', 'islap')
SMOKE_OPENHANDS = str(os.getenv('SMOKE_OPENHANDS') or '').strip().lower() in {'1', 'true', 'yes', 'on'}
CONVERSATION_ID = f'conv-{SMOKE_RUN_ID}'
SESSION_ID = f'sess-{SMOKE_RUN_ID}'
QUESTION = '后端 smoke：验证 AI runtime create/events/stream/approval/reject 关键链路'
OPENHANDS_QUESTION = 'query-service query timeout and slow query'
MUTATING_COMMAND = f'kubectl -n {NAMESPACE} rollout restart deployment/definitely-not-exist'
LONG_COMMAND = 'kubectl get pod ' + ('x' * 400)

cases = []


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
            return resp.status, (json.loads(raw) if raw else {})
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


def read_sse_events(url, max_events=4, timeout=10):
    req = urllib.request.Request(url, headers={'Accept': 'text/event-stream'})
    events = []
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        buffer = ''
        while len(events) < max_events:
            chunk = resp.read(256)
            if not chunk:
                break
            buffer += chunk.decode('utf-8', errors='ignore')
            while '\n\n' in buffer and len(events) < max_events:
                block, buffer = buffer.split('\n\n', 1)
                block = block.strip()
                if not block:
                    continue
                event_name = 'message'
                data_text = ''
                for line in block.splitlines():
                    if line.startswith('event:'):
                        event_name = line.split(':', 1)[1].strip() or 'message'
                    elif line.startswith('data:'):
                        data_text += line.split(':', 1)[1].strip()
                parsed = None
                if data_text:
                    try:
                        parsed = json.loads(data_text)
                    except Exception:
                        parsed = {'raw': data_text}
                events.append({'event': event_name, 'data': parsed})
    return events


def build_generic_exec_spec(command_text, timeout_s=10):
    safe_timeout = max(3, int(timeout_s or 10))
    safe_command = str(command_text or '')
    try:
        command_argv = [token for token in shlex.split(safe_command) if str(token).strip()]
    except Exception:
        command_argv = [safe_command] if safe_command else []
    if not command_argv and safe_command:
        command_argv = [safe_command]
    return {
        'tool': 'generic_exec',
        'args': {
            'command': safe_command,
            'command_argv': command_argv,
            'target_kind': 'runtime_node',
            'target_identity': 'runtime:local',
            'timeout_s': safe_timeout,
        },
        'command': safe_command,
        'command_argv': command_argv,
        'target_kind': 'runtime_node',
        'target_identity': 'runtime:local',
        'timeout_s': safe_timeout,
    }


def record(case_id, passed, detail):
    cases.append({'id': case_id, 'passed': bool(passed), 'detail': detail})


def create_runtime_run():
    return request_json(
        'POST',
        f'{BASE_AI_V2}/threads/{urllib.parse.quote(thread_id)}/runs',
        {
            'question': QUESTION,
            'analysis_context': {
                'analysis_type': 'log',
                'service_name': 'query-service',
                'runtime_mode': 'followup_runtime',
                'agent_mode': 'followup_analysis_runtime',
            },
            'runtime_options': {
                'conversation_id': CONVERSATION_ID,
            },
        },
        timeout=30,
    )


def create_openhands_runtime_run():
    return request_json(
        'POST',
        f'{BASE_AI_V2}/threads/{urllib.parse.quote(thread_id)}/runs',
        {
            'question': OPENHANDS_QUESTION,
            'analysis_context': {
                'analysis_type': 'log',
                'service_name': 'query-service',
                'component_type': 'query',
                'namespace': NAMESPACE,
            },
            'runtime_options': {
                'conversation_id': CONVERSATION_ID,
                'auto_exec_readonly': False,
                'enable_skills': True,
                'max_skills': 1,
            },
            'runtime_backend': 'openhands',
        },
        timeout=30,
    )


def is_terminal_status(status):
    return str(status or '').strip().lower() in {'completed', 'failed', 'cancelled', 'blocked'}


v1_status, v1_data = request_json('POST', f'{BASE_AI_V1}/runs', {
    'session_id': SESSION_ID,
    'question': QUESTION,
    'analysis_context': {
        'analysis_type': 'log',
        'service_name': 'query-service',
        'runtime_mode': 'followup_runtime',
        'agent_mode': 'followup_analysis_runtime',
    },
    'runtime_options': {
        'conversation_id': CONVERSATION_ID,
    },
})
record(
    'case0_runtime_v1_api_disabled',
    v1_status == 410 and str(v1_data.get('code') or '') == 'RUNTIME_V1_DISABLED',
    {
        'http_status': v1_status,
        'response': v1_data,
    },
)

thread_status, thread_data = request_json(
    'POST',
    f'{BASE_AI_V2}/threads',
    {
        'session_id': SESSION_ID,
        'conversation_id': CONVERSATION_ID,
        'title': 'ai runtime backend smoke',
    },
)
thread = thread_data.get('thread', {}) if isinstance(thread_data, dict) else {}
thread_id = str(thread.get('thread_id') or '')
session_id = str(thread.get('session_id') or SESSION_ID)

create_status, create_data = create_runtime_run()
run = create_data.get('run', {}) if isinstance(create_data, dict) else {}
run_id = str(run.get('run_id') or '')
run_engine = run.get('engine') if isinstance(run.get('engine'), dict) else {}

record(
    'case1_create_run_contract',
    thread_status == 200
    and create_status == 200
    and thread_id.startswith('thr-')
    and run_id.startswith('run-')
    and str(run_engine.get('outer') or '').startswith('temporal')
    and str(run_engine.get('inner') or '').startswith('langgraph')
    and str(run.get('status') or '') in {'queued', 'running', 'waiting_user_input', 'waiting_approval'},
    {
        'thread_http_status': thread_status,
        'thread': thread,
        'http_status': create_status,
        'run': run,
    },
)

events_status, events_data = request_json(
    'GET',
    f'{BASE_AI_V2}/runs/{urllib.parse.quote(run_id)}/events?after_seq=0&limit=20',
)
initial_events = events_data.get('events') if isinstance(events_data, dict) else []
if not isinstance(initial_events, list):
    initial_events = []
event_types = [
    str(item.get('event_type') or '')
    for item in initial_events
]
record(
    'case2_initial_events_contract',
    events_status == 200
    and 'run_started' in event_types
    and 'message_started' in event_types
    and 'reasoning_step' in event_types,
    {
        'http_status': events_status,
        'event_types': event_types,
    },
)

stream_events = []
stream_error = None
try:
    stream_events = read_sse_events(
        f'{BASE_AI_V2}/runs/{urllib.parse.quote(run_id)}/events/stream?after_seq=0',
        max_events=4,
        timeout=10,
    )
except Exception as exc:
    stream_error = str(exc)
stream_event_names = [str(item.get('event') or '') for item in stream_events]
record(
    'case3_stream_contract',
    not stream_error and 'run_started' in stream_event_names and 'message_started' in stream_event_names,
    {
        'error': stream_error,
        'events': stream_events,
    },
)

command_run_id = run_id
command_run_bootstrap = {
    'mode': 'reuse_initial_run',
    'http_status': 0,
    'response': {},
}
bootstrap_snapshot_status, bootstrap_snapshot_data = request_json(
    'GET',
    f'{BASE_AI_V2}/runs/{urllib.parse.quote(run_id)}',
)
bootstrap_snapshot_run = bootstrap_snapshot_data.get('run', {}) if isinstance(bootstrap_snapshot_data, dict) else {}
if (
    not command_run_id
    or is_terminal_status(bootstrap_snapshot_run.get('status'))
):
    command_run_bootstrap_status, command_run_bootstrap_data = create_runtime_run()
    command_run_bootstrap_run = (
        command_run_bootstrap_data.get('run', {})
        if isinstance(command_run_bootstrap_data, dict)
        else {}
    )
    candidate_command_run_id = str(command_run_bootstrap_run.get('run_id') or '')
    if candidate_command_run_id:
        command_run_id = candidate_command_run_id
    command_run_bootstrap = {
        'mode': 'created_new_run',
        'http_status': command_run_bootstrap_status,
        'response': command_run_bootstrap_data,
        'snapshot_http_status': bootstrap_snapshot_status,
        'snapshot_run': bootstrap_snapshot_run,
    }

command_status, command_data = request_json(
    'POST',
    f'{BASE_AI_V2}/runs/{urllib.parse.quote(command_run_id)}/actions/command',
    {
        'command': MUTATING_COMMAND,
        'command_spec': build_generic_exec_spec(MUTATING_COMMAND, 10),
        'purpose': 'smoke approval trigger',
        'title': 'smoke approval trigger',
        'tool_name': 'command.exec',
        'confirmed': False,
        'elevated': False,
        'timeout_seconds': 10,
    },
    timeout=45,
)
approval = command_data.get('approval', {}) if isinstance(command_data, dict) else {}
approval_id = str(approval.get('approval_id') or '')
if command_status == 409 and 'run is already terminal' in str(command_data).lower():
    retry_run_status, retry_run_data = create_runtime_run()
    retry_run = retry_run_data.get('run', {}) if isinstance(retry_run_data, dict) else {}
    retry_run_id = str(retry_run.get('run_id') or '')
    if retry_run_id:
        command_run_id = retry_run_id
        command_run_bootstrap = {
            'mode': 'retry_after_terminal',
            'http_status': retry_run_status,
            'response': retry_run_data,
        }
        command_status, command_data = request_json(
            'POST',
            f'{BASE_AI_V2}/runs/{urllib.parse.quote(command_run_id)}/actions/command',
            {
                'command': MUTATING_COMMAND,
                'command_spec': build_generic_exec_spec(MUTATING_COMMAND, 10),
                'purpose': 'smoke approval trigger',
                'title': 'smoke approval trigger',
                'tool_name': 'command.exec',
                'confirmed': False,
                'elevated': False,
                'timeout_seconds': 10,
            },
            timeout=45,
        )
        approval = command_data.get('approval', {}) if isinstance(command_data, dict) else {}
        approval_id = str(approval.get('approval_id') or '')

refetched_status, refetched_data = request_json('GET', f'{BASE_AI_V2}/runs/{urllib.parse.quote(command_run_id)}')
refetched_run = refetched_data.get('run', {}) if isinstance(refetched_data, dict) else {}
command_result_status = str(command_data.get('status') or '')
command_error = str(command_data.get('error') or '').strip().lower()
approval_path = (
    command_status == 200
    and command_result_status in {'elevation_required', 'confirmation_required'}
    and bool(approval_id)
    and str(refetched_run.get('status') or '') == 'waiting_approval'
)
policy_denied_path = (
    command_status == 200
    and command_result_status == 'permission_required'
    and str(refetched_run.get('status') or '') in {'running', 'waiting_user_input'}
)
diagnosis_reask_path = (
    command_status == 200
    and command_result_status == 'waiting_user_input'
    and str((command_data.get('error') or {}).get('code') or '') == 'diagnosis_contract_incomplete'
    and str(refetched_run.get('status') or '') == 'waiting_user_input'
)
pending_user_input = refetched_run.get('summary', {}).get('pending_user_input', {}) if isinstance(refetched_run.get('summary'), dict) else {}
timeout_reask_path = (
    command_status == 599
    and 'timed out' in command_error
    and str(refetched_run.get('status') or '') == 'waiting_user_input'
    and 'diagnosis_contract' in str(pending_user_input.get('reason') or '').lower()
)
record(
    'case4_mutating_command_requires_approval_or_policy_deny',
    approval_path or policy_denied_path or diagnosis_reask_path or timeout_reask_path,
    {
        'http_status': command_status,
        'response': command_data,
        'refetched_run': refetched_run,
        'refetched_http_status': refetched_status,
        'command_run_id': command_run_id,
        'command_run_bootstrap': command_run_bootstrap,
        'approval_path': approval_path,
        'policy_denied_path': policy_denied_path,
        'diagnosis_reask_path': diagnosis_reask_path,
        'timeout_reask_path': timeout_reask_path,
    },
)

if approval_id:
    reject_status, reject_data = request_json(
        'POST',
        f'{BASE_AI_V2}/runs/{urllib.parse.quote(command_run_id)}/approvals/{urllib.parse.quote(approval_id)}/resolve',
        {
            'decision': 'rejected',
            'comment': 'smoke reject path',
            'confirmed': False,
            'elevated': False,
        },
    )
    reject_run = reject_data.get('run', {}) if isinstance(reject_data, dict) else {}
    reject_events_status = 0
    reject_event_types = []
    approval_resolved_status = ''
    approval_list_status = 0
    for _ in range(12):
        snapshot_status, snapshot_data = request_json('GET', f'{BASE_AI_V2}/runs/{urllib.parse.quote(command_run_id)}')
        snapshot_run = snapshot_data.get('run', {}) if isinstance(snapshot_data, dict) else {}
        if isinstance(snapshot_run, dict) and snapshot_run:
            reject_run = snapshot_run
        reject_events_status, reject_events_data = request_json(
            'GET',
            f'{BASE_AI_V2}/runs/{urllib.parse.quote(command_run_id)}/events?after_seq=0&limit=300',
        )
        reject_events = reject_events_data.get('events') if isinstance(reject_events_data, dict) else []
        if not isinstance(reject_events, list):
            reject_events = []
        reject_event_types = [str(item.get('event_type') or '') for item in reject_events]
        approval_list_status, approval_list_data = request_json(
            'GET',
            f'{BASE_AI_V2}/runs/{urllib.parse.quote(command_run_id)}/approvals',
        )
        approvals = approval_list_data.get('approvals') if isinstance(approval_list_data, dict) else []
        if not isinstance(approvals, list):
            approvals = []
        for item in approvals:
            if str(item.get('approval_id') or '') == approval_id:
                approval_resolved_status = str(item.get('status') or '').strip().lower()
                break
        if approval_resolved_status == 'rejected':
            break
        time.sleep(1.0)
    record(
        'case5_reject_transitions_by_policy',
        reject_status == 200
        and approval_resolved_status == 'rejected'
        and (
            'run_status_changed' in reject_event_types
            or str(reject_run.get('status') or '') in {'running', 'blocked', 'waiting_user_input', 'waiting_approval'}
        )
        and (
            'action_replanned' in reject_event_types
            or str(reject_run.get('status') or '') in {'running', 'blocked', 'waiting_user_input', 'waiting_approval'}
        ),
        {
            'http_status': reject_status,
            'run': reject_run,
            'approval_status': approval_resolved_status,
            'approval_list_status': approval_list_status,
            'event_types': reject_event_types[-12:],
            'reject_events_status': reject_events_status,
        },
    )
else:
    record(
        'case5_reject_transitions_by_policy',
        True,
        {
            'skipped': True,
            'reason': 'no approval ticket issued (policy deny or diagnosis contract re-ask gate)',
            'command_status': command_result_status,
        },
    )

precheck_status, precheck_data = request_json(
    'POST',
    f'{BASE_EXEC}/precheck',
    {
        'session_id': session_id,
        'message_id': str(run.get('assistant_message_id') or ''),
        'action_id': 'act-long-command',
        'command': LONG_COMMAND,
    },
)
precheck_message = str(precheck_data.get('message') or '').lower()
precheck_policy_result = str((precheck_data.get('policy_decision') or {}).get('result') or '').lower()
record(
    'case6_precheck_rejects_overlong_command',
    precheck_status == 200
    and str(precheck_data.get('status') or '') == 'permission_required'
    and (
        'max chars' in precheck_message
        or 'policy denied by opa' in precheck_message
        or precheck_policy_result == 'deny'
    )
    and str(precheck_data.get('dispatch_backend') or '') != 'local_fallback'
    and str(precheck_data.get('effective_executor_type') or '') != 'local_process',
    {
        'http_status': precheck_status,
        'response': precheck_data,
    },
)

openhands_run_id = ''
openhands_action_id = ''
openhands_action_exec_status = 0
openhands_action_exec_data = {}
if SMOKE_OPENHANDS:
    openhands_status, openhands_data = create_openhands_runtime_run()
    openhands_run = openhands_data.get('run', {}) if isinstance(openhands_data, dict) else {}
    openhands_run_id = str(openhands_run.get('run_id') or '')
    openhands_engine = openhands_run.get('engine') if isinstance(openhands_run.get('engine'), dict) else {}
    openhands_summary = openhands_run.get('summary') if isinstance(openhands_run.get('summary'), dict) else {}
    openhands_inner_backend = openhands_summary.get('inner_backend') if isinstance(openhands_summary.get('inner_backend'), dict) else {}
    record(
        'case7_openhands_create_run_contract',
        openhands_status == 200
        and openhands_run_id.startswith('run-')
        and str(openhands_engine.get('inner') or '') == 'openhands-v1'
        and str(openhands_inner_backend.get('backend') or '') == 'openhands-v1',
        {
            'http_status': openhands_status,
            'run': openhands_run,
        },
    )

    openhands_actions_status, openhands_actions_data = request_json(
        'GET',
        f'{BASE_AI_V2}/runs/{urllib.parse.quote(openhands_run_id)}/actions?limit=20',
    )
    openhands_actions = openhands_actions_data.get('actions') if isinstance(openhands_actions_data, dict) else []
    if not isinstance(openhands_actions, list):
        openhands_actions = []
    first_openhands_action = openhands_actions[0] if openhands_actions else {}
    openhands_action_id = str(first_openhands_action.get('action_id') or '')
    record(
        'case8_openhands_preview_actions_contract',
        openhands_actions_status == 200
        and bool(openhands_action_id)
        and str(first_openhands_action.get('status') or '') == 'planned'
        and str(first_openhands_action.get('reason_code') or '') == 'inner_backend_preview'
        and bool(str(first_openhands_action.get('skill_name') or ''))
        and bool(str(first_openhands_action.get('step_id') or '')),
        {
            'http_status': openhands_actions_status,
            'actions': openhands_actions[:3],
        },
    )

    if openhands_action_id:
        openhands_action_exec_status, openhands_action_exec_data = request_json(
            'POST',
            f'{BASE_AI_V2}/runs/{urllib.parse.quote(openhands_run_id)}/actions/command',
            {
                'action_id': openhands_action_id,
                'purpose': '',
                'title': '',
                'command': '',
                'command_spec': {},
                'confirmed': False,
                'elevated': False,
                'timeout_seconds': 20,
            },
            timeout=45,
        )
    openhands_action_exec_result = str(openhands_action_exec_data.get('status') or '')
    record(
        'case9_openhands_preview_action_exec_path',
        openhands_action_exec_status == 200
        and openhands_action_exec_result in {
            'completed',
            'running',
            'permission_required',
            'confirmation_required',
            'elevation_required',
            'waiting_user_input',
            'blocked',
        },
        {
            'http_status': openhands_action_exec_status,
            'action_id': openhands_action_id,
            'response': openhands_action_exec_data,
        },
    )
else:
    record(
        'case7_openhands_create_run_contract',
        True,
        {'skipped': True, 'reason': 'set SMOKE_OPENHANDS=true to enable'},
    )
    record(
        'case8_openhands_preview_actions_contract',
        True,
        {'skipped': True, 'reason': 'set SMOKE_OPENHANDS=true to enable'},
    )
    record(
        'case9_openhands_preview_action_exec_path',
        True,
        {'skipped': True, 'reason': 'set SMOKE_OPENHANDS=true to enable'},
    )

overall = all(item['passed'] for item in cases)
print(json.dumps({
    'run_id': SMOKE_RUN_ID,
    'generated_at': datetime.now(timezone.utc).isoformat(),
    'namespace': NAMESPACE,
    'target_service': 'ai-service',
    'runtime_run_id': run_id,
    'command_run_id': command_run_id,
    'openhands_enabled': SMOKE_OPENHANDS,
    'openhands_run_id': openhands_run_id,
    'openhands_action_id': openhands_action_id,
    'runtime_session_id': session_id,
    'overall_passed': overall,
    'cases': cases,
}, ensure_ascii=False))
PY
"
)"

if [[ -z "$PAYLOAD_JSON" ]]; then
  fail "empty report payload"
fi

echo "$PAYLOAD_JSON" > "$REPORT_FILE"
echo "$PAYLOAD_JSON" > "${ARTIFACT_DIR}/latest.json"
echo "[INFO] report saved: $REPORT_FILE"

OVERALL="$(printf '%s' "$PAYLOAD_JSON" | python3 -c 'import json,sys; print("true" if json.load(sys.stdin).get("overall_passed") else "false")')"
if [[ "$OVERALL" != "true" ]]; then
  fail "ai runtime backend smoke failed. see: $REPORT_FILE"
fi

echo "[INFO] ai runtime backend smoke passed"
