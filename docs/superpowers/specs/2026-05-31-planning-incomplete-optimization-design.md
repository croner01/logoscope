# AI Analysis 会话阻断优化方案

## 背景

AI Analysis 页面（`AIAnalysis.tsx`）中，AI 生成的诊断动作（actions）可能因为 command_spec 不完整被判定为 `spec_blocked`，进而触发 `planning_incomplete` 阻断。阻断后会话无法继续，用户需重新发起分析。

## 问题分析

阻断触发条件（`followup_planning_helpers.py:1769`）：

1. `executable_total <= 0` — 所有动作不可执行
2. `spec_blocked_ratio >= 0.5` — ≥50% 因 spec 问题被阻塞
3. `observed_executable_actions <= 0` — 无已执行成功的观测
4. `ready_template_actions_total <= 0` — 无可用模板兜底

spec_blocked 的常见原因：

- 缺少 command_spec 字段（`missing_or_invalid_command_spec`）
- kubectl 命令格式粘连（`glued_command_tokens`）
- 缺少 target_kind / target_identity 必要字段
- 命令头不受支持（`unsupported_command_head`）

## 优化方案（方案 A）

### Section 1: Prompt 增强

**文件**: `ai-service/ai/langchain_runtime/prompts.py`

在 `FOLLOWUP_SYSTEM_PROMPT` 中：

1. **规则 5 补充 JSON 示例**：在原抽象描述后增加具体 command_spec 示例：

```
command_spec 示例（generic_exec）：
  {"tool": "generic_exec", "args": {"command": "kubectl get pods -n islap", "target_kind": "k8s_cluster", "target_identity": "namespace:islap", "timeout_s": 30}}

command_spec 示例（kubectl_clickhouse_query）：
  {"tool": "kubectl_clickhouse_query", "args": {"target_kind": "clickhouse_cluster", "target_identity": "database:logs", "query": "SELECT ...", "timeout_s": 45}}
```

2. **新增规则 17**：明确要求每个 action 必须同时满足三个条件：

```
17) 每个 action 必须同时满足以下条件才可执行：
    - command_spec.tool 必须是 generic_exec / kubectl_clickhouse_query 之一
    - command_spec.args.target_kind 不能为空
    - command_spec.args.target_identity 不能为空
    缺少任意一项都会导致该 action 被标记为 spec_blocked，整个计划可能被阻断无法继续。
```

### Section 2: 服务端预填

**文件**: `ai-service/ai/followup_planning_helpers.py` + `ai-service/ai/followup_command_spec.py`

新增 `_prefill_command_spec(action, analysis_context)` 函数，在所有 action 进入编译前调用：

```python
def _prefill_command_spec(action: Dict[str, Any], analysis_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    自动补全 command_spec 中缺失的 target_kind / target_identity / timeout_s。
    仅在字段完全缺失时注入，不覆盖 AI 已填的值。
    """
    spec = action.get("command_spec")
    if not isinstance(spec, dict):
        return action
    args = spec.get("args")
    if not isinstance(args, dict):
        args = {}
        spec["args"] = args

    # 推断 target_kind
    if not args.get("target_kind") and not spec.get("target_kind"):
        command = str(args.get("command", "") or spec.get("command", "")).lower()
        if "kubectl" in command:
            args["target_kind"] = "k8s_cluster"
        elif "clickhouse" in command:
            args["target_kind"] = "clickhouse_cluster"
        else:
            args["target_kind"] = "runtime_node"

    # 推断 target_identity
    if not args.get("target_identity") and not spec.get("target_identity"):
        t_kind = args.get("target_kind", "") or spec.get("target_kind", "")
        if t_kind == "k8s_cluster":
            ns = _resolve_namespace_from_context(analysis_context)
            args["target_identity"] = f"namespace:{ns}" if ns else "namespace:default"
        elif t_kind == "clickhouse_cluster":
            args["target_identity"] = "database:logs"
        else:
            args["target_identity"] = "runtime:local"

    # 补全 timeout_s
    if not args.get("timeout_s"):
        args["timeout_s"] = 30

    action["command_spec"] = spec
    return action
```

调用位置：在 `_build_followup_react_loop` 的 action 遍历循环中，compile 之前调用。

### Section 3: 命令修复增强

**文件**: `ai-service/ai/followup_planning_helpers.py` + `ai-service/ai/followup_command_spec.py`

1. **扩展 COMMAND_REPAIR_PROMPT 触发时机**：
   - `compile_followup_command_spec` 返回 `reason="glued_command_tokens"` 或 `reason="invalid_kubectl_token"` 时
   - 调用现有 `COMMAND_REPAIR_PROMPT` 修复命令文本
   - 用修复后的文本重新编译一次
   - 若仍失败，才标记 spec_blocked

2. **模板兜底增强**：
   - 增加对 `kubectl get pods`, `kubectl logs`, `kubectl describe pod` 等常见命令的模板生成
   - 自动注入 `target_kind=k8s_cluster` 和 `target_identity=namespace:<ns>`

## 文件改动清单

| 文件 | 改动 |
|------|------|
| `ai-service/ai/langchain_runtime/prompts.py` | 规则 5 增加 JSON 示例；新增规则 17 |
| `ai-service/ai/followup_planning_helpers.py` | 新增 `_prefill_command_spec()`；在 action 循环中调用；增加 repair 重试 |
| `ai-service/ai/followup_command_spec.py` | 在 compile 中集成 repair 重试逻辑 |
| `ai-service/ai/followup_command.py` | 可选：增强命令修复器的覆盖范围 |

## 风险与注意事项

- 服务端预填可能在极少数情况下注入错误的 target_kind（如混合命令同时包含 kubectl 和 clickhouse），但不会比目前直接阻断更差
- prompt 增加 JSON 示例会增加 token 消耗约 200-300 tokens/请求
- 预填只补全缺失字段，不修改已填写的字段，避免干扰 AI 的明确选择
