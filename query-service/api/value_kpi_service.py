"""Value KPI domain services extracted from query_routes."""

from __future__ import annotations

import csv
import io
import os
import re
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


def _read_positive_int_env(name: str, default_value: int, min_value: int = 1, max_value: int = 200000) -> int:
    """Read positive integer env with clamped fallback."""
    raw = os.getenv(name, str(default_value))
    try:
        parsed = int(str(raw).strip())
    except Exception:
        parsed = default_value
    return max(min_value, min(parsed, max_value))


VALUE_KPI_INCIDENT_LOG_LIMIT = _read_positive_int_env("VALUE_KPI_INCIDENT_LOG_LIMIT", 20000)
VALUE_KPI_INFERENCE_LOG_LIMIT = _read_positive_int_env("VALUE_KPI_INFERENCE_LOG_LIMIT", 20000)
VALUE_KPI_INFERENCE_LOG_LIMIT_LONG_WINDOW = _read_positive_int_env("VALUE_KPI_INFERENCE_LOG_LIMIT_LONG_WINDOW", 10000)
VALUE_KPI_CORRELATION_SAMPLE_LIMIT = _read_positive_int_env("VALUE_KPI_CORRELATION_SAMPLE_LIMIT", 50000)
VALUE_KPI_SERVICE_COUNT_SAMPLE_LIMIT = _read_positive_int_env("VALUE_KPI_SERVICE_COUNT_SAMPLE_LIMIT", 50000)
VALUE_KPI_PREAGG_TABLE_CACHE_TTL_SECONDS = _read_positive_int_env("VALUE_KPI_PREAGG_TABLE_CACHE_TTL_SECONDS", 60)
VALUE_KPI_SCAN_HARD_LIMIT = _read_positive_int_env(
    "VALUE_KPI_SCAN_HARD_LIMIT",
    20000,
    min_value=1000,
    max_value=200000,
)
_PREAGG_SCHEMA_VERSIONS = {"legacy", "v2", "auto"}

_TABLE_EXISTS_CACHE: Dict[str, Tuple[bool, float]] = {}


def _cap_scan_limit(value: int) -> int:
    """Cap potentially expensive scan limits with a global hard limit."""
    return max(1, min(int(value), int(VALUE_KPI_SCAN_HARD_LIMIT)))


def _read_preagg_schema_version() -> str:
    raw = str(os.getenv("PREAGG_SCHEMA_VERSION", "auto") or "auto").strip().lower()
    if raw not in _PREAGG_SCHEMA_VERSIONS:
        return "auto"
    return raw


def _should_try_v2_preagg() -> bool:
    return _read_preagg_schema_version() in {"v2", "auto"}


def _should_try_legacy_preagg() -> bool:
    return _read_preagg_schema_version() in {"legacy", "auto"}


def _interval_seconds(interval_text: str) -> int:
    """Convert sanitized interval text (e.g. '7 DAY') into seconds."""
    match = re.match(r"^\s*(\d+)\s+([A-Za-z]+)\s*$", str(interval_text or ""))
    if not match:
        return 7 * 24 * 3600
    amount = max(int(match.group(1)), 0)
    unit = str(match.group(2) or "").upper()
    if unit in {"MINUTE", "MINUTES"}:
        return amount * 60
    if unit in {"HOUR", "HOURS"}:
        return amount * 3600
    if unit in {"WEEK", "WEEKS"}:
        return amount * 7 * 24 * 3600
    return amount * 24 * 3600


def _has_table(storage_adapter: Any, database: str, table_name: str) -> bool:
    """Check table existence with short TTL cache to avoid repetitive system-table scans."""
    cache_key = f"{database}.{table_name}"
    current_ts = time.time()
    cached = _TABLE_EXISTS_CACHE.get(cache_key)
    if cached and cached[1] > current_ts:
        return bool(cached[0])

    try:
        rows = storage_adapter.execute_query(
            """
            SELECT count() AS cnt
            FROM system.tables
            WHERE database = {db:String}
              AND name = {table:String}
            """,
            {"db": database, "table": table_name},
        )
        exists = bool(int((rows[0].get("cnt") if rows else 0) or 0))
    except Exception:
        exists = False
    _TABLE_EXISTS_CACHE[cache_key] = (exists, current_ts + float(VALUE_KPI_PREAGG_TABLE_CACHE_TTL_SECONDS))
    return exists


def evaluate_value_kpi_alerts(
    metrics: Dict[str, Any],
    max_mttd_minutes: float,
    max_mttr_minutes: float,
    min_trace_log_correlation_rate: float,
    min_topology_coverage_rate: float,
    min_release_regression_pass_rate: float,
    suppressed_metrics: Set[str],
) -> List[Dict[str, Any]]:
    """Evaluate alert status for value KPI metrics."""
    rules = [
        ("mttd_minutes", ">", float(metrics.get("mttd_minutes", 0.0)), float(max_mttd_minutes), "warning"),
        ("mttr_minutes", ">", float(metrics.get("mttr_minutes", 0.0)), float(max_mttr_minutes), "critical"),
        (
            "trace_log_correlation_rate",
            "<",
            float(metrics.get("trace_log_correlation_rate", 0.0)),
            float(min_trace_log_correlation_rate),
            "warning",
        ),
        (
            "topology_coverage_rate",
            "<",
            float(metrics.get("topology_coverage_rate", 0.0)),
            float(min_topology_coverage_rate),
            "warning",
        ),
        (
            "release_regression_pass_rate",
            "<",
            float(metrics.get("release_regression_pass_rate", 0.0)),
            float(min_release_regression_pass_rate),
            "critical",
        ),
    ]

    alerts: List[Dict[str, Any]] = []
    for metric_name, operator, current_value, threshold, severity in rules:
        triggered = (current_value > threshold) if operator == ">" else (current_value < threshold)
        if not triggered:
            continue
        alerts.append(
            {
                "metric": metric_name,
                "operator": operator,
                "threshold": threshold,
                "value": current_value,
                "expression": f"{metric_name}{operator}{threshold}",
                "severity": severity,
                "triggered": True,
                "suppressed": metric_name in suppressed_metrics,
            }
        )
    return alerts


def ensure_value_kpi_snapshot_table(storage_adapter: Any) -> None:
    """Ensure snapshot table exists."""
    if not storage_adapter:
        return

    create_table_query = """
    CREATE TABLE IF NOT EXISTS logs.value_kpi_snapshots (
        snapshot_id String,
        source String,
        time_window String,
        window_start DateTime64(3, 'UTC'),
        window_end DateTime64(3, 'UTC'),
        mttd_minutes Float64,
        mttr_minutes Float64,
        trace_log_correlation_rate Float64,
        topology_coverage_rate Float64,
        release_regression_pass_rate Float64,
        incident_count UInt32,
        release_gate_total UInt32,
        release_gate_passed UInt32,
        release_gate_failed UInt32,
        release_gate_bypassed UInt32,
        created_at DateTime64(3, 'UTC') DEFAULT now64(3, 'UTC')
    )
    ENGINE = MergeTree()
    ORDER BY (created_at, snapshot_id)
    SETTINGS index_granularity = 8192
    """
    storage_adapter.execute_query(create_table_query)


def store_value_kpi_snapshot(
    storage_adapter: Any,
    computed: Dict[str, Any],
    time_window: str,
    source: str,
    window_start: datetime,
    window_end: datetime,
    sanitize_interval_fn: Callable[[str, str], str],
) -> Dict[str, Any]:
    """Persist value KPI snapshot."""
    ensure_value_kpi_snapshot_table(storage_adapter)

    snapshot_id = f"vkpi-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    metrics = computed.get("metrics", {})
    incidents = computed.get("incident_summary", {})
    gate = computed.get("release_gate_summary", {})

    window_start_text = window_start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    window_end_text = window_end.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    safe_time_window = sanitize_interval_fn(time_window, "7 DAY")

    insert_query = """
    INSERT INTO logs.value_kpi_snapshots (
        snapshot_id,
        source,
        time_window,
        window_start,
        window_end,
        mttd_minutes,
        mttr_minutes,
        trace_log_correlation_rate,
        topology_coverage_rate,
        release_regression_pass_rate,
        incident_count,
        release_gate_total,
        release_gate_passed,
        release_gate_failed,
        release_gate_bypassed
    ) VALUES (
        {snapshot_id:String},
        {source:String},
        {time_window:String},
        {window_start:String},
        {window_end:String},
        {mttd_minutes:Float64},
        {mttr_minutes:Float64},
        {trace_log_correlation_rate:Float64},
        {topology_coverage_rate:Float64},
        {release_regression_pass_rate:Float64},
        {incident_count:UInt32},
        {release_gate_total:UInt32},
        {release_gate_passed:UInt32},
        {release_gate_failed:UInt32},
        {release_gate_bypassed:UInt32}
    )
    """
    storage_adapter.execute_query(
        insert_query,
        {
            "snapshot_id": snapshot_id,
            "source": source,
            "time_window": safe_time_window,
            "window_start": window_start_text,
            "window_end": window_end_text,
            "mttd_minutes": float(metrics.get("mttd_minutes", 0.0)),
            "mttr_minutes": float(metrics.get("mttr_minutes", 0.0)),
            "trace_log_correlation_rate": float(metrics.get("trace_log_correlation_rate", 0.0)),
            "topology_coverage_rate": float(metrics.get("topology_coverage_rate", 0.0)),
            "release_regression_pass_rate": float(metrics.get("release_regression_pass_rate", 0.0)),
            "incident_count": int(incidents.get("incident_count", 0) or 0),
            "release_gate_total": int(gate.get("total", 0) or 0),
            "release_gate_passed": int(gate.get("passed", 0) or 0),
            "release_gate_failed": int(gate.get("failed", 0) or 0),
            "release_gate_bypassed": int(gate.get("bypassed", 0) or 0),
        },
    )

    return {
        "snapshot_id": snapshot_id,
        "source": source,
        "time_window": safe_time_window,
        "window_start": window_start.astimezone(timezone.utc).isoformat(),
        "window_end": window_end.astimezone(timezone.utc).isoformat(),
        "metrics": metrics,
        "incident_summary": incidents,
        "release_gate_summary": gate,
    }


def list_value_kpi_snapshots(storage_adapter: Any, limit: int, source: Optional[str] = None) -> List[Dict[str, Any]]:
    """Query value KPI snapshots."""
    if not storage_adapter:
        return []

    ensure_value_kpi_snapshot_table(storage_adapter)

    conditions = []
    params: Dict[str, Any] = {"limit": int(limit)}
    if source:
        conditions.append("source = {source:String}")
        params["source"] = source

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = f"""
    SELECT
        snapshot_id,
        source,
        time_window,
        window_start,
        window_end,
        mttd_minutes,
        mttr_minutes,
        trace_log_correlation_rate,
        topology_coverage_rate,
        release_regression_pass_rate,
        incident_count,
        release_gate_total,
        release_gate_passed,
        release_gate_failed,
        release_gate_bypassed,
        created_at
    FROM logs.value_kpi_snapshots
    {where_clause}
    ORDER BY created_at DESC
    LIMIT {{limit:UInt32}}
    """
    return storage_adapter.execute_query(query, params)


def normalize_datetime(value: Any, to_datetime_fn: Callable[[Any], datetime]) -> datetime:
    """Normalize datetime to naive UTC."""
    dt = to_datetime_fn(value)
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def estimate_mttd_mttr_minutes(
    storage_adapter: Any,
    time_window: str,
    start_time: Optional[str],
    end_time: Optional[str],
    build_time_filter_clause_fn: Callable[..., Tuple[str, Dict[str, Any]]],
    normalize_datetime_fn: Callable[[Any], datetime],
) -> Dict[str, Any]:
    """Estimate MTTD/MTTR using logs as fallback."""
    if not storage_adapter:
        return {"mttd_minutes": 0.0, "mttr_minutes": 0.0, "incident_count": 0}

    where_clause, params = build_time_filter_clause_fn(
        column_name="timestamp",
        time_window=time_window,
        start_time=start_time,
        end_time=end_time,
        param_prefix="incident",
    )

    query = f"""
    SELECT
        timestamp,
        service_name,
        level_norm AS level,
        message
    FROM logs.logs
    PREWHERE {where_clause}
         AND level_norm IN ('ERROR', 'FATAL', 'WARN', 'INFO')
    ORDER BY timestamp DESC
    LIMIT {{incident_log_limit:Int32}}
    """
    params["incident_log_limit"] = _cap_scan_limit(VALUE_KPI_INCIDENT_LOG_LIMIT)
    rows = storage_adapter.execute_query(query, params)
    if rows:
        # Fetch newest logs first to reduce scan depth, then restore timeline order for incident inference.
        rows = list(reversed(rows))
    by_service: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_service[str(row.get("service_name") or "unknown")].append(row)

    detect_samples: List[float] = []
    recover_samples: List[float] = []
    incident_count = 0
    recovery_keywords = ("resolved", "recovered", "恢复", "已恢复", "rollback complete", "回滚完成")

    for _, service_rows in by_service.items():
        current_incident: Optional[Dict[str, Any]] = None
        for row in service_rows:
            ts = normalize_datetime_fn(row.get("timestamp"))
            level = str(row.get("level") or "").upper()
            message = str(row.get("message") or "").lower()

            is_error = level in {"ERROR", "FATAL"}
            is_warn_or_worse = level in {"WARN", "ERROR", "FATAL"}
            has_recovery_keyword = any(keyword in message for keyword in recovery_keywords)

            if is_error and current_incident is None:
                current_incident = {
                    "opened_at": ts,
                    "detected_at": ts,
                    "last_error_at": ts,
                }
                continue

            if current_incident is None:
                continue

            if is_error:
                current_incident["last_error_at"] = ts
                if current_incident.get("detected_at") is None:
                    current_incident["detected_at"] = ts
                continue

            if is_warn_or_worse and current_incident.get("detected_at") is None:
                current_incident["detected_at"] = ts

            recovered = False
            if has_recovery_keyword:
                recovered = True
            elif level == "INFO":
                last_error_at = current_incident.get("last_error_at", current_incident["opened_at"])
                recovered = (ts - last_error_at).total_seconds() >= 120

            if recovered:
                opened_at = current_incident["opened_at"]
                detected_at = current_incident.get("detected_at") or opened_at
                detect_samples.append(max((detected_at - opened_at).total_seconds() / 60.0, 0.0))
                recover_samples.append(max((ts - opened_at).total_seconds() / 60.0, 0.0))
                incident_count += 1
                current_incident = None

    mttd = sum(detect_samples) / len(detect_samples) if detect_samples else 0.0
    mttr = sum(recover_samples) / len(recover_samples) if recover_samples else 0.0
    return {
        "mttd_minutes": round(mttd, 2),
        "mttr_minutes": round(mttr, 2),
        "incident_count": incident_count,
    }


def _empty_release_gate_summary() -> Dict[str, Any]:
    return {
        "total": 0,
        "passed": 0,
        "failed": 0,
        "bypassed": 0,
        "release_total": 0,
        "release_passed": 0,
        "release_failed": 0,
        "drill_total": 0,
        "trace_smoke_failed": 0,
        "ai_contract_failed": 0,
        "query_contract_failed": 0,
        "pass_rate": 0.0,
        "trace_smoke_pass_rate": 0.0,
        "ai_contract_pass_rate": 0.0,
        "query_contract_pass_rate": 0.0,
        "last_result": None,
    }


def query_release_gate_summary(
    storage_adapter: Any,
    time_window: str,
    start_time: Optional[str],
    end_time: Optional[str],
    build_time_filter_clause_fn: Callable[..., Tuple[str, Dict[str, Any]]],
    safe_ratio_fn: Callable[[float, float], float],
    logger: Any,
) -> Dict[str, Any]:
    """Query release gate summary with legacy schema fallback."""
    if not storage_adapter:
        return _empty_release_gate_summary()

    where_clause, params = build_time_filter_clause_fn(
        column_name="started_at",
        time_window=time_window,
        start_time=start_time,
        end_time=end_time,
        param_prefix="gate",
    )

    try:
        drill_candidate_expr = (
            "positionCaseInsensitiveUTF8(candidate, 'drill') > 0 "
            "OR positionCaseInsensitiveUTF8(candidate, 'failure-check') > 0 "
            "OR positionCaseInsensitiveUTF8(candidate, 'bypass-check') > 0"
        )

        legacy_schema = False
        summary_query = f"""
            SELECT
                count() AS total,
                countIf(status = 'passed') AS passed,
                countIf(status = 'failed') AS failed,
                countIf(status = 'bypassed') AS bypassed,
                countIf(status IN ('passed', 'failed') AND NOT ({drill_candidate_expr})) AS release_total,
                countIf(status = 'passed' AND NOT ({drill_candidate_expr})) AS release_passed,
                countIf(status = 'failed' AND NOT ({drill_candidate_expr})) AS release_failed,
                countIf({drill_candidate_expr}) AS drill_total,
                countIf(status IN ('passed', 'failed') AND NOT ({drill_candidate_expr}) AND ifNull(trace_smoke_exit_code, smoke_exit_code) != 0) AS trace_smoke_failed,
                countIf(status IN ('passed', 'failed') AND NOT ({drill_candidate_expr}) AND ifNull(ai_contract_exit_code, 0) != 0) AS ai_contract_failed,
                countIf(status IN ('passed', 'failed') AND NOT ({drill_candidate_expr}) AND ifNull(query_contract_exit_code, 0) != 0) AS query_contract_failed
            FROM logs.release_gate_reports
            PREWHERE {where_clause}
        """
        try:
            rows = storage_adapter.execute_query(summary_query, params)
        except Exception as summary_exc:
            legacy_schema = True
            logger.warning(f"release gate 汇总查询缺少新列，回退旧口径: {summary_exc}")
            legacy_summary_query = f"""
                SELECT
                    count() AS total,
                    countIf(status = 'passed') AS passed,
                    countIf(status = 'failed') AS failed,
                    countIf(status = 'bypassed') AS bypassed,
                    countIf(status IN ('passed', 'failed') AND NOT ({drill_candidate_expr})) AS release_total,
                    countIf(status = 'passed' AND NOT ({drill_candidate_expr})) AS release_passed,
                    countIf(status = 'failed' AND NOT ({drill_candidate_expr})) AS release_failed,
                    countIf({drill_candidate_expr}) AS drill_total
                FROM logs.release_gate_reports
                PREWHERE {where_clause}
            """
            rows = storage_adapter.execute_query(legacy_summary_query, params)

        summary = rows[0] if rows else {}
        total = int(summary.get("total") or 0)
        passed = int(summary.get("passed") or 0)
        failed = int(summary.get("failed") or 0)
        bypassed = int(summary.get("bypassed") or 0)
        release_total_raw = summary.get("release_total")
        release_passed_raw = summary.get("release_passed")
        release_failed_raw = summary.get("release_failed")

        release_total = int(release_total_raw) if release_total_raw is not None else (passed + failed)
        release_passed = int(release_passed_raw) if release_passed_raw is not None else passed
        if release_failed_raw is not None:
            release_failed = int(release_failed_raw)
        elif release_total_raw is not None:
            release_failed = max(release_total - release_passed, 0)
        else:
            release_failed = failed
        if release_total > 0 and (release_passed + release_failed) > release_total:
            release_failed = max(release_total - release_passed, 0)
        drill_total = int(summary.get("drill_total") or 0)
        trace_smoke_failed = int(summary.get("trace_smoke_failed") or 0)
        ai_contract_failed = int(summary.get("ai_contract_failed") or 0)
        query_contract_failed = int(summary.get("query_contract_failed") or 0)
        if legacy_schema:
            trace_smoke_failed = 0
            ai_contract_failed = 0
            query_contract_failed = 0

        release_non_failed_trace = max(release_total - trace_smoke_failed, 0)
        release_non_failed_ai = max(release_total - ai_contract_failed, 0)
        release_non_failed_query = max(release_total - query_contract_failed, 0)
        pass_rate = safe_ratio_fn(float(release_passed), float(release_total))
        trace_smoke_pass_rate = safe_ratio_fn(float(release_non_failed_trace), float(release_total))
        ai_contract_pass_rate = safe_ratio_fn(float(release_non_failed_ai), float(release_total))
        query_contract_pass_rate = safe_ratio_fn(float(release_non_failed_query), float(release_total))

        if legacy_schema:
            last_query = f"""
                SELECT
                    gate_id,
                    started_at,
                    finished_at,
                    status,
                    candidate,
                    tag,
                    target,
                    trace_id,
                    smoke_exit_code,
                    report_path,
                    summary
                FROM logs.release_gate_reports
                PREWHERE {where_clause}
                ORDER BY started_at DESC
                LIMIT 1
            """
        else:
            last_query = f"""
                SELECT
                    gate_id,
                    started_at,
                    finished_at,
                    status,
                    candidate,
                    tag,
                    target,
                    trace_id,
                    smoke_exit_code,
                    ifNull(trace_smoke_exit_code, smoke_exit_code) AS trace_smoke_exit_code,
                    ifNull(ai_contract_exit_code, 0) AS ai_contract_exit_code,
                    ifNull(query_contract_exit_code, 0) AS query_contract_exit_code,
                    report_path,
                    summary
                FROM logs.release_gate_reports
                PREWHERE {where_clause}
                ORDER BY started_at DESC
                LIMIT 1
            """
        last_rows = storage_adapter.execute_query(last_query, params)
        last_result = last_rows[0] if last_rows else None
        if last_result:
            last_result.setdefault("trace_smoke_exit_code", int(last_result.get("smoke_exit_code") or 0))
            last_result.setdefault("ai_contract_exit_code", 0)
            last_result.setdefault("query_contract_exit_code", 0)

        return {
            "total": total,
            "passed": passed,
            "failed": failed,
            "bypassed": bypassed,
            "release_total": release_total,
            "release_passed": release_passed,
            "release_failed": release_failed,
            "drill_total": drill_total,
            "trace_smoke_failed": trace_smoke_failed,
            "ai_contract_failed": ai_contract_failed,
            "query_contract_failed": query_contract_failed,
            "pass_rate": round(pass_rate, 4),
            "trace_smoke_pass_rate": round(trace_smoke_pass_rate, 4),
            "ai_contract_pass_rate": round(ai_contract_pass_rate, 4),
            "query_contract_pass_rate": round(query_contract_pass_rate, 4),
            "last_result": last_result,
        }
    except Exception as exc:
        logger.warning(f"读取 release gate 报告失败，返回默认值: {exc}")
        return _empty_release_gate_summary()


def compute_inference_coverage_rate(
    storage_adapter: Any,
    time_window: str,
    start_time: Optional[str],
    end_time: Optional[str],
    build_time_filter_clause_fn: Callable[..., Tuple[str, Dict[str, Any]]],
    infer_trace_lite_fragments_fn: Callable[[List[Dict[str, Any]]], Tuple[List[Dict[str, Any]], Dict[str, Any]]],
    safe_ratio_fn: Callable[[float, float], float],
    logger: Any,
) -> float:
    """Compute topology inference coverage rate."""
    if not storage_adapter:
        return 0.0

    where_logs, params_logs = build_time_filter_clause_fn(
        column_name="timestamp",
        time_window=time_window,
        start_time=start_time,
        end_time=end_time,
        param_prefix="infer_cov_logs",
    )
    logs_query = f"""
    SELECT
        timestamp,
        service_name,
        namespace,
        message,
        trace_id,
        attributes_json
    FROM logs.logs
    PREWHERE {where_logs}
    LIMIT {{infer_cov_log_limit:Int32}}
    """
    infer_cov_limit = (
        VALUE_KPI_INFERENCE_LOG_LIMIT
        if _interval_seconds(time_window) <= 24 * 3600
        else VALUE_KPI_INFERENCE_LOG_LIMIT_LONG_WINDOW
    )
    params_logs["infer_cov_log_limit"] = _cap_scan_limit(infer_cov_limit)

    try:
        log_rows = storage_adapter.execute_query(logs_query, params_logs)
        if not log_rows:
            return 0.0

        fragments, _ = infer_trace_lite_fragments_fn(log_rows)

        inferred_services = {
            str(service).strip()
            for fragment in fragments
            for service in (fragment.get("source_service"), fragment.get("target_service"))
            if service and str(service).strip() and str(service).strip().lower() != "unknown"
        }
        observed_services = {
            str(row.get("service_name")).strip()
            for row in log_rows
            if row.get("service_name") and str(row.get("service_name")).strip().lower() != "unknown"
        }
        return safe_ratio_fn(float(len(inferred_services)), float(len(observed_services)))
    except Exception as exc:
        logger.warning(f"计算 inference 覆盖率失败，回退为 0: {exc}")
        return 0.0


def _empty_value_kpi_payload() -> Dict[str, Any]:
    return {
        "metrics": {
            "mttd_minutes": 0.0,
            "mttr_minutes": 0.0,
            "trace_log_correlation_rate": 0.0,
            "topology_coverage_rate": 0.0,
            "release_regression_pass_rate": 0.0,
        },
        "release_gate_summary": {
            "total": 0,
            "passed": 0,
            "failed": 0,
            "bypassed": 0,
            "release_total": 0,
            "release_passed": 0,
            "release_failed": 0,
            "drill_total": 0,
            "pass_rate": 0.0,
            "trace_smoke_failed": 0,
            "ai_contract_failed": 0,
            "query_contract_failed": 0,
            "trace_smoke_pass_rate": 0.0,
            "ai_contract_pass_rate": 0.0,
            "query_contract_pass_rate": 0.0,
            "last_result": None,
        },
        "incident_summary": {
            "incident_count": 0,
        },
    }


def compute_value_kpis(
    storage_adapter: Any,
    time_window: str,
    start_time: Optional[str],
    end_time: Optional[str],
    use_cache: bool,
    sanitize_interval_fn: Callable[[str, str], str],
    build_cache_key_fn: Callable[..., str],
    get_cached_fn: Callable[[str], Optional[Dict[str, Any]]],
    set_cached_fn: Callable[[str, Dict[str, Any]], None],
    build_time_filter_clause_fn: Callable[..., Tuple[str, Dict[str, Any]]],
    safe_ratio_fn: Callable[[float, float], float],
    compute_inference_coverage_rate_fn: Callable[[str, Optional[str], Optional[str]], float],
    estimate_mttd_mttr_minutes_fn: Callable[[str, Optional[str], Optional[str]], Dict[str, Any]],
    query_release_gate_summary_fn: Callable[[str, Optional[str], Optional[str]], Dict[str, Any]],
) -> Dict[str, Any]:
    """Compute value KPI payload with cache and fallbacks."""
    if not storage_adapter:
        return _empty_value_kpi_payload()

    safe_window = sanitize_interval_fn(time_window, "7 DAY")
    cache_key = build_cache_key_fn(
        time_window=safe_window,
        start_time=start_time,
        end_time=end_time,
    )
    if use_cache:
        cached_value = get_cached_fn(cache_key)
        if cached_value is not None:
            return cached_value

    where_logs, params_logs = build_time_filter_clause_fn(
        column_name="timestamp",
        time_window=safe_window,
        start_time=start_time,
        end_time=end_time,
        param_prefix="logs",
    )
    use_exact_correlation = bool(start_time and end_time) or _interval_seconds(safe_window) <= 24 * 3600
    correlation_params = dict(params_logs)
    correlated_expr = (
        "notEmpty(trace_id) "
        "AND notEmpty(span_id) "
        "AND trace_id_source != 'synthetic'"
    )
    if use_exact_correlation:
        correlation_query = f"""
        SELECT
            count() AS total_logs,
            countIf({correlated_expr}) AS correlated_logs
        FROM logs.logs
        PREWHERE {where_logs}
        """
    else:
        correlation_params["correlation_sample_limit"] = _cap_scan_limit(VALUE_KPI_CORRELATION_SAMPLE_LIMIT)
        correlation_query = f"""
        SELECT
            count() AS total_logs,
            countIf({correlated_expr}) AS correlated_logs
        FROM (
            SELECT trace_id, span_id, trace_id_source
            FROM logs.logs
            PREWHERE {where_logs}
            LIMIT {{correlation_sample_limit:Int32}}
        )
        """
    correlation_rows = storage_adapter.execute_query(correlation_query, correlation_params)
    total_logs = float((correlation_rows[0].get("total_logs") if correlation_rows else 0) or 0.0)
    correlated_logs = float((correlation_rows[0].get("correlated_logs") if correlation_rows else 0) or 0.0)
    trace_log_correlation_rate = safe_ratio_fn(correlated_logs, total_logs)

    where_traces, params_traces = build_time_filter_clause_fn(
        column_name="timestamp",
        time_window=safe_window,
        start_time=start_time,
        end_time=end_time,
        param_prefix="traces",
    )

    where_logs_stats, params_logs_stats = build_time_filter_clause_fn(
        column_name="ts_minute",
        time_window=safe_window,
        start_time=start_time,
        end_time=end_time,
        param_prefix="logs_stats",
    )
    services_logs_query_preagg_v2 = f"""
    SELECT uniqCombined64(service_name) AS total_services
    FROM logs.obs_counts_1m
    PREWHERE {where_logs_stats}
      AND signal = 'log'
      AND dim_name = 'level'
    WHERE length(trim(service_name)) > 0
    """
    services_logs_query_preagg_legacy = f"""
    SELECT uniqCombined64(service_name) AS total_services
    FROM logs.logs_stats_1m
    PREWHERE {where_logs_stats}
    WHERE length(trim(service_name)) > 0
    """
    services_logs_query_fallback = f"""
    SELECT uniqCombined64(service_name) AS total_services
    FROM (
        SELECT service_name
        FROM logs.logs
        PREWHERE {where_logs}
        WHERE length(trim(service_name)) > 0
        LIMIT {{service_count_sample_limit:Int32}}
    )
    """
    services_traces_query = f"""
    SELECT uniqCombined64(service_name) AS traced_services
    FROM (
        SELECT service_name
        FROM logs.traces
        PREWHERE {where_traces}
        WHERE length(trim(service_name)) > 0
        LIMIT {{service_count_sample_limit:Int32}}
    )
    """
    params_traces_with_limit = dict(params_traces)
    params_traces_with_limit["service_count_sample_limit"] = _cap_scan_limit(VALUE_KPI_SERVICE_COUNT_SAMPLE_LIMIT)
    use_v2_preagg = _should_try_v2_preagg() and _has_table(storage_adapter, "logs", "obs_counts_1m")
    use_legacy_preagg = _should_try_legacy_preagg() and _has_table(storage_adapter, "logs", "logs_stats_1m")
    if use_v2_preagg:
        services_logs_rows = storage_adapter.execute_query(services_logs_query_preagg_v2, params_logs_stats)
    elif use_legacy_preagg:
        services_logs_rows = storage_adapter.execute_query(services_logs_query_preagg_legacy, params_logs_stats)
    else:
        params_logs_with_limit = dict(params_logs)
        params_logs_with_limit["service_count_sample_limit"] = _cap_scan_limit(VALUE_KPI_SERVICE_COUNT_SAMPLE_LIMIT)
        services_logs_rows = storage_adapter.execute_query(services_logs_query_fallback, params_logs_with_limit)
    services_traces_rows = storage_adapter.execute_query(services_traces_query, params_traces_with_limit)
    total_services = float((services_logs_rows[0].get("total_services") if services_logs_rows else 0) or 0.0)
    traced_services = float((services_traces_rows[0].get("traced_services") if services_traces_rows else 0) or 0.0)
    topology_trace_coverage_rate = safe_ratio_fn(traced_services, total_services)
    topology_inference_coverage_rate = 0.0
    if topology_trace_coverage_rate < 0.50:
        topology_inference_coverage_rate = compute_inference_coverage_rate_fn(safe_window, start_time, end_time)
    topology_coverage_rate = max(topology_trace_coverage_rate, topology_inference_coverage_rate)

    incident_summary = estimate_mttd_mttr_minutes_fn(safe_window, start_time, end_time)
    release_gate_summary = query_release_gate_summary_fn(safe_window, start_time, end_time)

    result = {
        "metrics": {
            "mttd_minutes": incident_summary["mttd_minutes"],
            "mttr_minutes": incident_summary["mttr_minutes"],
            "trace_log_correlation_rate": round(trace_log_correlation_rate, 4),
            "topology_coverage_rate": round(topology_coverage_rate, 4),
            "topology_coverage_trace_rate": round(topology_trace_coverage_rate, 4),
            "topology_coverage_inference_rate": round(topology_inference_coverage_rate, 4),
            "release_regression_pass_rate": round(release_gate_summary["pass_rate"], 4),
        },
        "release_gate_summary": release_gate_summary,
        "incident_summary": {
            "incident_count": incident_summary["incident_count"],
        },
    }
    if use_cache:
        set_cached_fn(cache_key, result)
    return result


def build_value_kpi_weekly_csv(
    weeks: int,
    compute_value_kpis_fn: Callable[[datetime, datetime], Dict[str, Any]],
    now_utc: Optional[datetime] = None,
) -> Tuple[str, str]:
    """Build weekly KPI CSV content and filename."""
    run_time = now_utc or datetime.now(timezone.utc)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "week_index",
            "week_start_utc",
            "week_end_utc",
            "mttd_minutes",
            "mttr_minutes",
            "trace_log_correlation_rate",
            "topology_coverage_rate",
            "release_regression_pass_rate",
            "release_gate_total",
            "release_gate_passed",
            "release_gate_failed",
            "incident_count",
        ]
    )

    for idx in range(weeks):
        end_dt = run_time - timedelta(days=idx * 7)
        start_dt = end_dt - timedelta(days=7)
        computed = compute_value_kpis_fn(start_dt, end_dt)
        metrics = computed["metrics"]
        gate = computed["release_gate_summary"]
        incidents = computed["incident_summary"]
        writer.writerow(
            [
                idx + 1,
                start_dt.isoformat(),
                end_dt.isoformat(),
                metrics.get("mttd_minutes", 0.0),
                metrics.get("mttr_minutes", 0.0),
                metrics.get("trace_log_correlation_rate", 0.0),
                metrics.get("topology_coverage_rate", 0.0),
                metrics.get("release_regression_pass_rate", 0.0),
                gate.get("total", 0),
                gate.get("passed", 0),
                gate.get("failed", 0),
                incidents.get("incident_count", 0),
            ]
        )

    csv_text = output.getvalue()
    output.close()
    filename = f"value-kpi-weekly-{run_time.strftime('%Y%m%d-%H%M%S')}.csv"
    return csv_text, filename
