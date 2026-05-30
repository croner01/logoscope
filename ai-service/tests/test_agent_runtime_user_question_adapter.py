from ai.agent_runtime.user_question_adapter import build_business_question


def test_build_business_question_sql_preflight_failed_returns_sql_target():
    question = build_business_question(
        failure_code="sql_preflight_failed",
        failure_message="sql_preflight_failed: Syntax error",
        purpose="定位慢查询根因",
        title="执行命令",
        command="kubectl ...",
    )
    assert question["kind"] == "business_question"
    assert question["question_kind"] == "sql_target"
    assert "SQL 目标" in question["title"]


def test_build_business_question_missing_spec_requests_structured_command_spec():
    question = build_business_question(
        failure_code="missing_or_invalid_command_spec",
        failure_message="missing_or_invalid_command_spec: command_spec is required",
        purpose="定位慢查询根因",
        title="执行命令",
        command="kubectl ...",
        last_user_input_question_kind="execution_scope",
        last_user_input_text="先看最近 1 小时的 clickhouse 慢查询",
    )
    assert question["kind"] == "business_question"
    assert question["question_kind"] == "command_spec"
    assert "结构化命令参数" in question["title"]


def test_build_business_question_missing_target_identity_asks_for_database_target():
    question = build_business_question(
        failure_code="missing_target_identity",
        failure_message="target_identity is required when query does not uniquely identify a database target",
        purpose="确认慢查询影响范围",
        title="执行数据库查询",
        command='clickhouse-client --query "SELECT count() FROM otel_logs LIMIT 10"',
    )
    assert question["kind"] == "business_question"
    assert question["question_kind"] == "command_target"
    assert "数据库目标" in question["title"]
    assert "database:logs" in question["prompt"]


def test_build_business_question_dedupes_diagnosis_goal_for_same_action():
    question = build_business_question(
        failure_code="unknown_semantics",
        failure_message="unknown command semantics",
        purpose="定位慢查询根因",
        title="执行命令",
        command="kubectl ...",
        current_action_id="lc-1",
        last_user_input_question_kind="diagnosis_goal",
        last_user_input_action_id="lc-1",
        last_user_input_text="先定位根因",
    )
    assert question["kind"] == "business_question"
    assert question["question_kind"] == "execution_scope"
    assert "执行范围" in question["title"]
    assert "前一轮给的排查目标我已收到" in question["prompt"]


def test_build_business_question_dedupes_diagnosis_goal_across_replanned_actions():
    question = build_business_question(
        failure_code="unknown_semantics",
        failure_message="unknown command semantics",
        purpose="定位慢查询根因",
        title="执行命令",
        command="kubectl ...",
        current_action_id="lc-2",
        last_user_input_question_kind="diagnosis_goal",
        last_user_input_action_id="lc-1",
        last_user_input_text="先定位根因",
    )
    assert question["kind"] == "business_question"
    assert question["question_kind"] == "execution_scope"
    assert "执行范围" in question["title"]
