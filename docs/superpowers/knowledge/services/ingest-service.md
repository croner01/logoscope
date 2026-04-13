# ingest-service

## Summary
`ingest-service` is the OTLP ingest entry for logs, metrics, and traces. It adapts request formats and writes normalized envelopes into the queueing layer rather than serving as the primary long-term query surface.

## Responsibilities
- Accept `/v1/logs`, `/v1/metrics`, `/v1/traces`
- Normalize incoming payload metadata
- Forward records into Kafka-backed ingest flow

## Boundaries
- Owns protocol adaptation and queue write
- Does not own query-time diagnosis or topology rendering

## Upstream / Downstream
- Upstream: Fluent Bit, OTel Collector, OTLP clients
- Downstream: Kafka topics such as `logs.raw`; semantic-engine worker consumes resulting envelopes

## APIs and Interfaces
- `POST /v1/logs`
- `POST /v1/metrics`
- `POST /v1/traces`

## Storage / Topics
- Writes to queue topics rather than directly serving user queries
- Key log topic: `logs.raw`

## Preferred Evidence Sources
- ingest-service pod logs
- queue writer / transform logic
- upstream collector payload shape

## Common Failures and Cautions
- Do not confuse successful ingest acceptance with downstream persistence success
- Request-format adaptation issues often appear before queue or storage failures

## Diagnosis Entry Hints
- Check whether payloads reached Kafka before blaming query-side services
- When logs are missing downstream, verify `/v1/logs` path and envelope transformation first

## Sources
- `/root/logoscope/AGENTS.md`
- `/root/logoscope/docs/api/reference.md`
- `/root/logoscope/docs/architecture/log-ingest-query-runtime-path.zh-CN.md`
- `/root/logoscope/ingest-service/README.md`
