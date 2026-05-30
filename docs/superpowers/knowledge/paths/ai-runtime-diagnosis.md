# ai-runtime-diagnosis

## Summary
This path explains how runtime diagnosis builds context, plans evidence collection, enforces execution policy, and reports blocked reasons.

## Participating Components
- frontend AIAnalysis / runtime lab
- ai-service runtime API
- follow-up session / planning / orchestration helpers
- command execution backend

## Step-by-Step Flow
1. frontend builds diagnosis context from selected logs, trace IDs, request IDs, and time windows
2. ai-service creates a runtime run and normalizes context
3. follow-up logic plans actions and may generate template commands
4. readonly execution policy decides whether commands auto-run
5. runtime summary reports blocked or completed status with explicit reason taxonomy

## Failure Surfaces
- missing fault anchors at entry
- template-ready actions mislabeled as planning failures
- readonly auto-exec disabled but not surfaced clearly
- backend-unready execution path

## Preferred Evidence Sources
- runtime event stream
- gate decision metadata
- blocked reason detail
- action and observation counts

## Recommended First Checks
- inspect `analysis_context` first
- inspect `ready_template_actions` and `observed_actions`
- inspect whether blocked reason is planning, policy, backend, or evidence related, and whether low confidence comes from missing anchors or from collecting evidence at the wrong fault layer

## Common Misreads
- low confidence may come from missing observations, not weak reasoning alone
- a blocked run does not always mean the planner failed; repeated requests for `trace_id` / `request_id` can indicate wrong evidence-layer choice, not just missing context

## Sources
- `/root/logoscope/docs/superpowers/specs/2026-04-12-runtime-diagnosis-reliability-design.md`
- `/root/logoscope/docs/superpowers/plans/2026-04-12-runtime-diagnosis-reliability-plan.md`
