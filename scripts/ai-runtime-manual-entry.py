#!/usr/bin/env python3
"""
Human-facing AI runtime backend manual entry.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict


NAMESPACE = os.getenv("NAMESPACE", "islap")
STATE_FILE = Path(os.getenv("STATE_FILE", "/tmp/ai-runtime-manual-entry-state.json"))
BASE_AI = "http://127.0.0.1:8090/api/v1/ai"
BASE_AI_V2 = "http://127.0.0.1:8090/api/v2"
BASE_EXEC = "http://exec-service:8095/api/v1/exec"

POD_HTTP_CLIENT = r"""
import json
import sys
import urllib.error
import urllib.request

envelope = json.loads(sys.stdin.read() or "{}")
url = envelope.get("url", "")
method = envelope.get("method", "GET")
payload = envelope.get("payload") or {}
timeout = int(payload.pop("_timeout", 20)) if isinstance(payload, dict) else 20
body = None
headers = {"Accept": "application/json"}
if payload:
    body = json.dumps(payload).encode("utf-8")
    headers["Content-Type"] = "application/json"
req = urllib.request.Request(url, data=body, headers=headers, method=method)
try:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
        print(raw or "{}")
except urllib.error.HTTPError as exc:
    raw = exc.read().decode("utf-8", errors="ignore")
    print(raw or json.dumps({"detail": f"HTTP {exc.code}"}))
    raise SystemExit(exc.code)
"""

POD_STREAM_CLIENT = r"""
import json
import sys
import urllib.request

run_id = sys.argv[1]
after_seq = int(sys.argv[2])
max_events = int(sys.argv[3])
url = f"http://127.0.0.1:8090/api/v1/ai/runs/{run_id}/stream?after_seq={after_seq}"
req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
count = 0
with urllib.request.urlopen(req, timeout=30) as resp:
    buffer = ""
    while count < max_events:
        chunk = resp.read(256)
        if not chunk:
            break
        buffer += chunk.decode("utf-8", errors="ignore")
        while "\n\n" in buffer and count < max_events:
            block, buffer = buffer.split("\n\n", 1)
            block = block.strip()
            if not block:
                continue
            event_name = "message"
            data_text = ""
            for line in block.splitlines():
                if line.startswith("event:"):
                    event_name = line.split(":", 1)[1].strip() or "message"
                elif line.startswith("data:"):
                    data_text += line.split(":", 1)[1].strip()
            data = {}
            if data_text:
                try:
                    data = json.loads(data_text)
                except Exception:
                    data = {"raw": data_text}
            print(json.dumps({"event": event_name, "data": data}, ensure_ascii=False))
            count += 1
"""


def fail(message: str) -> None:
    print(f"[ERROR] {message}", file=sys.stderr)
    raise SystemExit(1)


def run(cmd: list[str], input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )


def find_ai_pod() -> str:
    result = run(
        [
            "kubectl",
            "-n",
            NAMESPACE,
            "get",
            "pod",
            "-l",
            "app=ai-service",
            "-o",
            "jsonpath={.items[0].metadata.name}",
        ]
    )
    if result.returncode != 0:
        fail(result.stderr.strip() or "failed to find ai-service pod")
    pod_name = result.stdout.strip()
    if not pod_name:
        fail(f"ai-service pod not found in namespace {NAMESPACE}")
    return pod_name


def exec_python_in_ai_pod(code: str, args: list[str] | None = None, input_text: str | None = None) -> str:
    pod_name = find_ai_pod()
    cmd = [
        "kubectl",
        "-n",
        NAMESPACE,
        "exec",
        "-i",
        pod_name,
        "-c",
        "ai-service",
        "--",
        "python",
        "-c",
        code,
        *(args or []),
    ]
    result = run(cmd, input_text=input_text)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        fail(stderr or stdout or f"command failed: {' '.join(cmd)}")
    return result.stdout


def request_json(url: str, method: str = "GET", payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    envelope = {
        "url": url,
        "method": method,
        "payload": payload or {},
    }
    raw = exec_python_in_ai_pod(POD_HTTP_CLIENT, input_text=json.dumps(envelope, ensure_ascii=False))
    return json.loads(raw or "{}")


def load_state() -> Dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    return json.loads(STATE_FILE.read_text(encoding="utf-8") or "{}")


def save_state(state: Dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def print_json(data: Dict[str, Any]) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def resolve_run_id(explicit_run_id: str | None) -> str:
    run_id = (explicit_run_id or "").strip() or str(load_state().get("run_id") or "").strip()
    if not run_id:
        fail("run id is required")
    return run_id


def latest_pending_approval_payload(events_payload: Dict[str, Any]) -> Dict[str, Any] | None:
    events = events_payload.get("events") or []
    resolved = {
        str((item.get("payload") or {}).get("approval_id") or "")
        for item in events
        if str(item.get("event_type") or "") == "approval_resolved"
    }
    for item in reversed(events):
        if str(item.get("event_type") or "") != "approval_required":
            continue
        payload = item.get("payload") or {}
        approval_id = str(payload.get("approval_id") or "")
        if approval_id and approval_id not in resolved:
            return payload
    return None


def cmd_create_run(args: argparse.Namespace) -> None:
    conversation_id = args.conversation_id or f"manual-{subprocess.run(['date', '-u', '+%Y%m%d%H%M%S'], text=True, capture_output=True).stdout.strip()}"
    analysis_context = {
        "analysis_type": args.analysis_type,
        "service_name": args.service,
    }
    if args.mode == "followup_runtime":
        analysis_context["runtime_mode"] = "followup_runtime"
        analysis_context["agent_mode"] = "followup_analysis_runtime"
    if str(getattr(args, "api_version", "v1")) == "v2":
        thread_payload = {
            "session_id": args.session_id or "",
            "conversation_id": conversation_id,
            "title": args.title or args.question,
        }
        thread_response = request_json(f"{BASE_AI_V2}/threads", "POST", thread_payload)
        thread = thread_response.get("thread") or {}
        thread_id = str(thread.get("thread_id") or "").strip()
        if not thread_id:
            fail("failed to create v2 thread")
        runtime_options = {
            "conversation_id": conversation_id,
            "auto_exec_readonly": bool(args.auto_exec_readonly),
            "enable_skills": bool(args.enable_skills),
            "max_skills": int(args.max_skills),
        }
        run_payload = {
            "question": args.question,
            "analysis_context": analysis_context,
            "runtime_options": runtime_options,
            "runtime_backend": str(args.runtime_backend or "").strip(),
        }
        response = request_json(f"{BASE_AI_V2}/threads/{thread_id}/runs", "POST", run_payload)
        run_payload_response = response.get("run") or {}
        save_state(
            {
                "run_id": str(run_payload_response.get("run_id") or ""),
                "thread_id": thread_id,
                "session_id": str(thread.get("session_id") or args.session_id or ""),
                "conversation_id": conversation_id,
                "assistant_message_id": str(run_payload_response.get("assistant_message_id") or ""),
            }
        )
        print_json(response)
        return
    payload = {
        "session_id": args.session_id or "",
        "question": args.question,
        "analysis_context": analysis_context,
        "runtime_options": {
            "conversation_id": conversation_id,
        },
    }
    response = request_json(f"{BASE_AI}/runs", "POST", payload)
    run_payload = response.get("run") or {}
    save_state(
        {
            "run_id": str(run_payload.get("run_id") or ""),
            "session_id": str(run_payload.get("session_id") or ""),
            "conversation_id": str(run_payload.get("conversation_id") or ""),
            "assistant_message_id": str(run_payload.get("assistant_message_id") or ""),
        }
    )
    print_json(response)


def cmd_stream(args: argparse.Namespace) -> None:
    run_id = resolve_run_id(args.run_id)
    raw = exec_python_in_ai_pod(POD_STREAM_CLIENT, args=[run_id, str(args.after_seq), str(args.max_events)])
    sys.stdout.write(raw)


def cmd_events(args: argparse.Namespace) -> None:
    run_id = resolve_run_id(args.run_id)
    payload = request_json(f"{BASE_AI}/runs/{run_id}/events?after_seq=0&limit={args.limit}")
    print_json(payload)


def cmd_run(args: argparse.Namespace) -> None:
    run_id = resolve_run_id(args.run_id)
    payload = request_json(f"{BASE_AI}/runs/{run_id}")
    print_json(payload)


def cmd_actions(args: argparse.Namespace) -> None:
    run_id = resolve_run_id(args.run_id)
    payload = request_json(f"{BASE_AI_V2}/runs/{run_id}/actions?limit={int(args.limit)}")
    print_json(payload)


def cmd_exec(args: argparse.Namespace) -> None:
    run_id = resolve_run_id(args.run_id)
    purpose = str(args.purpose or "").strip() or str(args.title or "").strip() or str(args.command or "").strip()
    payload = {
        "command": args.command,
        "purpose": purpose,
        "title": args.title,
        "tool_name": "command.exec",
        "confirmed": bool(args.confirmed),
        "elevated": bool(args.elevated),
        "timeout_seconds": int(args.timeout_seconds),
    }
    response = request_json(f"{BASE_AI}/runs/{run_id}/commands", "POST", payload)
    print_json(response)


def cmd_exec_action(args: argparse.Namespace) -> None:
    run_id = resolve_run_id(args.run_id)
    payload = {
        "action_id": args.action_id,
        "purpose": "",
        "title": "",
        "command": "",
        "command_spec": {},
        "confirmed": bool(args.confirmed),
        "elevated": bool(args.elevated),
        "approval_token": str(args.approval_token or ""),
        "timeout_seconds": int(args.timeout_seconds),
    }
    response = request_json(f"{BASE_AI_V2}/runs/{run_id}/actions/command", "POST", payload)
    print_json(response)


def cmd_latest_approval(args: argparse.Namespace) -> None:
    run_id = resolve_run_id(args.run_id)
    events_payload = request_json(f"{BASE_AI}/runs/{run_id}/events?after_seq=0&limit=500")
    print_json({"pending_approval": latest_pending_approval_payload(events_payload)})


def cmd_resolve_approval(args: argparse.Namespace, decision: str) -> None:
    run_id = resolve_run_id(args.run_id)
    approval_id = (args.approval_id or "").strip()
    if not approval_id:
        events_payload = request_json(f"{BASE_AI}/runs/{run_id}/events?after_seq=0&limit=500")
        pending = latest_pending_approval_payload(events_payload)
        approval_id = str((pending or {}).get("approval_id") or "")
    if not approval_id:
        fail("approval id is required")
    payload = {
        "approval_id": approval_id,
        "decision": decision,
        "comment": args.comment,
        "confirmed": bool(args.confirmed),
        "elevated": bool(args.elevated),
    }
    response = request_json(f"{BASE_AI}/runs/{run_id}/approve", "POST", payload)
    print_json(response)


def cmd_precheck(args: argparse.Namespace) -> None:
    payload = request_json(f"{BASE_EXEC}/precheck", "POST", {"command": args.command})
    print_json(payload)


def cmd_state(_args: argparse.Namespace) -> None:
    print_json(load_state())


def cmd_clear_state(_args: argparse.Namespace) -> None:
    if STATE_FILE.exists():
        STATE_FILE.unlink()
    print("{}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="AI Runtime backend manual entry",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create-run", help="Create a runtime run and persist ids locally")
    create_parser.add_argument("--question", default="请分析 query-service 当前异常，并在需要审批时暂停等待我确认。")
    create_parser.add_argument("--analysis-type", default="log")
    create_parser.add_argument("--service", default="query-service")
    create_parser.add_argument("--session-id", default="")
    create_parser.add_argument("--conversation-id", default="")
    create_parser.add_argument("--title", default="")
    create_parser.add_argument("--mode", choices=["passive", "followup_runtime"], default="passive")
    create_parser.add_argument("--api-version", choices=["v1", "v2"], default="v1")
    create_parser.add_argument("--runtime-backend", default="")
    create_parser.add_argument("--auto-exec-readonly", action=argparse.BooleanOptionalAction, default=True)
    create_parser.add_argument("--enable-skills", action=argparse.BooleanOptionalAction, default=True)
    create_parser.add_argument("--max-skills", type=int, default=3)
    create_parser.set_defaults(func=cmd_create_run)

    stream_parser = subparsers.add_parser("stream", help="Tail canonical SSE events for a run")
    stream_parser.add_argument("--run-id", default="")
    stream_parser.add_argument("--after-seq", type=int, default=0)
    stream_parser.add_argument("--max-events", type=int, default=20)
    stream_parser.set_defaults(func=cmd_stream)

    events_parser = subparsers.add_parser("events", help="Show persisted runtime events")
    events_parser.add_argument("--run-id", default="")
    events_parser.add_argument("--limit", type=int, default=200)
    events_parser.set_defaults(func=cmd_events)

    run_parser = subparsers.add_parser("run", help="Show current run snapshot")
    run_parser.add_argument("--run-id", default="")
    run_parser.set_defaults(func=cmd_run)

    actions_parser = subparsers.add_parser("actions", help="Show v2 runtime actions for a run")
    actions_parser.add_argument("--run-id", default="")
    actions_parser.add_argument("--limit", type=int, default=20)
    actions_parser.set_defaults(func=cmd_actions)

    exec_parser = subparsers.add_parser("exec", help="Execute a command inside a run")
    exec_parser.add_argument("--run-id", default="")
    exec_parser.add_argument("--command", required=True)
    exec_parser.add_argument("--title", default="manual command execution")
    exec_parser.add_argument("--purpose", default="")
    exec_parser.add_argument("--confirmed", action="store_true")
    exec_parser.add_argument("--elevated", action="store_true")
    exec_parser.add_argument("--timeout-seconds", type=int, default=20)
    exec_parser.set_defaults(func=cmd_exec)

    exec_action_parser = subparsers.add_parser("exec-action", help="Execute a v2 action by action_id")
    exec_action_parser.add_argument("--run-id", default="")
    exec_action_parser.add_argument("--action-id", required=True)
    exec_action_parser.add_argument("--confirmed", action="store_true")
    exec_action_parser.add_argument("--elevated", action="store_true")
    exec_action_parser.add_argument("--approval-token", default="")
    exec_action_parser.add_argument("--timeout-seconds", type=int, default=20)
    exec_action_parser.set_defaults(func=cmd_exec_action)

    latest_parser = subparsers.add_parser("latest-approval", help="Print latest pending approval")
    latest_parser.add_argument("--run-id", default="")
    latest_parser.set_defaults(func=cmd_latest_approval)

    approve_parser = subparsers.add_parser("approve", help="Approve an approval")
    approve_parser.add_argument("--run-id", default="")
    approve_parser.add_argument("--approval-id", default="")
    approve_parser.add_argument("--comment", default="manual approve")
    approve_parser.add_argument("--confirmed", action="store_true", default=True)
    approve_parser.add_argument("--elevated", action="store_true", default=True)
    approve_parser.set_defaults(func=lambda args: cmd_resolve_approval(args, "approved"))

    reject_parser = subparsers.add_parser("reject", help="Reject an approval")
    reject_parser.add_argument("--run-id", default="")
    reject_parser.add_argument("--approval-id", default="")
    reject_parser.add_argument("--comment", default="manual reject")
    reject_parser.add_argument("--confirmed", action="store_true", default=False)
    reject_parser.add_argument("--elevated", action="store_true", default=False)
    reject_parser.set_defaults(func=lambda args: cmd_resolve_approval(args, "rejected"))

    precheck_parser = subparsers.add_parser("precheck", help="Call exec-service precheck directly")
    precheck_parser.add_argument("--command", required=True)
    precheck_parser.set_defaults(func=cmd_precheck)

    state_parser = subparsers.add_parser("state", help="Show locally cached ids")
    state_parser.set_defaults(func=cmd_state)

    clear_parser = subparsers.add_parser("clear-state", help="Remove locally cached ids")
    clear_parser.set_defaults(func=cmd_clear_state)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
