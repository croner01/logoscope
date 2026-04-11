-- Observability pre-aggregation v2 (standardized draft)
-- Date: 2026-03-02
-- Scope:
-- 1) Keep compatibility with current query-service API fields
-- 2) Replace old 3-table 1m pre-agg model with 2 rollup tables
-- 3) Use logs.xxx namespace only
-- 4) Do not include cleanup for old logs.mv_*_1m objects

-- -----------------------------------------------------------------------------
-- 0) Ensure database exists
-- -----------------------------------------------------------------------------
CREATE DATABASE IF NOT EXISTS logs;

-- -----------------------------------------------------------------------------
-- 1) Rollup table for logs + metrics counts (1-minute bucket)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS logs.obs_counts_1m
(
    ts_minute DateTime('UTC'),
    signal Enum8('log' = 1, 'metric' = 2),
    service_name LowCardinality(String),
    dim_name LowCardinality(String),  -- log: level, metric: metric_name
    dim_value String,
    count UInt64,
    error_count UInt64
)
ENGINE = SummingMergeTree()
PARTITION BY toDate(ts_minute)
ORDER BY (signal, ts_minute, service_name, dim_name, dim_value)
TTL ts_minute + INTERVAL 30 DAY DELETE
SETTINGS index_granularity = 8192, ttl_only_drop_parts = 1;

-- -----------------------------------------------------------------------------
-- 2) Rollup table for trace stats (1-minute bucket)
--    AggregatingMergeTree is used to keep exact API-compatible fields:
--    span_count / error_span_count / trace_count / error_trace_count
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS logs.obs_traces_1m
(
    ts_minute DateTime('UTC'),
    service_name LowCardinality(String),
    operation_name String,
    span_count_state AggregateFunction(sum, UInt64),
    error_span_count_state AggregateFunction(sum, UInt64),
    trace_id_state AggregateFunction(uniqCombined64, String),
    error_trace_id_state AggregateFunction(uniqCombined64, String)
)
ENGINE = AggregatingMergeTree()
PARTITION BY toDate(ts_minute)
ORDER BY (ts_minute, service_name, operation_name)
TTL ts_minute + INTERVAL 30 DAY DELETE
SETTINGS index_granularity = 8192, ttl_only_drop_parts = 1;

-- -----------------------------------------------------------------------------
-- 3) Materialized views (new writes)
--
-- NOTE:
-- - Use one cutover point to avoid overlap between backfill and MV real-time flow.
-- - Replace 2026-03-02 00:00:00 with your real cutover timestamp.
-- -----------------------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS logs.mv_obs_counts_1m_from_logs
TO logs.obs_counts_1m
AS
SELECT
    toStartOfMinute(timestamp) AS ts_minute,
    CAST('log', 'Enum8(\'log\' = 1, \'metric\' = 2)') AS signal,
    service_name,
    'level' AS dim_name,
    level AS dim_value,
    count() AS count,
    countIf(lowerUTF8(level) IN ('error', 'fatal')) AS error_count
FROM logs.logs
WHERE timestamp >= toDateTime64('2026-03-02 00:00:00', 9, 'UTC')
GROUP BY ts_minute, service_name, dim_value;

CREATE MATERIALIZED VIEW IF NOT EXISTS logs.mv_obs_counts_1m_from_metrics
TO logs.obs_counts_1m
AS
SELECT
    toStartOfMinute(timestamp) AS ts_minute,
    CAST('metric', 'Enum8(\'log\' = 1, \'metric\' = 2)') AS signal,
    service_name,
    'metric_name' AS dim_name,
    metric_name AS dim_value,
    count() AS count,
    toUInt64(0) AS error_count
FROM logs.metrics
WHERE timestamp >= toDateTime64('2026-03-02 00:00:00', 9, 'UTC')
GROUP BY ts_minute, service_name, dim_value;

CREATE MATERIALIZED VIEW IF NOT EXISTS logs.mv_obs_traces_1m_from_traces
TO logs.obs_traces_1m
AS
SELECT
    toStartOfMinute(timestamp) AS ts_minute,
    service_name,
    operation_name,
    sumState(toUInt64(1)) AS span_count_state,
    sumState(toUInt64(
        if(toString(status) IN ('2', 'STATUS_CODE_ERROR', 'ERROR'), 1, 0)
    )) AS error_span_count_state,
    uniqCombined64State(trace_id) AS trace_id_state,
    uniqCombined64StateIf(
        trace_id,
        toString(status) IN ('2', 'STATUS_CODE_ERROR', 'ERROR')
    ) AS error_trace_id_state
FROM logs.traces
WHERE timestamp >= toDateTime64('2026-03-02 00:00:00', 9, 'UTC')
GROUP BY ts_minute, service_name, operation_name;

-- -----------------------------------------------------------------------------
-- 4) One-time backfill (historical data before cutover)
--
-- NOTE:
-- - Run once.
-- - If rerun is required, clear only new v2 tables (obs_counts_1m/obs_traces_1m)
--   before rerun to avoid duplicate aggregation.
-- -----------------------------------------------------------------------------
INSERT INTO logs.obs_counts_1m
SELECT
    toStartOfMinute(timestamp) AS ts_minute,
    CAST('log', 'Enum8(\'log\' = 1, \'metric\' = 2)') AS signal,
    service_name,
    'level' AS dim_name,
    level AS dim_value,
    count() AS count,
    countIf(lowerUTF8(level) IN ('error', 'fatal')) AS error_count
FROM logs.logs
WHERE timestamp < toDateTime64('2026-03-02 00:00:00', 9, 'UTC')
GROUP BY ts_minute, service_name, dim_value;

INSERT INTO logs.obs_counts_1m
SELECT
    toStartOfMinute(timestamp) AS ts_minute,
    CAST('metric', 'Enum8(\'log\' = 1, \'metric\' = 2)') AS signal,
    service_name,
    'metric_name' AS dim_name,
    metric_name AS dim_value,
    count() AS count,
    toUInt64(0) AS error_count
FROM logs.metrics
WHERE timestamp < toDateTime64('2026-03-02 00:00:00', 9, 'UTC')
GROUP BY ts_minute, service_name, dim_value;

INSERT INTO logs.obs_traces_1m
SELECT
    toStartOfMinute(timestamp) AS ts_minute,
    service_name,
    operation_name,
    sumState(toUInt64(1)) AS span_count_state,
    sumState(toUInt64(
        if(toString(status) IN ('2', 'STATUS_CODE_ERROR', 'ERROR'), 1, 0)
    )) AS error_span_count_state,
    uniqCombined64State(trace_id) AS trace_id_state,
    uniqCombined64StateIf(
        trace_id,
        toString(status) IN ('2', 'STATUS_CODE_ERROR', 'ERROR')
    ) AS error_trace_id_state
FROM logs.traces
WHERE timestamp < toDateTime64('2026-03-02 00:00:00', 9, 'UTC')
GROUP BY ts_minute, service_name, operation_name;

-- -----------------------------------------------------------------------------
-- 5) Compatibility query examples (for API field mapping)
-- -----------------------------------------------------------------------------
-- Logs total:
-- SELECT sum(count) AS total
-- FROM logs.obs_counts_1m
-- WHERE signal = 'log'
--   AND ts_minute BETWEEN toStartOfMinute(now() - INTERVAL 1 HOUR) AND toStartOfMinute(now());

-- Metrics total:
-- SELECT sum(count) AS total
-- FROM logs.obs_counts_1m
-- WHERE signal = 'metric'
--   AND ts_minute BETWEEN toStartOfMinute(now() - INTERVAL 1 HOUR) AND toStartOfMinute(now());

-- Traces total (API-compatible):
-- SELECT
--   sumMerge(span_count_state) AS span_count,
--   sumMerge(error_span_count_state) AS error_span_count,
--   uniqCombined64Merge(trace_id_state) AS trace_count,
--   uniqCombined64Merge(error_trace_id_state) AS error_trace_count
-- FROM logs.obs_traces_1m
-- WHERE ts_minute BETWEEN toStartOfMinute(now() - INTERVAL 1 HOUR) AND toStartOfMinute(now());
