-- Release 2: structural optimization package
-- Goals:
-- 1) Add trace edge aggregation table for online topology/inference reads
-- 2) Add latest-state views to replace FINAL-heavy hot paths
-- 3) Add partitioned v2 tables for management datasets

CREATE DATABASE IF NOT EXISTS logs;

-- 1) Trace edge aggregate table.
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

-- Optional backfill (last 7 days). Execute in low-traffic window.
INSERT INTO logs.trace_edges_1m
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
    countIf(lowerUTF8(child.status) IN ('error', 'failed', 'fail', 'status_code_error', '2')) AS error_count,
    round(avg(toFloat64OrZero(toString(child.duration_ms))), 2) AS avg_duration_ms,
    countIf(toFloat64OrZero(toString(child.duration_ms)) >= 1000) AS timeout_count,
    sum(
        greatest(
            toFloat64OrZero(JSONExtractString(child.attributes_json, 'retry_count')),
            toFloat64OrZero(JSONExtractString(child.attributes_json, 'retries'))
        )
    ) AS retries_sum,
    sum(
        greatest(
            toFloat64OrZero(JSONExtractString(child.attributes_json, 'pending')),
            toFloat64OrZero(JSONExtractString(child.attributes_json, 'pending_count'))
        )
    ) AS pending_sum,
    sum(
        greatest(
            toFloat64OrZero(JSONExtractString(child.attributes_json, 'dlq')),
            toFloat64OrZero(JSONExtractString(child.attributes_json, 'dlq_count'))
        )
    ) AS dlq_sum,
    quantileTDigest(0.95)(toFloat64OrZero(toString(child.duration_ms))) AS p95_ms,
    quantileTDigest(0.99)(toFloat64OrZero(toString(child.duration_ms))) AS p99_ms,
    sum(toFloat64OrZero(toString(child.duration_ms))) AS duration_sum_ms
FROM logs.traces AS child
INNER JOIN logs.traces AS parent
    ON child.trace_id = parent.trace_id
   AND child.parent_span_id = parent.span_id
WHERE child.timestamp >= now() - INTERVAL 7 DAY
  AND notEmpty(child.trace_id)
  AND notEmpty(child.parent_span_id)
  AND notEmpty(child.service_name)
  AND notEmpty(parent.service_name)
  AND (SELECT count() FROM logs.trace_edges_1m) = 0
GROUP BY ts_minute, source_service, target_service, namespace;

-- 2) Latest-state views for FINAL replacement.
CREATE VIEW IF NOT EXISTS logs.v_ai_analysis_sessions_latest AS
SELECT
    session_id,
    argMax(analysis_type, _updated_at) AS analysis_type,
    argMax(title, _updated_at) AS title,
    argMax(service_name, _updated_at) AS service_name,
    argMax(input_text, _updated_at) AS input_text,
    argMax(trace_id, _updated_at) AS trace_id,
    argMax(summary_text, _updated_at) AS summary_text,
    argMax(context_json, _updated_at) AS context_json,
    argMax(result_json, _updated_at) AS result_json,
    argMax(analysis_method, _updated_at) AS analysis_method,
    argMax(llm_model, _updated_at) AS llm_model,
    argMax(llm_provider, _updated_at) AS llm_provider,
    argMax(source, _updated_at) AS source,
    argMax(status, _updated_at) AS status,
    max(created_at) AS created_at,
    max(_updated_at) AS updated_at,
    argMax(is_pinned, _updated_at) AS is_pinned,
    argMax(is_archived, _updated_at) AS is_archived,
    argMax(is_deleted, _updated_at) AS is_deleted
FROM
(
    SELECT *, updated_at AS _updated_at
    FROM logs.ai_analysis_sessions
)
GROUP BY session_id;

CREATE VIEW IF NOT EXISTS logs.v_alert_rules_latest AS
SELECT
    rule_id,
    argMax(name, _updated_at) AS name,
    argMax(description, _updated_at) AS description,
    argMax(metric_name, _updated_at) AS metric_name,
    argMax(service_name, _updated_at) AS service_name,
    argMax(cond, _updated_at) AS cond,
    argMax(threshold, _updated_at) AS threshold,
    argMax(duration, _updated_at) AS duration,
    argMax(min_occurrence_count, _updated_at) AS min_occurrence_count,
    argMax(severity, _updated_at) AS severity,
    argMax(enabled, _updated_at) AS enabled,
    argMax(labels_json, _updated_at) AS labels_json,
    argMax(notification_enabled, _updated_at) AS notification_enabled,
    argMax(notification_channels_json, _updated_at) AS notification_channels_json,
    argMax(notification_cooldown_seconds, _updated_at) AS notification_cooldown_seconds,
    max(created_at) AS created_at,
    max(_updated_at) AS updated_at,
    argMax(deleted, _updated_at) AS deleted
FROM
(
    SELECT *, updated_at AS _updated_at
    FROM logs.alert_rules
)
GROUP BY rule_id;

CREATE VIEW IF NOT EXISTS logs.v_alert_events_latest AS
SELECT
    event_id,
    argMax(rule_id, _updated_at) AS rule_id,
    argMax(rule_name, _updated_at) AS rule_name,
    argMax(metric_name, _updated_at) AS metric_name,
    argMax(service_name, _updated_at) AS service_name,
    argMax(current_value, _updated_at) AS current_value,
    argMax(threshold, _updated_at) AS threshold,
    argMax(cond, _updated_at) AS cond,
    argMax(severity, _updated_at) AS severity,
    argMax(message, _updated_at) AS message,
    argMax(status, _updated_at) AS status,
    max(fired_at) AS fired_at,
    argMax(resolved_at, _updated_at) AS resolved_at,
    argMax(first_triggered_at, _updated_at) AS first_triggered_at,
    argMax(last_triggered_at, _updated_at) AS last_triggered_at,
    argMax(acknowledged_at, _updated_at) AS acknowledged_at,
    argMax(silenced_until, _updated_at) AS silenced_until,
    argMax(occurrence_count, _updated_at) AS occurrence_count,
    argMax(last_notified_at, _updated_at) AS last_notified_at,
    argMax(notification_count, _updated_at) AS notification_count,
    argMax(labels_json, _updated_at) AS labels_json,
    max(_updated_at) AS updated_at,
    argMax(deleted, _updated_at) AS deleted
FROM
(
    SELECT *, updated_at AS _updated_at
    FROM logs.alert_events
)
GROUP BY event_id;

CREATE VIEW IF NOT EXISTS logs.v_ai_cases_latest AS
SELECT
    case_id,
    argMax(problem_type, _updated_at) AS problem_type,
    argMax(severity, _updated_at) AS severity,
    argMax(summary, _updated_at) AS summary,
    argMax(log_content, _updated_at) AS log_content,
    argMax(service_name, _updated_at) AS service_name,
    argMax(root_causes_json, _updated_at) AS root_causes_json,
    argMax(solutions_json, _updated_at) AS solutions_json,
    argMax(context_json, _updated_at) AS context_json,
    argMax(tags_json, _updated_at) AS tags_json,
    argMax(similarity_features_json, _updated_at) AS similarity_features_json,
    argMax(resolved, _updated_at) AS resolved,
    argMax(resolution, _updated_at) AS resolution,
    max(created_at) AS created_at,
    max(_updated_at) AS updated_at,
    argMax(resolved_at, _updated_at) AS resolved_at,
    argMax(llm_provider, _updated_at) AS llm_provider,
    argMax(llm_model, _updated_at) AS llm_model,
    argMax(llm_metadata_json, _updated_at) AS llm_metadata_json,
    argMax(source, _updated_at) AS source,
    argMax(is_deleted, _updated_at) AS is_deleted
FROM
(
    SELECT *, updated_at AS _updated_at
    FROM logs.ai_cases
)
GROUP BY case_id;

-- 3) Partitioned v2 management tables.
CREATE TABLE IF NOT EXISTS logs.value_kpi_snapshots_v2
AS logs.value_kpi_snapshots
ENGINE = MergeTree()
PARTITION BY toYYYYMM(created_at)
ORDER BY (created_at, snapshot_id)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS logs.release_gate_reports_v2
AS logs.release_gate_reports
ENGINE = MergeTree()
PARTITION BY toYYYYMM(started_at)
ORDER BY (started_at, gate_id)
SETTINGS index_granularity = 8192;

-- Data copy should be performed in a controlled maintenance window:
-- INSERT INTO logs.value_kpi_snapshots_v2 SELECT * FROM logs.value_kpi_snapshots;
-- INSERT INTO logs.release_gate_reports_v2 SELECT * FROM logs.release_gate_reports;
-- RENAME TABLE logs.value_kpi_snapshots TO logs.value_kpi_snapshots_bak_yyyymmdd,
--              logs.value_kpi_snapshots_v2 TO logs.value_kpi_snapshots;
-- RENAME TABLE logs.release_gate_reports TO logs.release_gate_reports_bak_yyyymmdd,
--              logs.release_gate_reports_v2 TO logs.release_gate_reports;
