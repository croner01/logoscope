# AI Runtime Backend Manual Check

本文档用于后端优先联调，不依赖前端页面改造即可验证 AI runtime 是否贴近目标产品逻辑。

入口脚本：

- [scripts/ai-runtime-backend-smoke.sh](/root/logoscope/scripts/ai-runtime-backend-smoke.sh)
- [scripts/ai-runtime-manual-entry.sh](/root/logoscope/scripts/ai-runtime-manual-entry.sh)
- [scripts/exec-runtime-replay-check.sh](/root/logoscope/scripts/exec-runtime-replay-check.sh)

## 目标

验证以下四类能力是否已经具备稳定后端基础：

1. run/event/stream 基础契约可用
2. `approval_required -> approve/reject` 状态机可用
3. `max chars / policy reject` 这类预检原因可明确返回
4. 人工刷新前，后端是否已经具备恢复当前 run 的真实数据能力

## 安全基线（K8s 推荐）

`exec-service` 在集群环境建议使用以下策略开关，确保“策略可审计 + fail-closed”：

```text
EXEC_POLICY_DECISION_MODE=opa_enforced
EXEC_POLICY_ALLOW_NON_ENFORCED_MODES=false
EXEC_POLICY_OPA_URL=http://opa.<namespace>.svc.cluster.local:8181/v1/data/runtime/command/v1
EXEC_POLICY_DECISION_STORE_BACKEND=clickhouse
EXEC_POLICY_DECISION_SQLITE_ENABLED=false
EXEC_POLICY_DECISION_CH_URL=http://clickhouse:8123
EXEC_POLICY_DECISION_CH_DATABASE=logs
EXEC_POLICY_DECISION_CH_TABLE=exec_policy_decisions
EXEC_POLICY_DECISION_CH_FAIL_OPEN=false
EXEC_RUNTIME_HISTORY_STORE_BACKEND=clickhouse
EXEC_RUNTIME_HISTORY_CH_URL=http://clickhouse:8123
EXEC_RUNTIME_HISTORY_CH_DATABASE=logs
EXEC_RUNTIME_HISTORY_RUN_TABLE=exec_command_runs
EXEC_RUNTIME_HISTORY_EVENT_TABLE=exec_command_events
EXEC_RUNTIME_HISTORY_AUDIT_TABLE=exec_command_audits
EXEC_RUNTIME_HISTORY_CH_FAIL_OPEN=false
```

说明：

1. `memory/sqlite` 只用于本地开发与单测，不建议作为 K8s 基线。
2. `EXEC_POLICY_DECISION_SQLITE_ENABLED=false` 可显式禁用 sqlite backend，避免误配置绕回本地文件持久化。
3. `EXEC_POLICY_DECISION_CH_FAIL_OPEN=false` 表示策略审计存储不可用时拒绝放行，符合安全优先策略。
4. `EXEC_RUNTIME_HISTORY_CH_FAIL_OPEN=false` 表示运行态审计存储不可用时拒绝写入，避免审计缺口。
5. 策略决策回放可通过 `decision_id` 关联 `run_id` 查询。
6. `EXEC_POLICY_ALLOW_NON_ENFORCED_MODES=false` 保证生产配置下不会被误设为 `local/opa_shadow`。

## OpenHands rollout 开关

当 runtime v4 进入 OpenHands backend 灰度时，必须显式设置以下变量：

```text
AI_RUNTIME_V4_AGENT_BACKEND=openhands
AI_RUNTIME_V4_OPENHANDS_ENABLED=true
AI_RUNTIME_V4_OPENHANDS_HELPER_ENABLED=true
AI_RUNTIME_V4_OPENHANDS_HELPER_PYTHON=/opt/openharness-venv/bin/python
AI_RUNTIME_V4_OPENHANDS_HELPER_SCRIPT=/app/ai/runtime_v4/backend/openhands_helper.py
```

说明：

1. 只设置 `AI_RUNTIME_V4_AGENT_BACKEND=openhands` 不会放行运行，后端应 fail-closed。
2. helper 未开启时，OpenHands backend 仍走本地静态/provider fallback，不会真实调用 `openharness` 包。
3. `openharness-ai` 依赖与 `ai-service` 主运行时冲突，因此必须安装在独立 `/opt/openharness-venv`，不能直接并入主 `/opt/venv`。
4. 第一阶段仅允许 readonly backend 路径，不允许 OpenHands 成为审批真源。
5. OpenHands helper 只输出受控工具意图，不直接执行命令。
6. 高风险动作仍必须走现有 `approval_required -> resolve approval -> resume/replan` 链路。
7. `/api/v2` 返回结构保持不变，变化仅体现在 `engine.inner=openhands-v1`。
8. OpenHands backend 会复用现有 `ai.skills`，将 `DiagnosticSkill` 产生的 `SkillStep` 转换为 preview action；这些 action 应保留 `skill_name` 和 `step_id`。

## 一键 smoke

执行：

```bash
scripts/ai-runtime-backend-smoke.sh
```

如果要手工验证 OpenHands backend 灰度，先导出：

```bash
export AI_RUNTIME_V4_AGENT_BACKEND=openhands
export AI_RUNTIME_V4_OPENHANDS_ENABLED=true
export AI_RUNTIME_V4_OPENHANDS_HELPER_ENABLED=true
```

一键 smoke 可通过 `SMOKE_OPENHANDS=true` 打开 OpenHands 附加用例：

```bash
SMOKE_OPENHANDS=true scripts/ai-runtime-backend-smoke.sh
```

新增 OpenHands smoke 用例会验证：

- 创建 OpenHands runtime v4 run 后 `engine.inner=openhands-v1`
- `/api/v2/runs/{run_id}/actions` 返回 `planned` preview action
- preview action 带 `skill_name / step_id`
- 只传 `action_id` 调用 `/actions/command` 能进入现有执行/审批链路

预期覆盖：

- create run 成功
- events 返回 `run_started / message_started / reasoning_step`
- stream 能立即吐出 canonical SSE 事件
- mutating command 触发 `approval_required`
- reject approval 后 run 变为 `blocked`
- 超长命令预检返回 `command exceeds max chars(320)`

报告输出目录：

```text
reports/ai-runtime-backend-smoke/
```

## 人工联调入口

### 1. 创建 run

```bash
scripts/ai-runtime-manual-entry.sh create-run \
  --question "分析 query-service 当前异常，并在需要审批时暂停等待我确认。"
```

说明：

- 默认 `create-run` 走 `passive` 模式，只创建一个稳定保持 `running` 的 run，适合后续手工验证 `exec / approval / reject / events`。
- 如果要观察完整 agent 跑完一轮，再显式加：

```bash
scripts/ai-runtime-manual-entry.sh create-run \
  --mode followup_runtime \
  --question "分析 query-service 当前异常，并在需要审批时暂停等待我确认。"
```

脚本会把 `run_id / session_id / conversation_id` 写到本地状态文件，后续命令默认复用。

### 1.1 创建 OpenHands runtime v4 run

OpenHands backend 走 `/api/v2`，需要显式指定 `--api-version v2` 和 `--runtime-backend openhands`：

```bash
scripts/ai-runtime-manual-entry.sh create-run \
  --api-version v2 \
  --runtime-backend openhands \
  --no-auto-exec-readonly \
  --enable-skills \
  --max-skills 1 \
  --question "query-service query timeout and slow query"
```

预期：

- `GET /api/v2/runs/{run_id}` 中 `engine.inner=openhands-v1`
- run summary 中 `inner_backend.provider=openharness-subprocess`
- run summary 中包含 `inner_backend.selected_skills`
- `actions` 命令能看到 `planned` 状态的 preview action

### 2. 看实时事件流

```bash
scripts/ai-runtime-manual-entry.sh stream --max-events 20
```

观察点：

- 是否立即收到 `run_started`
- 是否能持续收到后续事件，而不是只能刷新后才看到
- `approval_required` 到达时事件里是否带完整 `approval_id / command / risk_level`

### 3. 看持久化事件

```bash
scripts/ai-runtime-manual-entry.sh events --limit 200
```

观察点：

- stream 中看到的事件，是否能在 events 查询里复现
- 刷新恢复是否有足够事件支撑，而不是只依赖前端内存态

### 4. 触发审批链路

使用一个不会改动真实对象的不存在 deployment：

```bash
scripts/ai-runtime-manual-entry.sh exec \
  --command "kubectl -n islap rollout restart deployment/definitely-not-exist" \
  --title "manual approval trigger"
```

预期：

- 返回 `elevation_required` 或 `confirmation_required`
- run 进入 `waiting_approval`
- events 中写入 `approval_required`

### 4.1 查看并执行 OpenHands preview action

查看 OpenHands backend 规划出的 actions：

```bash
scripts/ai-runtime-manual-entry.sh actions --limit 20
```

执行第一个 preview action：

```bash
scripts/ai-runtime-manual-entry.sh exec-action \
  --action-id planned-preview-1
```

观察点：

- `exec-action` 请求只传 `action_id`，后端应自动补齐 `command_spec`
- 如果 preview action 是只读命令，应走现有受控执行路径
- 如果 preview action 最终被判定为风险命令，应进入现有人工审批链路，而不是由 OpenHands 直接放行

### 5. 查看待审批项

```bash
scripts/ai-runtime-manual-entry.sh latest-approval
```

### 6. 拒绝审批

```bash
scripts/ai-runtime-manual-entry.sh reject
```

预期：

- run 状态变为 `blocked`
- events 中出现 `approval_resolved`
- 后续 hydrate run 时仍保持 `blocked`，不能回退成 `running`

### 7. 批准审批

```bash
scripts/ai-runtime-manual-entry.sh approve
```

说明：

- 该命令会继续触发实际执行
- 当前推荐只对不存在资源执行，避免修改真实对象

### 8. 单独验证预检

短命令：

```bash
scripts/ai-runtime-manual-entry.sh precheck \
  --command "kubectl -n islap get pods"
```

超长命令：

```bash
scripts/ai-runtime-manual-entry.sh precheck \
  --command "kubectl get pod $(python3 - <<'PY'
print('x' * 400)
PY
)"
```

观察点：

- `status`
- `message`
- `command_type`
- `approval_policy`
- 是否明确暴露 `max chars` 这类限制原因

## 与需求对齐的人工检查项

在后端联调阶段，人工主要看这些点：

1. 能否创建并持久化一个 run，而不是只靠前端临时态。
2. stream 与 events 是否一致，刷新后能恢复，而不是刷新后才“突然出现结果”。
3. `approval_required` 是否有明确结构化字段，不只是一段模糊自然语言。
4. reject approval 后 run 是否稳定落到 `blocked/rejected` 语义。
5. `max chars / policy reject` 是否能给前端提供明确 reason code 或至少稳定 reason text。
6. 开启 OpenHands backend 后，`GET /api/v2/runs/{run_id}` 返回的 `engine.inner` 是否为 `openhands-v1`。
7. OpenHands backend 规划出的 skills action 是否能在 `/api/v2/runs/{run_id}/actions` 中看到 `skill_name / step_id`。
8. 只传 preview `action_id` 调用 `/api/v2/runs/{run_id}/actions/command` 时，是否仍走现有命令执行与审批链路。

## 当前边界

这套脚本和文档只验证后端基础能力，不判断前端体验是否已经符合“类似 Trae 的连续会话页”。

前端体验是否达标，仍要结合：

- 主流是否只保留 `user / assistant / approval`
- `unknown/manual` 是否也能形成强中断入口
- 是否不再依赖“刷新当前 run”作为主要恢复方式
