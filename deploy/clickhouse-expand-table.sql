-- ClickHouse表结构扩展脚本
-- 目标：添加P0优化字段
-- 日期：2026-02-07

-- 步骤1: 创建新表结构（带所有新字段）
CREATE TABLE IF NOT EXISTS logs.logs_v2
(
    `id` String,
    `timestamp` DateTime64(9, 'UTC'),
    `observed_timestamp` DateTime64(9, 'UTC'),  -- ⭐ 新增：观察时间戳
    `service_name` String,
    `pod_name` String,
    `namespace` String,
    `node_name` String,
    `pod_id` String,                             -- ⭐ 新增：Pod UID
    `container_name` String,                     -- ⭐ 新增：容器名称
    `container_id` String,                       -- ⭐ 新增：容器ID（docker_id）
    `container_image` String,                    -- ⭐ 新增：容器镜像
    `level` String,
    `severity_number` UInt8,                     -- ⭐ 新增：OTLP严重程度数值
    `message` String,
    `trace_id` String,
    `span_id` String,
    `flags` UInt32,                              -- ⭐ 新增：LogRecord标志
    `labels` String,                             -- K8s labels (JSON)
    `attributes_json` String,                    -- ⭐ 新增：完整attributes (JSON)
    `host_ip` String
)
ENGINE = MergeTree
PARTITION BY toDate(timestamp)
ORDER BY (timestamp, service_name, level, pod_name)
TTL toDateTime(timestamp) + INTERVAL 30 DAY DELETE
SETTINGS index_granularity = 8192, ttl_only_drop_parts = 1;

-- 步骤2: 从旧表迁移数据
INSERT INTO logs.logs_v2
SELECT
    id,
    timestamp,
    timestamp as observed_timestamp,  -- 回填：使用相同的timestamp
    service_name,
    pod_name,
    namespace,
    node_name,
    '' as pod_id,                -- 回填：空字符串
    '' as container_name,        -- 回填：空字符串
    '' as container_id,          -- 回填：空字符串
    '' as container_image,       -- 回填：空字符串
    level,
    0 as severity_number,        -- 回填：默认值
    message,
    trace_id,
    span_id,
    0 as flags,                  -- 回填：默认值
    labels,
    '{}' as attributes_json,    -- 回填：空JSON
    host_ip
FROM logs.logs;

-- 步骤3: 验证数据迁移
SELECT 'Old table count:' as metric, count(*) as count FROM logs.logs
UNION ALL
SELECT 'New table count:', count(*) FROM logs.logs_v2;

-- 步骤4: 重命名表（原子切换）
-- 注意：这会删除旧表，请先确认数据迁移成功！
-- RENAME TABLE logs.logs TO logs_old;
-- RENAME TABLE logs.logs_v2 TO logs.logs;

-- 步骤5: （可选）删除旧表
-- DROP TABLE logs_old;
