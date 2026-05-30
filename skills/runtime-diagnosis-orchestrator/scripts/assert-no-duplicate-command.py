#!/usr/bin/env python3
"""
Validate AI runtime events for duplicate command execution and stream loss.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Set, Tuple


TERMINAL_STATUSES: Set[str] = {"completed", "failed", "cancelled", "timed_out"}


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _normalize_command(value: Any) -> str:
    return " ".join(_as_str(value).split())


def _load_events(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, dict) and isinstance(payload.get("events"), list):
        return [item for item in payload["events"] if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    raise ValueError("events payload must be {'events': [...]} or a list")


def _http_get_json(url: str, timeout_seconds: int = 8) -> Dict[str, Any]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            data = response.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"http get failed: {url}: {exc}") from exc
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid json from {url}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"unexpected response type from {url}")
    return parsed


def _iter_events(events: Iterable[Dict[str, Any]]) -> Iterable[Tuple[str, Dict[str, Any]]]:
    for event in events:
        event_type = _as_str(event.get("event_type")).strip().lower()
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if not event_type:
            continue
        yield event_type, payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Check duplicate command execution and stream loss from runtime events.")
    parser.add_argument("--events-file", required=True, help="Path to events JSON file.")
    parser.add_argument("--run-id", default="", help="Optional run id for logs.")
    parser.add_argument(
        "--exec-base-url",
        default="",
        help="Optional exec-service base url, e.g. http://127.0.0.1:8095",
    )
    args = parser.parse_args()

    try:
        events = _load_events(args.events_file)
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[ERROR] failed to load events: {exc}")
        return 2

    key_to_runs: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
    run_started: Set[str] = set()
    run_terminal: Dict[str, str] = {}
    run_output_chars: Dict[str, int] = defaultdict(int)
    run_output_chunks: Dict[str, int] = defaultdict(int)

    for event_type, payload in _iter_events(events):
        command_run_id = _as_str(payload.get("command_run_id")).strip()

        if event_type == "tool_call_started" and command_run_id:
            action_id = _as_str(payload.get("action_id")).strip()
            command = _normalize_command(payload.get("command"))
            key = (action_id, command)
            key_to_runs[key].add(command_run_id)
            run_started.add(command_run_id)
            continue

        if event_type == "tool_call_finished" and command_run_id:
            status = _as_str(payload.get("status")).strip().lower()
            if status:
                run_terminal[command_run_id] = status
            continue

        if event_type == "tool_call_output_delta" and command_run_id:
            text = _as_str(payload.get("text"))
            run_output_chunks[command_run_id] += 1
            run_output_chars[command_run_id] += len(text)

    duplicate_keys = {
        key: sorted(list(run_ids))
        for key, run_ids in key_to_runs.items()
        if len(run_ids) > 1
    }
    missing_terminal = sorted(run_started - set(run_terminal.keys()))

    print(f"[INFO] run_id={_as_str(args.run_id) or 'unknown'} events={len(events)}")
    print(f"[INFO] started_runs={len(run_started)} terminal_runs={len(run_terminal)}")

    if run_started:
        print("[INFO] per_run_output:")
        for run_id in sorted(run_started):
            status = run_terminal.get(run_id, "missing")
            print(
                f"  - {run_id}: status={status} chunks={run_output_chunks.get(run_id, 0)} "
                f"chars={run_output_chars.get(run_id, 0)}"
            )

    if duplicate_keys:
        print("[ERROR] duplicate command execution detected:")
        for (action_id, command), run_ids in sorted(duplicate_keys.items(), key=lambda x: (x[0][0], x[0][1])):
            print(f"  - action_id={action_id or '<empty>'} command={command or '<empty>'}")
            print(f"    runs={', '.join(run_ids)}")

    if missing_terminal:
        print("[ERROR] missing terminal tool_call_finished for command runs:")
        for run_id in missing_terminal:
            print(f"  - {run_id}")

    stream_mismatch: List[str] = []
    if args.exec_base_url:
        base = args.exec_base_url.rstrip("/")
        for run_id in sorted(run_started):
            url = f"{base}/api/v1/exec/runs/{run_id}"
            try:
                payload = _http_get_json(url)
            except Exception as exc:  # pylint: disable=broad-except
                print(f"[WARN] failed to fetch exec run {run_id}: {exc}")
                continue
            run = payload.get("run") if isinstance(payload.get("run"), dict) else {}
            exec_stdout = _as_str(run.get("stdout"))
            exec_stderr = _as_str(run.get("stderr"))
            exec_chars = len(exec_stdout) + len(exec_stderr)
            ai_chars = run_output_chars.get(run_id, 0)
            exec_status = _as_str(run.get("status")).strip().lower()
            if exec_status in TERMINAL_STATUSES and exec_chars > ai_chars:
                stream_mismatch.append(
                    f"{run_id}: exec_chars={exec_chars} ai_delta_chars={ai_chars} status={exec_status}"
                )

    if stream_mismatch:
        print("[ERROR] stream mismatch detected (AI bridge output < exec terminal output):")
        for line in stream_mismatch:
            print(f"  - {line}")

    has_error = bool(duplicate_keys or missing_terminal or stream_mismatch)
    if has_error:
        print("[RESULT] FAIL")
        return 1

    print("[RESULT] PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
