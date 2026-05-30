# Kubernetes Playbook

Use this reference for cluster runtime evidence collection.

## Focus Areas
- Workload health (deployment, pod lifecycle, restarts, OOM, probe failure)
- Service path (endpoints, DNS, network policy)
- Control-plane symptoms (scheduling, node pressure, evictions)

## Typical Evidence Questions
- Is failure isolated to one pod/node or systemic?
- Is service routing consistent with target endpoints?
- Is throttling/resource pressure causing latency spikes?

## Read-Only Command Patterns
- `kubectl -n <ns> get pods -o wide`
- `kubectl -n <ns> describe pod <pod>`
- `kubectl -n <ns> logs <pod> --since=<window>`
- `kubectl -n <ns> get svc,endpoints`
- `kubectl get nodes`

## Correlation Keys
- `trace_id/request_id` across pod logs
- `pod -> node` mapping for host-level anomalies
- `namespace/service` to OpenStack project mapping

## Guardrails
- Use label selectors and bounded `--since/--tail`.
- Prefer deterministic queries over broad free-text grep first.
- Record exact namespace/context for each command.
