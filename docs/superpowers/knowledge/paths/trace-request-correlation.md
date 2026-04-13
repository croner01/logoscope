# trace-request-correlation

## Summary
This path explains how diagnosis should correlate a fault across `trace_id`, `request_id`, and timestamp windows when some correlation keys are missing.

## Participating Components
- frontend AIAnalysis context builder
- ai-service follow-up context logic
- query-service log and trace read surfaces
- semantic-engine normalized fields

## Step-by-Step Flow
1. user input or prior analysis provides log text, trace ID, request ID, or timestamp anchors
2. frontend and backend normalize analysis context
3. diagnosis chooses the strongest available anchor set
4. evidence collection prefers explicit windows and request correlation over weak heuristic widening

## Failure Surfaces
- trace-only assumptions in services that expose only request IDs
- dropped timestamps or missing request windows
- over-broad fallback queries like `--since=15m`

## Preferred Evidence Sources
- raw log lines containing request IDs or timestamps
- normalized `analysis_context` fields
- follow-up related log windows and anchor timestamps

## Recommended First Checks
- confirm whether `request_id` is present even when `trace_id` is not
- confirm whether `request_flow_window_start/end` were preserved
- confirm whether the generated commands use explicit windows rather than broad defaults

## Common Misreads
- absence of `trace_id` does not imply diagnosis must stop
- request correlation and time windows can be first-class anchors

## Sources
- `/root/logoscope/docs/superpowers/specs/2026-04-12-runtime-diagnosis-reliability-design.md`
- `/root/logoscope/docs/architecture/log-ingest-query-runtime-path.zh-CN.md`
