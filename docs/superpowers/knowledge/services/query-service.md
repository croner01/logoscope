# query-service

## Summary
`query-service` is the primary read-path API for logs, traces, previews, and derived observability views. It is the main diagnosis surface when users are reading existing evidence rather than ingesting new data.

## Responsibilities
- Serve log and trace queries
- Provide preview and derived query APIs
- Support realtime and filtered read paths for frontend exploration

## Boundaries
- Owns read/query interfaces
- Does not own OTLP ingest or topology graph construction

## Upstream / Downstream
- Upstream: ClickHouse-backed persisted observability data
- Downstream: frontend explorers, AI analysis flows, operator queries

## APIs and Interfaces
- `GET /api/v1/logs`
- query-service trace/log preview endpoints
- realtime log WebSocket path

## Storage / Topics
- Reads ClickHouse tables such as `logs.logs`
- Uses query-side preview routes for topology/log correlation

## Preferred Evidence Sources
- query-service logs
- ClickHouse `system.query_log`
- frontend request parameters hitting query-service APIs

## Common Failures and Cautions
- Slow reads may be query-service symptoms but ClickHouse root causes
- Missing topology preview data may reflect upstream topology generation issues rather than query-service-only bugs

## Diagnosis Entry Hints
- For user-facing read failures, start with query-service logs plus ClickHouse query evidence
- Prefer request window + request_id correlation before assuming trace-only visibility

## Sources
- `/root/logoscope/AGENTS.md`
- `/root/logoscope/docs/api/reference.md`
- `/root/logoscope/docs/architecture/log-ingest-query-runtime-path.zh-CN.md`
- `/root/logoscope/docs/api/topology.md`
