# topology-generation-preview

## Summary
This path explains how topology views are built and previewed, and where diagnosis should inspect graph-generation versus graph-serving failures.

## Participating Components
- semantic-engine topology builders
- topology-service APIs
- query-service preview routes
- frontend topology pages
- ClickHouse / Neo4j graph data

## Step-by-Step Flow
1. traces, logs, metrics, and manual config contribute graph inputs
2. topology builders create hybrid or enhanced graph structures
3. topology-service serves graph and update APIs
4. query preview routes may join topology edges with log evidence
5. frontend renders graph and edge-preview views

## Failure Surfaces
- upstream graph-input loss
- confidence threshold filtering
- preview contract mismatch
- topology-service response issues

## Preferred Evidence Sources
- hybrid topology API output
- topology-service logs
- edge preview responses
- graph metadata or snapshot state

## Recommended First Checks
- compare topology API output with preview output
- verify whether missing edges are filtered, absent upstream, or broken in serving layer
- inspect confidence threshold and manual suppression effects

## Common Misreads
- empty topology is not always a topology-service-only fault
- preview failures do not always imply graph-construction failures

## Sources
- `/root/logoscope/docs/architecture/service-topology.md`
- `/root/logoscope/docs/api/topology.md`
- `/root/logoscope/docs/api/reference.md`
