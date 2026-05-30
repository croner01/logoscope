---
name: runtime-diagnosis-orchestrator
description: Orchestrate runtime diagnosis across OpenStack, Kubernetes, and host/system layers for AI-assisted troubleshooting. Use this skill when the task requires multi-hop evidence collection, command planning/execution, de-duplication, stream-recovery, and safe handoff toward remediation workflows.
metadata:
  short-description: Multi-layer runtime diagnosis orchestrator
---

# Runtime Diagnosis Orchestrator

Use this skill when troubleshooting needs evidence from multiple layers (`OpenStack`, `Kubernetes`, `system/host`) and the agent must execute commands without duplicate runs or output loss.

## Trigger Conditions

Apply this skill when one or more conditions hold:
- The incident spans services and infrastructure boundaries.
- The user asks for command-assisted diagnosis.
- Prior runs show duplicate command execution.
- Prior runs show command output stream interruption or incomplete output.
- The user wants a path from diagnosis to future automated remediation.

## Inputs

Required inputs:
- Incident goal: what decision must be made.
- Scope: service, namespace/project, cluster/region, host.
- Time window: start/end or relative window.

Optional inputs:
- Correlation keys (`trace_id`, `request_id`, `instance_id`, `pod`, `node`, `volume_id`).
- Existing hypotheses.
- Risk constraints (read-only, approval required, change freeze).

## Output Contract

Return all outputs in structured sections:
- `hypothesis_set`: ranked hypotheses with required evidence.
- `evidence_plan`: ordered actions by layer (`openstack`, `k8s`, `system`).
- `command_plan`: executable commands with `command_spec` and purpose.
- `execution_log`: command run mapping (`action_id -> command_run_id`) and status.
- `evidence_summary`: confirmed facts vs unknowns.
- `next_action`: `continue_diagnosis` or `ready_for_remediation_interface`.

## Operating Model

Work in strict phases:
1. Plan: produce hypothesis and minimum evidence set.
2. Execute: run only the smallest command set that can falsify/confirm hypotheses.
3. Reconcile: merge outputs, detect gaps, and decide whether to continue.
4. Conclude: summarize confidence and explicitly state blocked reasons.

Never skip reconciliation after command execution.

## Cross-Layer Strategy

Use layer sequence based on symptoms:
1. Application symptom first (error code, timeout, saturation).
2. Kubernetes control/data path second (pods, scheduling, network, DNS, service endpoints).
3. OpenStack substrate third (compute, network, volume, hypervisor side).
4. Host/system last-mile checks when cluster and cloud evidence conflict.

If a command needs write or privileged access, stop and switch to approval workflow.

## Command Planning Rules

For each command action, require:
- `action_id` (stable within run)
- `purpose`
- `expected_signal` (what result confirms/refutes)
- `command_spec` (not raw command only)
- `risk_level`
- `timeout_seconds`

Reject commands lacking a concrete expected signal.

## De-duplication and Idempotency Rules

To prevent repeated execution:
- Build `command_fingerprint` from normalized command, purpose, action_id, target identity.
- Use one active run per fingerprint in a run scope.
- If an active run exists, return `running_existing` instead of creating a new run.
- If a completed successful fingerprint exists and evidence is still valid in time window, reuse result.
- Only retry when retry reason is explicit (`ticket_invalid`, `backend_unavailable`, timeout recovery policy).

Do not issue the same command again only because stream disconnected.

## Stream Recovery Rules

When command stream interrupts:
1. Resume from `after_seq` using stream endpoint.
2. If stream remains unavailable, poll events endpoint by sequence.
3. If still missing terminal event, query run snapshot.
4. Mark command terminal only after terminal evidence is observed.

Terminal evidence includes one of:
- `command_finished`
- `command_cancelled`
- snapshot status in terminal set (`completed`, `failed`, `cancelled`)

## Safety and Approval Gates

Hard gate conditions:
- Any write or mutating operation.
- Potentially destructive operation.
- Escalated privilege operation.

For gated actions, emit:
- clear reason
- bounded blast radius
- rollback hint
- verification plan

## Remediation Interface Handoff

When diagnosis confidence is sufficient, output remediation-ready artifacts without executing changes:
- `change_plan` (ordered steps)
- `risk_gate` (approval and prechecks)
- `rollback_plan`
- `post_verify_plan`

Set `next_action=ready_for_remediation_interface` only when:
- root cause has direct evidence,
- required preconditions are observable,
- verification and rollback are both defined.

## Domain References

Load domain playbooks as needed:
- OpenStack: `references/openstack.md`
- Kubernetes: `references/k8s.md`
- System/Host: `references/system.md`
- Implementation slicing guide: `references/runtime-fix-slices.md`

Do not load all references by default; only load the layer currently being executed.

## Bundled Scripts

Use these scripts for run-level quality checks:
- `scripts/replay-run-check.sh <run_id>`: fetches runtime events, prints run summary, and executes duplicate/stream checks.
  - Local API mode: `scripts/replay-run-check.sh <run_id>`
  - K8s mode: `scripts/replay-run-check.sh <run_id> --via-kubectl --k8s-namespace islap --ai-service-ref deploy/ai-service`
- `scripts/assert-no-duplicate-command.py --events-file <path>`: validates duplicate command runs, missing terminal events, and optional AI-vs-exec output mismatch.

Recommended check flow:
1. Run `replay-run-check.sh` on the target run.
2. If failed, inspect duplicate action keys and command run ids.
3. Confirm stream mismatch before claiming frontend-only issue.

## Failure Handling

If diagnosis stalls:
- report exact missing evidence,
- provide smallest additional command set,
- avoid speculative conclusions.

If command execution quality degrades (duplicates or stream cuts):
- switch to stricter idempotency mode,
- reduce concurrency,
- force sequence-based replay before next plan iteration.
