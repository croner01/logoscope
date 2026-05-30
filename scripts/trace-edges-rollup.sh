#!/usr/bin/env bash
set -euo pipefail

MODE="${MODE:-kubectl}"                       # local | kubectl | docker
DATABASE="${DATABASE:-logs}"
NAMESPACE="${NAMESPACE:-islap}"
CLICKHOUSE_POD="${CLICKHOUSE_POD:-}"
DOCKER_CONTAINER="${DOCKER_CONTAINER:-clickhouse}"
ROLLUP_LAG_MINUTES="${ROLLUP_LAG_MINUTES:-2}"            # keep a small lag for late spans
ROLLUP_MAX_BATCH_MINUTES="${ROLLUP_MAX_BATCH_MINUTES:-20}"

log() {
  echo "[trace-edges-rollup] $*"
}

die() {
  echo "[trace-edges-rollup] ERROR: $*" >&2
  exit 1
}

run_query_local() {
  local sql="$1"
  clickhouse-client --database "${DATABASE}" -q "${sql}"
}

run_query_kubectl() {
  local sql="$1"
  kubectl -n "${NAMESPACE}" exec "${CLICKHOUSE_POD}" -- clickhouse-client --database "${DATABASE}" -q "${sql}"
}

run_query_docker() {
  local sql="$1"
  docker exec "${DOCKER_CONTAINER}" clickhouse-client --database "${DATABASE}" -q "${sql}"
}

run_query() {
  local sql="$1"
  case "${MODE}" in
    local) run_query_local "${sql}" ;;
    kubectl) run_query_kubectl "${sql}" ;;
    docker) run_query_docker "${sql}" ;;
    *) die "unsupported MODE=${MODE}, expected local|kubectl|docker" ;;
  esac
}

resolve_clickhouse_pod() {
  if [[ "${MODE}" != "kubectl" ]]; then
    return 0
  fi
  if [[ -n "${CLICKHOUSE_POD}" ]]; then
    return 0
  fi
  CLICKHOUSE_POD="$(
    kubectl -n "${NAMESPACE}" get pods -l app=clickhouse --no-headers 2>/dev/null \
      | awk '$3=="Running"{print $1; exit}'
  )"
  [[ -n "${CLICKHOUSE_POD}" ]] || die "cannot auto-detect clickhouse pod in namespace ${NAMESPACE}"
}

bootstrap_schema() {
  run_query "
  CREATE TABLE IF NOT EXISTS ${DATABASE}.trace_edges_1m (
      ts_minute DateTime('UTC'),
      source_service LowCardinality(String),
      target_service LowCardinality(String),
      namespace LowCardinality(String) DEFAULT '',
      call_count UInt64,
      error_count UInt64,
      avg_duration_ms Float64,
      timeout_count UInt64 DEFAULT 0,
      retries_sum Float64 DEFAULT 0,
      pending_sum Float64 DEFAULT 0,
      dlq_sum Float64 DEFAULT 0,
      p95_ms Float64 DEFAULT 0,
      p99_ms Float64 DEFAULT 0,
      duration_sum_ms Float64 DEFAULT 0
  )
  ENGINE = SummingMergeTree()
  PARTITION BY toDate(ts_minute)
  ORDER BY (ts_minute, source_service, target_service)
  TTL ts_minute + INTERVAL 30 DAY DELETE
  SETTINGS index_granularity = 8192, ttl_only_drop_parts = 1
  "

  run_query "
  ALTER TABLE ${DATABASE}.trace_edges_1m
      ADD COLUMN IF NOT EXISTS namespace LowCardinality(String) DEFAULT '',
      ADD COLUMN IF NOT EXISTS timeout_count UInt64 DEFAULT 0,
      ADD COLUMN IF NOT EXISTS retries_sum Float64 DEFAULT 0,
      ADD COLUMN IF NOT EXISTS pending_sum Float64 DEFAULT 0,
      ADD COLUMN IF NOT EXISTS dlq_sum Float64 DEFAULT 0,
      ADD COLUMN IF NOT EXISTS p95_ms Float64 DEFAULT 0,
      ADD COLUMN IF NOT EXISTS p99_ms Float64 DEFAULT 0,
      ADD COLUMN IF NOT EXISTS duration_sum_ms Float64 DEFAULT 0
  "

  run_query "
  CREATE TABLE IF NOT EXISTS ${DATABASE}.trace_edges_rollup_watermark (
      id UInt8,
      window_end DateTime('UTC'),
      updated_at DateTime('UTC') DEFAULT now()
  )
  ENGINE = ReplacingMergeTree(updated_at)
  ORDER BY (id)
  SETTINGS index_granularity = 256
  "

  run_query "
  INSERT INTO ${DATABASE}.trace_edges_rollup_watermark (id, window_end, updated_at)
  SELECT
      1 AS id,
      ifNull(
          (SELECT max(ts_minute) FROM ${DATABASE}.trace_edges_1m),
          toDateTime(toStartOfMinute(now()) - INTERVAL 10 MINUTE, 'UTC')
      ) AS window_end,
      now() AS updated_at
  WHERE (SELECT count() FROM ${DATABASE}.trace_edges_rollup_watermark) = 0
  "
}

main() {
  resolve_clickhouse_pod
  bootstrap_schema

  local window_line start_ts hard_end_ts batch_end_ts
  window_line="$(
    run_query "
    SELECT
        formatDateTime(max(window_end), '%F %T') AS start_ts,
        formatDateTime(toStartOfMinute(now()) - INTERVAL ${ROLLUP_LAG_MINUTES} MINUTE, '%F %T') AS hard_end_ts
    FROM ${DATABASE}.trace_edges_rollup_watermark FINAL
    FORMAT TSV
    " | tr -d '\r'
  )"
  IFS=$'\t' read -r start_ts hard_end_ts <<< "${window_line}"

  [[ -n "${start_ts:-}" ]] || die "cannot resolve start_ts from watermark table"
  [[ -n "${hard_end_ts:-}" ]] || die "cannot resolve hard_end_ts from current clock"

  if [[ "${start_ts}" == "${hard_end_ts}" || "${start_ts}" > "${hard_end_ts}" ]]; then
    log "no new minute window to roll up (start=${start_ts}, hard_end=${hard_end_ts})"
    return 0
  fi

  batch_end_ts="$(
    run_query "
    SELECT formatDateTime(
        least(
            toDateTime('${hard_end_ts}', 'UTC'),
            addMinutes(toDateTime('${start_ts}', 'UTC'), ${ROLLUP_MAX_BATCH_MINUTES})
        ),
        '%F %T'
    )
    FORMAT TSV
    " | tr -d '\r\n'
  )"
  [[ -n "${batch_end_ts:-}" ]] || die "cannot compute batch_end_ts"

  log "rolling up edge spans: [${start_ts}, ${batch_end_ts})"

  run_query "
  INSERT INTO ${DATABASE}.trace_edges_1m
  SELECT
      toStartOfMinute(child.timestamp) AS ts_minute,
      parent.service_name AS source_service,
      child.service_name AS target_service,
      multiIf(
          length(JSONExtractString(child.attributes_json, 'k8s.namespace.name')) > 0,
          JSONExtractString(child.attributes_json, 'k8s.namespace.name'),
          length(JSONExtractString(child.attributes_json, 'service_namespace')) > 0,
          JSONExtractString(child.attributes_json, 'service_namespace'),
          JSONExtractString(child.attributes_json, 'namespace')
      ) AS namespace,
      count() AS call_count,
      countIf(lower(toString(child.status)) IN ('error', 'failed', 'status_code_error', '2')) AS error_count,
      avg(span_duration_ms) AS avg_duration_ms,
      countIf(span_duration_ms >= 1000) AS timeout_count,
      sum(retries_value) AS retries_sum,
      sum(pending_value) AS pending_sum,
      sum(dlq_value) AS dlq_sum,
      quantileTDigest(0.95)(span_duration_ms) AS p95_ms,
      quantileTDigest(0.99)(span_duration_ms) AS p99_ms,
      sum(span_duration_ms) AS duration_sum_ms
  FROM (
      SELECT
          child.timestamp,
          child.trace_id,
          child.parent_span_id,
          child.service_name,
          child.status,
          child.attributes_json,
          greatest(
              toFloat64OrZero(toString(child.duration_ms)),
              toFloat64OrZero(JSONExtractString(child.attributes_json, 'duration_ms')),
              toFloat64OrZero(JSONExtractString(child.attributes_json, 'duration')),
              toFloat64OrZero(JSONExtractString(child.attributes_json, 'elapsed_ms'))
          ) AS span_duration_ms,
          greatest(
              toFloat64OrZero(JSONExtractString(child.attributes_json, 'retry_count')),
              toFloat64OrZero(JSONExtractString(child.attributes_json, 'retries'))
          ) AS retries_value,
          greatest(
              toFloat64OrZero(JSONExtractString(child.attributes_json, 'pending')),
              toFloat64OrZero(JSONExtractString(child.attributes_json, 'pending_count'))
          ) AS pending_value,
          greatest(
              toFloat64OrZero(JSONExtractString(child.attributes_json, 'dlq')),
              toFloat64OrZero(JSONExtractString(child.attributes_json, 'dlq_count'))
          ) AS dlq_value
      FROM ${DATABASE}.traces AS child
      WHERE child.timestamp >= toDateTime64('${start_ts}', 9, 'UTC')
        AND child.timestamp < toDateTime64('${batch_end_ts}', 9, 'UTC')
        AND notEmpty(child.trace_id)
        AND notEmpty(child.parent_span_id)
        AND notEmpty(child.service_name)
  ) AS child
  INNER JOIN ${DATABASE}.traces AS parent
      ON child.trace_id = parent.trace_id
     AND child.parent_span_id = parent.span_id
  WHERE notEmpty(parent.service_name)
    AND child.service_name != parent.service_name
  GROUP BY ts_minute, source_service, target_service, namespace
  "

  run_query "
  INSERT INTO ${DATABASE}.trace_edges_rollup_watermark (id, window_end, updated_at)
  VALUES (1, toDateTime('${batch_end_ts}', 'UTC'), now())
  "

  log "rollup done, watermark advanced to ${batch_end_ts}"
}

main "$@"
