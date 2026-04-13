# ai-service

## Summary
`ai-service` is the diagnosis orchestration layer for AI analysis, follow-up reasoning, runtime command planning, and blocked-reason reporting. It turns evidence plus runtime context into investigation guidance.

## Responsibilities
- Serve AI analysis and follow-up diagnosis APIs
- Orchestrate runtime planning, execution policy, and summaries
- Maintain diagnosis contracts, runtime history, and command-approval flow

## Boundaries
- Owns diagnosis orchestration and runtime summaries
- Does not own raw observability storage or frontend rendering

## Upstream / Downstream
- Upstream: user question, analysis context, follow-up evidence, related logs, runtime metadata
- Downstream: runtime runs, summaries, action plans, prompt injection, operator guidance

## APIs and Interfaces
- AI runtime run APIs
- follow-up analysis entrypoints
- runtime streaming / event endpoints

## Storage / Topics
- runtime store / session history
- uses upstream logs, trace results, and related references as context rather than primary storage ownership

## Preferred Evidence Sources
- ai-service runtime events
- follow-up planning payloads
- blocked reason / gate decision metadata

## Common Failures and Cautions
- Generic reasoning quality problems may be context-loss issues rather than LLM capability issues
- `planning_incomplete` must not be used when runnable template commands already exist

## Diagnosis Entry Hints
- Inspect runtime summary, selected actions, and gate decision before changing prompt wording
- Separate context-missing, planning, execution-policy, and backend-readiness failures

## Sources
- `/root/logoscope/docs/superpowers/specs/2026-04-12-runtime-diagnosis-reliability-design.md`
- `/root/logoscope/docs/superpowers/plans/2026-04-12-runtime-diagnosis-reliability-plan.md`
- `/root/logoscope/docs/design/ai-agent-runtime-implementation-v1.md`
