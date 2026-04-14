# log-ingest-query

## Summary
The log ingest and query path explains how a log enters Logoscope, is normalized and stored, and is later queried by users and runtime diagnosis.

## Participating Components
- Fluent Bit
- OTel Collector / Gateway
- ingest-service
- Kafka
- semantic-engine worker
- ClickHouse
- query-service
- frontend LogsExplorer

## Step-by-Step Flow
1. Upstream agents send logs into `ingest-service /v1/logs`
2. ingest-service transforms payloads and writes queue envelopes
3. semantic-engine worker consumes raw log envelopes and normalizes them
4. normalized log data lands in ClickHouse tables such as `logs.logs`
5. query-service serves read APIs and realtime query views
6. frontend explorers and diagnosis flows read from query-side surfaces

## Failure Surfaces
- malformed ingest payloads
- queue delivery gaps
- normalization field loss
- ClickHouse persistence or slow query issues
- query-service read path failures

## Preferred Evidence Sources
- ingest-service logs
- semantic-engine worker logs
- ClickHouse `logs.logs`
- ClickHouse `system.query_log`
- ClickHouse `system.processes`
- ClickHouse `system.metrics`
- query-service logs

## Recommended First Checks
- confirm whether the event was accepted by ingest-service
- confirm whether normalized rows exist in ClickHouse
- confirm whether query-service failures are storage-driven or API-driven; if the dominant symptom is slow query or timeout rather than missing data, pivot from ingest-path checks to ClickHouse execution/resource evidence

## Common Misreads
- missing log results in UI do not automatically mean ingest failed
- slow query symptoms often come from ClickHouse even when surfaced by query-service, and continuing to ask for more correlation anchors is not the best next move once storage/read-path latency is already the stronger fault surface

## Sources
- `/root/logoscope/docs/architecture/log-ingest-query-runtime-path.zh-CN.md`
- `/root/logoscope/docs/api/reference.md`
- `/root/logoscope/docs/design/SYSTEM_DESIGN.md`
