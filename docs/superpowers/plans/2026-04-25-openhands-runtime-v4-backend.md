# OpenHands Runtime V4 Backend Implementation Plan

## Goal

Introduce OpenHands as a pluggable runtime v4 inner-agent backend while keeping `ai-service` as the `/api/v2` control plane and `exec-service` as the only execution, policy, approval, and audit authority.

## Scope

In scope:

- runtime v4 backend interface
- LangGraph backend wrapper
- OpenHands backend skeleton
- backend selection by env and request hint
- OpenHands preview actions
- preview action execution through existing command path
- reuse product-native `ai.skills`
- preserve `skill_name` and `step_id` metadata

Out of scope:

- replacing all of `ai-service`
- replacing `/api/v1/ai`
- changing KB/case/history APIs
- making OpenHands an execution authority
- bypassing `exec-service` approval/precheck/audit

## Completed Tasks

- [x] Add `ai.runtime_v4.backend` package.
- [x] Add `RuntimeBackendRequest` and `RuntimeBackendResult`.
- [x] Wrap existing LangGraph inner loop in `LangGraphBackend`.
- [x] Add `OpenHandsBackend` skeleton behind `AI_RUNTIME_V4_OPENHANDS_ENABLED`.
- [x] Add backend factory with explicit request hint support.
- [x] Persist backend summary into run summary.
- [x] Preserve `engine.inner=openhands-v1` in create/get/idempotent snapshots.
- [x] Emit planning preview events.
- [x] List OpenHands preview actions from summary when no real action event exists.
- [x] Execute preview actions by `action_id` through existing `execute_command` path.
- [x] Keep `confirmed=False` and `elevated=False` on OpenHands-generated actions.
- [x] Reuse `ai.skills` matcher inside OpenHands backend.
- [x] Convert `SkillStep` into runtime `command.exec` tool intent.
- [x] Preserve `skill_name` and `step_id` in summary, actions API, and preview execution payload.
- [x] Add manual script support for OpenHands v2 run creation, action listing, and preview action execution by `action_id`.
- [x] Add optional OpenHands branch to backend smoke script with `SMOKE_OPENHANDS=true`.
- [x] Add provider abstraction for OpenHands planning/session integration.
- [x] Let `OpenHandsBackend` consume provider output first and fallback to static readonly/skills planning.
- [x] Preserve provider `thoughts` in backend summary and preview events.
- [x] Preserve structured tool args when mapping provider tool calls into runtime `command_spec`.
- [x] Validate OpenHands backend/provider readiness before starting the outer Temporal run.
- [x] Add isolated OpenHarness helper provider that invokes the real `openharness` package in a separate Python environment.
- [x] Add `ai-service/requirements-openharness.txt` and Docker image wiring for `/opt/openharness-venv`.
- [x] Keep local `ai.skills` preview actions alongside provider-generated tool intents.

## Current Verification

Use targeted verification because the repository has known unrelated baseline failures and coverage gate noise on narrow test runs.

```bash
pytest \
  ai-service/tests/test_runtime_v4_backend_factory.py \
  ai-service/tests/test_runtime_v4_openhands_provider.py \
  ai-service/tests/test_runtime_v4_openhands_backend.py \
  ai-service/tests/test_runtime_v4_tool_adapter.py \
  ai-service/tests/test_runtime_v4_orchestration_bridge.py \
  ai-service/tests/test_ai_runtime_v2_api.py -k "openhands" \
  tests/test_ai_service_deploy_openhands_env.py -k "openhands" \
  --no-cov -q
```

Expected result at this checkpoint: all selected tests pass.

## Next Tasks

- [x] Replace provider fallback with real OpenHands SDK session integration via isolated helper subprocess.
- [x] Map OpenHands/provider tool calls into runtime `command_spec` using `tool_adapter`.
- [x] Add summary/event mapping for provider thoughts and preview tool calls.
- [ ] Add runtime event mapping for richer OpenHands tool lifecycle states beyond preview.
- [ ] Add MCP tool registry mapping rules.
- [x] Add smoke/manual script support for creating an OpenHands run and executing a preview action by `action_id`.
- [ ] Run container/image build verification for `/opt/openharness-venv` and helper startup.
- [ ] Run K8s manual check with readonly preview and mutating command approval.

## Real Integration Notes

- `openharness-ai==0.1.7` currently conflicts with the main `ai-service` FastAPI stack (`starlette` / `anyio`).
- Because of that, the real package is installed into a dedicated `/opt/openharness-venv`, not the main `/opt/venv`.
- `OpenHandsBackend` still does not let OpenHarness execute commands directly. The helper only emits captured tool intents (`generic_exec`, `kubectl_clickhouse_query`), and `exec-service` remains the only execution/approval/audit authority.
- Helper activation is explicit: `AI_RUNTIME_V4_OPENHANDS_HELPER_ENABLED=true`.

## Non-Negotiable Safety Constraints

- OpenHands must not directly run cluster/database mutation commands.
- Risk decisions remain in `exec-service`.
- `confirmed` and `elevated` must default to `False` for backend-generated actions.
- Approval resolution must continue through `/api/v2/runs/{run_id}/approvals/{approval_id}/resolve`.
- OpenHands rollout must fail closed unless `AI_RUNTIME_V4_OPENHANDS_ENABLED=true`.
