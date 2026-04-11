#!/usr/bin/env bash
set -euo pipefail

# Regression guard: verify log event timestamp stays aligned with message-local CST clock.
#
# Exit codes:
#   0 = healthy
#   1 = regression detected
#   2 = insufficient samples / missing prerequisites
#
# Example:
#   NS=islap LOG_NAMESPACE=openstack WINDOW_MINUTES=10 scripts/check-log-tz-regression.sh

NS="${NS:-islap}"
LOG_NAMESPACE="${LOG_NAMESPACE:-openstack}"
WINDOW_MINUTES="${WINDOW_MINUTES:-10}"
SAMPLE_LIMIT="${SAMPLE_LIMIT:-3000}"
MAX_DRIFT_SECONDS="${MAX_DRIFT_SECONDS:-5}"
MAX_BAD_RATIO_PCT="${MAX_BAD_RATIO_PCT:-1}"
MIN_PARSED_ROWS="${MIN_PARSED_ROWS:-20}"
CLICKHOUSE_POD="${CLICKHOUSE_POD:-}"

if ! command -v kubectl >/dev/null 2>&1; then
  echo "[tz-regression] kubectl not found" >&2
  exit 2
fi

if [[ -z "${CLICKHOUSE_POD}" ]]; then
  CLICKHOUSE_POD="$(kubectl -n "${NS}" get pods -o name | grep -m1 'clickhouse' | cut -d/ -f2 || true)"
fi

if [[ -z "${CLICKHOUSE_POD}" ]]; then
  echo "[tz-regression] cannot find clickhouse pod in namespace=${NS}" >&2
  exit 2
fi

max_drift_ms="$((MAX_DRIFT_SECONDS * 1000))"

read -r parsed_rows unparsed_rows bad_rows bad_ratio_pct max_abs_diff_seconds <<EOF
$(kubectl -n "${NS}" exec "${CLICKHOUSE_POD}" -- clickhouse-client -q "
WITH src AS (
    SELECT
        timestamp,
        message,
        extract(message, '^(\\\\d{4}-\\\\d{2}-\\\\d{2} \\\\d{2}:\\\\d{2}:\\\\d{2}\\\\.\\\\d{3})') AS msg_ts_cst_str,
        parseDateTime64BestEffortOrNull(msg_ts_cst_str, 3, 'Asia/Shanghai') AS msg_ts_cst_dt
    FROM logs.logs
    WHERE timestamp > now() - INTERVAL ${WINDOW_MINUTES} MINUTE
      AND namespace = '${LOG_NAMESPACE}'
    ORDER BY timestamp DESC
    LIMIT ${SAMPLE_LIMIT}
)
SELECT
    countIf(isNotNull(msg_ts_cst_dt)) AS parsed_rows,
    countIf(isNull(msg_ts_cst_dt)) AS unparsed_rows,
    countIf(
        isNotNull(msg_ts_cst_dt)
        AND abs(
            toUnixTimestamp64Milli(timestamp)
            - toUnixTimestamp64Milli(toTimeZone(msg_ts_cst_dt, 'UTC'))
        ) > ${max_drift_ms}
    ) AS bad_rows,
    if(parsed_rows = 0, 0.0, round(bad_rows * 100.0 / parsed_rows, 3)) AS bad_ratio_pct,
    if(
        parsed_rows = 0,
        0.0,
        round(
            maxIf(
                abs(
                    toUnixTimestamp64Milli(timestamp)
                    - toUnixTimestamp64Milli(toTimeZone(msg_ts_cst_dt, 'UTC'))
                ) / 1000.0,
                isNotNull(msg_ts_cst_dt)
            ),
            3
        )
    ) AS max_abs_diff_seconds
FROM src
FORMAT TSVRaw
")
EOF

echo "[tz-regression] ns=${NS} log_namespace=${LOG_NAMESPACE} window_min=${WINDOW_MINUTES} sample_limit=${SAMPLE_LIMIT}"
echo "[tz-regression] parsed_rows=${parsed_rows} unparsed_rows=${unparsed_rows} bad_rows=${bad_rows} bad_ratio_pct=${bad_ratio_pct} max_abs_diff_seconds=${max_abs_diff_seconds}"

if [[ "${parsed_rows}" -lt "${MIN_PARSED_ROWS}" ]]; then
  echo "[tz-regression] insufficient parsed samples (<${MIN_PARSED_ROWS}), skip strict judgement" >&2
  exit 2
fi

regression=0

if [[ "${bad_rows}" -gt 0 ]]; then
  regression=1
fi

if awk "BEGIN {exit !(${bad_ratio_pct} > ${MAX_BAD_RATIO_PCT})}"; then
  regression=1
fi

if [[ "${regression}" -eq 1 ]]; then
  echo "[tz-regression] regression detected, showing top offenders..."
  kubectl -n "${NS}" exec "${CLICKHOUSE_POD}" -- clickhouse-client -q "
WITH src AS (
    SELECT
        id,
        timestamp,
        message,
        extract(message, '^(\\\\d{4}-\\\\d{2}-\\\\d{2} \\\\d{2}:\\\\d{2}:\\\\d{2}\\\\.\\\\d{3})') AS msg_ts_cst_str,
        parseDateTime64BestEffortOrNull(msg_ts_cst_str, 3, 'Asia/Shanghai') AS msg_ts_cst_dt
    FROM logs.logs
    WHERE timestamp > now() - INTERVAL ${WINDOW_MINUTES} MINUTE
      AND namespace = '${LOG_NAMESPACE}'
    ORDER BY timestamp DESC
    LIMIT ${SAMPLE_LIMIT}
)
SELECT
    id,
    timestamp AS db_ts_utc,
    msg_ts_cst_str,
    toTimeZone(msg_ts_cst_dt, 'UTC') AS msg_ts_utc,
    round(
      (
        toUnixTimestamp64Milli(timestamp)
        - toUnixTimestamp64Milli(toTimeZone(msg_ts_cst_dt, 'UTC'))
      ) / 3600000.0,
      3
    ) AS diff_hours,
    substring(message, 1, 180) AS message_head
FROM src
WHERE isNotNull(msg_ts_cst_dt)
  AND abs(
      toUnixTimestamp64Milli(timestamp)
      - toUnixTimestamp64Milli(toTimeZone(msg_ts_cst_dt, 'UTC'))
  ) > ${max_drift_ms}
ORDER BY timestamp DESC
LIMIT 5
FORMAT Vertical
"
  exit 1
fi

echo "[tz-regression] healthy"
exit 0
