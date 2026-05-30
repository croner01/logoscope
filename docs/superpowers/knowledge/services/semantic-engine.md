# semantic-engine

## Summary
`semantic-engine` is the normalization and intelligence layer. It transforms raw envelopes into structured events, performs correlation and classification, and contributes topology-building inputs.

## Responsibilities
- Normalize logs/traces/metrics into structured events
- Classify and correlate events
- Provide AI-analysis-adjacent intelligence and topology inputs

## Boundaries
- Owns semantic processing and enrichment
- Does not serve as the primary end-user query API for logs

## Upstream / Downstream
- Upstream: Kafka raw topics from ingest path
- Downstream: ClickHouse, Neo4j, AI-facing structured context

## APIs and Interfaces
- Internal worker consumption flow
- semantic analysis APIs exposed by semantic-engine service

## Storage / Topics
- Reads queue topics such as `logs.raw`
- Writes normalized data to ClickHouse / Neo4j paths

## Preferred Evidence Sources
- semantic-engine worker logs
- normalization output fields
- classification / correlation traces in structured results

## Common Failures and Cautions
- Missing fields downstream may come from normalization loss, not user query bugs
- Topology symptoms can start in semantic processing rather than topology-service presentation, and query rendering anomalies are not enough to blame frontend or query-service until semantic output shape is checked

## Diagnosis Entry Hints
- When service names or trace fields look malformed, inspect normalization before blaming query rendering
- For topology anomalies or missing downstream fields, verify whether semantic-engine produced the expected graph inputs and raw envelope -> normalized output transformation before investigating unrelated read-path latency

## Sources
- `/root/logoscope/AGENTS.md`
- `/root/logoscope/docs/design/SYSTEM_DESIGN.md`
- `/root/logoscope/docs/architecture/log-ingest-query-runtime-path.zh-CN.md`
- `/root/logoscope/docs/architecture/service-topology.md`
