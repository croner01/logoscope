#!/usr/bin/env bash
set -euo pipefail

# 每周覆盖率提阈检查：
# 1) 读取 backend-pytest 最新报告（thresholds + actuals）
# 2) 校验当前阈值是否达到里程碑要求
# 3) 根据 headroom 给出自动提阈建议
# 4) 可选：当存在提阈空间时以非 0 退出，驱动 CI 创建后续任务

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPORT_FILE="${REPORT_FILE:-${PROJECT_ROOT}/reports/backend-pytest/latest.json}"
ARTIFACT_DIR="${ARTIFACT_DIR:-${PROJECT_ROOT}/reports/coverage-threshold-weekly}"
TODAY_UTC="${TODAY_UTC:-$(date -u +%Y-%m-%d)}"
RAISE_STEP="${RAISE_STEP:-5}"
RAISE_BUFFER="${RAISE_BUFFER:-3}"
FAIL_ON_RAISE_GAP="${FAIL_ON_RAISE_GAP:-false}"

DEFAULT_COVERAGE_PLAN_JSON='[{"date":"2026-03-16","query":55,"topology":35,"ingest":35,"ai":50},{"date":"2026-03-30","query":60,"topology":40,"ingest":40,"ai":55},{"date":"2026-04-13","query":65,"topology":45,"ingest":45,"ai":58},{"date":"2026-04-27","query":70,"topology":50,"ingest":50,"ai":60}]'
COVERAGE_PLAN_JSON="${COVERAGE_PLAN_JSON:-$DEFAULT_COVERAGE_PLAN_JSON}"

json_escape() {
  printf '%s' "${1:-}" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

is_non_negative_int() {
  [[ "${1:-}" =~ ^[0-9]+$ ]]
}

if [[ ! -f "${REPORT_FILE}" ]]; then
  echo "[ERROR] backend pytest report not found: ${REPORT_FILE}" >&2
  exit 1
fi

if ! is_non_negative_int "${RAISE_STEP}"; then
  echo "[ERROR] RAISE_STEP must be a non-negative integer, got: ${RAISE_STEP}" >&2
  exit 1
fi

if ! is_non_negative_int "${RAISE_BUFFER}"; then
  echo "[ERROR] RAISE_BUFFER must be a non-negative integer, got: ${RAISE_BUFFER}" >&2
  exit 1
fi

mkdir -p "${ARTIFACT_DIR}"

RUN_ID="coverage-threshold-weekly-$(date -u +%Y%m%d-%H%M%S)-$RANDOM"
REPORT_OUT="${ARTIFACT_DIR}/${RUN_ID}.json"

python3 - "$REPORT_FILE" "$TODAY_UTC" "$RAISE_STEP" "$RAISE_BUFFER" "$FAIL_ON_RAISE_GAP" "$COVERAGE_PLAN_JSON" "$REPORT_OUT" <<'PY'
import json
import math
import os
import sys
from datetime import datetime, timezone


def fail(msg: str, code: int = 1) -> None:
    print(f"[ERROR] {msg}", file=sys.stderr)
    raise SystemExit(code)


def to_int(value, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        fail(f"{key} must be integer, got: {value!r}")
    if value < 0:
        fail(f"{key} must be >= 0, got: {value}")
    return value


def to_float(value, key: str) -> float:
    if isinstance(value, bool):
        fail(f"{key} must be numeric, got bool")
    if isinstance(value, (int, float)):
        return float(value)
    fail(f"{key} must be numeric, got: {value!r}")


def as_bool(value: str) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


report_file, today_utc, raise_step_raw, raise_buffer_raw, fail_on_raise_gap_raw, plan_json_raw, report_out = sys.argv[1:]

raise_step = to_int(int(raise_step_raw), "RAISE_STEP")
raise_buffer = to_int(int(raise_buffer_raw), "RAISE_BUFFER")
fail_on_raise_gap = as_bool(fail_on_raise_gap_raw)

try:
    today = datetime.strptime(today_utc, "%Y-%m-%d").date()
except ValueError as exc:
    fail(f"TODAY_UTC must be YYYY-MM-DD, got: {today_utc} ({exc})")

try:
    plan = json.loads(plan_json_raw)
except json.JSONDecodeError as exc:
    fail(f"invalid COVERAGE_PLAN_JSON: {exc}")

if not isinstance(plan, list) or not plan:
    fail("COVERAGE_PLAN_JSON must be a non-empty array")

normalized_plan = []
for idx, item in enumerate(plan):
    if not isinstance(item, dict):
        fail(f"COVERAGE_PLAN_JSON[{idx}] must be an object")
    missing = [k for k in ("date", "query", "topology", "ingest", "ai") if k not in item]
    if missing:
        fail(f"COVERAGE_PLAN_JSON[{idx}] missing fields: {', '.join(missing)}")
    try:
        item_date = datetime.strptime(str(item["date"]), "%Y-%m-%d").date()
    except ValueError as exc:
        fail(f"COVERAGE_PLAN_JSON[{idx}].date invalid: {exc}")
    normalized_plan.append(
        {
            "date": item_date,
            "query": to_int(int(item["query"]), f"plan[{idx}].query"),
            "topology": to_int(int(item["topology"]), f"plan[{idx}].topology"),
            "ingest": to_int(int(item["ingest"]), f"plan[{idx}].ingest"),
            "ai": to_int(int(item["ai"]), f"plan[{idx}].ai"),
        }
    )

normalized_plan.sort(key=lambda x: x["date"])

with open(report_file, "r", encoding="utf-8") as f:
    report = json.load(f)

if not isinstance(report, dict):
    fail("backend pytest report must be a JSON object")

thresholds = report.get("thresholds") or {}
actuals = report.get("actuals") or {}
if not isinstance(thresholds, dict):
    fail("backend pytest report thresholds must be an object")
if not isinstance(actuals, dict):
    fail("backend pytest report actuals must be an object")

services = {
    "query": {
        "threshold_key": "query_cov_min",
        "actual_key": "query_cov_percent",
    },
    "topology": {
        "threshold_key": "topology_cov_min",
        "actual_key": "topology_cov_percent",
    },
    "ingest": {
        "threshold_key": "ingest_cov_min",
        "actual_key": "ingest_cov_percent",
    },
    "ai": {
        "threshold_key": "ai_cov_min",
        "actual_key": "ai_cov_percent",
    },
}

current_thresholds = {}
current_actuals = {}
for service, mapping in services.items():
    current_thresholds[service] = to_int(int(thresholds.get(mapping["threshold_key"], 0)), mapping["threshold_key"])
    current_actuals[service] = to_float(actuals.get(mapping["actual_key"], 0.0), mapping["actual_key"])

effective = None
next_milestone = None
for item in normalized_plan:
    if item["date"] <= today:
        effective = item
    elif next_milestone is None:
        next_milestone = item

threshold_gap = {}
if effective is not None:
    for service in services:
        required = int(effective[service])
        current = current_thresholds[service]
        if current < required:
            threshold_gap[service] = {
                "required": required,
                "current": current,
                "gap": required - current,
            }

recommendations = {}
for service in services:
    current = current_thresholds[service]
    actual = current_actuals[service]
    safe_cap = int(math.floor(actual - raise_buffer))
    candidate = current

    if safe_cap >= current + raise_step:
        candidate = current + raise_step

    if next_milestone is not None:
        milestone_target = int(next_milestone[service])
        if safe_cap >= milestone_target:
            candidate = max(candidate, milestone_target)

    candidate = min(candidate, safe_cap)
    if candidate > current:
        recommendations[service] = {
            "current_threshold": current,
            "recommended_threshold": candidate,
            "actual_coverage": round(actual, 2),
            "raise_by": candidate - current,
        }

if threshold_gap:
    status = "failed"
    exit_code = 2
    summary = "thresholds below due milestone"
elif fail_on_raise_gap and recommendations:
    status = "failed"
    exit_code = 3
    summary = "threshold raise opportunity detected"
else:
    status = "passed"
    exit_code = 0
    summary = "weekly threshold check passed"

output = {
    "run_id": os.path.basename(report_out).replace(".json", ""),
    "generated_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    "status": status,
    "summary": summary,
    "today_utc": today.isoformat(),
    "source_report": os.path.realpath(report_file),
    "raise_step": raise_step,
    "raise_buffer": raise_buffer,
    "fail_on_raise_gap": fail_on_raise_gap,
    "current_thresholds": current_thresholds,
    "current_actuals": current_actuals,
    "effective_milestone": (
        {
            "date": effective["date"].isoformat(),
            "targets": {k: int(effective[k]) for k in ("query", "topology", "ingest", "ai")},
        }
        if effective
        else None
    ),
    "next_milestone": (
        {
            "date": next_milestone["date"].isoformat(),
            "targets": {k: int(next_milestone[k]) for k in ("query", "topology", "ingest", "ai")},
        }
        if next_milestone
        else None
    ),
    "threshold_gap": threshold_gap,
    "recommendations": recommendations,
}

with open(report_out, "w", encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print(f"[INFO] Weekly coverage threshold report: {report_out}")
print(f"[INFO] status={status} summary={summary}")
if recommendations:
    print(f"[INFO] recommendations={json.dumps(recommendations, ensure_ascii=False)}")
if threshold_gap:
    print(f"[ERROR] threshold_gap={json.dumps(threshold_gap, ensure_ascii=False)}", file=sys.stderr)

raise SystemExit(exit_code)
PY

ln -sfn "${REPORT_OUT}" "${ARTIFACT_DIR}/latest.json"
