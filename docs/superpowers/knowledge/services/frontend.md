# frontend

## Summary
`frontend` is the user interaction layer. It launches AI analysis, runtime follow-up, logs exploration, trace exploration, and topology views, and is responsible for carrying the right diagnosis anchors into backend runtime entrypoints.

## Responsibilities
- Provide AIAnalysis and runtime-lab user flows
- Render logs, traces, topology, and runtime thought streams
- Build frontend-side analysis context and follow-up context

## Boundaries
- Owns user interaction and context assembly
- Does not execute diagnosis logic or persist runtime evidence itself

## Upstream / Downstream
- Upstream: user input, selected logs, UI state
- Downstream: query-service, topology-service, ai-service runtime APIs

## APIs and Interfaces
- AI analysis pages and runtime hooks
- logs / traces / topology explorers
- runtime SSE / polling clients

## Storage / Topics
- No primary observability storage ownership
- Transports user-selected context to backend services

## Preferred Evidence Sources
- browser-visible runtime state
- request payloads sent to ai-service
- follow-up context construction helpers

## Common Failures and Cautions
- Missing backend evidence can begin with frontend context loss
- Over-strict frontend gating can block legitimate diagnosis flows before backend logic runs, and weak runtime diagnosis may reflect missing anchors or time windows in the request payload rather than backend reasoning quality

## Diagnosis Entry Hints
- Verify what `analysis_context` the page actually sends before assuming backend reasoning failure
- When trace IDs are absent, confirm whether log-mode + time-window fallback still carries request anchors
- When the backend already has a strong fault-layer signal, avoid forcing the frontend to supply perfect correlation fields before runtime diagnosis can continue

## Sources
- `/root/logoscope/AGENTS.md`
- `/root/logoscope/docs/design/SYSTEM_DESIGN.md`
- `/root/logoscope/docs/superpowers/specs/2026-04-12-runtime-diagnosis-reliability-design.md`
