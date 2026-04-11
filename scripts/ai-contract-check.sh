#!/usr/bin/env bash
set -euo pipefail

# SE-04 AI 接口契约检查脚本
# 校验项：
# 1) analyze-log-llm (use_llm=false) 返回统一结构
# 2) analyze-trace 使用 trace_id 返回统一结构
# 3) analyze-trace-llm 使用 trace_id 返回统一结构（LLM 未配置时允许 analysis_method=none）
# 4) analyze-trace 传 trace_data 被拒绝（422）
# 5) analyze-trace 传空 trace_id 返回 400
# 6) llm/runtime 与 llm/runtime/validate 契约可用
# 7) cases API 支持 create -> resolve -> delete -> list 不可见

NAMESPACE="${NAMESPACE:-islap}"
ARTIFACT_DIR="${ARTIFACT_DIR:-/root/logoscope/reports/ai-contract}"
TRACE_ID="${TRACE_ID:-trace-contract-smoke-001}"

mkdir -p "$ARTIFACT_DIR"
RUN_ID="ai-contract-$(date -u +%Y%m%d-%H%M%S)-$RANDOM"
REPORT_FILE="${ARTIFACT_DIR}/${RUN_ID}.json"

usage() {
  cat <<'EOF'
AI contract check (SE-04)

Env vars:
  NAMESPACE    Kubernetes namespace (default: islap)
  ARTIFACT_DIR Report output dir (default: /root/logoscope/reports/ai-contract)
  TRACE_ID     Trace id used for smoke request (default: trace-contract-smoke-001)

Example:
  scripts/ai-contract-check.sh
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

QUERY_POD="$(kubectl -n "$NAMESPACE" get pod -l app=query-service -o jsonpath='{.items[0].metadata.name}')"
if [[ -z "$QUERY_POD" ]]; then
  fail "query-service pod not found in namespace $NAMESPACE"
fi

PAYLOAD_JSON="$(
kubectl -n "$NAMESPACE" exec -i "$QUERY_POD" -c query-service -- env \
  TRACE_ID="${TRACE_ID}" \
  RUN_ID="${RUN_ID}" \
  python - <<'PY'
import json
import os
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone


def http_json(method, url, data=None, timeout=60):
    body = None
    headers = {}
    if data is not None:
        body = json.dumps(data).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode('utf-8', errors='ignore')
            payload = json.loads(raw) if raw else {}
            return int(resp.status), payload, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode('utf-8', errors='ignore')
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return int(exc.code), payload, raw
    except urllib.error.URLError as exc:
        return 599, {"error": str(exc)}, str(exc)


def has_analysis_shape(payload):
    if not isinstance(payload, dict):
        return False
    required = ("overview", "rootCauses", "solutions", "similarCases")
    return all(key in payload for key in required)


def has_overview_shape(payload):
    overview = payload.get("overview") if isinstance(payload, dict) else None
    if not isinstance(overview, dict):
        return False
    required = ("problem", "severity", "description", "confidence")
    return all(key in overview for key in required)


def record_case(cases, case_id, passed, detail):
    cases.append({
        "id": case_id,
        "passed": bool(passed),
        "detail": detail,
    })


trace_id = os.getenv("TRACE_ID", "trace-contract-smoke-001")
run_id = os.getenv("RUN_ID", "ai-contract")
semantic_base = os.getenv("AI_BASE_URL", "http://ai-service:8090/api/v1/ai")
cases = []

# case1: ai health
status, payload, _ = http_json("GET", f"{semantic_base}/health")
case1_ok = status == 200 and payload.get("status") == "healthy"
record_case(
    cases,
    "case1_ai_health",
    case1_ok,
    {"status_code": status, "service": payload.get("service"), "llm_enabled": payload.get("llm_enabled")},
)

# case2: analyze-log-llm with use_llm=false must return canonical shape
analysis_session_id = ""
assistant_message_id = ""
status, payload, _ = http_json(
    "POST",
    f"{semantic_base}/analyze-log-llm",
    data={
        "log_content": "Database connection timeout",
        "service_name": "query-service",
        "context": {"trace_id": trace_id, "source_service": "frontend"},
        "use_llm": False,
    },
)
case2_ok = status == 200 and has_analysis_shape(payload) and has_overview_shape(payload)
if isinstance(payload, dict) and payload.get("session_id"):
    analysis_session_id = str(payload.get("session_id"))
record_case(
    cases,
    "case2_analyze_log_rule_shape",
    case2_ok,
    {"status_code": status, "analysis_method": payload.get("analysis_method"), "session_id": analysis_session_id, "keys": sorted(payload.keys()) if isinstance(payload, dict) else []},
)

# case3: analyze-trace with trace_id must return canonical shape
status, payload, _ = http_json(
    "POST",
    f"{semantic_base}/analyze-trace",
    data={"trace_id": trace_id, "service_name": "query-service"},
)
case3_ok = status == 200 and has_analysis_shape(payload) and has_overview_shape(payload)
record_case(
    cases,
    "case3_analyze_trace_trace_id_shape",
    case3_ok,
    {"status_code": status, "keys": sorted(payload.keys()) if isinstance(payload, dict) else []},
)

# case4: analyze-trace-llm with trace_id must return canonical shape
status, payload, _ = http_json(
    "POST",
    f"{semantic_base}/analyze-trace-llm",
    data={"trace_id": trace_id, "service_name": "query-service"},
)
case4_ok = status == 200 and has_analysis_shape(payload) and has_overview_shape(payload)
record_case(
    cases,
    "case4_analyze_trace_llm_trace_id_shape",
    case4_ok,
    {
        "status_code": status,
        "analysis_method": payload.get("analysis_method"),
        "error": payload.get("error"),
        "keys": sorted(payload.keys()) if isinstance(payload, dict) else [],
    },
)

# case5: analyze-trace should reject legacy trace_data
status, payload, _ = http_json(
    "POST",
    f"{semantic_base}/analyze-trace",
    data={"trace_data": trace_id, "service_name": "query-service"},
)
details = payload.get("detail") if isinstance(payload, dict) else None
missing_trace_id = False
if isinstance(details, list):
    for item in details:
        loc = item.get("loc") if isinstance(item, dict) else None
        if isinstance(loc, list) and "trace_id" in loc:
            missing_trace_id = True
            break
case5_ok = status == 422 and missing_trace_id
record_case(
    cases,
    "case5_reject_legacy_trace_data",
    case5_ok,
    {"status_code": status, "detail": details},
)

# case6: analyze-trace with blank trace_id should return 400
status, payload, _ = http_json(
    "POST",
    f"{semantic_base}/analyze-trace",
    data={"trace_id": "   ", "service_name": "query-service"},
)
case6_ok = status == 400 and payload.get("detail") == "trace_id is required"
record_case(
    cases,
    "case6_reject_blank_trace_id",
    case6_ok,
    {"status_code": status, "detail": payload.get("detail")},
)

# case7: llm runtime status contract
status, payload, _ = http_json("GET", f"{semantic_base}/llm/runtime")
case7_ok = (
    status == 200
    and isinstance(payload, dict)
    and "configured_provider" in payload
    and "llm_enabled" in payload
    and isinstance(payload.get("supported_providers"), list)
)
record_case(
    cases,
    "case7_llm_runtime_status_contract",
    case7_ok,
    {
        "status_code": status,
        "provider": payload.get("configured_provider") if isinstance(payload, dict) else None,
        "llm_enabled": payload.get("llm_enabled") if isinstance(payload, dict) else None,
    },
)

# case8: llm runtime validate contract
status, payload, _ = http_json(
    "POST",
    f"{semantic_base}/llm/runtime/validate",
    data={
        "provider": "local",
        "model": "qwen2.5",
        "api_base": "http://127.0.0.1:11434/v1",
        "local_model_path": "/models/qwen2.5",
        "extra": {"purpose": "contract-check"},
    },
)
runtime = payload.get("runtime") if isinstance(payload, dict) else {}
case8_ok = status == 200 and payload.get("validated") is True and runtime.get("provider") == "local"
record_case(
    cases,
    "case8_llm_runtime_validate_contract",
    case8_ok,
    {"status_code": status, "validated": payload.get("validated"), "runtime_provider": runtime.get("provider")},
)

# case9: create case
created_case_id = ""
status, payload, _ = http_json(
    "POST",
    f"{semantic_base}/cases",
    data={
        "problem_type": "database",
        "severity": "high",
        "summary": f"AI contract synthetic case {run_id}",
        "log_content": f"[{run_id}] Database connection timeout",
        "service_name": "query-service",
        "root_causes": ["connection timeout"],
        "solutions": [{"title": "scale connection pool", "description": "increase max connections", "steps": ["tune pool config"]}],
        "context": {"source": "ai-contract-check", "run_id": run_id},
        "source": "ai-contract-check",
        "tags": ["contract", "synthetic"],
    },
)
if isinstance(payload, dict) and payload.get("id"):
    created_case_id = str(payload.get("id"))
case9_ok = status == 200 and bool(created_case_id)
record_case(
    cases,
    "case9_create_case",
    case9_ok,
    {"status_code": status, "case_id": created_case_id},
)

# case10: resolve case
case10_ok = False
case10_detail = {"status_code": None, "resolved": None, "case_id": created_case_id}
if created_case_id:
    status, payload, _ = http_json(
        "PATCH",
        f"{semantic_base}/cases/{created_case_id}/resolve",
        data={"resolution": "resolved by ai-contract-check"},
    )
    case10_ok = status == 200 and payload.get("resolved") is True
    case10_detail = {"status_code": status, "resolved": payload.get("resolved"), "case_id": created_case_id}
record_case(cases, "case10_resolve_case", case10_ok, case10_detail)

# case11: delete case
case11_ok = False
case11_detail = {"status_code": None, "status": None, "case_id": created_case_id}
if created_case_id:
    status, payload, _ = http_json("DELETE", f"{semantic_base}/cases/{created_case_id}")
    case11_ok = status == 200 and payload.get("status") == "ok"
    case11_detail = {"status_code": status, "status": payload.get("status"), "case_id": created_case_id}
record_case(cases, "case11_delete_case", case11_ok, case11_detail)

# case12: deleted case should not be visible in list
case12_ok = False
case12_detail = {"status_code": None, "still_exists": None, "case_id": created_case_id}
if created_case_id:
    status, payload, _ = http_json("GET", f"{semantic_base}/cases?limit=200")
    listed_cases = payload.get("cases") if isinstance(payload, dict) and isinstance(payload.get("cases"), list) else []
    still_exists = any(
        isinstance(item, dict) and str(item.get("id", "")) == created_case_id
        for item in listed_cases
    )
    case12_ok = status == 200 and not still_exists
    case12_detail = {"status_code": status, "still_exists": still_exists, "case_id": created_case_id}
record_case(cases, "case12_deleted_case_not_visible", case12_ok, case12_detail)

# case13: follow-up with analysis session should work
case13_ok = False
case13_detail = {"status_code": None, "analysis_session_id": analysis_session_id, "answer_exists": False}
if analysis_session_id:
    status, payload, _ = http_json(
        "POST",
        f"{semantic_base}/follow-up",
        data={
            "analysis_session_id": analysis_session_id,
            "question": "请给出下一步最优先排查动作",
            "analysis_context": {
                "analysis_type": "log",
                "service_name": "query-service",
            },
            "history": [],
            "reset": False,
        },
    )
    answer = payload.get("answer") if isinstance(payload, dict) else ""
    history = payload.get("history") if isinstance(payload, dict) and isinstance(payload.get("history"), list) else []
    returned_session_id = payload.get("analysis_session_id") if isinstance(payload, dict) else ""
    case13_ok = status == 200 and bool(answer) and str(returned_session_id) == analysis_session_id and len(history) >= 2
    case13_detail = {
        "status_code": status,
        "analysis_session_id": returned_session_id,
        "answer_exists": bool(answer),
        "history_len": len(history),
    }
record_case(cases, "case13_follow_up_with_session", case13_ok, case13_detail)

# case14: history detail should include session and messages
case14_ok = False
case14_detail = {"status_code": None, "analysis_session_id": analysis_session_id, "messages_len": 0}
if analysis_session_id:
    status, payload, _ = http_json("GET", f"{semantic_base}/history/{analysis_session_id}")
    messages = payload.get("messages") if isinstance(payload, dict) and isinstance(payload.get("messages"), list) else []
    assistant_candidates = [
        item for item in messages
        if isinstance(item, dict) and str(item.get("role", "")) == "assistant" and item.get("message_id")
    ]
    if assistant_candidates:
        assistant_message_id = str(assistant_candidates[-1].get("message_id"))
    case14_ok = status == 200 and str(payload.get("session_id", "")) == analysis_session_id and len(messages) >= 2
    case14_detail = {
        "status_code": status,
        "analysis_session_id": payload.get("session_id") if isinstance(payload, dict) else "",
        "messages_len": len(messages),
        "assistant_message_id": assistant_message_id,
    }
record_case(cases, "case14_history_detail_contains_messages", case14_ok, case14_detail)

# case15: history list should contain analysis session
case15_ok = False
case15_detail = {"status_code": None, "analysis_session_id": analysis_session_id, "exists_in_list": False}
if analysis_session_id:
    status, payload, _ = http_json("GET", f"{semantic_base}/history?limit=200")
    sessions = payload.get("sessions") if isinstance(payload, dict) and isinstance(payload.get("sessions"), list) else []
    exists_in_list = any(
        isinstance(item, dict) and str(item.get("session_id", "")) == analysis_session_id
        for item in sessions
    )
    case15_ok = status == 200 and exists_in_list
    case15_detail = {
        "status_code": status,
        "analysis_session_id": analysis_session_id,
        "exists_in_list": exists_in_list,
    }
record_case(cases, "case15_history_list_contains_session", case15_ok, case15_detail)

# case16: history session metadata update (rename + pin)
case16_ok = False
case16_detail = {"status_code": None, "analysis_session_id": analysis_session_id}
renamed_title = f"AI Contract Session {run_id}"
if analysis_session_id:
    status, payload, _ = http_json(
        "PATCH",
        f"{semantic_base}/history/{analysis_session_id}",
        data={"title": renamed_title, "is_pinned": True, "is_archived": False},
    )
    case16_ok = status == 200 and str(payload.get("title", "")) == renamed_title and payload.get("is_pinned") is True
    case16_detail = {
        "status_code": status,
        "analysis_session_id": payload.get("session_id") if isinstance(payload, dict) else "",
        "title": payload.get("title") if isinstance(payload, dict) else "",
        "is_pinned": payload.get("is_pinned") if isinstance(payload, dict) else None,
    }
record_case(cases, "case16_update_history_session_metadata", case16_ok, case16_detail)

# case17: history search should find renamed session
case17_ok = False
case17_detail = {"status_code": None, "analysis_session_id": analysis_session_id, "exists_in_search": False}
if analysis_session_id:
    encoded_q = urllib.parse.quote(renamed_title)
    status, payload, _ = http_json("GET", f"{semantic_base}/history?limit=200&q={encoded_q}")
    sessions = payload.get("sessions") if isinstance(payload, dict) and isinstance(payload.get("sessions"), list) else []
    exists_in_search = any(
        isinstance(item, dict) and str(item.get("session_id", "")) == analysis_session_id
        for item in sessions
    )
    case17_ok = status == 200 and exists_in_search
    case17_detail = {
        "status_code": status,
        "analysis_session_id": analysis_session_id,
        "exists_in_search": exists_in_search,
    }
record_case(cases, "case17_history_search", case17_ok, case17_detail)

# case18: create follow-up action draft from assistant message
case18_ok = False
case18_detail = {"status_code": None, "analysis_session_id": analysis_session_id, "assistant_message_id": assistant_message_id}
if analysis_session_id and assistant_message_id:
    status, payload, _ = http_json(
        "POST",
        f"{semantic_base}/history/{analysis_session_id}/messages/{assistant_message_id}/actions",
        data={"action_type": "ticket", "title": "Contract synthetic ticket"},
    )
    action = payload.get("action") if isinstance(payload, dict) else {}
    case18_ok = status == 200 and isinstance(action, dict) and action.get("action_type") == "ticket"
    case18_detail = {
        "status_code": status,
        "analysis_session_id": analysis_session_id,
        "assistant_message_id": assistant_message_id,
        "action_type": action.get("action_type") if isinstance(action, dict) else None,
    }
record_case(cases, "case18_followup_action_draft", case18_ok, case18_detail)

# case19: follow-up response should include references and token budget hints
case19_ok = False
case19_detail = {"status_code": None, "analysis_session_id": analysis_session_id}
if analysis_session_id:
    status, payload, _ = http_json(
        "POST",
        f"{semantic_base}/follow-up",
        data={
            "analysis_session_id": analysis_session_id,
            "question": "请继续给出可执行排查步骤，并引用关键片段",
            "analysis_context": {
                "analysis_type": "log",
                "service_name": "query-service",
            },
            "history": [],
            "reset": False,
        },
    )
    refs = payload.get("references") if isinstance(payload, dict) and isinstance(payload.get("references"), list) else []
    token_budget = payload.get("token_budget") if isinstance(payload, dict) else None
    token_estimate = payload.get("token_estimate") if isinstance(payload, dict) else None
    token_remaining = payload.get("token_remaining") if isinstance(payload, dict) else None
    case19_ok = (
        status == 200
        and isinstance(token_budget, int)
        and isinstance(token_estimate, int)
        and isinstance(token_remaining, int)
        and isinstance(refs, list)
    )
    case19_detail = {
        "status_code": status,
        "analysis_session_id": analysis_session_id,
        "references_len": len(refs),
        "token_budget": token_budget,
        "token_estimate": token_estimate,
        "token_remaining": token_remaining,
    }
record_case(cases, "case19_followup_response_budget_and_references", case19_ok, case19_detail)

# case20: delete history session should succeed
case20_ok = False
case20_detail = {"status_code": None, "analysis_session_id": analysis_session_id}
if analysis_session_id:
    status, payload, _ = http_json("DELETE", f"{semantic_base}/history/{analysis_session_id}")
    case20_ok = status == 200 and payload.get("status") == "ok"
    case20_detail = {
        "status_code": status,
        "analysis_session_id": payload.get("session_id") if isinstance(payload, dict) else "",
        "status": payload.get("status") if isinstance(payload, dict) else None,
    }
record_case(cases, "case20_delete_history_session", case20_ok, case20_detail)

# case21: deleted history session should not be visible and detail should 404
case21_ok = False
case21_detail = {"list_status_code": None, "detail_status_code": None, "analysis_session_id": analysis_session_id}
if analysis_session_id:
    list_status, list_payload, _ = http_json("GET", f"{semantic_base}/history?limit=200")
    sessions = list_payload.get("sessions") if isinstance(list_payload, dict) and isinstance(list_payload.get("sessions"), list) else []
    exists_in_list = any(
        isinstance(item, dict) and str(item.get("session_id", "")) == analysis_session_id
        for item in sessions
    )
    detail_status, detail_payload, _ = http_json("GET", f"{semantic_base}/history/{analysis_session_id}")
    case21_ok = list_status == 200 and not exists_in_list and detail_status == 404
    case21_detail = {
        "list_status_code": list_status,
        "detail_status_code": detail_status,
        "analysis_session_id": analysis_session_id,
        "exists_in_list": exists_in_list,
        "detail": detail_payload.get("detail") if isinstance(detail_payload, dict) else None,
    }
record_case(cases, "case21_deleted_history_not_visible", case21_ok, case21_detail)

# case22: trace-mode jump auto-run contract
# 模拟前端从日志页以 mode=trace 跳转后自动触发分析：
# 1) 以 trace_id 调用 analyze-trace；
# 2) 必须返回 session_id；
# 3) history/{session_id} 中 analysis_type/trace_id/input_text 应可回放。
case22_ok = False
case22_trace_id = f"{trace_id}-jump"
case22_session_id = ""
case22_detail = {
    "analyze_status_code": None,
    "history_status_code": None,
    "delete_status_code": None,
    "trace_id": case22_trace_id,
    "session_id": "",
}
status, payload, _ = http_json(
    "POST",
    f"{semantic_base}/analyze-trace",
    data={"trace_id": case22_trace_id, "service_name": "query-service"},
)
case22_detail["analyze_status_code"] = status
if status == 200 and has_analysis_shape(payload) and has_overview_shape(payload):
    case22_session_id = str(payload.get("session_id") or "")
    case22_detail["session_id"] = case22_session_id
    if case22_session_id:
        h_status, h_payload, _ = http_json("GET", f"{semantic_base}/history/{case22_session_id}")
        case22_detail["history_status_code"] = h_status
        analysis_type = str((h_payload or {}).get("analysis_type", "")).strip().lower()
        history_trace_id = str((h_payload or {}).get("trace_id", "")).strip()
        history_input_text = str((h_payload or {}).get("input_text", "")).strip()
        case22_ok = (
            h_status == 200
            and analysis_type == "trace"
            and history_trace_id == case22_trace_id
            and history_input_text == case22_trace_id
        )
        d_status, _, _ = http_json("DELETE", f"{semantic_base}/history/{case22_session_id}")
        case22_detail["delete_status_code"] = d_status
    else:
        case22_ok = False
record_case(cases, "case22_trace_mode_jump_autorun_contract", case22_ok, case22_detail)

report = {
    "run_id": run_id,
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "trace_id": trace_id,
    "cases": cases,
    "passed": all(item["passed"] for item in cases),
}
print(json.dumps(report, ensure_ascii=False))
PY
)"

printf '%s\n' "$PAYLOAD_JSON" > "$REPORT_FILE"
ln -sfn "$REPORT_FILE" "${ARTIFACT_DIR}/latest.json"

echo "[INFO] AI contract report: $REPORT_FILE"

python3 - <<'PY' "$PAYLOAD_JSON"
import json
import sys

payload = json.loads(sys.argv[1])
cases = payload.get("cases", [])
passed = [c for c in cases if c.get("passed")]
failed = [c for c in cases if not c.get("passed")]

print(f"[INFO] AI contract: {len(passed)}/{len(cases)} passed")
for case in cases:
    status = "PASS" if case.get("passed") else "FAIL"
    print(f"[INFO] {status} {case.get('id')}")

if failed:
    raise SystemExit(2)
PY

exit 0
