# Runtime Evidence Gating (Soft Gate + Auto-Downgrade)

Date: 2026-04-12

## 1. Goal and Scope

Goal
- Remove hard requirements for `trace_id` / `service_name` at runtime entry.
- Allow log analysis to proceed with only log text + time window.
- Auto-downgrade trace mode to log mode when `trace_id` is missing.

Out of scope
- No new storage or schema changes.
- No cross-service inference heuristics beyond existing trace/request extraction.
- No new external dependencies.

## 2. Input and Mode Behavior

Trace mode
- If `trace_id` is missing, auto-downgrade to log mode.
- If `trace_id` is later extracted from logs, upgrade to trace-linked queries.

Log mode
- No hard requirement on `service_name` or `trace_id`.
- Permit analysis based on log text + time window.

## 3. Evidence Extraction Strategy

- Reuse existing extraction logic for `trace_id` / `request_id` from log content and attributes.
- If identifiers are found, enrich context and enable correlation queries.
- If no identifiers are found, proceed with weaker evidence label.

## 4. Output and Risk Controls

- When evidence is weak, responses must:
  - include a clear evidence warning
  - soften assertions ("可能" / "待验证" language)
- When evidence is strong (trace/request), responses may be more assertive and include specific commands.

## 5. Frontend Interaction

- Trace mode input empty: do not block; warn about auto-downgrade to log analysis.
- Log mode input missing identifiers: do not block; run with time window and show evidence warning.

## 6. Backend Change Boundaries

- Remove hard gate in run creation that requires trace/service identifiers.
- Propagate "mode downgraded" and "evidence strength" into run summary.
- Keep evidence extraction centralized in request flow and followup helpers.

## 7. Testing

- Trace mode without trace_id auto-downgrades to log.
- Log mode without service_name/trace_id still runs.
- Log content contains trace_id/request_id: auto-enrich context.
- Weak evidence output shows warning + softened language.

## 8. Rollout and Observability

- Monitor ratio of downgraded runs.
- Track evidence_strength distribution in summaries.
- Validate no increase in "hallucinated" commands or ungrounded conclusions.
