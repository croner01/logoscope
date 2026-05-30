-- ClickHouse 线上审计 SQL 清单（DBA 视角）
-- 目标：一键评估优化优先级
-- 覆盖：
-- 1) part 数风险
-- 2) projection 命中率（估算）
-- 3) 慢 SQL
-- 4) FINAL 占比
--
-- 使用方式（建议在 ClickHouse 节点执行）:
-- clickhouse-client --multiquery < scripts/clickhouse-online-audit.sql
--
-- 可选：仅看业务库 logs（当前脚本默认已优先聚焦 logs）
-- 注意：query_log 相关查询依赖线上开启 log_queries（脚本第 0 节会检查）


-- =============================================================================
-- 0) 基础环境与日志可观测性检查
-- =============================================================================
SELECT '=== SECTION 0: ENVIRONMENT ===' AS section;
SELECT version() AS clickhouse_version, now() AS audit_time_utc;

SELECT
    name,
    value,
    changed
FROM system.settings
WHERE name IN (
    'log_queries',
    'log_query_threads',
    'log_queries_probability',
    'query_log_flush_interval_milliseconds'
)
ORDER BY name;

SELECT
    name,
    type
FROM system.columns
WHERE database = 'system'
  AND table = 'query_log'
  AND name IN (
      'event_time',
      'query_duration_ms',
      'query',
      'read_rows',
      'read_bytes',
      'memory_usage',
      'ProfileEvents',
      'databases',
      'tables',
      'query_id',
      'is_initial_query',
      'type'
  )
ORDER BY name;


-- =============================================================================
-- 1) Part 风险审计（核心）
-- 阈值参考：
-- - 单分区 active parts > 300：高风险
-- - 单分区 active parts > 150：中风险
-- =============================================================================
SELECT '=== SECTION 1: PART AUDIT ===' AS section;

-- 1.1 每表总体 part 概况
SELECT
    database,
    table,
    count() AS active_parts,
    uniqExact(partition) AS partitions,
    round(active_parts / nullIf(partitions, 0), 2) AS avg_parts_per_partition,
    sum(rows) AS total_rows,
    formatReadableSize(sum(bytes_on_disk)) AS total_bytes,
    round(sum(rows) / nullIf(active_parts, 0), 2) AS avg_rows_per_part
FROM system.parts
WHERE active
  AND database = 'logs'
GROUP BY database, table
ORDER BY active_parts DESC, total_rows DESC;

-- 1.2 分区热点（重点看 active_parts_in_partition）
SELECT
    database,
    table,
    partition,
    count() AS active_parts_in_partition,
    sum(rows) AS rows_in_partition,
    formatReadableSize(sum(bytes_on_disk)) AS bytes_in_partition,
    min(modification_time) AS oldest_part_time,
    max(modification_time) AS newest_part_time
FROM system.parts
WHERE active
  AND database = 'logs'
GROUP BY database, table, partition
HAVING active_parts_in_partition >= 100
ORDER BY active_parts_in_partition DESC, rows_in_partition DESC
LIMIT 300;

-- 1.3 小 part 比例（近 24h）
SELECT
    database,
    table,
    count() AS new_parts_24h,
    countIf(rows < 1000) AS tiny_parts_lt_1k,
    countIf(rows < 10000) AS small_parts_lt_10k,
    round(tiny_parts_lt_1k / nullIf(new_parts_24h, 0), 4) AS tiny_part_ratio,
    round(small_parts_lt_10k / nullIf(new_parts_24h, 0), 4) AS small_part_ratio,
    round(avg(rows), 2) AS avg_rows_per_new_part
FROM system.parts
WHERE active
  AND database = 'logs'
  AND modification_time >= now() - INTERVAL 24 HOUR
GROUP BY database, table
ORDER BY tiny_part_ratio DESC, new_parts_24h DESC;


-- =============================================================================
-- 2) Projection 使用率审计（兼容版估算）
-- 说明：
-- - 为兼容不同版本 query_log，避免依赖 ProfileEvents map 字段。
-- - 这里统计的是 projection 尝试率（query 中显式带 optimize_use_projections=1）。
-- =============================================================================
SELECT '=== SECTION 2: PROJECTION ATTEMPT AUDIT ===' AS section;

-- 2.1 当前定义了 projection 的表（通过 create_table_query 识别）
SELECT
    database,
    name AS table,
    engine
FROM system.tables
WHERE database = 'logs'
  AND positionCaseInsensitive(create_table_query, 'PROJECTION ') > 0
ORDER BY database, table;

-- 2.2 全局 projection 尝试率（Select/With 查询）
SELECT
    count() AS total_select_queries,
    countIf(match(lowerUTF8(query), 'optimize_use_projections\\s*=\\s*1')) AS select_queries_with_projection_hint,
    round(
        select_queries_with_projection_hint / nullIf(total_select_queries, 0),
        4
    ) AS projection_attempt_rate_estimated
FROM system.query_log
WHERE event_time >= now() - INTERVAL 24 HOUR
  AND type = 'QueryFinish'
  AND is_initial_query = 1
  AND match(query, '(?i)^\\s*(SELECT|WITH)\\b');

-- 2.3 按“定义了 projection 的表”统计 projection 尝试率
WITH projection_tables AS
(
    SELECT
        database,
        name AS table
    FROM system.tables
    WHERE database = 'logs'
      AND positionCaseInsensitive(create_table_query, 'PROJECTION ') > 0
),
query_rows AS
(
    SELECT
        query_id,
        databases,
        tables,
        query_duration_ms,
        match(lowerUTF8(query), 'optimize_use_projections\\s*=\\s*1') AS has_projection_hint
    FROM system.query_log
    WHERE event_time >= now() - INTERVAL 24 HOUR
      AND type = 'QueryFinish'
      AND is_initial_query = 1
      AND match(query, '(?i)^\\s*(SELECT|WITH)\\b')
)
SELECT
    z.1 AS database,
    z.2 AS table,
    count() AS select_queries,
    countIf(q.has_projection_hint) AS queries_with_projection_hint,
    round(queries_with_projection_hint / nullIf(select_queries, 0), 4) AS projection_attempt_rate_estimated,
    round(avg(q.query_duration_ms), 2) AS avg_ms,
    round(quantile(0.95)(q.query_duration_ms), 2) AS p95_ms
FROM query_rows AS q
ARRAY JOIN arrayZip(
    arraySlice(q.databases, 1, least(length(q.databases), length(q.tables))),
    arraySlice(q.tables, 1, least(length(q.databases), length(q.tables)))
) AS z
INNER JOIN projection_tables AS p
    ON p.database = z.1
   AND p.table = z.2
GROUP BY z.1, z.2
ORDER BY projection_attempt_rate_estimated ASC, p95_ms DESC;


-- =============================================================================
-- 3) 慢 SQL 审计
-- =============================================================================
SELECT '=== SECTION 3: SLOW QUERY AUDIT ===' AS section;

-- 3.1 Top 慢模板（按 p95）
SELECT
    cityHash64(replaceRegexpAll(lowerUTF8(query), '\\s+', ' ')) AS query_fingerprint,
    any(replaceRegexpAll(substring(query, 1, 240), '\\s+', ' ')) AS sample_query,
    count() AS executions,
    round(avg(query_duration_ms), 2) AS avg_ms,
    round(quantile(0.95)(query_duration_ms), 2) AS p95_ms,
    round(max(query_duration_ms), 2) AS max_ms,
    round(avg(read_rows), 2) AS avg_read_rows,
    round(avg(read_bytes) / 1024 / 1024, 2) AS avg_read_mb,
    round(avg(memory_usage) / 1024 / 1024, 2) AS avg_mem_mb
FROM system.query_log
WHERE event_time >= now() - INTERVAL 24 HOUR
  AND type = 'QueryFinish'
  AND is_initial_query = 1
  AND match(query, '(?i)^\\s*(SELECT|WITH)\\b')
GROUP BY query_fingerprint
HAVING executions >= 3
ORDER BY p95_ms DESC, executions DESC
LIMIT 50;

-- 3.2 Top 资源消耗 SQL（按总耗时）
SELECT
    cityHash64(replaceRegexpAll(lowerUTF8(query), '\\s+', ' ')) AS query_fingerprint,
    any(replaceRegexpAll(substring(query, 1, 240), '\\s+', ' ')) AS sample_query,
    count() AS executions,
    round(sum(query_duration_ms) / 1000, 2) AS total_exec_seconds,
    round(avg(query_duration_ms), 2) AS avg_ms,
    round(sum(read_rows), 2) AS total_read_rows,
    round(sum(read_bytes) / 1024 / 1024 / 1024, 2) AS total_read_gb
FROM system.query_log
WHERE event_time >= now() - INTERVAL 24 HOUR
  AND type = 'QueryFinish'
  AND is_initial_query = 1
GROUP BY query_fingerprint
ORDER BY total_exec_seconds DESC
LIMIT 50;

-- 3.3 慢 SQL 按表分布（用于定位重灾区表）
SELECT
    z.1 AS database,
    z.2 AS table,
    count() AS executions,
    round(avg(query_duration_ms), 2) AS avg_ms,
    round(quantile(0.95)(query_duration_ms), 2) AS p95_ms,
    round(sum(query_duration_ms) / 1000, 2) AS total_exec_seconds
FROM system.query_log
ARRAY JOIN arrayZip(
    arraySlice(databases, 1, least(length(databases), length(tables))),
    arraySlice(tables, 1, least(length(databases), length(tables)))
) AS z
WHERE event_time >= now() - INTERVAL 24 HOUR
  AND type = 'QueryFinish'
  AND is_initial_query = 1
  AND z.1 = 'logs'
GROUP BY z.1, z.2
ORDER BY p95_ms DESC, total_exec_seconds DESC
LIMIT 50;


-- =============================================================================
-- 4) FINAL 占比审计
-- =============================================================================
SELECT '=== SECTION 4: FINAL RATIO AUDIT ===' AS section;

-- 4.1 全局 FINAL 占比
SELECT
    count() AS total_select_queries,
    countIf(match(lowerUTF8(query), '(^|[^a-z0-9_])final([^a-z0-9_]|$)')) AS final_select_queries,
    round(final_select_queries / nullIf(total_select_queries, 0), 4) AS final_ratio
FROM system.query_log
WHERE event_time >= now() - INTERVAL 24 HOUR
  AND type = 'QueryFinish'
  AND is_initial_query = 1
  AND match(query, '(?i)^\\s*(SELECT|WITH)\\b');

-- 4.2 各表 FINAL 占比
SELECT
    z.1 AS database,
    z.2 AS table,
    count() AS select_queries,
    countIf(match(lowerUTF8(query), '(^|[^a-z0-9_])final([^a-z0-9_]|$)')) AS final_queries,
    round(final_queries / nullIf(select_queries, 0), 4) AS final_ratio,
    round(quantile(0.95)(query_duration_ms), 2) AS p95_ms
FROM system.query_log
ARRAY JOIN arrayZip(
    arraySlice(databases, 1, least(length(databases), length(tables))),
    arraySlice(tables, 1, least(length(databases), length(tables)))
) AS z
WHERE event_time >= now() - INTERVAL 24 HOUR
  AND type = 'QueryFinish'
  AND is_initial_query = 1
  AND z.1 = 'logs'
  AND match(query, '(?i)^\\s*(SELECT|WITH)\\b')
GROUP BY z.1, z.2
ORDER BY final_ratio DESC, p95_ms DESC;

-- 4.3 FINAL 慢查询模板（优先替换）
SELECT
    cityHash64(replaceRegexpAll(lowerUTF8(query), '\\s+', ' ')) AS query_fingerprint,
    any(replaceRegexpAll(substring(query, 1, 260), '\\s+', ' ')) AS sample_query,
    count() AS executions,
    round(avg(query_duration_ms), 2) AS avg_ms,
    round(quantile(0.95)(query_duration_ms), 2) AS p95_ms,
    round(avg(read_rows), 2) AS avg_read_rows
FROM system.query_log
WHERE event_time >= now() - INTERVAL 24 HOUR
  AND type = 'QueryFinish'
  AND is_initial_query = 1
  AND match(lowerUTF8(query), '(^|[^a-z0-9_])final([^a-z0-9_]|$)')
GROUP BY query_fingerprint
ORDER BY p95_ms DESC, executions DESC
LIMIT 50;


-- =============================================================================
-- 5) 一键优先级评分（用于排期）
-- 评分逻辑（可调）：
-- - 分区 part 压力：最高 40 分
-- - FINAL 占比：最高 30 分
-- - 慢查询 p95：最高 20 分
-- - projection 定义但命中偏低：最高 20 分
-- =============================================================================
SELECT '=== SECTION 5: PRIORITY SCOREBOARD ===' AS section;
WITH parts_metric AS
(
    SELECT
        database,
        table,
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
),
final_metric AS
(
    SELECT
        z.1 AS database,
        z.2 AS table,
        round(
            countIf(match(lowerUTF8(query), '(^|[^a-z0-9_])final([^a-z0-9_]|$)')) / nullIf(count(), 0),
            4
        ) AS final_ratio
    FROM system.query_log
    ARRAY JOIN arrayZip(
        arraySlice(databases, 1, least(length(databases), length(tables))),
        arraySlice(tables, 1, least(length(databases), length(tables)))
    ) AS z
    WHERE event_time >= now() - INTERVAL 24 HOUR
      AND type = 'QueryFinish'
      AND is_initial_query = 1
      AND z.1 = 'logs'
      AND match(query, '(?i)^\\s*(SELECT|WITH)\\b')
    GROUP BY z.1, z.2
),
slow_metric AS
(
    SELECT
        z.1 AS database,
        z.2 AS table,
        round(quantile(0.95)(query_duration_ms), 2) AS p95_ms
    FROM system.query_log
    ARRAY JOIN arrayZip(
        arraySlice(databases, 1, least(length(databases), length(tables))),
        arraySlice(tables, 1, least(length(databases), length(tables)))
    ) AS z
    WHERE event_time >= now() - INTERVAL 24 HOUR
      AND type = 'QueryFinish'
      AND is_initial_query = 1
      AND z.1 = 'logs'
    GROUP BY z.1, z.2
),
projection_metric AS
(
    WITH projection_tables AS
    (
        SELECT
            database,
            name AS table
        FROM system.tables
        WHERE database = 'logs'
          AND positionCaseInsensitive(create_table_query, 'PROJECTION ') > 0
    )
    SELECT
        z.1 AS database,
        z.2 AS table,
        round(
            countIf(match(lowerUTF8(query), 'optimize_use_projections\\s*=\\s*1'))
            / nullIf(count(), 0),
            4
        ) AS projection_attempt_rate_estimated
    FROM system.query_log
    ARRAY JOIN arrayZip(
        arraySlice(databases, 1, least(length(databases), length(tables))),
        arraySlice(tables, 1, least(length(databases), length(tables)))
    ) AS z
    INNER JOIN projection_tables AS p
        ON p.database = z.1
       AND p.table = z.2
    WHERE event_time >= now() - INTERVAL 24 HOUR
      AND type = 'QueryFinish'
      AND is_initial_query = 1
      AND match(query, '(?i)^\\s*(SELECT|WITH)\\b')
    GROUP BY z.1, z.2
)
SELECT
    p.database,
    p.table,
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
    ON p.database = f.database
   AND p.table = f.table
LEFT JOIN slow_metric AS s
    ON p.database = s.database
   AND p.table = s.table
LEFT JOIN projection_metric AS pm
    ON p.database = pm.database
   AND p.table = pm.table
ORDER BY priority_score DESC, p95_ms DESC, max_parts_in_partition DESC;


-- =============================================================================
-- 6) 自动结论（P0/P1/P2）与动作建议
-- 口径：
-- - P0: priority_score >= 60
-- - P1: 30 <= priority_score < 60
-- - P2: priority_score < 30
-- =============================================================================
SELECT '=== SECTION 6: ACTIONABLE RECOMMENDATIONS ===' AS section;
WITH parts_metric AS
(
    SELECT
        database,
        table,
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
),
final_metric AS
(
    SELECT
        z.1 AS database,
        z.2 AS table,
        round(
            countIf(match(lowerUTF8(query), '(^|[^a-z0-9_])final([^a-z0-9_]|$)')) / nullIf(count(), 0),
            4
        ) AS final_ratio
    FROM system.query_log
    ARRAY JOIN arrayZip(
        arraySlice(databases, 1, least(length(databases), length(tables))),
        arraySlice(tables, 1, least(length(databases), length(tables)))
    ) AS z
    WHERE event_time >= now() - INTERVAL 24 HOUR
      AND type = 'QueryFinish'
      AND is_initial_query = 1
      AND z.1 = 'logs'
      AND match(query, '(?i)^\\s*(SELECT|WITH)\\b')
    GROUP BY z.1, z.2
),
slow_metric AS
(
    SELECT
        z.1 AS database,
        z.2 AS table,
        round(quantile(0.95)(query_duration_ms), 2) AS p95_ms
    FROM system.query_log
    ARRAY JOIN arrayZip(
        arraySlice(databases, 1, least(length(databases), length(tables))),
        arraySlice(tables, 1, least(length(databases), length(tables)))
    ) AS z
    WHERE event_time >= now() - INTERVAL 24 HOUR
      AND type = 'QueryFinish'
      AND is_initial_query = 1
      AND z.1 = 'logs'
    GROUP BY z.1, z.2
),
projection_metric AS
(
    WITH projection_tables AS
    (
        SELECT
            database,
            name AS table
        FROM system.tables
        WHERE database = 'logs'
          AND positionCaseInsensitive(create_table_query, 'PROJECTION ') > 0
    )
    SELECT
        z.1 AS database,
        z.2 AS table,
        round(
            countIf(match(lowerUTF8(query), 'optimize_use_projections\\s*=\\s*1'))
            / nullIf(count(), 0),
            4
        ) AS projection_attempt_rate_estimated
    FROM system.query_log
    ARRAY JOIN arrayZip(
        arraySlice(databases, 1, least(length(databases), length(tables))),
        arraySlice(tables, 1, least(length(databases), length(tables)))
    ) AS z
    INNER JOIN projection_tables AS p
        ON p.database = z.1
       AND p.table = z.2
    WHERE event_time >= now() - INTERVAL 24 HOUR
      AND type = 'QueryFinish'
      AND is_initial_query = 1
      AND match(query, '(?i)^\\s*(SELECT|WITH)\\b')
    GROUP BY z.1, z.2
),
score_board AS
(
    SELECT
        p.database AS database_name,
        p.table AS table_name,
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
        ON p.database = f.database
       AND p.table = f.table
    LEFT JOIN slow_metric AS s
        ON p.database = s.database
       AND p.table = s.table
    LEFT JOIN projection_metric AS pm
        ON p.database = pm.database
       AND p.table = pm.table
)
SELECT
    database_name AS database,
    table_name AS table,
    priority_score,
    if(priority_score >= 60, 'P0', if(priority_score >= 30, 'P1', 'P2')) AS priority_level,
    max_parts_in_partition,
    final_ratio,
    p95_ms,
    projection_attempt_rate_estimated,
    arrayStringConcat(
        arrayFilter(x -> length(x) > 0, [
            if(max_parts_in_partition > 300, 'part_pressure:critical(>300)', if(max_parts_in_partition > 150, 'part_pressure:high(>150)', '')),
            if(final_ratio > 0.20, 'final_ratio:critical(>20%)', if(final_ratio > 0.05, 'final_ratio:high(>5%)', '')),
            if(p95_ms > 2000, 'latency:critical(p95>2000ms)', if(p95_ms > 500, 'latency:high(p95>500ms)', '')),
            if(projection_attempt_rate_estimated < 0.20, 'projection_attempt:low(<20%)', if(projection_attempt_rate_estimated < 0.50, 'projection_attempt:medium(<50%)', ''))
        ]),
        '; '
    ) AS diagnosis,
    arrayStringConcat(
        arrayFilter(x -> length(x) > 0, [
            if(max_parts_in_partition > 150, concat('检查并控制小批次写入；必要时执行 OPTIMIZE TABLE ', database_name, '.', table_name, ' FINAL（仅维护窗口）'), ''),
            if(final_ratio > 0.05, '将 FINAL 热查询改为 argMax/latest 快照读路径', ''),
            if(p95_ms > 500, '对 Top 慢模板做 EXPLAIN indexes=1, actions=1 并调整 ORDER BY/过滤列', ''),
            if(projection_attempt_rate_estimated < 0.50, concat('核查查询是否启用 optimize_use_projections=1，并复核 projection 设计（表：', database_name, '.', table_name, '）'), '')
        ]),
        ' | '
    ) AS recommended_actions
FROM score_board
ORDER BY priority_score DESC, p95_ms DESC, max_parts_in_partition DESC;
