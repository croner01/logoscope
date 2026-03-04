#!/usr/bin/env bash
set -euo pipefail

# AI KB 前端关键路径（接口契约）检查
# 目标：
# 1) 校验 runtime/options 在本地/远端异常场景可返回可降级策略
# 2) 校验 kb/search 与 manual-remediation 的前置校验错误码
# 3) 校验 providers/outbox 状态字段，支撑前端看板

NAMESPACE="${NAMESPACE:-islap}"
ARTIFACT_DIR="${ARTIFACT_DIR:-/root/logoscope/reports/frontend-ai-kb-critical-path}"

mkdir -p "$ARTIFACT_DIR"
RUN_ID="frontend-ai-kb-critical-$(date -u +%Y%m%d-%H%M%S)-$RANDOM"
REPORT_FILE="${ARTIFACT_DIR}/${RUN_ID}.json"

usage() {
  cat <<'EOF'
Frontend AI KB critical path check

Env vars:
  NAMESPACE    Kubernetes namespace (default: islap)
  ARTIFACT_DIR Report dir (default: /root/logoscope/reports/frontend-ai-kb-critical-path)

Example:
  scripts/frontend-ai-kb-critical-path-check.sh
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

AI_POD="$(kubectl -n "$NAMESPACE" get pod -l app=ai-service -o jsonpath='{.items[0].metadata.name}')"
if [[ -z "$AI_POD" ]]; then
  fail "ai-service pod not found in namespace $NAMESPACE"
fi

PAYLOAD_JSON="$(
kubectl -n "$NAMESPACE" exec "$AI_POD" -c ai-service -- /bin/sh -lc "
python - <<'PY'
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone


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


base = 'http://127.0.0.1:8090/api/v1/ai'
cases = []


def record(case_id, passed, detail):
    cases.append({'id': case_id, 'passed': bool(passed), 'detail': detail})


# case1: remote 未启用，强制本地策略
status, data = request_json('POST', f'{base}/kb/runtime/options', {
    'remote_enabled': False,
    'retrieval_mode': 'hybrid',
    'save_mode': 'local_and_remote',
})
passed = status == 200 and data.get('effective_retrieval_mode') == 'local' and data.get('effective_save_mode') == 'local_only'
record('case1_runtime_options_local_forced', passed, {'http_status': status, 'response': data})

# case2: remote 启用，允许 200 或 409/503（均需可降级字段）
status, data = request_json('POST', f'{base}/kb/runtime/options', {
    'remote_enabled': True,
    'retrieval_mode': 'hybrid',
    'save_mode': 'local_and_remote',
})
detail = data.get('detail') if isinstance(data.get('detail'), dict) else data
effective_retrieval = detail.get('effective_retrieval_mode')
effective_save = detail.get('effective_save_mode')
passed = status in (200, 409, 503) and effective_retrieval in ('local', 'hybrid') and effective_save in ('local_only', 'local_and_remote')
record('case2_runtime_options_remote_toggle', passed, {'http_status': status, 'response': data})

# case3: kb/search 短 query 校验
status, data = request_json('POST', f'{base}/kb/search', {'query': 'a'})
detail = data.get('detail') if isinstance(data.get('detail'), dict) else {}
passed = status == 400 and detail.get('code') == 'KBR-001'
record('case3_kb_search_validation', passed, {'http_status': status, 'response': data})

# case4: manual-remediation 步骤校验
status, data = request_json('PATCH', f'{base}/cases/case-non-exist/manual-remediation', {
    'manual_remediation_steps': ['bad'],
    'verification_result': 'pass',
    'verification_notes': '这是一段足够长的验证说明文本用于关键路径检查。',
})
detail = data.get('detail') if isinstance(data.get('detail'), dict) else {}
passed = status == 400 and detail.get('code') == 'KBR-003'
record('case4_manual_remediation_validation', passed, {'http_status': status, 'response': data})

# case5: providers status 字段
status, data = request_json('GET', f'{base}/kb/providers/status')
passed = status == 200 and all(key in data for key in ['mode', 'remote_available', 'outbox_queue_total', 'outbox_failed'])
record('case5_provider_status_contract', passed, {'http_status': status, 'response': data})

# case6: outbox status 字段
status, data = request_json('GET', f'{base}/kb/outbox/status')
passed = status == 200 and all(key in data for key in ['queue_total', 'pending', 'failed', 'items'])
record('case6_outbox_status_contract', passed, {'http_status': status, 'response': data})

overall = all(item['passed'] for item in cases)
print(json.dumps({
    'run_id': '${RUN_ID}',
    'generated_at': datetime.now(timezone.utc).isoformat(),
    'namespace': '${NAMESPACE}',
    'target_service': 'ai-service',
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
  fail "frontend AI KB critical path failed. see: $REPORT_FILE"
fi

echo "[INFO] frontend AI KB critical path passed"
