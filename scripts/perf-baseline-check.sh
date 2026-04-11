#!/usr/bin/env bash
set -euo pipefail

# P2 压测基线采集脚本
# 1) 对关键 API 做并发压测
# 2) 采集 p50/p95/p99、吞吐(req/s)、错误率
# 3) 输出报告到 reports/perf-baseline，支持 gate 阈值阻断

NAMESPACE="${NAMESPACE:-islap}"
ARTIFACT_DIR="${ARTIFACT_DIR:-/root/logoscope/reports/perf-baseline}"

PERF_PROFILE="${PERF_PROFILE:-standard}"
PROFILE_TOTAL_REQUESTS_DEFAULT=120
PROFILE_CONCURRENCY_DEFAULT=12
PROFILE_WARMUP_REQUESTS_DEFAULT=8
case "$PERF_PROFILE" in
  standard)
    ;;
  high)
    PROFILE_TOTAL_REQUESTS_DEFAULT=360
    PROFILE_CONCURRENCY_DEFAULT=36
    PROFILE_WARMUP_REQUESTS_DEFAULT=12
    ;;
  *)
    echo "[ERROR] invalid PERF_PROFILE: ${PERF_PROFILE} (allowed: standard|high)" >&2
    exit 1
    ;;
esac

TOTAL_REQUESTS="${TOTAL_REQUESTS:-$PROFILE_TOTAL_REQUESTS_DEFAULT}"
CONCURRENCY="${CONCURRENCY:-$PROFILE_CONCURRENCY_DEFAULT}"
WARMUP_REQUESTS="${WARMUP_REQUESTS:-$PROFILE_WARMUP_REQUESTS_DEFAULT}"
REQUEST_TIMEOUT_SECONDS="${REQUEST_TIMEOUT_SECONDS:-8}"

PERF_P95_MS_MAX="${PERF_P95_MS_MAX:-6000}"
PERF_P99_MS_MAX="${PERF_P99_MS_MAX:-9000}"
PERF_ERROR_RATE_MAX="${PERF_ERROR_RATE_MAX:-0.05}"
PERF_RPS_MIN="${PERF_RPS_MIN:-1.0}"
PERF_SLOW_REQUEST_MS="${PERF_SLOW_REQUEST_MS:-2000}"
PERF_SLOW_RATIO_MAX="${PERF_SLOW_RATIO_MAX:-0.20}"
PERF_MEMORY_PEAK_MB_MAX="${PERF_MEMORY_PEAK_MB_MAX:-2048}"

DEFAULT_ENDPOINTS_JSON='[
  {"name":"query_value_kpi","service":"query-service","url":"http://query-service:8002/api/v1/value/kpi?time_window=1%20HOUR"},
  {"name":"query_value_kpi_alerts","service":"query-service","url":"http://query-service:8002/api/v1/value/kpi/alerts?time_window=1%20HOUR"},
  {"name":"topology_realtime_stats","service":"topology-service","url":"http://topology-service:8003/api/v1/topology/stats/realtime?time_window=1%20HOUR"}
]'
PERF_ENDPOINTS_JSON="${PERF_ENDPOINTS_JSON:-$DEFAULT_ENDPOINTS_JSON}"
DEFAULT_SERVICE_THRESHOLDS_JSON='{
  "query-service": {
    "p95_ms_max": 5000,
    "p99_ms_max": 8000,
    "rps_min": 3.0
  },
  "topology-service": {
    "p95_ms_max": 6500,
    "p99_ms_max": 9000,
    "rps_min": 2.0
  }
}'
PERF_SERVICE_THRESHOLDS_JSON="${PERF_SERVICE_THRESHOLDS_JSON:-$DEFAULT_SERVICE_THRESHOLDS_JSON}"

usage() {
  cat <<'EOF'
Perf baseline check (P2)

Env vars:
  NAMESPACE                Kubernetes namespace (default: islap)
  ARTIFACT_DIR             Report output dir (default: /root/logoscope/reports/perf-baseline)
  PERF_PROFILE             Load profile (default: standard, optional: high)
  TOTAL_REQUESTS           Requests per endpoint (default: profile-based, standard=120, high=360)
  CONCURRENCY              Concurrency per endpoint (default: profile-based, standard=12, high=36)
  WARMUP_REQUESTS          Warmup requests per endpoint (default: profile-based, standard=8, high=12)
  REQUEST_TIMEOUT_SECONDS  HTTP timeout seconds (default: 8)
  PERF_P95_MS_MAX          p95 latency upper bound in ms (default: 6000)
  PERF_P99_MS_MAX          p99 latency upper bound in ms (default: 9000)
  PERF_ERROR_RATE_MAX      Error rate upper bound (default: 0.05)
  PERF_RPS_MIN             Throughput lower bound req/s (default: 1.0)
  PERF_SLOW_REQUEST_MS     Slow request latency threshold in ms (default: 2000)
  PERF_SLOW_RATIO_MAX      Slow request ratio upper bound (default: 0.20)
  PERF_MEMORY_PEAK_MB_MAX  Query pod memory peak upper bound in MB (default: 2048)
  PERF_ENDPOINTS_JSON      Endpoint list JSON, each item: {"name":"...","service":"...","url":"..."}
  PERF_SERVICE_THRESHOLDS_JSON
                           Service threshold overrides JSON, e.g. {"query-service":{"p95_ms_max":4500}}

Example:
  scripts/perf-baseline-check.sh
  TOTAL_REQUESTS=200 CONCURRENCY=20 PERF_P95_MS_MAX=4000 scripts/perf-baseline-check.sh
  PERF_PROFILE=high scripts/perf-baseline-check.sh
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

RUN_ID="perf-baseline-$(date -u +%Y%m%d-%H%M%S)-$RANDOM"
LOG_FILE="${ARTIFACT_DIR}/${RUN_ID}.log"
REPORT_FILE="${ARTIFACT_DIR}/${RUN_ID}.json"
GENERATED_AT="$(date -u +"%Y-%m-%dT%H:%M:%S.%3NZ")"

QUERY_POD="$(kubectl -n "$NAMESPACE" get pod -l app=query-service -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
if [[ -z "$QUERY_POD" ]]; then
  fail "query-service pod not found in namespace ${NAMESPACE}"
fi

echo "[INFO] Running perf baseline in pod: ${QUERY_POD}" >"$LOG_FILE"
echo "[INFO] Namespace: ${NAMESPACE}" >>"$LOG_FILE"
echo "[INFO] Profile=${PERF_PROFILE}" >>"$LOG_FILE"
echo "[INFO] Requests=${TOTAL_REQUESTS}, Concurrency=${CONCURRENCY}, Warmup=${WARMUP_REQUESTS}" >>"$LOG_FILE"
echo "[INFO] Thresholds: p95<=${PERF_P95_MS_MAX}ms, p99<=${PERF_P99_MS_MAX}ms, error_rate<=${PERF_ERROR_RATE_MAX}, rps>=${PERF_RPS_MIN}, slow_ratio<=${PERF_SLOW_RATIO_MAX}(>${PERF_SLOW_REQUEST_MS}ms), memory_peak<=${PERF_MEMORY_PEAK_MB_MAX}MB" >>"$LOG_FILE"

set +e
PAYLOAD_JSON="$(
kubectl -n "$NAMESPACE" exec -i "$QUERY_POD" -c query-service -- env \
  RUN_ID="${RUN_ID}" \
  GENERATED_AT="${GENERATED_AT}" \
  PERF_PROFILE="${PERF_PROFILE}" \
  TOTAL_REQUESTS="${TOTAL_REQUESTS}" \
  CONCURRENCY="${CONCURRENCY}" \
  WARMUP_REQUESTS="${WARMUP_REQUESTS}" \
  REQUEST_TIMEOUT_SECONDS="${REQUEST_TIMEOUT_SECONDS}" \
  PERF_P95_MS_MAX="${PERF_P95_MS_MAX}" \
  PERF_P99_MS_MAX="${PERF_P99_MS_MAX}" \
  PERF_ERROR_RATE_MAX="${PERF_ERROR_RATE_MAX}" \
  PERF_RPS_MIN="${PERF_RPS_MIN}" \
  PERF_SLOW_REQUEST_MS="${PERF_SLOW_REQUEST_MS}" \
  PERF_SLOW_RATIO_MAX="${PERF_SLOW_RATIO_MAX}" \
  PERF_MEMORY_PEAK_MB_MAX="${PERF_MEMORY_PEAK_MB_MAX}" \
  PERF_ENDPOINTS_JSON="${PERF_ENDPOINTS_JSON}" \
  PERF_SERVICE_THRESHOLDS_JSON="${PERF_SERVICE_THRESHOLDS_JSON}" \
  python - <<'PY'
import json
import math
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from threading import Event, Thread
from typing import Any, Dict, List, Optional, Tuple

THRESHOLD_KEYS = (
    "p95_ms_max",
    "p99_ms_max",
    "error_rate_max",
    "rps_min",
    "slow_request_ms",
    "slow_ratio_max",
    "memory_peak_mb_max",
)
THRESHOLD_MINIMUMS: Dict[str, float] = {
    "p95_ms_max": 1.0,
    "p99_ms_max": 1.0,
    "error_rate_max": 0.0,
    "rps_min": 0.0,
    "slow_request_ms": 1.0,
    "slow_ratio_max": 0.0,
    "memory_peak_mb_max": 1.0,
}


def read_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(value, minimum)


def read_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return max(value, minimum)


def percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    rank = int(math.ceil((p / 100.0) * len(sorted_values))) - 1
    rank = max(0, min(rank, len(sorted_values) - 1))
    return sorted_values[rank]


def read_int_file(path: str) -> Optional[int]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
    except (OSError, ValueError):
        return None
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def resolve_memory_sources() -> Tuple[str, Optional[str], Optional[str]]:
    # Prefer cgroup v2 paths, fallback to cgroup v1.
    candidates = [
        ("cgroup_v2", "/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.peak"),
        ("cgroup_v1", "/sys/fs/cgroup/memory/memory.usage_in_bytes", "/sys/fs/cgroup/memory/memory.max_usage_in_bytes"),
    ]
    for source, current_path, peak_path in candidates:
        if os.path.exists(current_path):
            if not os.path.exists(peak_path):
                peak_path = None
            return source, current_path, peak_path
    return "unavailable", None, None


def monitor_memory(current_path: str, stop_event: Event, samples: List[int], interval_seconds: float = 0.2) -> None:
    while not stop_event.is_set():
        value = read_int_file(current_path)
        if value is not None:
            samples.append(value)
        stop_event.wait(interval_seconds)


def normalize_threshold_overrides(raw: Any) -> Dict[str, float]:
    if not isinstance(raw, dict):
        return {}

    normalized: Dict[str, float] = {}
    for key in THRESHOLD_KEYS:
        if key not in raw:
            continue
        try:
            value = float(raw[key])
        except (TypeError, ValueError):
            continue
        normalized[key] = max(value, THRESHOLD_MINIMUMS.get(key, 0.0))
    return normalized


def request_once(url: str, timeout: float) -> Dict[str, Any]:
    started = time.perf_counter()
    status_code = 599
    success = False
    error_message = ""
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status_code = int(resp.status)
            resp.read()
        success = 200 <= status_code < 400
    except urllib.error.HTTPError as exc:
        status_code = int(exc.code)
        try:
            exc.read()
        except Exception as read_exc:  # noqa: BLE001
            print(
                f"Warning: failed to read HTTP error body for {url}: {type(read_exc).__name__}:{read_exc}",
                file=sys.stderr,
            )
        error_message = f"HTTPError:{status_code}"
    except Exception as exc:  # noqa: BLE001
        error_message = f"{type(exc).__name__}:{exc}"

    latency_ms = (time.perf_counter() - started) * 1000.0
    return {
        "success": success,
        "status_code": status_code,
        "latency_ms": latency_ms,
        "error": error_message,
    }


def warmup_endpoint(url: str, warmup_requests: int, timeout_seconds: float) -> None:
    for _ in range(warmup_requests):
        request_once(url, timeout_seconds)


def benchmark_endpoint(
    endpoint: Dict[str, Any],
    total_requests: int,
    concurrency: int,
    timeout_seconds: float,
    thresholds: Dict[str, float],
) -> Dict[str, Any]:
    latencies: List[float] = []
    success_count = 0
    failure_count = 0
    status_buckets: Dict[str, int] = {}
    error_samples: List[str] = []

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(request_once, endpoint["url"], timeout_seconds) for _ in range(total_requests)]
        for future in as_completed(futures):
            result = future.result()
            latency = float(result["latency_ms"])
            latencies.append(latency)
            status_key = str(result["status_code"])
            status_buckets[status_key] = status_buckets.get(status_key, 0) + 1
            if result["success"]:
                success_count += 1
            else:
                failure_count += 1
                if result["error"] and len(error_samples) < 8:
                    error_samples.append(str(result["error"]))

    duration_seconds = max(time.perf_counter() - started, 1e-9)
    throughput_rps = float(total_requests) / duration_seconds
    error_rate = float(failure_count) / float(total_requests) if total_requests > 0 else 1.0
    slow_count = sum(1 for latency in latencies if latency > thresholds["slow_request_ms"])
    slow_ratio = float(slow_count) / float(total_requests) if total_requests > 0 else 1.0

    p50 = percentile(latencies, 50)
    p95 = percentile(latencies, 95)
    p99 = percentile(latencies, 99)
    avg_latency = (sum(latencies) / len(latencies)) if latencies else 0.0

    checks = {
        "p95_ok": p95 <= thresholds["p95_ms_max"],
        "p99_ok": p99 <= thresholds["p99_ms_max"],
        "error_rate_ok": error_rate <= thresholds["error_rate_max"],
        "throughput_ok": throughput_rps >= thresholds["rps_min"],
        "slow_ratio_ok": slow_ratio <= thresholds["slow_ratio_max"],
    }
    passed = all(checks.values())

    return {
        "name": endpoint["name"],
        "service": endpoint.get("service", "default"),
        "url": endpoint["url"],
        "passed": passed,
        "checks": checks,
        "thresholds": thresholds,
        "metrics": {
            "total_requests": total_requests,
            "success_count": success_count,
            "failure_count": failure_count,
            "error_rate": round(error_rate, 6),
            "throughput_rps": round(throughput_rps, 3),
            "duration_seconds": round(duration_seconds, 3),
            "slow_request_ms_threshold": round(thresholds["slow_request_ms"], 3),
            "slow_count": slow_count,
            "slow_ratio": round(slow_ratio, 6),
            "latency_ms": {
                "min": round(min(latencies) if latencies else 0.0, 3),
                "avg": round(avg_latency, 3),
                "p50": round(p50, 3),
                "p95": round(p95, 3),
                "p99": round(p99, 3),
                "max": round(max(latencies) if latencies else 0.0, 3),
            },
            "status_codes": status_buckets,
        },
        "error_samples": error_samples,
    }


def build_error_report(run_id: str, generated_at: str, message: str) -> Dict[str, Any]:
    return {
        "run_id": run_id,
        "generated_at": generated_at,
        "passed": False,
        "summary": message,
        "config": {},
        "results": [],
    }


def main() -> None:
    run_id = os.getenv("RUN_ID", f"perf-baseline-{int(time.time())}")
    generated_at = os.getenv("GENERATED_AT", datetime.now(timezone.utc).isoformat())
    profile = os.getenv("PERF_PROFILE", "standard")
    total_requests = read_int("TOTAL_REQUESTS", 120, minimum=1)
    concurrency = read_int("CONCURRENCY", 12, minimum=1)
    warmup_requests = read_int("WARMUP_REQUESTS", 8, minimum=0)
    timeout_seconds = read_float("REQUEST_TIMEOUT_SECONDS", 8.0, minimum=0.1)

    thresholds = {
        "p95_ms_max": read_float("PERF_P95_MS_MAX", 6000.0, minimum=1.0),
        "p99_ms_max": read_float("PERF_P99_MS_MAX", 9000.0, minimum=1.0),
        "error_rate_max": read_float("PERF_ERROR_RATE_MAX", 0.05, minimum=0.0),
        "rps_min": read_float("PERF_RPS_MIN", 1.0, minimum=0.0),
        "slow_request_ms": read_float("PERF_SLOW_REQUEST_MS", 2000.0, minimum=1.0),
        "slow_ratio_max": read_float("PERF_SLOW_RATIO_MAX", 0.20, minimum=0.0),
        "memory_peak_mb_max": read_float("PERF_MEMORY_PEAK_MB_MAX", 2048.0, minimum=1.0),
    }

    service_thresholds_raw = os.getenv("PERF_SERVICE_THRESHOLDS_JSON", "{}")
    try:
        service_thresholds_any = json.loads(service_thresholds_raw)
    except json.JSONDecodeError:
        print(json.dumps(build_error_report(run_id, generated_at, "invalid PERF_SERVICE_THRESHOLDS_JSON"), ensure_ascii=False))
        return
    if not isinstance(service_thresholds_any, dict):
        print(json.dumps(build_error_report(run_id, generated_at, "PERF_SERVICE_THRESHOLDS_JSON must be an object"), ensure_ascii=False))
        return
    service_threshold_overrides: Dict[str, Dict[str, float]] = {}
    for service_name, override_raw in service_thresholds_any.items():
        service_key = str(service_name).strip()
        if not service_key:
            continue
        service_threshold_overrides[service_key] = normalize_threshold_overrides(override_raw)

    endpoints_raw = os.getenv("PERF_ENDPOINTS_JSON", "[]")
    try:
        endpoints = json.loads(endpoints_raw)
    except json.JSONDecodeError:
        print(json.dumps(build_error_report(run_id, generated_at, "invalid PERF_ENDPOINTS_JSON"), ensure_ascii=False))
        return

    if not isinstance(endpoints, list) or not endpoints:
        print(json.dumps(build_error_report(run_id, generated_at, "no endpoints configured"), ensure_ascii=False))
        return

    normalized_endpoints: List[Dict[str, Any]] = []
    for idx, endpoint in enumerate(endpoints):
        if not isinstance(endpoint, dict):
            continue
        name = str(endpoint.get("name", "")).strip() or f"endpoint_{idx + 1}"
        service = str(endpoint.get("service", "")).strip() or "default"
        url = str(endpoint.get("url", "")).strip()
        if not url:
            continue
        normalized_endpoints.append(
            {
                "name": name,
                "service": service,
                "url": url,
                "threshold_overrides": normalize_threshold_overrides(endpoint.get("thresholds")),
            }
        )

    if not normalized_endpoints:
        print(json.dumps(build_error_report(run_id, generated_at, "endpoint list is empty after validation"), ensure_ascii=False))
        return

    memory_source, memory_current_path, memory_peak_path = resolve_memory_sources()
    memory_samples: List[int] = []
    memory_start_bytes = read_int_file(memory_current_path) if memory_current_path else None
    memory_peak_start_bytes = read_int_file(memory_peak_path) if memory_peak_path else None
    stop_event = Event()
    monitor_thread: Optional[Thread] = None
    if memory_current_path:
        monitor_thread = Thread(target=monitor_memory, args=(memory_current_path, stop_event, memory_samples), daemon=True)
        monitor_thread.start()

    endpoint_results: List[Dict[str, Any]] = []
    try:
        for endpoint in normalized_endpoints:
            if warmup_requests > 0:
                warmup_endpoint(endpoint["url"], warmup_requests, timeout_seconds)

        for endpoint in normalized_endpoints:
            service_thresholds = dict(thresholds)
            service_thresholds.update(service_threshold_overrides.get("default", {}))
            service_thresholds.update(service_threshold_overrides.get(endpoint["service"], {}))
            service_thresholds.update(endpoint.get("threshold_overrides", {}))
            endpoint_results.append(
                benchmark_endpoint(
                    endpoint=endpoint,
                    total_requests=total_requests,
                    concurrency=concurrency,
                    timeout_seconds=timeout_seconds,
                    thresholds=service_thresholds,
                )
            )
    finally:
        if monitor_thread is not None:
            stop_event.set()
            monitor_thread.join(timeout=1.0)

    aggregate_total = sum(int(item.get("metrics", {}).get("total_requests", 0)) for item in endpoint_results)
    aggregate_failed = sum(int(item.get("metrics", {}).get("failure_count", 0)) for item in endpoint_results)
    aggregate_slow = sum(int(item.get("metrics", {}).get("slow_count", 0)) for item in endpoint_results)
    aggregate_duration = sum(float(item.get("metrics", {}).get("duration_seconds", 0.0)) for item in endpoint_results)
    aggregate_error_rate = (float(aggregate_failed) / float(aggregate_total)) if aggregate_total > 0 else 1.0
    aggregate_slow_ratio = (float(aggregate_slow) / float(aggregate_total)) if aggregate_total > 0 else 1.0
    aggregate_throughput = (float(aggregate_total) / aggregate_duration) if aggregate_duration > 0 else 0.0

    service_buckets: Dict[str, Dict[str, Any]] = {}
    for item in endpoint_results:
        service = str(item.get("service", "default"))
        bucket = service_buckets.setdefault(
            service,
            {
                "service": service,
                "endpoint_count": 0,
                "total_requests": 0,
                "failure_count": 0,
                "slow_count": 0,
                "duration_seconds": 0.0,
                "p95_max_ms": 0.0,
                "p99_max_ms": 0.0,
                "thresholds": dict(thresholds),
            },
        )
        bucket["endpoint_count"] += 1
        endpoint_metrics = item.get("metrics", {})
        latency_metrics = endpoint_metrics.get("latency_ms", {})
        bucket["total_requests"] += int(endpoint_metrics.get("total_requests", 0))
        bucket["failure_count"] += int(endpoint_metrics.get("failure_count", 0))
        bucket["slow_count"] += int(endpoint_metrics.get("slow_count", 0))
        bucket["duration_seconds"] += float(endpoint_metrics.get("duration_seconds", 0.0))
        bucket["p95_max_ms"] = max(bucket["p95_max_ms"], float(latency_metrics.get("p95", 0.0)))
        bucket["p99_max_ms"] = max(bucket["p99_max_ms"], float(latency_metrics.get("p99", 0.0)))

    service_aggregates: List[Dict[str, Any]] = []
    for service_name in sorted(service_buckets.keys()):
        bucket = service_buckets[service_name]
        merged_thresholds = dict(thresholds)
        merged_thresholds.update(service_threshold_overrides.get("default", {}))
        merged_thresholds.update(service_threshold_overrides.get(service_name, {}))

        total = int(bucket["total_requests"])
        failed = int(bucket["failure_count"])
        slow = int(bucket["slow_count"])
        duration = float(bucket["duration_seconds"])
        error_rate = (float(failed) / float(total)) if total > 0 else 1.0
        slow_ratio = (float(slow) / float(total)) if total > 0 else 1.0
        throughput = (float(total) / duration) if duration > 0 else 0.0

        checks = {
            "p95_ok": float(bucket["p95_max_ms"]) <= merged_thresholds["p95_ms_max"],
            "p99_ok": float(bucket["p99_max_ms"]) <= merged_thresholds["p99_ms_max"],
            "error_rate_ok": error_rate <= merged_thresholds["error_rate_max"],
            "throughput_ok": throughput >= merged_thresholds["rps_min"],
            "slow_ratio_ok": slow_ratio <= merged_thresholds["slow_ratio_max"],
        }
        service_passed = all(checks.values())
        service_aggregates.append(
            {
                "service": service_name,
                "passed": service_passed,
                "checks": checks,
                "thresholds": merged_thresholds,
                "metrics": {
                    "endpoint_count": int(bucket["endpoint_count"]),
                    "total_requests": total,
                    "failure_count": failed,
                    "slow_count": slow,
                    "error_rate": round(error_rate, 6),
                    "slow_ratio": round(slow_ratio, 6),
                    "throughput_rps": round(throughput, 3),
                    "duration_seconds": round(duration, 3),
                    "p95_max_ms": round(float(bucket["p95_max_ms"]), 3),
                    "p99_max_ms": round(float(bucket["p99_max_ms"]), 3),
                },
            }
        )
    service_aggregate_ok = all(item.get("passed", False) for item in service_aggregates)

    memory_peak_observed_bytes = max(memory_samples) if memory_samples else (memory_start_bytes or 0)
    memory_peak_read_bytes = read_int_file(memory_peak_path) if memory_peak_path else None
    if memory_peak_read_bytes is not None:
        memory_peak_observed_bytes = max(memory_peak_observed_bytes, memory_peak_read_bytes)
    memory_start_ref_bytes = memory_start_bytes or 0
    memory_delta_bytes = max(0, memory_peak_observed_bytes - memory_start_ref_bytes)
    memory_peak_mb = float(memory_peak_observed_bytes) / (1024.0 * 1024.0)

    memory_available = memory_current_path is not None
    global_checks = {
        "aggregate_error_rate_ok": aggregate_error_rate <= thresholds["error_rate_max"],
        "aggregate_throughput_ok": aggregate_throughput >= thresholds["rps_min"],
        "aggregate_slow_ratio_ok": aggregate_slow_ratio <= thresholds["slow_ratio_max"],
        "service_aggregate_ok": service_aggregate_ok,
        "memory_peak_ok": True if not memory_available else memory_peak_mb <= thresholds["memory_peak_mb_max"],
    }
    endpoints_passed = all(item.get("passed", False) for item in endpoint_results)
    passed = endpoints_passed and all(global_checks.values())
    failed_endpoints = [item.get("name") for item in endpoint_results if not item.get("passed", False)]
    failed_global_checks = [name for name, ok in global_checks.items() if not ok]

    summary_parts: List[str] = []
    if failed_endpoints:
        summary_parts.append(f"failed endpoints: {', '.join(failed_endpoints)}")
    if failed_global_checks:
        summary_parts.append(f"failed checks: {', '.join(failed_global_checks)}")
    summary = "perf baseline passed" if not summary_parts else "; ".join(summary_parts)

    report = {
        "run_id": run_id,
        "generated_at": generated_at,
        "passed": passed,
        "summary": summary,
        "config": {
            "profile": profile,
            "total_requests": total_requests,
            "concurrency": concurrency,
            "warmup_requests": warmup_requests,
            "request_timeout_seconds": timeout_seconds,
            "thresholds": thresholds,
            "service_threshold_overrides": service_threshold_overrides,
            "endpoint_count": len(normalized_endpoints),
        },
        "aggregate": {
            "total_requests": aggregate_total,
            "failure_count": aggregate_failed,
            "slow_count": aggregate_slow,
            "error_rate": round(aggregate_error_rate, 6),
            "slow_ratio": round(aggregate_slow_ratio, 6),
            "throughput_rps": round(aggregate_throughput, 3),
        },
        "global_checks": global_checks,
        "service_aggregates": service_aggregates,
        "memory": {
            "available": memory_available,
            "source": memory_source,
            "current_path": memory_current_path or "",
            "peak_path": memory_peak_path or "",
            "sample_count": len(memory_samples),
            "start_current_bytes": memory_start_bytes if memory_start_bytes is not None else -1,
            "start_peak_bytes": memory_peak_start_bytes if memory_peak_start_bytes is not None else -1,
            "peak_observed_bytes": memory_peak_observed_bytes,
            "peak_observed_mb": round(memory_peak_mb, 3),
            "peak_delta_bytes": memory_delta_bytes,
            "peak_delta_mb": round(float(memory_delta_bytes) / (1024.0 * 1024.0), 3),
            "threshold_mb_max": thresholds["memory_peak_mb_max"],
            "degraded": not memory_available,
            "degraded_reason": "" if memory_available else "memory cgroup files unavailable, skip memory peak gate",
        },
        "results": endpoint_results,
    }
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
PY
)"
KUBECTL_EXIT_CODE=$?
set -e

if [[ "$KUBECTL_EXIT_CODE" -ne 0 ]]; then
  cat >>"$LOG_FILE" <<EOF
[ERROR] kubectl exec failed with code ${KUBECTL_EXIT_CODE}
EOF
  cat >"$REPORT_FILE" <<EOF
{
  "run_id": "${RUN_ID}",
  "generated_at": "${GENERATED_AT}",
  "passed": false,
  "summary": "kubectl exec failed with code ${KUBECTL_EXIT_CODE}",
  "config": {},
  "results": []
}
EOF
  ln -sfn "$REPORT_FILE" "${ARTIFACT_DIR}/latest.json"
  echo "[INFO] Perf baseline report: ${REPORT_FILE}"
  echo "[ERROR] Perf baseline execution failed"
  exit "$KUBECTL_EXIT_CODE"
fi

if ! python3 - <<'PY' "$PAYLOAD_JSON" >/dev/null 2>&1
import json
import sys
json.loads(sys.argv[1])
PY
then
  echo "[ERROR] Invalid JSON payload from perf baseline probe" >>"$LOG_FILE"
  cat >"$REPORT_FILE" <<EOF
{
  "run_id": "${RUN_ID}",
  "generated_at": "${GENERATED_AT}",
  "passed": false,
  "summary": "invalid JSON payload from perf probe",
  "config": {},
  "results": []
}
EOF
else
  printf '%s\n' "$PAYLOAD_JSON" >"$REPORT_FILE"
fi

printf '%s\n' "$PAYLOAD_JSON" >>"$LOG_FILE"
ln -sfn "$REPORT_FILE" "${ARTIFACT_DIR}/latest.json"

echo "[INFO] Perf baseline report: ${REPORT_FILE}"
echo "[INFO] Perf baseline latest: ${ARTIFACT_DIR}/latest.json"

python3 - <<'PY' "$REPORT_FILE"
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as f:
    payload = json.load(f)

print("[INFO] Perf baseline summary:", payload.get("summary", ""))
config = payload.get("config", {})
print("[INFO] Perf baseline profile:", config.get("profile", "standard"))
for item in payload.get("results", []):
    metrics = item.get("metrics", {})
    latency = metrics.get("latency_ms", {})
    print(
        "[INFO] endpoint={name} service={service} passed={passed} p50={p50}ms p95={p95}ms p99={p99}ms rps={rps} err={err} slow={slow}".format(
            name=item.get("name"),
            service=item.get("service", "default"),
            passed=item.get("passed"),
            p50=latency.get("p50", 0),
            p95=latency.get("p95", 0),
            p99=latency.get("p99", 0),
            rps=metrics.get("throughput_rps", 0),
            err=metrics.get("error_rate", 0),
            slow=metrics.get("slow_ratio", 0),
        )
    )

for item in payload.get("service_aggregates", []):
    metrics = item.get("metrics", {})
    print(
        "[INFO] service={service} passed={passed} p95_max={p95}ms p99_max={p99}ms rps={rps} err={err} slow={slow}".format(
            service=item.get("service"),
            passed=item.get("passed"),
            p95=metrics.get("p95_max_ms", 0),
            p99=metrics.get("p99_max_ms", 0),
            rps=metrics.get("throughput_rps", 0),
            err=metrics.get("error_rate", 0),
            slow=metrics.get("slow_ratio", 0),
        )
    )

aggregate = payload.get("aggregate", {})
memory = payload.get("memory", {})
print(
    "[INFO] aggregate err={err} slow={slow} rps={rps}".format(
        err=aggregate.get("error_rate", 0),
        slow=aggregate.get("slow_ratio", 0),
        rps=aggregate.get("throughput_rps", 0),
    )
)
print(
    "[INFO] memory available={available} peak={peak}MB delta={delta}MB threshold={threshold}MB degraded={degraded}".format(
        available=memory.get("available"),
        peak=memory.get("peak_observed_mb", 0),
        delta=memory.get("peak_delta_mb", 0),
        threshold=memory.get("threshold_mb_max", 0),
        degraded=memory.get("degraded"),
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
  echo "[ERROR] Perf baseline gate failed"
  exit 2
fi

exit 0
