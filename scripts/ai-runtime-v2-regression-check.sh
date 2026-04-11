#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${NAMESPACE:-islap}"
ARTIFACT_DIR="${ARTIFACT_DIR:-/root/logoscope/reports/ai-runtime-v2-regression}"
RUN_FRONTEND_TESTS="${RUN_FRONTEND_TESTS:-1}"

usage() {
  cat <<'EOF'
AI Runtime v2 regression check

Coverage:
  1) 业务追问适配：diagnosis_goal 去重（含 replan 后 action_id 变化）
  2) 超时追问适配：timeout_scope
  3) service 层回退取值：summary/history 也能驱动去重
  4) waiting_user_input 最新态：run.summary 仅保留当前 pending
  5) command bridge 目标上下文字段透传
  6) frontend runtime transcript 合约测试（可开关）

Env vars:
  NAMESPACE           Kubernetes namespace (default: islap)
  ARTIFACT_DIR        Report dir (default: /root/logoscope/reports/ai-runtime-v2-regression)
  RUN_FRONTEND_TESTS  1=run `npm --prefix frontend run test:agent-runtime`, 0=skip (default: 1)

Example:
  scripts/ai-runtime-v2-regression-check.sh
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

mkdir -p "$ARTIFACT_DIR"
RUN_ID="ai-runtime-v2-regression-$(date -u +%Y%m%d-%H%M%S)-$RANDOM"
REPORT_FILE="${ARTIFACT_DIR}/${RUN_ID}.json"

TMP_DIR="$(mktemp -d)"
BACKEND_FILE="${TMP_DIR}/backend.json"
FRONTEND_LOG_FILE="${TMP_DIR}/frontend.log"
trap 'rm -rf "${TMP_DIR}"' EXIT

AI_POD="$(kubectl -n "$NAMESPACE" get pod -l app=ai-service -o jsonpath='{.items[0].metadata.name}')"
if [[ -z "$AI_POD" ]]; then
  fail "ai-service pod not found in namespace $NAMESPACE"
fi

BACKEND_PAYLOAD_JSON="$(
kubectl -n "$NAMESPACE" exec -i "$AI_POD" -c ai-service -- env REGRESSION_RUN_ID="${RUN_ID}" python - <<'PY'
import importlib.util
import json
import os
import pathlib
import shlex
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict

RUN_ID = os.getenv('REGRESSION_RUN_ID', 'ai-runtime-v2-regression')
cases = []
APP_ROOT = pathlib.Path('/app')


def load_module_from_path(module_name: str, module_path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(module_name, str(module_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f'cannot load module: {module_path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def record(case_id: str, passed: bool, detail: Dict[str, Any]) -> None:
    cases.append(
        {
            'id': case_id,
            'passed': bool(passed),
            'detail': detail if isinstance(detail, dict) else {'detail': str(detail)},
        }
    )


try:
    adapter_path = APP_ROOT / 'ai' / 'agent_runtime' / 'user_question_adapter.py'
    service_path = APP_ROOT / 'ai' / 'agent_runtime' / 'service.py'
    bridge_path = APP_ROOT / 'ai' / 'agent_runtime' / 'command_bridge.py'

    adapter_module = load_module_from_path('runtime_user_question_adapter_probe', adapter_path)
    build_business_question = adapter_module.build_business_question

    baseline = build_business_question(
        failure_code='unknown_semantics',
        failure_message='unknown command semantics',
        purpose='定位慢查询根因',
        title='执行命令',
        command='kubectl ...',
    )
    record(
        'case01_question_kind_baseline_is_diagnosis_goal',
        baseline.get('question_kind') == 'diagnosis_goal',
        {'question': baseline},
    )

    deduped = build_business_question(
        failure_code='unknown_semantics',
        failure_message='unknown command semantics',
        purpose='定位慢查询根因',
        title='执行命令',
        command='kubectl ...',
        current_action_id='act-2',
        last_user_input_question_kind='diagnosis_goal',
        last_user_input_action_id='act-1',
        last_user_input_text='先定位根因',
    )
    record(
        'case02_dedup_diagnosis_goal_across_replan',
        deduped.get('question_kind') == 'execution_scope'
        and '前一轮给的排查目标我已收到' in str(deduped.get('prompt') or ''),
        {'question': deduped},
    )

    timeout_question = build_business_question(
        failure_code='command_timed_out',
        failure_message='command timed out after 30s',
        purpose='定位慢查询根因',
        title='执行命令',
        command='kubectl ...',
    )
    record(
        'case03_timeout_maps_to_timeout_scope',
        timeout_question.get('question_kind') == 'timeout_scope',
        {'question': timeout_question},
    )

    base_v2 = 'http://127.0.0.1:8090/api/v2'

    def request_json(method: str, url: str, payload: Dict[str, Any] | None = None):
        body = None
        headers = {'Accept': 'application/json'}
        if payload is not None:
            body = json.dumps(payload).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
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

    def build_generic_exec_spec(command_text: str, timeout_seconds: int = 20) -> Dict[str, Any]:
        safe_timeout = max(3, int(timeout_seconds or 20))
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

    probe_session_id = f'sess-regression-probe-{RUN_ID}'
    probe_conversation_id = f'conv-regression-probe-{RUN_ID}'
    thread_status, thread_payload = request_json(
        'POST',
        f'{base_v2}/threads',
        {
            'session_id': probe_session_id,
            'conversation_id': probe_conversation_id,
            'title': 'ai-runtime-v2 regression probe',
        },
    )
    probe_thread = thread_payload.get('thread') if isinstance(thread_payload, dict) else {}
    probe_thread_id = str((probe_thread or {}).get('thread_id') or '')
    run_status_code, create_run_payload = request_json(
        'POST',
        f'{base_v2}/threads/{urllib.parse.quote(probe_thread_id)}/runs',
        {
            'question': 'regression probe: unknown semantics question-kind transitions',
            'analysis_context': {
                'analysis_type': 'log',
                'service_name': 'query-service',
            },
            'runtime_options': {
                'conversation_id': probe_conversation_id,
                'unknown_semantics_max_retries': 3,
            },
        },
    )
    probe_run = create_run_payload.get('run') if isinstance(create_run_payload, dict) else {}
    probe_run_id = str((probe_run or {}).get('run_id') or '')

    unknown_semantics_probe_command = 'kubectl not-a-real-subcmd'

    action1_status, action1_payload = request_json(
        'POST',
        f'{base_v2}/runs/{urllib.parse.quote(probe_run_id)}/actions/command',
        {
            'action_id': 'act-regression-unknown-a',
            'command': unknown_semantics_probe_command,
            'command_spec': build_generic_exec_spec(unknown_semantics_probe_command, 20),
            'purpose': '尝试执行排查动作',
            'title': '执行未知命令1',
            'tool_name': 'command.exec',
            'confirmed': False,
            'elevated': False,
            'timeout_seconds': 20,
        },
    )
    input1_status, input1_payload = request_json(
        'POST',
        f'{base_v2}/runs/{urllib.parse.quote(probe_run_id)}/input',
        {
            'text': '先定位根因',
            'source': 'user',
        },
    )

    # 等待第一轮输入被消费，避免第二次 action 命中 unresolved pending action。
    probe_snapshot_after_input = {}
    for _ in range(10):
        snap_status, snap_payload = request_json(
            'GET',
            f'{base_v2}/runs/{urllib.parse.quote(probe_run_id)}',
        )
        if snap_status == 200:
            probe_snapshot_after_input = snap_payload
            run_status = str(((snap_payload.get('run') or {}).get('status')) or '').strip().lower()
            if run_status != 'waiting_user_input':
                break
        time.sleep(0.4)

    action2_status, action2_payload = request_json(
        'POST',
        f'{base_v2}/runs/{urllib.parse.quote(probe_run_id)}/actions/command',
        {
            'action_id': 'act-regression-unknown-b',
            'command': unknown_semantics_probe_command,
            'command_spec': build_generic_exec_spec(unknown_semantics_probe_command, 20),
            'purpose': '尝试执行排查动作',
            'title': '执行未知命令2',
            'tool_name': 'command.exec',
            'confirmed': False,
            'elevated': False,
            'timeout_seconds': 20,
        },
    )

    events_status, events_payload = request_json(
        'GET',
        f'{base_v2}/runs/{urllib.parse.quote(probe_run_id)}/events?after_seq=0&limit=500',
    )
    events = events_payload.get('events') if isinstance(events_payload, dict) else []
    if not isinstance(events, list):
        events = []
    waiting_events = [
        event for event in events
        if str(event.get('event_type') or '') == 'action_waiting_user_input'
    ]
    waiting_payloads = [event.get('payload') if isinstance(event.get('payload'), dict) else {} for event in waiting_events]
    waiting_pairs = [
        (
            str(payload.get('action_id') or ''),
            str(payload.get('question_kind') or ''),
            str(payload.get('title') or ''),
        )
        for payload in waiting_payloads
    ]
    latest_waiting_payload = waiting_payloads[-1] if waiting_payloads else {}
    first_waiting_payload = waiting_payloads[0] if waiting_payloads else {}

    actions_status, actions_payload = request_json(
        'GET',
        f'{base_v2}/runs/{urllib.parse.quote(probe_run_id)}/actions?limit=20',
    )
    actions = actions_payload.get('actions') if isinstance(actions_payload, dict) else []
    if not isinstance(actions, list):
        actions = []
    latest_action = actions[0] if actions else {}

    probe_ok_base = (
        thread_status == 200
        and run_status_code == 200
        and probe_thread_id.startswith('thr-')
        and probe_run_id.startswith('run-')
        and action1_status == 200
        and action2_status == 200
    )
    record(
        'case04_service_question_fallback_uses_summary_or_history',
        probe_ok_base
        and str((action1_payload or {}).get('status') or '') == 'waiting_user_input'
        and str((action2_payload or {}).get('status') or '') == 'waiting_user_input'
        and str(first_waiting_payload.get('question_kind') or '') == 'diagnosis_goal'
        and str(latest_waiting_payload.get('question_kind') or '') == 'execution_scope',
        {
            'thread_http_status': thread_status,
            'run_http_status': run_status_code,
            'action1_http_status': action1_status,
            'action2_http_status': action2_status,
            'input1_http_status': input1_status,
            'events_http_status': events_status,
            'waiting_pairs': waiting_pairs,
            'action1_status': (action1_payload or {}).get('status'),
            'action2_status': (action2_payload or {}).get('status'),
            'snapshot_after_input': probe_snapshot_after_input,
            'input1': input1_payload,
        },
    )

    record(
        'case05_latest_pending_user_input_state_only_tracks_current_action',
        actions_status == 200
        and str(latest_action.get('action_id') or '') == 'act-regression-unknown-b'
        and str(latest_action.get('status') or '') == 'waiting_user_input'
        and ('执行范围' in str(latest_action.get('title') or '')),
        {
            'actions_http_status': actions_status,
            'latest_action': latest_action,
            'actions': actions[:5],
        },
    )

    bridge_text = bridge_path.read_text(encoding='utf-8', errors='ignore')
    key_counts = {
        'target_cluster_id': bridge_text.count('"target_cluster_id"'),
        'target_namespace': bridge_text.count('"target_namespace"'),
        'target_node_name': bridge_text.count('"target_node_name"'),
        'resolved_target_context': bridge_text.count('"resolved_target_context"'),
    }
    record(
        'case06_command_bridge_emits_target_context_fields',
        all(value > 0 for value in key_counts.values()),
        {
            'key_counts': key_counts,
        },
    )
except Exception as exc:
    record(
        'case00_backend_probe_runtime_error',
        False,
        {'error': str(exc)},
    )

overall = all(bool(item.get('passed')) for item in cases)
print(
    json.dumps(
        {
            'run_id': RUN_ID,
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'target_service': 'ai-service',
            'overall_passed': overall,
            'cases': cases,
        },
        ensure_ascii=False,
    )
)
PY
)"

if [[ -z "$BACKEND_PAYLOAD_JSON" ]]; then
  fail "empty backend payload"
fi
printf '%s\n' "$BACKEND_PAYLOAD_JSON" > "$BACKEND_FILE"

FRONTEND_STATUS="skipped"
FRONTEND_REASON="frontend runtime transcript test skipped by config"
if [[ "$RUN_FRONTEND_TESTS" == "1" ]]; then
  if command -v npm >/dev/null 2>&1; then
    if npm --prefix frontend run test:agent-runtime >"$FRONTEND_LOG_FILE" 2>&1; then
      FRONTEND_STATUS="passed"
      FRONTEND_REASON="npm --prefix frontend run test:agent-runtime passed"
    else
      FRONTEND_STATUS="failed"
      FRONTEND_REASON="npm --prefix frontend run test:agent-runtime failed"
    fi
  else
    FRONTEND_STATUS="skipped"
    FRONTEND_REASON="npm command not found; frontend case skipped"
  fi
fi

export NAMESPACE
export AI_POD
export RUN_ID
export RUN_FRONTEND_TESTS
export FRONTEND_STATUS
export FRONTEND_REASON
export FRONTEND_LOG_FILE
python3 - "$BACKEND_FILE" "$REPORT_FILE" <<'PY'
import json
import os
import pathlib
import sys
from datetime import datetime, timezone

backend_path = pathlib.Path(sys.argv[1])
report_path = pathlib.Path(sys.argv[2])
backend_payload = json.loads(backend_path.read_text(encoding='utf-8') or '{}')
cases = backend_payload.get('cases') if isinstance(backend_payload.get('cases'), list) else []

frontend_status = os.getenv('FRONTEND_STATUS', 'skipped').strip().lower()
frontend_reason = os.getenv('FRONTEND_REASON', '').strip()
frontend_log_file = os.getenv('FRONTEND_LOG_FILE', '').strip()
frontend_enabled = os.getenv('RUN_FRONTEND_TESTS', '1').strip() == '1'
frontend_detail = {
    'enabled': frontend_enabled,
    'status': frontend_status,
    'reason': frontend_reason,
}

if frontend_status == 'failed':
    log_text = ''
    try:
        log_text = pathlib.Path(frontend_log_file).read_text(encoding='utf-8', errors='ignore')
    except Exception:
        log_text = ''
    frontend_detail['log_tail'] = '\n'.join(log_text.strip().splitlines()[-80:])

if frontend_status == 'passed':
    frontend_case = {
        'id': 'case07_frontend_runtime_transcript_contract',
        'passed': True,
        'detail': frontend_detail,
    }
elif frontend_status == 'failed':
    frontend_case = {
        'id': 'case07_frontend_runtime_transcript_contract',
        'passed': False,
        'detail': frontend_detail,
    }
else:
    frontend_case = {
        'id': 'case07_frontend_runtime_transcript_contract',
        'passed': True,
        'detail': {**frontend_detail, 'skipped': True},
    }

cases.append(frontend_case)
overall_passed = all(bool(item.get('passed')) for item in cases)
report_payload = {
    'run_id': os.getenv('RUN_ID', backend_payload.get('run_id', 'ai-runtime-v2-regression')),
    'generated_at': datetime.now(timezone.utc).isoformat(),
    'namespace': os.getenv('NAMESPACE', ''),
    'ai_pod': os.getenv('AI_POD', ''),
    'overall_passed': overall_passed,
    'backend_probe': backend_payload,
    'cases': cases,
}
report_path.write_text(json.dumps(report_payload, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(report_payload, ensure_ascii=False))
PY

cp "$REPORT_FILE" "${ARTIFACT_DIR}/latest.json"
echo "[INFO] report saved: $REPORT_FILE"

OVERALL="$(python3 -c 'import json,sys; print("true" if json.load(open(sys.argv[1], "r", encoding="utf-8")).get("overall_passed") else "false")' "$REPORT_FILE")"
if [[ "$OVERALL" != "true" ]]; then
  fail "ai runtime v2 regression failed. see: $REPORT_FILE"
fi

echo "[INFO] ai runtime v2 regression passed"
