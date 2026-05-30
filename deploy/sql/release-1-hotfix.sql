-- Release 1: low-risk hotfix package
-- Goals:
-- 1) Backfill MATERIALIZE for newly added indexes/materialized columns
-- 2) Keep substring search semantics while improving selectivity
-- 3) Add compatibility alias for traces namespace reads
-- 4) Add pod/namespace projection for common context queries

CREATE DATABASE IF NOT EXISTS logs;

-- 1) Compatibility bridge for legacy traces namespace reads.
ALTER TABLE logs.traces
ADD COLUMN IF NOT EXISTS namespace LowCardinality(String) ALIAS traces_namespace;

-- 2) Ensure skip indexes/materialized columns exist (idempotent).
ALTER TABLE logs.logs
ADD INDEX IF NOT EXISTS idx_logs_service_name service_name TYPE set(256) GRANULARITY 1;
ALTER TABLE logs.logs
ADD INDEX IF NOT EXISTS idx_logs_namespace namespace TYPE set(256) GRANULARITY 1;
ALTER TABLE logs.logs
ADD INDEX IF NOT EXISTS idx_logs_id id TYPE bloom_filter(0.01) GRANULARITY 4;
ALTER TABLE logs.logs
ADD INDEX IF NOT EXISTS idx_logs_pod_name pod_name TYPE bloom_filter(0.01) GRANULARITY 4;

ALTER TABLE logs.traces
ADD COLUMN IF NOT EXISTS traces_namespace LowCardinality(String)
MATERIALIZED multiIf(
    length(JSONExtractString(attributes_json, 'k8s.namespace.name')) > 0,
    JSONExtractString(attributes_json, 'k8s.namespace.name'),
    length(JSONExtractString(attributes_json, 'service_namespace')) > 0,
    JSONExtractString(attributes_json, 'service_namespace'),
    JSONExtractString(attributes_json, 'namespace')
);
ALTER TABLE logs.traces
ADD INDEX IF NOT EXISTS idx_traces_namespace traces_namespace TYPE set(256) GRANULARITY 1;

ALTER TABLE logs.metrics
ADD COLUMN IF NOT EXISTS metrics_namespace LowCardinality(String)
MATERIALIZED if(
    length(JSONExtractString(attributes_json, 'service_namespace')) > 0,
    JSONExtractString(attributes_json, 'service_namespace'),
    JSONExtractString(attributes_json, 'namespace')
);
ALTER TABLE logs.metrics
ADD INDEX IF NOT EXISTS idx_metrics_namespace metrics_namespace TYPE set(256) GRANULARITY 1;
ALTER TABLE logs.metrics
ADD INDEX IF NOT EXISTS idx_metrics_metric_name metric_name TYPE bloom_filter(0.01) GRANULARITY 4;

-- 3) Materialize historical parts so old data can benefit.
ALTER TABLE logs.logs MATERIALIZE INDEX idx_logs_service_name;
ALTER TABLE logs.logs MATERIALIZE INDEX idx_logs_namespace;
ALTER TABLE logs.logs MATERIALIZE INDEX idx_logs_id;
ALTER TABLE logs.logs MATERIALIZE INDEX idx_logs_pod_name;

ALTER TABLE logs.traces MATERIALIZE COLUMN traces_namespace;
ALTER TABLE logs.traces MATERIALIZE INDEX idx_traces_namespace;

ALTER TABLE logs.metrics MATERIALIZE COLUMN metrics_namespace;
ALTER TABLE logs.metrics MATERIALIZE INDEX idx_metrics_namespace;
ALTER TABLE logs.metrics MATERIALIZE INDEX idx_metrics_metric_name;

-- 4) Keep %keyword% semantics: add ngram index.
ALTER TABLE logs.logs
ADD INDEX IF NOT EXISTS idx_logs_message_ngram message TYPE ngrambf_v1(3, 65536, 3, 0) GRANULARITY 1;
ALTER TABLE logs.logs MATERIALIZE INDEX idx_logs_message_ngram;

-- 5) Add projection for pod/namespace/time-heavy paths.
ALTER TABLE logs.logs
ADD PROJECTION IF NOT EXISTS proj_logs_pod_ns_time
(
    SELECT
        id, timestamp, service_name, level_norm, pod_name, namespace, trace_id, span_id
    ORDER BY (pod_name, namespace, timestamp, id)
);
ALTER TABLE logs.logs MATERIALIZE PROJECTION proj_logs_pod_ns_time;
