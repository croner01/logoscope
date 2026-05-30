-- Release 3: performance + incremental rollup package
-- Goals:
-- 1) Keep trace_edges_1m continuously updated (minute-level rollup)
-- 2) Switch read path to aggregated edge table (fallback kept in code)
-- 3) Converge baseline DDL for substring search and context projection

CREATE DATABASE IF NOT EXISTS logs;

-- 1) logs substring search / context projection baseline.
ALTER TABLE logs.logs
ADD INDEX IF NOT EXISTS idx_logs_message_ngram message TYPE ngrambf_v1(3, 65536, 3, 0) GRANULARITY 1;

ALTER TABLE logs.logs
ADD PROJECTION IF NOT EXISTS proj_logs_pod_ns_time
(
    SELECT
        id, timestamp, service_name, level_norm, pod_name, namespace, trace_id, span_id
    ORDER BY (pod_name, namespace, timestamp, id)
);

-- 2) trace edge aggregate table (compatible with existing release-2 table).
CREATE TABLE IF NOT EXISTS logs.trace_edges_1m (
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
SETTINGS index_granularity = 8192, ttl_only_drop_parts = 1;

ALTER TABLE logs.trace_edges_1m
    ADD COLUMN IF NOT EXISTS namespace LowCardinality(String) DEFAULT '',
    ADD COLUMN IF NOT EXISTS timeout_count UInt64 DEFAULT 0,
    ADD COLUMN IF NOT EXISTS retries_sum Float64 DEFAULT 0,
    ADD COLUMN IF NOT EXISTS pending_sum Float64 DEFAULT 0,
    ADD COLUMN IF NOT EXISTS dlq_sum Float64 DEFAULT 0,
    ADD COLUMN IF NOT EXISTS p95_ms Float64 DEFAULT 0,
    ADD COLUMN IF NOT EXISTS p99_ms Float64 DEFAULT 0,
    ADD COLUMN IF NOT EXISTS duration_sum_ms Float64 DEFAULT 0;

-- 3) watermark table for incremental rollup script.
CREATE TABLE IF NOT EXISTS logs.trace_edges_rollup_watermark (
    id UInt8,
    window_end DateTime('UTC'),
    updated_at DateTime('UTC') DEFAULT now()
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (id)
SETTINGS index_granularity = 256;

INSERT INTO logs.trace_edges_rollup_watermark (id, window_end, updated_at)
SELECT
    1 AS id,
    ifNull(
        (SELECT max(ts_minute) FROM logs.trace_edges_1m),
        toDateTime(toStartOfMinute(now()) - INTERVAL 10 MINUTE, 'UTC')
    ) AS window_end,
    now() AS updated_at
WHERE (SELECT count() FROM logs.trace_edges_rollup_watermark) = 0;
