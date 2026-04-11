#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SQL_FILE="${ROOT_DIR}/scripts/clickhouse-online-audit.sql"
REPORT_DIR="${ROOT_DIR}/reports/clickhouse-audit"
mkdir -p "${REPORT_DIR}"

TS="$(date -u +%Y%m%d-%H%M%S)"
OUT_FILE="${REPORT_DIR}/clickhouse-online-audit-${TS}.txt"
FULL_STDERR_FILE="${REPORT_DIR}/clickhouse-online-audit-${TS}.full.stderr.log"
FALLBACK_STDERR_FILE="${REPORT_DIR}/clickhouse-online-audit-${TS}-minimal-fallback.stderr.log"
FAILURE_SUMMARY_FILE="${REPORT_DIR}/clickhouse-online-audit-${TS}.full-failure-summary.txt"

CH_HOST="${CLICKHOUSE_HOST:-}"
CH_PORT="${CLICKHOUSE_PORT:-}"
CH_USER="${CLICKHOUSE_USER:-}"
CH_PASSWORD="${CLICKHOUSE_PASSWORD:-}"
CH_DATABASE="${CLICKHOUSE_DATABASE:-default}"
AUDIT_OUTPUT_FORMAT="${CLICKHOUSE_AUDIT_OUTPUT_FORMAT:-PrettyCompact}"
ERROR_SUMMARY_LINES="${CLICKHOUSE_AUDIT_ERROR_SUMMARY_LINES:-20}"
SUMMARY_TOP_N="${CLICKHOUSE_AUDIT_SUMMARY_TOP_N:-10}"
if ! [[ "${ERROR_SUMMARY_LINES}" =~ ^[0-9]+$ ]]; then
  ERROR_SUMMARY_LINES="20"
fi
if ! [[ "${SUMMARY_TOP_N}" =~ ^[0-9]+$ ]]; then
  SUMMARY_TOP_N="10"
fi

EXEC_MODE="${CLICKHOUSE_AUDIT_EXEC_MODE:-auto}" # auto|local|kubectl|docker
AUDIT_PROFILE="${CLICKHOUSE_AUDIT_PROFILE:-full}" # full|minimal
AUTO_FALLBACK_TO_MINIMAL="${CLICKHOUSE_AUDIT_AUTO_FALLBACK_TO_MINIMAL:-1}"

K8S_NAMESPACE="${CLICKHOUSE_NAMESPACE:-${NAMESPACE:-islap}}"
K8S_POD_LABEL="${CLICKHOUSE_POD_LABEL:-app=clickhouse}"
K8S_POD_NAME="${CLICKHOUSE_POD_NAME:-}"
K8S_CONTAINER="${CLICKHOUSE_CONTAINER:-}"
K8S_CONTEXT="${CLICKHOUSE_CONTEXT:-}"

DOCKER_CONTAINER="${CLICKHOUSE_DOCKER_CONTAINER:-}"

CLIENT_ARGS=("--multiquery" "--database" "${CH_DATABASE}" "--format" "${AUDIT_OUTPUT_FORMAT}")
QUERY_ARGS=("--database" "${CH_DATABASE}")
if [[ -n "${CH_HOST}" ]]; then CLIENT_ARGS+=("--host" "${CH_HOST}"); fi
if [[ -n "${CH_PORT}" ]]; then CLIENT_ARGS+=("--port" "${CH_PORT}"); fi
if [[ -n "${CH_USER}" ]]; then CLIENT_ARGS+=("--user" "${CH_USER}"); fi
if [[ -n "${CH_PASSWORD}" ]]; then CLIENT_ARGS+=("--password" "${CH_PASSWORD}"); fi
if [[ -n "${CH_HOST}" ]]; then QUERY_ARGS+=("--host" "${CH_HOST}"); fi
if [[ -n "${CH_PORT}" ]]; then QUERY_ARGS+=("--port" "${CH_PORT}"); fi
if [[ -n "${CH_USER}" ]]; then QUERY_ARGS+=("--user" "${CH_USER}"); fi
if [[ -n "${CH_PASSWORD}" ]]; then QUERY_ARGS+=("--password" "${CH_PASSWORD}"); fi

resolve_k8s_pod() {
  if [[ -n "${K8S_POD_NAME}" ]]; then
    echo "${K8S_POD_NAME}"
    return 0
  fi
  local -a kubectl_cmd=("kubectl")
  if [[ -n "${K8S_CONTEXT}" ]]; then
    kubectl_cmd+=("--context" "${K8S_CONTEXT}")
  fi
  kubectl_cmd+=("-n" "${K8S_NAMESPACE}" "get" "pods" "-l" "${K8S_POD_LABEL}" "--field-selector=status.phase=Running" "-o" "jsonpath={range .items[*]}{.metadata.name}{\"\\n\"}{end}")
  local pod
  pod="$("${kubectl_cmd[@]}" | head -n 1)"
  if [[ -z "${pod}" ]]; then
    return 1
  fi
  echo "${pod}"
}

run_local() {
  local sql_file="$1"
  clickhouse-client "${CLIENT_ARGS[@]}" < "${sql_file}"
}

run_local_query() {
  local query_sql="$1"
  local output_format="$2"
  clickhouse-client "${QUERY_ARGS[@]}" --format "${output_format}" --query "${query_sql}"
}

run_kubectl() {
  local sql_file="$1"
  if ! command -v kubectl >/dev/null 2>&1; then
    echo "ERROR: kubectl not found in PATH" >&2
    return 1
  fi

  local pod
  pod="$(resolve_k8s_pod)" || {
    echo "ERROR: cannot find running ClickHouse pod in namespace=${K8S_NAMESPACE}, label=${K8S_POD_LABEL}" >&2
    echo "Hint: set CLICKHOUSE_POD_NAME explicitly." >&2
    return 1
  }

  local -a kubectl_cmd=("kubectl")
  if [[ -n "${K8S_CONTEXT}" ]]; then
    kubectl_cmd+=("--context" "${K8S_CONTEXT}")
  fi
  kubectl_cmd+=("-n" "${K8S_NAMESPACE}" "exec" "-i" "${pod}")
  if [[ -n "${K8S_CONTAINER}" ]]; then
    kubectl_cmd+=("-c" "${K8S_CONTAINER}")
  fi
  kubectl_cmd+=("--" "clickhouse-client" "${CLIENT_ARGS[@]}")
  "${kubectl_cmd[@]}" < "${sql_file}"
}

run_kubectl_query() {
  local query_sql="$1"
  local output_format="$2"
  if ! command -v kubectl >/dev/null 2>&1; then
    echo "ERROR: kubectl not found in PATH" >&2
    return 1
  fi

  local pod
  pod="$(resolve_k8s_pod)" || {
    echo "ERROR: cannot find running ClickHouse pod in namespace=${K8S_NAMESPACE}, label=${K8S_POD_LABEL}" >&2
    echo "Hint: set CLICKHOUSE_POD_NAME explicitly." >&2
    return 1
  }

  local -a kubectl_cmd=("kubectl")
  if [[ -n "${K8S_CONTEXT}" ]]; then
    kubectl_cmd+=("--context" "${K8S_CONTEXT}")
  fi
  kubectl_cmd+=("-n" "${K8S_NAMESPACE}" "exec" "-i" "${pod}")
  if [[ -n "${K8S_CONTAINER}" ]]; then
    kubectl_cmd+=("-c" "${K8S_CONTAINER}")
  fi
  kubectl_cmd+=("--" "clickhouse-client" "${QUERY_ARGS[@]}" "--format" "${output_format}" "--query" "${query_sql}")
  "${kubectl_cmd[@]}"
}

run_docker() {
  local sql_file="$1"
  if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker not found in PATH" >&2
    return 1
  fi
  if [[ -z "${DOCKER_CONTAINER}" ]]; then
    echo "ERROR: CLICKHOUSE_DOCKER_CONTAINER is required for docker mode" >&2
    return 1
  fi
  docker exec -i "${DOCKER_CONTAINER}" clickhouse-client "${CLIENT_ARGS[@]}" < "${sql_file}"
}

run_docker_query() {
  local query_sql="$1"
  local output_format="$2"
  if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker not found in PATH" >&2
    return 1
  fi
  if [[ -z "${DOCKER_CONTAINER}" ]]; then
    echo "ERROR: CLICKHOUSE_DOCKER_CONTAINER is required for docker mode" >&2
    return 1
  fi
  docker exec -i "${DOCKER_CONTAINER}" clickhouse-client "${QUERY_ARGS[@]}" --format "${output_format}" --query "${query_sql}"
}

select_mode() {
  case "${EXEC_MODE}" in
    local|kubectl|docker)
      echo "${EXEC_MODE}"
      ;;
    auto)
      if command -v clickhouse-client >/dev/null 2>&1; then
        echo "local"
      elif command -v kubectl >/dev/null 2>&1; then
        echo "kubectl"
      elif command -v docker >/dev/null 2>&1 && [[ -n "${DOCKER_CONTAINER}" ]]; then
        echo "docker"
      else
        echo "ERROR"
      fi
      ;;
    *)
      echo "ERROR"
      ;;
  esac
}

RUN_MODE="$(select_mode)"
if [[ "${RUN_MODE}" == "ERROR" ]]; then
  cat >&2 <<EOF
ERROR: cannot determine ClickHouse audit execution mode.
- Set CLICKHOUSE_AUDIT_EXEC_MODE=local|kubectl|docker
- Or install clickhouse-client locally
- Or ensure kubectl can access ClickHouse pod
- Or set CLICKHOUSE_DOCKER_CONTAINER for docker mode
EOF
  exit 1
fi

build_minimal_sql() {
  local tmp_file
  tmp_file="$(mktemp /tmp/clickhouse-audit-minimal.XXXXXX.sql)"
  cat > "${tmp_file}" <<'EOF'
-- ClickHouse Online Audit (Minimal Compatibility Profile)
SELECT '=== SECTION 0: ENVIRONMENT ===' AS section;
SELECT version() AS clickhouse_version, now() AS audit_time_utc;

SELECT '=== SECTION 1: PART AUDIT ===' AS section;
SELECT
    database,
    table,
    count() AS active_parts,
    uniqExact(partition) AS partitions,
    round(active_parts / nullIf(partitions, 0), 2) AS avg_parts_per_partition,
    sum(rows) AS total_rows,
    formatReadableSize(sum(bytes_on_disk)) AS total_bytes
FROM system.parts
WHERE active
  AND database = 'logs'
GROUP BY database, table
ORDER BY active_parts DESC, total_rows DESC;

SELECT
    database,
    table,
    partition,
    count() AS active_parts_in_partition,
    sum(rows) AS rows_in_partition
FROM system.parts
WHERE active
  AND database = 'logs'
GROUP BY database, table, partition
HAVING active_parts_in_partition >= 100
ORDER BY active_parts_in_partition DESC, rows_in_partition DESC
LIMIT 300;

SELECT '=== SECTION 2: SLOW QUERY AUDIT ===' AS section;
SELECT
    cityHash64(replaceRegexpAll(lowerUTF8(query), '\\s+', ' ')) AS query_fingerprint,
    any(replaceRegexpAll(substring(query, 1, 240), '\\s+', ' ')) AS sample_query,
    count() AS executions,
    round(avg(query_duration_ms), 2) AS avg_ms,
    round(quantile(0.95)(query_duration_ms), 2) AS p95_ms,
    round(max(query_duration_ms), 2) AS max_ms
FROM system.query_log
WHERE event_time >= now() - INTERVAL 24 HOUR
  AND type = 'QueryFinish'
  AND is_initial_query = 1
GROUP BY query_fingerprint
HAVING executions >= 3
ORDER BY p95_ms DESC, executions DESC
LIMIT 50;

SELECT '=== SECTION 3: FINAL RATIO AUDIT ===' AS section;
SELECT
    count() AS total_select_queries,
    countIf(positionCaseInsensitive(query, 'FINAL') > 0) AS final_select_queries,
    round(final_select_queries / nullIf(total_select_queries, 0), 4) AS final_ratio
FROM system.query_log
WHERE event_time >= now() - INTERVAL 24 HOUR
  AND type = 'QueryFinish'
  AND is_initial_query = 1
  AND positionCaseInsensitive(query, 'SELECT') > 0;
EOF
  echo "${tmp_file}"
}

print_report_header() {
  local report_file="$1"
  local sql_file="$2"
  local profile_name="$3"
  local stderr_file="$4"
  {
    echo "# ClickHouse Online Audit"
    echo "# UTC Time: $(date -u '+%Y-%m-%d %H:%M:%S')"
    echo "# SQL File: ${sql_file}"
    echo "# Database: ${CH_DATABASE}"
    echo "# Run Mode: ${RUN_MODE}"
    echo "# Audit Profile: ${profile_name}"
    echo "# Output Format: ${AUDIT_OUTPUT_FORMAT}"
    echo "# Stderr Log: ${stderr_file}"
    if [[ -n "${CH_HOST}" ]]; then
      echo "# Host: ${CH_HOST}:${CH_PORT:-9000}"
    fi
    if [[ "${RUN_MODE}" == "kubectl" ]]; then
      echo "# K8s Namespace: ${K8S_NAMESPACE}"
      echo "# K8s Pod Label: ${K8S_POD_LABEL}"
      if [[ -n "${K8S_POD_NAME}" ]]; then
        echo "# K8s Pod Name: ${K8S_POD_NAME}"
      fi
      if [[ -n "${K8S_CONTAINER}" ]]; then
        echo "# K8s Container: ${K8S_CONTAINER}"
      fi
      if [[ -n "${K8S_CONTEXT}" ]]; then
        echo "# K8s Context: ${K8S_CONTEXT}"
      fi
    fi
    if [[ "${RUN_MODE}" == "docker" ]]; then
      echo "# Docker Container: ${DOCKER_CONTAINER}"
    fi
    echo
  } > "${report_file}"
}

append_full_failure_summary() {
  local report_file="$1"
  local full_exit_code="$2"
  local full_stderr_file="$3"
  if [[ -z "${full_exit_code}" ]]; then
    return 0
  fi

  {
    echo "# Full Profile Status: FAILED"
    echo "# Full Profile Exit Code: ${full_exit_code}"
    echo "# Full Profile Stderr Log: ${full_stderr_file}"
    echo "# Full Profile Error Summary (first ${ERROR_SUMMARY_LINES} lines):"
    if [[ -s "${full_stderr_file}" ]]; then
      sed -n "1,${ERROR_SUMMARY_LINES}p" "${full_stderr_file}" | sed 's/^/#   /'
    else
      echo "#   (stderr is empty)"
    fi
    echo
  } >> "${report_file}"
}

write_failure_summary_file() {
  local summary_file="$1"
  local full_report_file="$2"
  local full_exit_code="$3"
  local full_stderr_file="$4"

  {
    echo "# ClickHouse Online Audit - Full Profile Failure Summary"
    echo "# UTC Time: $(date -u '+%Y-%m-%d %H:%M:%S')"
    echo "# Exit Code: ${full_exit_code}"
    echo "# Full Report: ${full_report_file}"
    echo "# Full Stderr Log: ${full_stderr_file}"
    echo "# Error Summary (first ${ERROR_SUMMARY_LINES} lines):"
    if [[ -s "${full_stderr_file}" ]]; then
      sed -n "1,${ERROR_SUMMARY_LINES}p" "${full_stderr_file}"
    else
      echo "(stderr is empty)"
    fi
  } > "${summary_file}"
}

run_mode_with_sql_file() {
  local sql_file="$1"
  case "${RUN_MODE}" in
    local) run_local "${sql_file}" ;;
    kubectl) run_kubectl "${sql_file}" ;;
    docker) run_docker "${sql_file}" ;;
  esac
}

run_mode_with_query() {
  local query_sql="$1"
  local output_format="$2"
  case "${RUN_MODE}" in
    local) run_local_query "${query_sql}" "${output_format}" ;;
    kubectl) run_kubectl_query "${query_sql}" "${output_format}" ;;
    docker) run_docker_query "${query_sql}" "${output_format}" ;;
  esac
}

tsv_to_markdown_table() {
  awk -F '\t' '
    function esc(s) {
      gsub(/\|/, "\\|", s)
      gsub(/\r/, "", s)
      gsub(/\n/, " ", s)
      return s
    }
    NR == 1 {
      head = "|"
      sep = "|"
      for (i = 1; i <= NF; i++) {
        head = head " " esc($i) " |"
        sep = sep " --- |"
      }
      print head
      print sep
      next
    }
    {
      row = "|"
      for (i = 1; i <= NF; i++) {
        row = row " " esc($i) " |"
      }
      print row
    }
  '
}

append_summary_query_section() {
  local summary_file="$1"
  local stderr_file="$2"
  local section_title="$3"
  local section_sql="$4"
  local empty_message="$5"
  local query_result
  local query_exit_code

  {
    echo "## ${section_title}"
    echo
  } >> "${summary_file}"

  if query_result="$(run_mode_with_query "${section_sql}" "TabSeparatedWithNames" 2>> "${stderr_file}")"; then
    if [[ -z "${query_result}" ]]; then
      echo "${empty_message}" >> "${summary_file}"
      echo >> "${summary_file}"
      return 0
    fi
    if [[ "$(printf '%s\n' "${query_result}" | wc -l)" -le 1 ]]; then
      echo "${empty_message}" >> "${summary_file}"
      echo >> "${summary_file}"
      return 0
    fi
    printf '%s\n' "${query_result}" | tsv_to_markdown_table >> "${summary_file}"
    echo >> "${summary_file}"
    return 0
  else
    query_exit_code="$?"
  fi
  {
    echo "_查询失败（exit ${query_exit_code}），详情见 stderr：\`${stderr_file}\`_"
    echo
  } >> "${summary_file}"
}

safe_query_data_line() {
  local query_sql="$1"
  local stderr_file="$2"
  local query_result
  if query_result="$(run_mode_with_query "${query_sql}" "TabSeparatedWithNames" 2>> "${stderr_file}")"; then
    printf '%s\n' "${query_result}" | sed -n '2p'
    return 0
  fi
  return 1
}

append_summary_conclusion_section() {
  local summary_file="$1"
  local summary_stderr_file="$2"

  local p0_count="0"
  local p1_count="0"
  local p2_count="0"
  local final_ratio="0"
  local projection_attempt_rate="0"
  local projection_table_count="0"
  local part_hot_table="N/A"
  local part_hot_parts="0"
  local slow_hot_table="N/A"
  local slow_hot_p95_ms="0"
  local result_line

  local priority_count_sql
  read -r -d '' priority_count_sql <<'SQL' || true
WITH parts_metric AS
(
    SELECT
        database AS db_name,
        table AS table_name,
        max(parts_in_partition) AS max_parts_in_partition
    FROM
    (
        SELECT
            database,
            table,
            partition,
            count() AS parts_in_partition
        FROM system.parts
        WHERE active
          AND database = 'logs'
        GROUP BY database, table, partition
    )
    GROUP BY db_name, table_name
),
logs_tables AS
(
    SELECT name AS table_name
    FROM system.tables
    WHERE database = 'logs'
),
query_table_refs AS
(
    SELECT
        extract(lowerUTF8(query), 'logs\\W+([a-z0-9_]+)') AS table_name,
        query_duration_ms,
        has_final,
        has_projection_hint
    FROM
    (
        SELECT
            query,
            query_duration_ms,
            match(lowerUTF8(query), '(^|[^a-z0-9_])final([^a-z0-9_]|$)') AS has_final,
            match(lowerUTF8(query), 'optimize_use_projections\\s*=\\s*1') AS has_projection_hint
        FROM system.query_log
        WHERE event_time >= now() - INTERVAL 24 HOUR
          AND type = 'QueryFinish'
          AND is_initial_query = 1
          AND match(query, '(?i)^\\s*(SELECT|WITH)\\b')
    )
    WHERE length(extract(lowerUTF8(query), 'logs\\W+([a-z0-9_]+)')) > 0
),
final_metric AS
(
    SELECT
        'logs' AS db_name,
        q.table_name AS table_name,
        round(
            countIf(q.has_final) / nullIf(count(), 0),
            4
        ) AS final_ratio
    FROM query_table_refs AS q
    INNER JOIN logs_tables AS lt
        ON lt.table_name = q.table_name
    GROUP BY q.table_name
),
slow_metric AS
(
    SELECT
        'logs' AS db_name,
        q.table_name AS table_name,
        round(quantile(0.95)(q.query_duration_ms), 2) AS p95_ms
    FROM query_table_refs AS q
    INNER JOIN logs_tables AS lt
        ON lt.table_name = q.table_name
    GROUP BY q.table_name
),
projection_metric AS
(
    WITH projection_tables AS
    (
        SELECT name AS table_name
        FROM system.tables
        WHERE database = 'logs'
          AND positionCaseInsensitive(create_table_query, 'PROJECTION ') > 0
    )
    SELECT
        'logs' AS db_name,
        q.table_name AS table_name,
        round(
            countIf(q.has_projection_hint)
            / nullIf(count(), 0),
            4
        ) AS projection_attempt_rate_estimated
    FROM query_table_refs AS q
    INNER JOIN projection_tables AS p
        ON p.table_name = q.table_name
    GROUP BY q.table_name
),
score_board AS
(
    SELECT
        p.db_name AS db_name,
        p.table_name AS table_name,
        (
            if(p.max_parts_in_partition > 300, 40, if(p.max_parts_in_partition > 150, 20, 0))
          + if(coalesce(f.final_ratio, 0.0) > 0.20, 30, if(coalesce(f.final_ratio, 0.0) > 0.05, 15, 0))
          + if(coalesce(s.p95_ms, 0.0) > 2000, 20, if(coalesce(s.p95_ms, 0.0) > 500, 10, 0))
          + if(coalesce(pm.projection_attempt_rate_estimated, 1.0) < 0.20, 20, if(coalesce(pm.projection_attempt_rate_estimated, 1.0) < 0.50, 10, 0))
        ) AS priority_score
    FROM parts_metric AS p
    LEFT JOIN final_metric AS f
        ON p.db_name = f.db_name
       AND p.table_name = f.table_name
    LEFT JOIN slow_metric AS s
        ON p.db_name = s.db_name
       AND p.table_name = s.table_name
    LEFT JOIN projection_metric AS pm
        ON p.db_name = pm.db_name
       AND p.table_name = pm.table_name
)
SELECT
    countIf(priority_score >= 60) AS p0_count,
    countIf(priority_score >= 30 AND priority_score < 60) AS p1_count,
    countIf(priority_score < 30) AS p2_count
FROM score_board;
SQL
  if result_line="$(safe_query_data_line "${priority_count_sql}" "${summary_stderr_file}")"; then
    IFS=$'\t' read -r p0_count p1_count p2_count <<< "${result_line}"
  fi
  if ! [[ "${p0_count}" =~ ^[0-9]+$ ]]; then p0_count="0"; fi
  if ! [[ "${p1_count}" =~ ^[0-9]+$ ]]; then p1_count="0"; fi
  if ! [[ "${p2_count}" =~ ^[0-9]+$ ]]; then p2_count="0"; fi

  local final_ratio_sql
  read -r -d '' final_ratio_sql <<'SQL' || true
SELECT
    round(
        countIf(match(lowerUTF8(query), '(^|[^a-z0-9_])final([^a-z0-9_]|$)'))
        / nullIf(count(), 0),
        4
    ) AS final_ratio
FROM system.query_log
WHERE event_time >= now() - INTERVAL 24 HOUR
  AND type = 'QueryFinish'
  AND is_initial_query = 1
  AND match(query, '(?i)^\\s*(SELECT|WITH)\\b');
SQL
  if result_line="$(safe_query_data_line "${final_ratio_sql}" "${summary_stderr_file}")"; then
    final_ratio="${result_line}"
  fi

  local projection_global_sql
  read -r -d '' projection_global_sql <<'SQL' || true
SELECT
    round(
        countIf(match(lowerUTF8(query), 'optimize_use_projections\\s*=\\s*1'))
        / nullIf(count(), 0),
        4
    ) AS projection_attempt_rate_estimated
FROM system.query_log
WHERE event_time >= now() - INTERVAL 24 HOUR
  AND type = 'QueryFinish'
  AND is_initial_query = 1
  AND match(query, '(?i)^\\s*(SELECT|WITH)\\b');
SQL
  if result_line="$(safe_query_data_line "${projection_global_sql}" "${summary_stderr_file}")"; then
    projection_attempt_rate="${result_line}"
  fi

  local projection_table_count_sql
  read -r -d '' projection_table_count_sql <<'SQL' || true
SELECT count() AS projection_table_count
FROM system.tables
WHERE database = 'logs'
  AND positionCaseInsensitive(create_table_query, 'PROJECTION ') > 0;
SQL
  if result_line="$(safe_query_data_line "${projection_table_count_sql}" "${summary_stderr_file}")"; then
    projection_table_count="${result_line}"
  fi

  local part_hot_sql
  read -r -d '' part_hot_sql <<'SQL' || true
SELECT
    concat(database, '.', table) AS table_name,
    max(parts_in_partition) AS max_parts_in_partition
FROM
(
    SELECT
        database,
        table,
        partition,
        count() AS parts_in_partition
    FROM system.parts
    WHERE active
      AND database = 'logs'
    GROUP BY database, table, partition
)
GROUP BY database, table
ORDER BY max_parts_in_partition DESC
LIMIT 1;
SQL
  if result_line="$(safe_query_data_line "${part_hot_sql}" "${summary_stderr_file}")"; then
    IFS=$'\t' read -r part_hot_table part_hot_parts <<< "${result_line}"
  fi
  if ! [[ "${part_hot_parts}" =~ ^[0-9]+$ ]]; then part_hot_parts="0"; fi

  local slow_hot_table_sql
  read -r -d '' slow_hot_table_sql <<'SQL' || true
WITH logs_tables AS
(
    SELECT name AS table_name
    FROM system.tables
    WHERE database = 'logs'
),
query_table_refs AS
(
    SELECT
        extract(lowerUTF8(query), 'logs\\W+([a-z0-9_]+)') AS table_name,
        query_duration_ms
    FROM system.query_log
    WHERE event_time >= now() - INTERVAL 24 HOUR
      AND type = 'QueryFinish'
      AND is_initial_query = 1
      AND match(query, '(?i)^\\s*(SELECT|WITH)\\b')
      AND length(extract(lowerUTF8(query), 'logs\\W+([a-z0-9_]+)')) > 0
)
SELECT
    concat('logs.', q.table_name) AS table_name,
    round(quantile(0.95)(q.query_duration_ms), 2) AS p95_ms
FROM query_table_refs AS q
INNER JOIN logs_tables AS lt
    ON lt.table_name = q.table_name
GROUP BY q.table_name
ORDER BY p95_ms DESC
LIMIT 1;
SQL
  if result_line="$(safe_query_data_line "${slow_hot_table_sql}" "${summary_stderr_file}")"; then
    IFS=$'\t' read -r slow_hot_table slow_hot_p95_ms <<< "${result_line}"
  fi

  local -a recommendations=()
  if (( p0_count > 0 )) || (( part_hot_parts > 150 )); then
    recommendations+=("优先处理小 part 堆积：将写入批次增大（目标单批 10k+ 行）、评估按天/小时分区合理性，并针对 ${part_hot_table} 建立定时 OPTIMIZE/MERGE 治理策略。")
  fi
  if awk -v v="${final_ratio}" 'BEGIN { exit !(v + 0 > 0.05) }'; then
    recommendations+=("压降 FINAL 使用：将高频 FINAL 查询改写为去重视图或预聚合路径，重点关注 FINAL 占比高于 5% 的表，避免读放大。")
  fi
  if (( projection_table_count > 0 )) && awk -v v="${projection_attempt_rate}" 'BEGIN { exit !(v + 0 < 0.20) }'; then
    recommendations+=("提升 Projection 实际使用率：对已有 projection 的查询统一注入 'optimize_use_projections=1' 并校验执行计划，低命中表优先改造。")
  fi
  if awk -v v="${slow_hot_p95_ms}" 'BEGIN { exit !(v + 0 > 500) }'; then
    recommendations+=("针对慢表 ${slow_hot_table} 做定向优化：补充 PREWHERE/ORDER BY 对齐过滤条件，减少回表字段并评估是否新增物化聚合表。")
  fi

  local -a fallback_recommendations=(
    "保持写入与分区治理的常态化巡检：周维度跟踪 max_parts_in_partition 与 avg_rows_per_part，防止 part 指标反弹。"
    "建立 SQL 模板分级治理：对高频慢查询固定模板，持续跟踪 p95 与读放大（read_rows/read_bytes）变化。"
    "对核心查询链路设置变更门禁：新增查询先验证是否命中 projection/索引，再放量上线。"
  )
  local fallback_item
  for fallback_item in "${fallback_recommendations[@]}"; do
    if [[ "${#recommendations[@]}" -ge 3 ]]; then
      break
    fi
    recommendations+=("${fallback_item}")
  done

  {
    echo "## 结论区"
    echo
    echo "### 风险分级汇总"
    echo "- P0 表数量: ${p0_count}"
    echo "- P1 表数量: ${p1_count}"
    echo "- P2 表数量: ${p2_count}"
    echo "- 全局 FINAL 占比: ${final_ratio}"
    echo "- 全局 Projection 尝试率: ${projection_attempt_rate}"
    echo "- Part 压力最高表: ${part_hot_table} (max_parts_in_partition=${part_hot_parts})"
    echo "- 慢查询压力最高表: ${slow_hot_table} (p95_ms=${slow_hot_p95_ms})"
    echo
    echo "### 首要改造建议（Top 3）"
    echo "1. ${recommendations[0]}"
    echo "2. ${recommendations[1]}"
    echo "3. ${recommendations[2]}"
    echo
  } >> "${summary_file}"
}

generate_summary_markdown() {
  local report_file="$1"
  local profile_name="$2"
  local summary_file="$3"
  local summary_stderr_file="$4"
  local full_exit_code="${5:-}"
  local full_stderr_file="${6:-}"

  : > "${summary_stderr_file}"
  {
    echo "# ClickHouse Audit Summary"
    echo
    echo "- UTC Time: $(date -u '+%Y-%m-%d %H:%M:%S')"
    echo "- Database: \`${CH_DATABASE}\`"
    echo "- Run Mode: \`${RUN_MODE}\`"
    echo "- Audit Profile: \`${profile_name}\`"
    echo "- Source Report: \`${report_file}\`"
    echo "- Summary TopN: \`${SUMMARY_TOP_N}\`"
    echo "- Summary Stderr: \`${summary_stderr_file}\`"
    if [[ -n "${full_exit_code}" ]]; then
      echo "- Full Profile Status: \`FAILED\`"
      echo "- Full Profile Exit Code: \`${full_exit_code}\`"
      echo "- Full Profile Stderr: \`${full_stderr_file}\`"
    fi
    echo
  } > "${summary_file}"

  local part_risk_sql
  read -r -d '' part_risk_sql <<SQL || true
WITH part_partition AS
(
    SELECT
        database,
        table,
        partition,
        count() AS parts_in_partition
    FROM system.parts
    WHERE active
      AND database = 'logs'
    GROUP BY database, table, partition
),
part_table AS
(
    SELECT
        database,
        table,
        count() AS active_parts,
        uniqExact(partition) AS partitions,
        round(active_parts / nullIf(partitions, 0), 2) AS avg_parts_per_partition,
        round(sum(rows) / nullIf(active_parts, 0), 2) AS avg_rows_per_part
    FROM system.parts
    WHERE active
      AND database = 'logs'
    GROUP BY database, table
),
part_hot AS
(
    SELECT
        database,
        table,
        max(parts_in_partition) AS max_parts_in_partition
    FROM part_partition
    GROUP BY database, table
)
SELECT
    t.database,
    t.table,
    h.max_parts_in_partition,
    t.active_parts,
    t.partitions,
    t.avg_parts_per_partition,
    t.avg_rows_per_part,
    multiIf(h.max_parts_in_partition > 300, 'HIGH', h.max_parts_in_partition > 150, 'MEDIUM', 'LOW') AS risk_level
FROM part_table AS t
LEFT JOIN part_hot AS h
    ON t.database = h.database
   AND t.table = h.table
ORDER BY h.max_parts_in_partition DESC, t.active_parts DESC
LIMIT ${SUMMARY_TOP_N};
SQL
  append_summary_query_section "${summary_file}" "${summary_stderr_file}" "Part 风险（Top ${SUMMARY_TOP_N}）" "${part_risk_sql}" "_无 part 风险数据_"

  local projection_global_sql
  read -r -d '' projection_global_sql <<'SQL' || true
SELECT
    count() AS total_select_queries,
    countIf(match(lowerUTF8(query), 'optimize_use_projections\\s*=\\s*1')) AS projection_hint_queries,
    round(projection_hint_queries / nullIf(total_select_queries, 0), 4) AS projection_attempt_rate_estimated
FROM system.query_log
WHERE event_time >= now() - INTERVAL 24 HOUR
  AND type = 'QueryFinish'
  AND is_initial_query = 1
  AND match(query, '(?i)^\\s*(SELECT|WITH)\\b');
SQL
  append_summary_query_section "${summary_file}" "${summary_stderr_file}" "Projection 尝试率（全局）" "${projection_global_sql}" "_无 projection 尝试率数据_"

  local projection_table_sql
  read -r -d '' projection_table_sql <<SQL || true
WITH projection_tables AS
(
    SELECT name AS table_name
    FROM system.tables
    WHERE database = 'logs'
      AND positionCaseInsensitive(create_table_query, 'PROJECTION ') > 0
),
query_rows AS
(
    SELECT
        extract(lowerUTF8(query), 'logs\\W+([a-z0-9_]+)') AS table_name,
        query_duration_ms,
        match(lowerUTF8(query), 'optimize_use_projections\\s*=\\s*1') AS has_projection_hint
    FROM system.query_log
    WHERE event_time >= now() - INTERVAL 24 HOUR
      AND type = 'QueryFinish'
      AND is_initial_query = 1
      AND match(query, '(?i)^\\s*(SELECT|WITH)\\b')
      AND length(extract(lowerUTF8(query), 'logs\\W+([a-z0-9_]+)')) > 0
)
SELECT
    'logs' AS database,
    q.table_name AS table,
    count() AS select_queries,
    countIf(q.has_projection_hint) AS projection_hint_queries,
    round(projection_hint_queries / nullIf(select_queries, 0), 4) AS projection_attempt_rate_estimated,
    round(quantile(0.95)(q.query_duration_ms), 2) AS p95_ms
FROM query_rows AS q
INNER JOIN projection_tables AS p
    ON p.table_name = q.table_name
GROUP BY q.table_name
ORDER BY projection_attempt_rate_estimated ASC, p95_ms DESC
LIMIT ${SUMMARY_TOP_N};
SQL
  append_summary_query_section "${summary_file}" "${summary_stderr_file}" "Projection 尝试率（按表 Top ${SUMMARY_TOP_N}）" "${projection_table_sql}" "_无定义 projection 的表或无相关查询_"

  local slow_sql_top_sql
  read -r -d '' slow_sql_top_sql <<SQL || true
SELECT
    cityHash64(replaceRegexpAll(lowerUTF8(query), '\\s+', ' ')) AS query_fingerprint,
    any(replaceRegexpAll(substring(query, 1, 140), '\\s+', ' ')) AS sample_query,
    count() AS executions,
    round(avg(query_duration_ms), 2) AS avg_ms,
    round(quantile(0.95)(query_duration_ms), 2) AS p95_ms,
    round(sum(query_duration_ms) / 1000, 2) AS total_exec_seconds
FROM system.query_log
WHERE event_time >= now() - INTERVAL 24 HOUR
  AND type = 'QueryFinish'
  AND is_initial_query = 1
  AND match(query, '(?i)^\\s*(SELECT|WITH)\\b')
GROUP BY query_fingerprint
HAVING executions >= 1
ORDER BY p95_ms DESC, executions DESC
LIMIT ${SUMMARY_TOP_N};
SQL
  append_summary_query_section "${summary_file}" "${summary_stderr_file}" "慢 SQL TopN（按 p95）" "${slow_sql_top_sql}" "_无慢 SQL TopN 数据_"

  local final_global_sql
  read -r -d '' final_global_sql <<'SQL' || true
SELECT
    count() AS total_select_queries,
    countIf(match(lowerUTF8(query), '(^|[^a-z0-9_])final([^a-z0-9_]|$)')) AS final_select_queries,
    round(final_select_queries / nullIf(total_select_queries, 0), 4) AS final_ratio
FROM system.query_log
WHERE event_time >= now() - INTERVAL 24 HOUR
  AND type = 'QueryFinish'
  AND is_initial_query = 1
  AND match(query, '(?i)^\\s*(SELECT|WITH)\\b');
SQL
  append_summary_query_section "${summary_file}" "${summary_stderr_file}" "FINAL 占比（全局）" "${final_global_sql}" "_无 FINAL 占比数据_"

  local final_table_sql
  read -r -d '' final_table_sql <<SQL || true
WITH logs_tables AS
(
    SELECT name AS table_name
    FROM system.tables
    WHERE database = 'logs'
),
query_table_refs AS
(
    SELECT
        extract(lowerUTF8(query), 'logs\\W+([a-z0-9_]+)') AS table_name,
        query_duration_ms,
        match(lowerUTF8(query), '(^|[^a-z0-9_])final([^a-z0-9_]|$)') AS has_final
    FROM system.query_log
    WHERE event_time >= now() - INTERVAL 24 HOUR
      AND type = 'QueryFinish'
      AND is_initial_query = 1
      AND match(query, '(?i)^\\s*(SELECT|WITH)\\b')
      AND length(extract(lowerUTF8(query), 'logs\\W+([a-z0-9_]+)')) > 0
)
SELECT
    'logs' AS database,
    q.table_name AS table,
    count() AS select_queries,
    countIf(q.has_final) AS final_queries,
    round(final_queries / nullIf(select_queries, 0), 4) AS final_ratio,
    round(quantile(0.95)(q.query_duration_ms), 2) AS p95_ms
FROM query_table_refs AS q
INNER JOIN logs_tables AS lt
    ON lt.table_name = q.table_name
GROUP BY q.table_name
ORDER BY final_ratio DESC, p95_ms DESC
LIMIT ${SUMMARY_TOP_N};
SQL
  append_summary_query_section "${summary_file}" "${summary_stderr_file}" "FINAL 占比（按表 Top ${SUMMARY_TOP_N}）" "${final_table_sql}" "_无按表 FINAL 占比数据_"

  local priority_sql
  read -r -d '' priority_sql <<SQL || true
WITH parts_metric AS
(
    SELECT
        database AS db_name,
        table AS table_name,
        max(parts_in_partition) AS max_parts_in_partition
    FROM
    (
        SELECT
            database,
            table,
            partition,
            count() AS parts_in_partition
        FROM system.parts
        WHERE active
          AND database = 'logs'
        GROUP BY database, table, partition
    )
    GROUP BY db_name, table_name
),
logs_tables AS
(
    SELECT name AS table_name
    FROM system.tables
    WHERE database = 'logs'
),
query_table_refs AS
(
    SELECT
        extract(lowerUTF8(query), 'logs\\W+([a-z0-9_]+)') AS table_name,
        query_duration_ms,
        match(lowerUTF8(query), '(^|[^a-z0-9_])final([^a-z0-9_]|$)') AS has_final,
        match(lowerUTF8(query), 'optimize_use_projections\\s*=\\s*1') AS has_projection_hint
    FROM system.query_log
    WHERE event_time >= now() - INTERVAL 24 HOUR
      AND type = 'QueryFinish'
      AND is_initial_query = 1
      AND match(query, '(?i)^\\s*(SELECT|WITH)\\b')
      AND length(extract(lowerUTF8(query), 'logs\\W+([a-z0-9_]+)')) > 0
),
final_metric AS
(
    SELECT
        'logs' AS db_name,
        q.table_name AS table_name,
        round(
            countIf(q.has_final) / nullIf(count(), 0),
            4
        ) AS final_ratio
    FROM query_table_refs AS q
    INNER JOIN logs_tables AS lt
        ON lt.table_name = q.table_name
    GROUP BY q.table_name
),
slow_metric AS
(
    SELECT
        'logs' AS db_name,
        q.table_name AS table_name,
        round(quantile(0.95)(q.query_duration_ms), 2) AS p95_ms
    FROM query_table_refs AS q
    INNER JOIN logs_tables AS lt
        ON lt.table_name = q.table_name
    GROUP BY q.table_name
),
projection_metric AS
(
    WITH projection_tables AS
    (
        SELECT name AS table_name
        FROM system.tables
        WHERE database = 'logs'
          AND positionCaseInsensitive(create_table_query, 'PROJECTION ') > 0
    )
    SELECT
        'logs' AS db_name,
        q.table_name AS table_name,
        round(
            countIf(q.has_projection_hint)
            / nullIf(count(), 0),
            4
        ) AS projection_attempt_rate_estimated
    FROM query_table_refs AS q
    INNER JOIN projection_tables AS p
        ON p.table_name = q.table_name
    GROUP BY q.table_name
),
score_board AS
(
    SELECT
        p.db_name AS db_name,
        p.table_name AS table_name,
        p.max_parts_in_partition,
        coalesce(f.final_ratio, 0.0) AS final_ratio,
        coalesce(s.p95_ms, 0.0) AS p95_ms,
        coalesce(pm.projection_attempt_rate_estimated, 1.0) AS projection_attempt_rate_estimated,
        (
            if(p.max_parts_in_partition > 300, 40, if(p.max_parts_in_partition > 150, 20, 0))
          + if(coalesce(f.final_ratio, 0.0) > 0.20, 30, if(coalesce(f.final_ratio, 0.0) > 0.05, 15, 0))
          + if(coalesce(s.p95_ms, 0.0) > 2000, 20, if(coalesce(s.p95_ms, 0.0) > 500, 10, 0))
          + if(coalesce(pm.projection_attempt_rate_estimated, 1.0) < 0.20, 20, if(coalesce(pm.projection_attempt_rate_estimated, 1.0) < 0.50, 10, 0))
        ) AS priority_score
    FROM parts_metric AS p
    LEFT JOIN final_metric AS f
        ON p.db_name = f.db_name
       AND p.table_name = f.table_name
    LEFT JOIN slow_metric AS s
        ON p.db_name = s.db_name
       AND p.table_name = s.table_name
    LEFT JOIN projection_metric AS pm
        ON p.db_name = pm.db_name
       AND p.table_name = pm.table_name
)
SELECT
    sb.db_name AS database,
    sb.table_name AS table,
    max_parts_in_partition,
    final_ratio,
    p95_ms,
    projection_attempt_rate_estimated,
    priority_score,
    multiIf(priority_score >= 60, 'P0', priority_score >= 30, 'P1', 'P2') AS priority_level
FROM score_board AS sb
ORDER BY priority_score DESC, p95_ms DESC, max_parts_in_partition DESC
LIMIT ${SUMMARY_TOP_N};
SQL
  append_summary_query_section "${summary_file}" "${summary_stderr_file}" "优化优先级（Top ${SUMMARY_TOP_N}）" "${priority_sql}" "_无优先级数据_"
  append_summary_conclusion_section "${summary_file}" "${summary_stderr_file}"
}

summary_file_path_from_report() {
  local report_file="$1"
  echo "${report_file%.txt}.summary.md"
}

summary_stderr_path_from_report() {
  local report_file="$1"
  echo "${report_file%.txt}.summary.stderr.log"
}

run_audit_with_file() {
  local sql_file="$1"
  local profile_name="$2"
  local report_file="$3"
  local stderr_file="$4"
  local full_exit_code="${5:-}"
  local full_stderr_file="${6:-}"

  : > "${stderr_file}"
  print_report_header "${report_file}" "${sql_file}" "${profile_name}" "${stderr_file}"
  append_full_failure_summary "${report_file}" "${full_exit_code}" "${full_stderr_file}"

  run_mode_with_sql_file "${sql_file}" 2>> "${stderr_file}" | tee -a "${report_file}"
}

if [[ "${AUDIT_PROFILE}" == "minimal" ]]; then
  MINIMAL_SQL_FILE="$(build_minimal_sql)"
  trap 'rm -f "${MINIMAL_SQL_FILE:-}"' EXIT
  if run_audit_with_file "${MINIMAL_SQL_FILE}" "minimal" "${OUT_FILE}" "${FULL_STDERR_FILE}"; then
    SUMMARY_FILE="$(summary_file_path_from_report "${OUT_FILE}")"
    SUMMARY_STDERR_FILE="$(summary_stderr_path_from_report "${OUT_FILE}")"
    generate_summary_markdown "${OUT_FILE}" "minimal" "${SUMMARY_FILE}" "${SUMMARY_STDERR_FILE}"
    echo
    echo "Saved audit report: ${OUT_FILE}"
    echo "Saved summary: ${SUMMARY_FILE}"
    if [[ -s "${SUMMARY_STDERR_FILE}" ]]; then
      echo "Saved summary stderr log (non-empty): ${SUMMARY_STDERR_FILE}"
    else
      rm -f "${SUMMARY_STDERR_FILE}"
    fi
    if [[ -s "${FULL_STDERR_FILE}" ]]; then
      echo "Saved stderr log (non-empty): ${FULL_STDERR_FILE}"
    else
      rm -f "${FULL_STDERR_FILE}"
    fi
    exit 0
  else
    MINIMAL_EXIT_CODE="$?"
    echo "ERROR: minimal audit failed." >&2
    echo "Exit code: ${MINIMAL_EXIT_CODE}" >&2
    echo "Report: ${OUT_FILE}" >&2
    echo "Stderr log: ${FULL_STDERR_FILE}" >&2
    if [[ -s "${FULL_STDERR_FILE}" ]]; then
      echo "Error summary (first ${ERROR_SUMMARY_LINES} lines):" >&2
      sed -n "1,${ERROR_SUMMARY_LINES}p" "${FULL_STDERR_FILE}" >&2
    fi
    exit "${MINIMAL_EXIT_CODE}"
  fi
fi

if run_audit_with_file "${SQL_FILE}" "full" "${OUT_FILE}" "${FULL_STDERR_FILE}"; then
  SUMMARY_FILE="$(summary_file_path_from_report "${OUT_FILE}")"
  SUMMARY_STDERR_FILE="$(summary_stderr_path_from_report "${OUT_FILE}")"
  generate_summary_markdown "${OUT_FILE}" "full" "${SUMMARY_FILE}" "${SUMMARY_STDERR_FILE}"
  echo
  echo "Saved audit report: ${OUT_FILE}"
  echo "Saved summary: ${SUMMARY_FILE}"
  if [[ -s "${SUMMARY_STDERR_FILE}" ]]; then
    echo "Saved summary stderr log (non-empty): ${SUMMARY_STDERR_FILE}"
  else
    rm -f "${SUMMARY_STDERR_FILE}"
  fi
  if [[ -s "${FULL_STDERR_FILE}" ]]; then
    echo "Saved stderr log (non-empty): ${FULL_STDERR_FILE}"
  else
    rm -f "${FULL_STDERR_FILE}"
  fi
  exit 0
else
  FULL_EXIT_CODE="$?"
fi
write_failure_summary_file "${FAILURE_SUMMARY_FILE}" "${OUT_FILE}" "${FULL_EXIT_CODE}" "${FULL_STDERR_FILE}"

if [[ "${AUTO_FALLBACK_TO_MINIMAL}" != "1" ]]; then
  echo "ERROR: full audit failed and fallback disabled (CLICKHOUSE_AUDIT_AUTO_FALLBACK_TO_MINIMAL=${AUTO_FALLBACK_TO_MINIMAL})" >&2
  echo "Exit code: ${FULL_EXIT_CODE}" >&2
  echo "Full report: ${OUT_FILE}" >&2
  echo "Failure summary: ${FAILURE_SUMMARY_FILE}" >&2
  echo "Stderr log: ${FULL_STDERR_FILE}" >&2
  exit "${FULL_EXIT_CODE}"
fi

echo >&2
echo "WARN: full audit failed, fallback to minimal compatibility profile..." >&2
MINIMAL_SQL_FILE="$(build_minimal_sql)"
trap 'rm -f "${MINIMAL_SQL_FILE:-}"' EXIT
OUT_FILE="${REPORT_DIR}/clickhouse-online-audit-${TS}-minimal-fallback.txt"
if run_audit_with_file "${MINIMAL_SQL_FILE}" "minimal-fallback" "${OUT_FILE}" "${FALLBACK_STDERR_FILE}" "${FULL_EXIT_CODE}" "${FULL_STDERR_FILE}"; then
  SUMMARY_FILE="$(summary_file_path_from_report "${OUT_FILE}")"
  SUMMARY_STDERR_FILE="$(summary_stderr_path_from_report "${OUT_FILE}")"
  generate_summary_markdown "${OUT_FILE}" "minimal-fallback" "${SUMMARY_FILE}" "${SUMMARY_STDERR_FILE}" "${FULL_EXIT_CODE}" "${FULL_STDERR_FILE}"
  :
else
  FALLBACK_EXIT_CODE="$?"
  echo "ERROR: fallback minimal profile also failed." >&2
  echo "Fallback report: ${OUT_FILE}" >&2
  echo "Fallback stderr log: ${FALLBACK_STDERR_FILE}" >&2
  exit "${FALLBACK_EXIT_CODE}"
fi

echo >&2
echo "WARN: full profile failed; generated fallback report instead." >&2
echo "Saved audit report: ${OUT_FILE}"
echo "Saved summary: ${SUMMARY_FILE}"
if [[ -s "${SUMMARY_STDERR_FILE}" ]]; then
  echo "Saved summary stderr log (non-empty): ${SUMMARY_STDERR_FILE}"
else
  rm -f "${SUMMARY_STDERR_FILE}"
fi
echo "Saved full failure summary: ${FAILURE_SUMMARY_FILE}"
echo "Saved full stderr log: ${FULL_STDERR_FILE}"
if [[ -s "${FALLBACK_STDERR_FILE}" ]]; then
  echo "Saved fallback stderr log (non-empty): ${FALLBACK_STDERR_FILE}"
else
  rm -f "${FALLBACK_STDERR_FILE}"
fi

exit 0
