CREATE TABLE IF NOT EXISTS logs.ai_agent_runs (
    run_id String,
    session_id String,
    conversation_id String,
    analysis_type String,
    engine String,
    runtime_version String,
    user_message_id String,
    assistant_message_id String,
    service_name String,
    trace_id String,
    status String,
    input_json String,
    context_json String,
    summary_json String,
    error_code String,
    error_detail String,
    created_at DateTime64(3, 'UTC'),
    updated_at DateTime64(3, 'UTC'),
    ended_at Nullable(DateTime64(3, 'UTC'))
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY toYYYYMM(created_at)
ORDER BY (run_id)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS logs.ai_agent_run_events (
    run_id String,
    event_id String,
    seq UInt64,
    event_type String,
    payload_json String,
    created_at DateTime64(3, 'UTC')
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(created_at)
ORDER BY (run_id, seq, created_at, event_id)
SETTINGS index_granularity = 8192;

ALTER TABLE logs.ai_agent_runs
    ADD COLUMN IF NOT EXISTS conversation_id String DEFAULT '' AFTER session_id;

CREATE TABLE IF NOT EXISTS logs.ai_agent_tool_calls (
    tool_call_id String,
    run_id String,
    step_id String,
    tool_name String,
    title String,
    status String,
    input_json String,
    result_json String,
    error_code String,
    error_detail String,
    started_at DateTime64(3, 'UTC'),
    updated_at DateTime64(3, 'UTC'),
    ended_at Nullable(DateTime64(3, 'UTC'))
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY toYYYYMM(started_at)
ORDER BY (run_id, tool_call_id)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS logs.ai_agent_command_runs (
    command_run_id String,
    run_id String,
    tool_call_id String,
    message_id String,
    action_id String,
    command String,
    command_type String,
    risk_level String,
    status String,
    requires_confirmation UInt8 DEFAULT 0,
    requires_elevation UInt8 DEFAULT 0,
    exit_code Int32 DEFAULT 0,
    timed_out UInt8 DEFAULT 0,
    output_truncated UInt8 DEFAULT 0,
    error_code String,
    error_detail String,
    started_at DateTime64(3, 'UTC'),
    updated_at DateTime64(3, 'UTC'),
    ended_at Nullable(DateTime64(3, 'UTC'))
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY toYYYYMM(started_at)
ORDER BY (run_id, command_run_id)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS logs.ai_agent_command_events (
    command_run_id String,
    seq UInt64,
    event_type String,
    stream String,
    text String,
    payload_json String,
    created_at DateTime64(3, 'UTC')
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(created_at)
ORDER BY (command_run_id, seq, created_at)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS logs.exec_command_runs (
    run_id String,
    status LowCardinality(String),
    created_at DateTime64(3, 'UTC'),
    updated_at DateTime64(3, 'UTC'),
    record_json String
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY toYYYYMM(created_at)
ORDER BY (run_id, updated_at)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS logs.exec_command_events (
    run_id String,
    event_id String,
    seq UInt64,
    event_type LowCardinality(String),
    created_at DateTime64(3, 'UTC'),
    record_json String
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(created_at)
ORDER BY (run_id, seq, created_at, event_id)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS logs.exec_command_audits (
    run_id String,
    audit_id String,
    created_at DateTime64(3, 'UTC'),
    record_json String
)
ENGINE = MergeTree()
PARTITION BY toYYYYMM(created_at)
ORDER BY (run_id, created_at, audit_id)
SETTINGS index_granularity = 8192;

DROP VIEW IF EXISTS logs.v_ai_agent_runs_latest;

CREATE VIEW IF NOT EXISTS logs.v_ai_agent_runs_latest AS
SELECT
    run_id,
    argMax(session_id, _updated_at) AS session_id,
    argMax(conversation_id, _updated_at) AS conversation_id,
    argMax(analysis_type, _updated_at) AS analysis_type,
    argMax(engine, _updated_at) AS engine,
    argMax(runtime_version, _updated_at) AS runtime_version,
    argMax(user_message_id, _updated_at) AS user_message_id,
    argMax(assistant_message_id, _updated_at) AS assistant_message_id,
    argMax(service_name, _updated_at) AS service_name,
    argMax(trace_id, _updated_at) AS trace_id,
    argMax(status, _updated_at) AS status,
    argMax(input_json, _updated_at) AS input_json,
    argMax(context_json, _updated_at) AS context_json,
    argMax(summary_json, _updated_at) AS summary_json,
    argMax(error_code, _updated_at) AS error_code,
    argMax(error_detail, _updated_at) AS error_detail,
    max(created_at) AS created_at,
    max(_updated_at) AS updated_at,
    argMax(ended_at, _updated_at) AS ended_at
FROM
(
    SELECT *, updated_at AS _updated_at
    FROM logs.ai_agent_runs
)
GROUP BY run_id;

CREATE VIEW IF NOT EXISTS logs.v_ai_agent_tool_calls_latest AS
SELECT
    tool_call_id,
    argMax(run_id, _updated_at) AS run_id,
    argMax(step_id, _updated_at) AS step_id,
    argMax(tool_name, _updated_at) AS tool_name,
    argMax(title, _updated_at) AS title,
    argMax(status, _updated_at) AS status,
    argMax(input_json, _updated_at) AS input_json,
    argMax(result_json, _updated_at) AS result_json,
    argMax(error_code, _updated_at) AS error_code,
    argMax(error_detail, _updated_at) AS error_detail,
    max(started_at) AS started_at,
    max(_updated_at) AS updated_at,
    argMax(ended_at, _updated_at) AS ended_at
FROM
(
    SELECT *, updated_at AS _updated_at
    FROM logs.ai_agent_tool_calls
)
GROUP BY tool_call_id;

CREATE VIEW IF NOT EXISTS logs.v_ai_agent_command_runs_latest AS
SELECT
    command_run_id,
    argMax(run_id, _updated_at) AS run_id,
    argMax(tool_call_id, _updated_at) AS tool_call_id,
    argMax(message_id, _updated_at) AS message_id,
    argMax(action_id, _updated_at) AS action_id,
    argMax(command, _updated_at) AS command,
    argMax(command_type, _updated_at) AS command_type,
    argMax(risk_level, _updated_at) AS risk_level,
    argMax(status, _updated_at) AS status,
    argMax(requires_confirmation, _updated_at) AS requires_confirmation,
    argMax(requires_elevation, _updated_at) AS requires_elevation,
    argMax(exit_code, _updated_at) AS exit_code,
    argMax(timed_out, _updated_at) AS timed_out,
    argMax(output_truncated, _updated_at) AS output_truncated,
    argMax(error_code, _updated_at) AS error_code,
    argMax(error_detail, _updated_at) AS error_detail,
    max(started_at) AS started_at,
    max(_updated_at) AS updated_at,
    argMax(ended_at, _updated_at) AS ended_at
FROM
(
    SELECT *, updated_at AS _updated_at
    FROM logs.ai_agent_command_runs
)
GROUP BY command_run_id;

ALTER TABLE logs.ai_analysis_sessions
    ADD COLUMN IF NOT EXISTS latest_run_id String DEFAULT '',
    ADD COLUMN IF NOT EXISTS runtime_version String DEFAULT '';
