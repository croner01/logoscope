-- =============================================================================
-- Migration 001: Add source_cluster column to logs.logs
--
-- source_cluster 标识日志来源的远端 K8s 集群，
-- 由 fluent-bit-relay 的 modify filter 注入 relay_name，
-- 经 Ingest Service → Semantic Engine 写入此列。
--
-- 存量数据该字段为空字符串（''），表示来自本地 Fluent Bit 或旧数据。
-- 新接入的远端集群 relay 会填入对应的集群名称。
--
-- 使用方式:
--   clickhouse-client --query "$(cat deploy/migrations/001-add-source-cluster.sql)"
-- =============================================================================

ALTER TABLE logs.logs
ADD COLUMN IF NOT EXISTS source_cluster String DEFAULT '' AFTER container_image;
