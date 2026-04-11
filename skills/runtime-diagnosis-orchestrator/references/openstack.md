# OpenStack Playbook

Use this reference for cloud substrate evidence collection.

## Focus Areas
- Compute state (`server`, hypervisor placement, status transitions)
- Network path (ports, security groups, floating IP, router)
- Storage path (volume attach/detach, backend latency, errors)

## Typical Evidence Questions
- Is the VM healthy but upstream dependency failing?
- Is there network reachability drift between tenant and service endpoint?
- Is volume or backend storage path causing IO timeout?

## Read-Only Command Patterns
- `openstack server show <instance>`
- `openstack port list --server <instance>`
- `openstack volume show <volume_id>`
- `openstack hypervisor show <host>`

## Correlation Keys
- `instance_id` to `node` and `pod` scheduling zone
- `project_id` to namespace/service ownership map
- `volume_id` to application storage errors

## Guardrails
- Prefer tenant-scoped read commands first.
- Avoid noisy list commands without filter in large regions.
- Attach absolute time window to every log/event query.
