# topology-service

## Summary
`topology-service` serves topology query, manual adjustment, and realtime topology update APIs. It presents graph views built from traces, logs, metrics, and manual configuration inputs.

## Responsibilities
- Serve topology APIs and WebSocket updates
- Support manual node/edge adjustments
- Expose hybrid / enhanced / stats topology views

## Boundaries
- Owns topology presentation and adjustment APIs
- Does not own raw log ingest or generic query-service log exploration

## Upstream / Downstream
- Upstream: topology graph inputs from traces, logs, metrics, and manual config
- Downstream: frontend topology pages, preview and graph consumers

## APIs and Interfaces
- `GET /api/v1/topology/hybrid`
- `GET /api/v1/topology/enhanced`
- `GET /api/v1/topology/stats`
- `POST /api/v1/topology/edges/manual`
- `WS /ws/topology`

## Storage / Topics
- Serves graph data backed by Neo4j / hybrid topology logic
- Reads topology snapshots and graph metadata paths

## Preferred Evidence Sources
- topology-service logs
- hybrid topology responses
- edge preview contracts and topology metadata

## Common Failures and Cautions
- Empty topology can come from upstream graph-input loss, not only topology-service API bugs
- Trace ID absence does not imply topology generation is impossible because hybrid topology also uses logs and metrics; preview anomalies and empty topology should be separated from read-path slow-query incidents because they are not the same fault surface

## Diagnosis Entry Hints
- For topology anomalies, compare topology API output with upstream graph-building expectations
- Use hybrid topology and edge preview together; if topology data is empty, first decide whether graph input is absent, filtered, or served incorrectly before expanding into generic correlation-only troubleshooting

## Sources
- `/root/logoscope/AGENTS.md`
- `/root/logoscope/docs/api/reference.md`
- `/root/logoscope/docs/api/topology.md`
- `/root/logoscope/docs/architecture/service-topology.md`
