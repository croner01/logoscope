#!/usr/bin/env bash
set -euo pipefail

# P2 周趋势性能回归检查脚本
# 1) 基于 perf-baseline 历史报告构建近 7 天基线
# 2) 对比当前报告服务维度 p95/p99/吞吐/错误率
# 3) 输出趋势门禁报告到 reports/perf-trend，支持 release-gate 阻断

ARTIFACT_DIR="${ARTIFACT_DIR:-/root/logoscope/reports/perf-trend}"
PERF_REPORT_DIR="${PERF_REPORT_DIR:-/root/logoscope/reports/perf-baseline}"
PERF_REPORT_FILE="${PERF_REPORT_FILE:-${PERF_REPORT_DIR}/latest.json}"

PERF_TREND_WINDOW_DAYS="${PERF_TREND_WINDOW_DAYS:-7}"
PERF_TREND_MIN_SAMPLES="${PERF_TREND_MIN_SAMPLES:-4}"
PERF_TREND_P95_REGRESSION_MAX="${PERF_TREND_P95_REGRESSION_MAX:-0.30}"
PERF_TREND_P99_REGRESSION_MAX="${PERF_TREND_P99_REGRESSION_MAX:-0.35}"
PERF_TREND_ERROR_RATE_DELTA_MAX="${PERF_TREND_ERROR_RATE_DELTA_MAX:-0.02}"
PERF_TREND_RPS_DROP_MAX="${PERF_TREND_RPS_DROP_MAX:-0.30}"

usage() {
  cat <<'EOF'
Perf weekly trend check (P2)

Env vars:
  ARTIFACT_DIR                      Report output dir (default: /root/logoscope/reports/perf-trend)
  PERF_REPORT_DIR                   Perf baseline report dir (default: /root/logoscope/reports/perf-baseline)
  PERF_REPORT_FILE                  Current perf report file (default: PERF_REPORT_DIR/latest.json)
  PERF_TREND_WINDOW_DAYS            Trend window days (default: 7)
  PERF_TREND_MIN_SAMPLES            Min history samples per service (default: 4)
  PERF_TREND_P95_REGRESSION_MAX     Allowed p95 regression ratio (default: 0.30)
  PERF_TREND_P99_REGRESSION_MAX     Allowed p99 regression ratio (default: 0.35)
  PERF_TREND_ERROR_RATE_DELTA_MAX   Allowed error-rate increase (default: 0.02)
  PERF_TREND_RPS_DROP_MAX           Allowed throughput drop ratio (default: 0.30)

Example:
  scripts/perf-trend-check.sh
  PERF_TREND_WINDOW_DAYS=14 PERF_TREND_MIN_SAMPLES=6 scripts/perf-trend-check.sh
EOF
}

fail() {
  printf '[ERROR] %s\n' "$1" >&2
  exit 1
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

command -v python3 >/dev/null 2>&1 || fail "missing command: python3"

mkdir -p "$ARTIFACT_DIR"
[[ -f "$PERF_REPORT_FILE" ]] || fail "perf report file not found: ${PERF_REPORT_FILE}"

RUN_ID="perf-trend-$(date -u +%Y%m%d-%H%M%S)-$RANDOM"
GENERATED_AT="$(date -u +"%Y-%m-%dT%H:%M:%S.%3NZ")"
REPORT_FILE="${ARTIFACT_DIR}/${RUN_ID}.json"

PAYLOAD_JSON="$(
RUN_ID="${RUN_ID}" \
GENERATED_AT="${GENERATED_AT}" \
PERF_REPORT_DIR="${PERF_REPORT_DIR}" \
PERF_REPORT_FILE="${PERF_REPORT_FILE}" \
PERF_TREND_WINDOW_DAYS="${PERF_TREND_WINDOW_DAYS}" \
PERF_TREND_MIN_SAMPLES="${PERF_TREND_MIN_SAMPLES}" \
PERF_TREND_P95_REGRESSION_MAX="${PERF_TREND_P95_REGRESSION_MAX}" \
PERF_TREND_P99_REGRESSION_MAX="${PERF_TREND_P99_REGRESSION_MAX}" \
PERF_TREND_ERROR_RATE_DELTA_MAX="${PERF_TREND_ERROR_RATE_DELTA_MAX}" \
PERF_TREND_RPS_DROP_MAX="${PERF_TREND_RPS_DROP_MAX}" \
python3 - <<'PY'
import glob
import json
import os
import statistics
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple


def parse_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def metric_median(values: List[float]) -> float:
    if not values:
        return 0.0
    return float(statistics.median(values))


def extract_service_aggregates(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    services = payload.get("service_aggregates")
    if isinstance(services, list) and services:
        normalized: List[Dict[str, Any]] = []
        for item in services:
            if not isinstance(item, dict):
                continue
            metrics = item.get("metrics", {})
            if not isinstance(metrics, dict):
                metrics = {}
            normalized.append(
                {
                    "service": str(item.get("service", "default")),
                    "metrics": {
                        "p95_max_ms": parse_float(metrics.get("p95_max_ms")),
                        "p99_max_ms": parse_float(metrics.get("p99_max_ms")),
                        "throughput_rps": parse_float(metrics.get("throughput_rps")),
                        "error_rate": parse_float(metrics.get("error_rate")),
                    },
                }
            )
        if normalized:
            return normalized

    # Backward-compatible fallback for old reports without service_aggregates.
    results = payload.get("results", [])
    if not isinstance(results, list):
        return []

    def infer_service_name(item: Dict[str, Any]) -> str:
        service = str(item.get("service", "")).strip()
        if service:
            return service
        parsed = urlparse(str(item.get("url", "")).strip())
        host = (parsed.hostname or "").strip().lower()
        if host:
            return host
        return "default"

    buckets: Dict[str, Dict[str, float]] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        service = infer_service_name(item)
        metrics = item.get("metrics", {})
        latency = metrics.get("latency_ms", {}) if isinstance(metrics, dict) else {}
        bucket = buckets.setdefault(
            service,
            {
                "p95_max_ms": 0.0,
                "p99_max_ms": 0.0,
                "throughput_rps": 0.0,
                "error_rate": 0.0,
                "endpoint_count": 0.0,
            },
        )
        bucket["p95_max_ms"] = max(bucket["p95_max_ms"], parse_float(latency.get("p95")))
        bucket["p99_max_ms"] = max(bucket["p99_max_ms"], parse_float(latency.get("p99")))
        bucket["throughput_rps"] += parse_float(metrics.get("throughput_rps"))
        bucket["error_rate"] += parse_float(metrics.get("error_rate"))
        bucket["endpoint_count"] += 1.0

    normalized = []
    for service, bucket in buckets.items():
        count = max(bucket.get("endpoint_count", 0.0), 1.0)
        normalized.append(
            {
                "service": service,
                "metrics": {
                    "p95_max_ms": bucket["p95_max_ms"],
                    "p99_max_ms": bucket["p99_max_ms"],
                    "throughput_rps": bucket["throughput_rps"],
                    "error_rate": bucket["error_rate"] / count,
                },
            }
        )
    return normalized


def load_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict):
        return payload
    return None


def main() -> None:
    run_id = os.getenv("RUN_ID")
    generated_at = os.getenv("GENERATED_AT")
    report_dir = os.getenv("PERF_REPORT_DIR", "/root/logoscope/reports/perf-baseline")
    report_file = os.getenv("PERF_REPORT_FILE", os.path.join(report_dir, "latest.json"))

    window_days = max(parse_int(os.getenv("PERF_TREND_WINDOW_DAYS"), 7), 1)
    min_samples = max(parse_int(os.getenv("PERF_TREND_MIN_SAMPLES"), 4), 1)
    thresholds = {
        "p95_regression_max": max(parse_float(os.getenv("PERF_TREND_P95_REGRESSION_MAX"), 0.30), 0.0),
        "p99_regression_max": max(parse_float(os.getenv("PERF_TREND_P99_REGRESSION_MAX"), 0.35), 0.0),
        "error_rate_delta_max": max(parse_float(os.getenv("PERF_TREND_ERROR_RATE_DELTA_MAX"), 0.02), 0.0),
        "rps_drop_max": max(parse_float(os.getenv("PERF_TREND_RPS_DROP_MAX"), 0.30), 0.0),
    }

    current_payload = load_json(report_file)
    if current_payload is None:
        print(json.dumps({"run_id": run_id, "generated_at": generated_at, "passed": False, "summary": "invalid current perf report"}))
        return

    current_services = extract_service_aggregates(current_payload)
    current_profile = str(current_payload.get("config", {}).get("profile", "standard"))
    current_generated_at = parse_iso_datetime(current_payload.get("generated_at")) or datetime.now(timezone.utc)
    window_start = current_generated_at - timedelta(days=window_days)
    current_run_id = str(current_payload.get("run_id", ""))

    history_files = sorted(glob.glob(os.path.join(report_dir, "perf-baseline-*.json")))
    history_by_service: Dict[str, List[Dict[str, float]]] = {}
    scanned_files = 0
    matched_files = 0

    for path in history_files:
        scanned_files += 1
        historical_payload = load_json(path)
        if historical_payload is None:
            continue
        historical_run_id = str(historical_payload.get("run_id", ""))
        if historical_run_id == current_run_id:
            continue
        if str(historical_payload.get("config", {}).get("profile", "standard")) != current_profile:
            continue
        if not bool(historical_payload.get("passed", False)):
            continue
        historical_service_aggregates = historical_payload.get("service_aggregates")
        if not isinstance(historical_service_aggregates, list) or not historical_service_aggregates:
            # Skip legacy reports to avoid cross-version metric bias in trend gate.
            continue
        historical_dt = parse_iso_datetime(historical_payload.get("generated_at"))
        if historical_dt is None or historical_dt < window_start:
            continue
        matched_files += 1
        for service_item in extract_service_aggregates(historical_payload):
            service = str(service_item.get("service", "default"))
            metrics = service_item.get("metrics", {})
            if not isinstance(metrics, dict):
                continue
            history_by_service.setdefault(service, []).append(
                {
                    "p95_max_ms": parse_float(metrics.get("p95_max_ms")),
                    "p99_max_ms": parse_float(metrics.get("p99_max_ms")),
                    "throughput_rps": parse_float(metrics.get("throughput_rps")),
                    "error_rate": parse_float(metrics.get("error_rate")),
                }
            )

    services_report: List[Dict[str, Any]] = []
    failed_services: List[str] = []
    degraded = False
    degraded_reasons: List[str] = []

    if not current_services:
        degraded = True
        degraded_reasons.append("current report has no service_aggregates")

    for service_item in current_services:
        service_name = str(service_item.get("service", "default"))
        current_metrics = service_item.get("metrics", {})
        if not isinstance(current_metrics, dict):
            current_metrics = {}

        history = history_by_service.get(service_name, [])
        sample_count = len(history)
        if sample_count < min_samples:
            degraded = True
            reason = f"insufficient history samples for {service_name}: {sample_count} < {min_samples}"
            degraded_reasons.append(reason)
            services_report.append(
                {
                    "service": service_name,
                    "passed": True,
                    "degraded": True,
                    "reason": reason,
                    "sample_count": sample_count,
                    "current": {
                        "p95_max_ms": parse_float(current_metrics.get("p95_max_ms")),
                        "p99_max_ms": parse_float(current_metrics.get("p99_max_ms")),
                        "throughput_rps": parse_float(current_metrics.get("throughput_rps")),
                        "error_rate": parse_float(current_metrics.get("error_rate")),
                    },
                    "baseline": {},
                    "deltas": {},
                    "checks": {},
                }
            )
            continue

        baseline = {
            "p95_max_ms": metric_median([item["p95_max_ms"] for item in history]),
            "p99_max_ms": metric_median([item["p99_max_ms"] for item in history]),
            "throughput_rps": metric_median([item["throughput_rps"] for item in history]),
            "error_rate": metric_median([item["error_rate"] for item in history]),
        }
        current = {
            "p95_max_ms": parse_float(current_metrics.get("p95_max_ms")),
            "p99_max_ms": parse_float(current_metrics.get("p99_max_ms")),
            "throughput_rps": parse_float(current_metrics.get("throughput_rps")),
            "error_rate": parse_float(current_metrics.get("error_rate")),
        }

        p95_regression = 0.0 if baseline["p95_max_ms"] <= 0 else (current["p95_max_ms"] - baseline["p95_max_ms"]) / baseline["p95_max_ms"]
        p99_regression = 0.0 if baseline["p99_max_ms"] <= 0 else (current["p99_max_ms"] - baseline["p99_max_ms"]) / baseline["p99_max_ms"]
        error_rate_delta = current["error_rate"] - baseline["error_rate"]
        rps_drop = 0.0 if baseline["throughput_rps"] <= 0 else max(0.0, (baseline["throughput_rps"] - current["throughput_rps"]) / baseline["throughput_rps"])

        checks = {
            "p95_regression_ok": p95_regression <= thresholds["p95_regression_max"],
            "p99_regression_ok": p99_regression <= thresholds["p99_regression_max"],
            "error_rate_delta_ok": error_rate_delta <= thresholds["error_rate_delta_max"],
            "rps_drop_ok": rps_drop <= thresholds["rps_drop_max"],
        }
        service_passed = all(checks.values())
        if not service_passed:
            failed_services.append(service_name)

        services_report.append(
            {
                "service": service_name,
                "passed": service_passed,
                "degraded": False,
                "reason": "",
                "sample_count": sample_count,
                "current": current,
                "baseline": baseline,
                "deltas": {
                    "p95_regression": round(p95_regression, 6),
                    "p99_regression": round(p99_regression, 6),
                    "error_rate_delta": round(error_rate_delta, 6),
                    "rps_drop": round(rps_drop, 6),
                },
                "checks": checks,
            }
        )

    passed = len(failed_services) == 0
    if failed_services:
        summary = f"perf trend failed services: {', '.join(sorted(set(failed_services)))}"
    elif degraded:
        unique_reasons = sorted(set(degraded_reasons))
        summary = "perf trend passed with degradation: " + "; ".join(unique_reasons[:3])
    else:
        summary = "perf trend passed"

    report = {
        "run_id": run_id,
        "generated_at": generated_at,
        "passed": passed,
        "degraded": degraded,
        "summary": summary,
        "config": {
            "window_days": window_days,
            "min_samples": min_samples,
            "thresholds": thresholds,
            "profile": current_profile,
            "current_perf_report": report_file,
            "current_perf_run_id": current_run_id,
        },
        "history": {
            "report_dir": report_dir,
            "scanned_files": scanned_files,
            "matched_files": matched_files,
        },
        "services": services_report,
    }
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
PY
)"

if ! python3 - <<'PY' "$PAYLOAD_JSON" >/dev/null 2>&1
import json
import sys
json.loads(sys.argv[1])
PY
then
  fail "invalid JSON payload from perf trend analyzer"
fi

printf '%s\n' "$PAYLOAD_JSON" >"$REPORT_FILE"
ln -sfn "$REPORT_FILE" "${ARTIFACT_DIR}/latest.json"

echo "[INFO] Perf trend report: ${REPORT_FILE}"
echo "[INFO] Perf trend latest: ${ARTIFACT_DIR}/latest.json"

python3 - <<'PY' "$REPORT_FILE"
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as f:
    payload = json.load(f)

print("[INFO] Perf trend summary:", payload.get("summary", ""))
print("[INFO] Perf trend degraded:", payload.get("degraded"))
for item in payload.get("services", []):
    deltas = item.get("deltas", {})
    print(
        "[INFO] service={service} passed={passed} degraded={degraded} samples={samples} p95_reg={p95} p99_reg={p99} rps_drop={rps_drop} err_delta={err}".format(
            service=item.get("service"),
            passed=item.get("passed"),
            degraded=item.get("degraded"),
            samples=item.get("sample_count"),
            p95=deltas.get("p95_regression", 0),
            p99=deltas.get("p99_regression", 0),
            rps_drop=deltas.get("rps_drop", 0),
            err=deltas.get("error_rate_delta", 0),
        )
    )
PY

OVERALL_PASSED="$(
python3 - <<'PY' "$REPORT_FILE"
import json
import sys
with open(sys.argv[1], "r", encoding="utf-8") as f:
    payload = json.load(f)
print("true" if payload.get("passed") else "false")
PY
)"

if [[ "$OVERALL_PASSED" != "true" ]]; then
  echo "[ERROR] Perf trend gate failed"
  exit 2
fi

exit 0
