-- ClickHouse 分区/冷热策略优化模板
-- 目标：
-- 1) 核心表统一按日分区（toDate(timestamp)）
-- 2) 建立分层 TTL：热数据窗口 + 保留期删除
-- 3) 可选启用 TO VOLUME 冷层迁移（依赖 cold volume）
--
-- 使用方式：
-- - 先执行第 1 节检查现状
-- - 再执行第 2 节在线调整（仅修改 TTL）
-- - 最后在维护窗口按第 3 节完成分区迁移（建新表+回填+切换）

USE logs;

-- =============================================================================
-- 1) 现状检查：分区表达式 + TTL + 分区体量
-- =============================================================================

SELECT
    name,
    engine,
    partition_key,
    if(positionCaseInsensitive(create_table_query, 'TTL') > 0, 1, 0) AS has_ttl
FROM system.tables
WHERE database = 'logs'
  AND name IN ('logs', 'traces', 'events', 'metrics')
ORDER BY name;

SELECT
    table,
    partition,
    count() AS part_count,
    sum(rows) AS rows,
    formatReadableSize(sum(bytes_on_disk)) AS bytes_on_disk
FROM system.parts
WHERE database = 'logs'
  AND active
  AND table IN ('logs', 'traces', 'events', 'metrics')
GROUP BY table, partition
ORDER BY table, partition DESC
LIMIT 200;

-- =============================================================================
-- 2) 在线 TTL 调整（不改分区，仅更新保留/冷热策略）
-- =============================================================================
-- 默认删除策略：
-- - logs/traces/events: 30 天
-- - metrics: 7 天

ALTER TABLE logs.logs
    MODIFY TTL toDateTime(timestamp) + INTERVAL 30 DAY DELETE;

ALTER TABLE logs.traces
    MODIFY TTL toDateTime(timestamp) + INTERVAL 30 DAY DELETE;

ALTER TABLE logs.events
    MODIFY TTL toDateTime(timestamp) + INTERVAL 30 DAY DELETE;

ALTER TABLE logs.metrics
    MODIFY TTL toDateTime(timestamp) + INTERVAL 7 DAY DELETE;

-- 若已配置 cold volume，可改为冷热分层：
-- ALTER TABLE logs.logs
--     MODIFY TTL toDateTime(timestamp) + INTERVAL 7 DAY TO VOLUME 'cold',
--                toDateTime(timestamp) + INTERVAL 30 DAY DELETE;
-- ALTER TABLE logs.traces
--     MODIFY TTL toDateTime(timestamp) + INTERVAL 7 DAY TO VOLUME 'cold',
--                toDateTime(timestamp) + INTERVAL 30 DAY DELETE;
-- ALTER TABLE logs.events
--     MODIFY TTL toDateTime(timestamp) + INTERVAL 7 DAY TO VOLUME 'cold',
--                toDateTime(timestamp) + INTERVAL 30 DAY DELETE;
-- ALTER TABLE logs.metrics
--     MODIFY TTL toDateTime(timestamp) + INTERVAL 1 DAY TO VOLUME 'cold',
--                toDateTime(timestamp) + INTERVAL 7 DAY DELETE;

-- =============================================================================
-- 3) 维护窗口迁移：按日分区（以 logs.logs 为例）
-- =============================================================================
-- 注意：
-- - 需在低峰执行；
-- - 回填期间建议暂停大流量写入或采用双写策略；
-- - 执行前请完整备份。

-- 3.1 创建新表（字段按当前线上 logs.logs 实际 schema 调整）
/*
CREATE TABLE logs.logs_v3_daily
(
    timestamp DateTime64(9, 'UTC'),
    observed_timestamp DateTime64(9, 'UTC'),
    trace_id String,
    span_id String,
    trace_flags UInt8,
    service_name String LowCardinality,
    host_name String,
    pod_id String,
    container_name String,
    container_id String,
    container_image String,
    level String,
    severity_number UInt8,
    flags UInt8,
    message String,
    labels String,
    attributes_json String,
    node_name String,
    host_ip String,
    cpu_limit String,
    cpu_request String,
    memory_limit String,
    memory_request String
)
ENGINE = MergeTree()
PARTITION BY toDate(timestamp)
ORDER BY (timestamp, service_name, trace_id, span_id)
TTL toDateTime(timestamp) + INTERVAL 30 DAY DELETE
SETTINGS index_granularity = 8192, ttl_only_drop_parts = 1;
*/

-- 3.2 回填历史数据
/*
INSERT INTO logs.logs_v3_daily
SELECT *
FROM logs.logs;
*/

-- 3.3 校验记录数
/*
SELECT 'old' AS table_name, count() AS rows FROM logs.logs
UNION ALL
SELECT 'new' AS table_name, count() AS rows FROM logs.logs_v3_daily;
*/

-- 3.4 原子切换（确认校验通过后）
/*
RENAME TABLE logs.logs TO logs.logs_backup_yyyymmdd,
             logs.logs_v3_daily TO logs.logs;
*/

-- 3.5 观察稳定后再删除备份表
/*
DROP TABLE logs.logs_backup_yyyymmdd;
*/
