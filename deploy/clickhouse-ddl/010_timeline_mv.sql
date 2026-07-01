-- Timeline Materialized View
-- 将 logs.events 投影为按 entity_id + 时间聚合的时间线视图
-- 生产环境中由 TimelineProjection Python 包装器查询
CREATE MATERIALIZED VIEW IF NOT EXISTS logs.timeline_mv
ENGINE = MergeTree()
ORDER BY (entity_id, toStartOfMinute(timestamp))
POPULATE AS
SELECT
    entity_id,
    timestamp,
    event_id,
    service_name,
    event_category,
    severity,
    message
FROM logs.events
WHERE entity_id != ''
