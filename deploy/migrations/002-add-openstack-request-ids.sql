-- 002: Add openstack_request_id and openstack_global_request_id columns
-- to the logs.logs table for structured OpenStack request tracing.
--
-- These columns are populated by the Semantic Engine normalizer when
-- it detects OpenStack log format patterns in the message field.
--
-- Run: cat 002-add-openstack-request-ids.sql | clickhouse-client

ALTER TABLE logs.logs
ADD COLUMN IF NOT EXISTS openstack_request_id         String DEFAULT '';

ALTER TABLE logs.logs
ADD COLUMN IF NOT EXISTS openstack_global_request_id   String DEFAULT '';

-- Bloom filter skip index for fast exact-match lookups
ALTER TABLE logs.logs
ADD INDEX IF NOT EXISTS idx_openstack_request_id
    (openstack_request_id)
TYPE bloom_filter(0.01)
GRANULARITY 4;

ALTER TABLE logs.logs
ADD INDEX IF NOT EXISTS idx_openstack_global_request_id
    (openstack_global_request_id)
TYPE bloom_filter(0.01)
GRANULARITY 4;
