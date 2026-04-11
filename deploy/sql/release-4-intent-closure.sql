-- Release 4: AI runtime intent-led execution closure
-- Purpose:
-- 1) Introduce intent / approval-grant / execution-record canonical stores.
-- 2) Provide latest-state views to avoid FINAL for runtime reads.
-- 3) Support idempotent reconciliation and replay.

CREATE DATABASE IF NOT EXISTS logs;

-- 1) Intent canonical table (append-only, latest by updated_at)
CREATE TABLE IF NOT EXISTS logs.ai_runtime_v4_intents (
    intent_id String,
    run_id String,
    thread_id String,
    session_id String,
    action_id String,
    fingerprint String,
    intent_type LowCardinality(String),
    evidence_gap String,
    purpose_class LowCardinality(String),
    target_scope String,
    target_kind LowCardinality(String),
    target_identity String,
    risk_tier LowCardinality(String),
    status LowCardinality(String),
    budget_snapshot_json String,
    source_json String,
    metadata_json String,
    error_code String,
    error_detail String,
    created_at DateTime64(3, 'UTC'),
    updated_at DateTime64(3, 'UTC'),
    ended_at Nullable(DateTime64(3, 'UTC'))
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY toYYYYMM(created_at)
ORDER BY (run_id, intent_id, updated_at)
SETTINGS index_granularity = 8192;


-- 2) Approval grant table (reusable approval token)
CREATE TABLE IF NOT EXISTS logs.ai_runtime_v4_approval_grants (
    grant_id String,
    approval_id String,
    run_id String,
    intent_id String,
    fingerprint String,
    target_scope String,
    target_kind LowCardinality(String),
    target_identity String,
    risk_tier LowCardinality(String),
    decision LowCardinality(String),
    approved_by String,
    ttl_expires_at Nullable(DateTime64(3, 'UTC')),
    max_reuse UInt16 DEFAULT 1,
    reuse_count UInt16 DEFAULT 0,
    revoked UInt8 DEFAULT 0,
    reason String,
    metadata_json String,
    created_at DateTime64(3, 'UTC'),
    updated_at DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY toYYYYMM(created_at)
ORDER BY (fingerprint, target_scope, created_at, grant_id)
SETTINGS index_granularity = 8192;


-- 3) Execution record table (idempotent execution fact source)
CREATE TABLE IF NOT EXISTS logs.ai_runtime_v4_execution_records (
    exec_record_id String,
    run_id String,
    intent_id String,
    action_id String,
    tool_call_id String,
    command_run_id String,
    fingerprint String,
    command String,
    command_type LowCardinality(String),
    risk_level LowCardinality(String),
    dispatch_status LowCardinality(String),
    observed_status LowCardinality(String),
    settled_status LowCardinality(String),
    exit_code Int32 DEFAULT 0,
    timed_out UInt8 DEFAULT 0,
    output_truncated UInt8 DEFAULT 0,
    backend_unavailable UInt8 DEFAULT 0,
    error_code String,
    error_detail String,
    stderr String,
    stdout_sample String,
    observed_at Nullable(DateTime64(3, 'UTC')),
    settled_at Nullable(DateTime64(3, 'UTC')),
    created_at DateTime64(3, 'UTC'),
    updated_at DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY toYYYYMM(created_at)
ORDER BY (run_id, fingerprint, command_run_id, updated_at)
SETTINGS index_granularity = 8192;


-- Optional reconciliation checkpoint for background settle workers.
CREATE TABLE IF NOT EXISTS logs.ai_runtime_v4_reconcile_checkpoints (
    worker_name String,
    cursor_key String,
    cursor_value String,
    lease_until Nullable(DateTime64(3, 'UTC')),
    heartbeat_at DateTime64(3, 'UTC'),
    updated_at DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (worker_name, cursor_key)
SETTINGS index_granularity = 8192;


-- Latest-state views.
CREATE VIEW IF NOT EXISTS logs.v_ai_runtime_v4_intents_latest AS
SELECT
    intent_id,
    argMax(run_id, _updated_at) AS run_id,
    argMax(thread_id, _updated_at) AS thread_id,
    argMax(session_id, _updated_at) AS session_id,
    argMax(action_id, _updated_at) AS action_id,
    argMax(fingerprint, _updated_at) AS fingerprint,
    argMax(intent_type, _updated_at) AS intent_type,
    argMax(evidence_gap, _updated_at) AS evidence_gap,
    argMax(purpose_class, _updated_at) AS purpose_class,
    argMax(target_scope, _updated_at) AS target_scope,
    argMax(target_kind, _updated_at) AS target_kind,
    argMax(target_identity, _updated_at) AS target_identity,
    argMax(risk_tier, _updated_at) AS risk_tier,
    argMax(status, _updated_at) AS status,
    argMax(budget_snapshot_json, _updated_at) AS budget_snapshot_json,
    argMax(source_json, _updated_at) AS source_json,
    argMax(metadata_json, _updated_at) AS metadata_json,
    argMax(error_code, _updated_at) AS error_code,
    argMax(error_detail, _updated_at) AS error_detail,
    max(created_at) AS created_at,
    max(_updated_at) AS updated_at,
    argMax(ended_at, _updated_at) AS ended_at
FROM
(
    SELECT *, updated_at AS _updated_at
    FROM logs.ai_runtime_v4_intents
)
GROUP BY intent_id;


CREATE VIEW IF NOT EXISTS logs.v_ai_runtime_v4_approval_grants_latest AS
SELECT
    grant_id,
    argMax(approval_id, _updated_at) AS approval_id,
    argMax(run_id, _updated_at) AS run_id,
    argMax(intent_id, _updated_at) AS intent_id,
    argMax(fingerprint, _updated_at) AS fingerprint,
    argMax(target_scope, _updated_at) AS target_scope,
    argMax(target_kind, _updated_at) AS target_kind,
    argMax(target_identity, _updated_at) AS target_identity,
    argMax(risk_tier, _updated_at) AS risk_tier,
    argMax(decision, _updated_at) AS decision,
    argMax(approved_by, _updated_at) AS approved_by,
    argMax(ttl_expires_at, _updated_at) AS ttl_expires_at,
    argMax(max_reuse, _updated_at) AS max_reuse,
    argMax(reuse_count, _updated_at) AS reuse_count,
    argMax(revoked, _updated_at) AS revoked,
    argMax(reason, _updated_at) AS reason,
    argMax(metadata_json, _updated_at) AS metadata_json,
    max(created_at) AS created_at,
    max(_updated_at) AS updated_at
FROM
(
    SELECT *, updated_at AS _updated_at
    FROM logs.ai_runtime_v4_approval_grants
)
GROUP BY grant_id;


CREATE VIEW IF NOT EXISTS logs.v_ai_runtime_v4_execution_records_latest AS
SELECT
    exec_record_id,
    argMax(run_id, _updated_at) AS run_id,
    argMax(intent_id, _updated_at) AS intent_id,
    argMax(action_id, _updated_at) AS action_id,
    argMax(tool_call_id, _updated_at) AS tool_call_id,
    argMax(command_run_id, _updated_at) AS command_run_id,
    argMax(fingerprint, _updated_at) AS fingerprint,
    argMax(command, _updated_at) AS command,
    argMax(command_type, _updated_at) AS command_type,
    argMax(risk_level, _updated_at) AS risk_level,
    argMax(dispatch_status, _updated_at) AS dispatch_status,
    argMax(observed_status, _updated_at) AS observed_status,
    argMax(settled_status, _updated_at) AS settled_status,
    argMax(exit_code, _updated_at) AS exit_code,
    argMax(timed_out, _updated_at) AS timed_out,
    argMax(output_truncated, _updated_at) AS output_truncated,
    argMax(backend_unavailable, _updated_at) AS backend_unavailable,
    argMax(error_code, _updated_at) AS error_code,
    argMax(error_detail, _updated_at) AS error_detail,
    argMax(stderr, _updated_at) AS stderr,
    argMax(stdout_sample, _updated_at) AS stdout_sample,
    argMax(observed_at, _updated_at) AS observed_at,
    argMax(settled_at, _updated_at) AS settled_at,
    max(created_at) AS created_at,
    max(_updated_at) AS updated_at
FROM
(
    SELECT *, updated_at AS _updated_at
    FROM logs.ai_runtime_v4_execution_records
)
GROUP BY exec_record_id;


-- Recommended follow-up operations (manual, maintenance window):
-- 1) Backfill historical execution facts:
--    INSERT INTO logs.ai_runtime_v4_execution_records (...)
--    SELECT ... FROM logs.exec_command_runs / logs.ai_agent_* WHERE ...
--
-- 2) Add query-side projections/index views after cardinality baseline.
-- 3) Enable TTL rules after retention policy review.

