# AI Agent 命令粘连治理架构与落地方案（V1）

## 1. 文档信息
- 文档版本：v1
- 状态：Draft（可评审）
- 更新时间：2026-03-30
- 作者：Codex
- 适用范围：`ai-service` 追问链路、`exec-service` 预检与执行链路

## 2. 问题定义

### 2.1 现象
在 AI Runtime Lab 的追问场景中，系统经常出现：
1. 证据缺口（`evidence_gaps`）明确存在；
2. 但动作列表中的命令因“粘连/结构化参数不完整”被判为不可执行；
3. 最终进入“缺口未命中，且暂无可执行候选命令”。

### 2.2 本质
问题不是“是否知道缺什么证据”，而是“是否能形成可通过编译与策略预检的结构化命令（`command_spec`）”。

### 2.3 设计目标
1. 提升“首轮可执行查询动作命中率”；
2. 降低 `semantic_incomplete` 比例；
3. 保持现有安全边界（不允许自由文本绕过结构化执行）；
4. 在无法执行时，保证系统能前推一步，给出可落地命令模板与补参路径。

## 3. 现状架构（分层）

## 3.1 编排层（Follow-up Core）
- 入口：`_run_follow_up_analysis_core`
- 关键职责：构建动作、执行 ReAct 闭环、汇总 `react_loop`
- 代码：`/root/logoscope/ai-service/api/ai.py`

核心路径：
1. LLM 返回 answer + actions；
2. `_build_followup_actions` 归一化动作；
3. `_run_followup_auto_exec_react_loop` 多轮执行；
4. `_build_followup_react_loop` 产出 observe/replan 状态；
5. `_append_followup_react_summary` 回写回答。

## 3.2 动作规范化层（Planning）
- 代码：`/root/logoscope/ai-service/ai/followup_planning_helpers.py`
- 核心函数：`_build_followup_actions`、`_try_repair_structured_spec`、`_build_followup_react_loop`

职责：
1. 对 LLM 动作进行 `command_spec` 编译；
2. 编译失败触发 `build_command_spec_self_repair_payload` 自修复；
3. 无法修复时下调为 `executable=false` 并保留 `reason`；
4. 生成 ReAct `replan.next_actions` 与 `replan.items`。

## 3.3 命令编译与粘连检测层（Command Spec Compiler）
- 代码：`/root/logoscope/ai-service/ai/followup_command_spec.py`
- 核心函数：`compile_followup_command_spec`

职责：
1. 将输入标准化为 `command_spec(tool+args)`；
2. 检测粘连、非法 token、target 缺失；
3. 返回结构化错误原因；
4. 编译通过后返回 canonical command + canonical spec。

## 3.4 ReAct 执行层（Orchestration）
- 代码：`/root/logoscope/ai-service/ai/followup_orchestration_helpers.py`
- 核心函数：`_run_followup_auto_exec_react_loop`、`_run_followup_readonly_auto_exec`

职责：
1. 每轮只选 `executable=true && command_type=query`；
2. 无 `command_spec` 或编译失败直接 `semantic_incomplete`；
3. precheck 通过才会发起 exec run；
4. 执行后基于 observation 进行 replan。

## 3.5 执行预检层（Exec Precheck）
- 代码：`/root/logoscope/exec-service/api/execute.py`
- 核心函数：`precheck_command`

职责：
1. `classify_command_with_auto_rewrite` 分类与自动重写；
2. `evaluate_query_whitelist` 白名单校验；
3. target capability / runtime preflight 校验；
4. 返回 `ok / confirmation_required / elevation_required / permission_required`。

## 3.6 策略与模板层（Policy）
- 代码：`/root/logoscope/exec-service/core/policy.py`
- 核心函数：`classify_command_with_auto_rewrite`、`evaluate_query_whitelist`

职责：
1. 命令安全 token 解析；
2. 支持的 query command 模板判定；
3. query whitelist 与免审批边界控制。

## 3.7 运行态事件层（Runtime Service）
- 代码：`/root/logoscope/exec-service/core/runtime_service.py`
- 职责：run 生命周期管理、流式事件输出、stdout/stderr 聚合、审计落库。

## 4. 命令粘连治理状态机

```text
Drafted
  -> SpecCompiling
      -> SpecCompiled
      -> SpecInvalid(reason)
SpecInvalid
  -> AutoRepairing(reason)
      -> SpecCompiling
          -> SpecCompiled
          -> SemanticIncomplete
SpecCompiled
  -> Precheck
      -> Executable
      -> ConfirmationRequired
      -> ElevationRequired
      -> PermissionRequired
Executable
  -> Executing
      -> Executed(exit=0)
      -> Failed(exit!=0)
SemanticIncomplete/PermissionRequired/Failed
  -> Replan
```

关键原则：
1. `SpecInvalid` 不进入自动执行；
2. 只有 `SpecCompiled + Precheck=ok + query whitelist pass` 才可无人工执行；
3. 每轮执行后都要产生结构化观察并驱动 replan。

## 5. 粘连错误码体系（现状 + 规范化建议）

## 5.1 现状错误码（已实现）
1. `glued_command_tokens`
2. `glued_sql_tokens`
3. `invalid_kubectl_token`
4. `suspicious_selector_namespace_glue`
5. `unsupported_command_head`
6. `missing_or_invalid_command_spec`
7. `missing_target_identity`
8. `clickhouse_multi_statement_not_allowed`

## 5.2 建议分组（文档规范，不破坏兼容）
1. `GLUE_SYNTAX`: `glued_command_tokens`, `glued_sql_tokens`
2. `GLUE_K8S_TOKEN`: `invalid_kubectl_token`, `suspicious_selector_namespace_glue`
3. `SPEC_MISSING`: `missing_or_invalid_command_spec`, `missing_target_identity`
4. `SECURITY_GUARD`: `unsupported_command_head`, `clickhouse_multi_statement_not_allowed`

说明：对外 API 继续返回现有 reason，内部指标聚合按分组映射。

## 6. 关键处理算法（详细）

## 6.1 规划阶段：动作归一化与修复
输入：LLM actions（可能含自由文本 command 或半结构化 command_spec）

处理流程：
1. `normalize_followup_command_spec` 标准化；
2. `compile_followup_command_spec` 编译；
3. 若失败：`build_command_spec_self_repair_payload` 产出建议 spec；
4. 用建议 spec 再编译；
5. 若仍失败且含高风险/链式 shell 痕迹，清空 command，保留 reason；
6. 输出 action：`command_spec`、`executable`、`reason`。

## 6.2 执行阶段：只读自动执行闸门
处理流程：
1. 若 `command_spec` 为空：`semantic_incomplete`；
2. 若编译失败：`semantic_incomplete`（附 compile reason）；
3. 执行 precheck：策略、白名单、能力门禁；
4. precheck `ok` 且是安全 query head 才可执行；
5. 其它状态进入 `replan`。

## 6.3 无候选命令时的前推
处理流程：
1. 识别 `no_executable_query_candidates`；
2. 基于 action title/purpose/reason 生成命令模板；
3. 写入 `replan.next_actions`；
4. 在 thought summary 中提示“补全 command_spec(tool+args+target_identity) 后继续”。

## 7. 数据契约（建议作为评审基线）

## 7.1 Action（规划后）
```json
{
  "id": "lc-1",
  "title": "查询Temporal日志",
  "action_type": "query",
  "command_type": "query",
  "command": "kubectl -n islap logs deploy/temporal --since=15m --tail=200",
  "command_spec": {
    "tool": "generic_exec",
    "args": {
      "command_argv": ["kubectl", "-n", "islap", "logs", "deploy/temporal", "--since=15m", "--tail=200"],
      "target_kind": "k8s_cluster",
      "target_identity": "namespace:islap",
      "timeout_s": 60
    }
  },
  "executable": true,
  "reason": ""
}
```

## 7.2 Observation（执行失败/语义不完整）
```json
{
  "status": "semantic_incomplete",
  "action_id": "lc-2",
  "command": "",
  "message": "glued_sql_tokens: sql keyword 'SELECT' must be separated by spaces",
  "auto_executed": false
}
```

## 7.3 Replan（无可执行候选）
```json
{
  "replan": {
    "needed": true,
    "items": [
      {
        "reason": "no_executable_query_candidates",
        "execution_disposition": "structured_spec_required",
        "summary": "当前计划没有可自动执行的结构化查询命令，请先补全 command_spec 后继续执行。"
      },
      {
        "reason": "command_template_suggested",
        "summary": "补全结构化命令后执行：kubectl -n islap logs deploy/temporal --since=15m --tail=200（补齐错误上下文与调用链线索）"
      }
    ]
  }
}
```

## 8. 非功能约束
1. 不允许通过自由文本 command 绕过 `command_spec` 编译器；
2. 不允许降级成 shell substitution 执行（默认关闭）；
3. 写命令必须经过确认/提权，不参与 readonly auto-exec；
4. 白名单模板未命中的 query 默认需要人工确认。

## 9. 可观测性与告警

## 9.1 指标定义
1. `ai_followup_action_spec_compile_success_total`
2. `ai_followup_action_spec_compile_failed_total{reason}`
3. `ai_followup_glue_repair_attempt_total{reason}`
4. `ai_followup_glue_repair_success_total{reason}`
5. `ai_followup_no_executable_query_candidates_total`
6. `ai_followup_semantic_incomplete_total{reason}`
7. `ai_followup_first_round_auto_exec_success_total`

## 9.2 告警建议
1. `no_executable_query_candidates_rate > 20%` 连续 15 分钟告警；
2. `glued_sql_tokens` 24h 环比上升 > 50% 告警；
3. `semantic_incomplete` 占比 > 30% 告警。

## 10. 两周实施计划（可拆 PR）

## 第 1 周：稳定性与契约收敛
1. PR-1：补齐 reason 分组映射与统一埋点。
2. PR-2：完善 `build_command_spec_self_repair_payload` 的修复覆盖（重点 kubectl/sql 粘连）。
3. PR-3：在前端 Runtime Lab 增加 `reason -> fix_hint` 展示与“一键复制命令模板”。
4. PR-4：新增回归测试矩阵（粘连样本集 + 目标缺失样本集）。

验收标准：
1. `glue_repair_success_rate >= 40%`（基线提升）；
2. 新增 case 不出现“仅报错不前推”的总结。

## 第 2 周：执行转化率优化
1. PR-5：将模板命令自动生成 `command_spec` 草稿（默认 target_identity 推断）。
2. PR-6：加入“同会话二次规划”快速通道，减少用户重复输入。
3. PR-7：新增 dashboard + 告警规则。
4. PR-8：灰度开关与回滚开关（按环境和租户）。

验收标准：
1. `first_round_auto_exec_rate` 提升 >= 15%；
2. `semantic_incomplete_to_executable_conversion_rate` 提升 >= 20%。

## 11. 风险与回滚

## 11.1 风险
1. 自动修复过度导致误修（语义偏离原命令）；
2. target_identity 推断错误导致预检拒绝率升高；
3. 新增模板建议可能引入“看起来可执行但环境不可达”的噪声。

## 11.2 回滚策略
1. 开关化：`AI_FOLLOWUP_GLUE_AUTO_REPAIR_ENABLED`（建议新增）；
2. 模板建议开关：`AI_FOLLOWUP_COMMAND_TEMPLATE_HINTS_ENABLED`（建议新增）；
3. 问题出现时仅关闭“自动修复/模板提示”，保留核心执行安全链路。

## 12. 测试矩阵
1. 单测：编译器 reason 识别、修复 payload、replan 生成。
2. 集成：followup -> precheck -> execute -> replan 全链路。
3. 回归：典型故障域（k8s logs、clickhouse sql、postgres psql）。
4. E2E：AI Runtime Lab 对话 + 事件流 + action 状态展示一致性。

## 13. 评审关注点
1. 是否允许在“修复失败”时继续保留 display-only command；
2. `target_identity` 推断规则是否需要租户级差异化；
3. query whitelist 模板是否需要按环境扩展（例如 openstack 场景）。

## 14. 附：关键代码位置
1. `/root/logoscope/ai-service/api/ai.py`
2. `/root/logoscope/ai-service/ai/followup_planning_helpers.py`
3. `/root/logoscope/ai-service/ai/followup_orchestration_helpers.py`
4. `/root/logoscope/ai-service/ai/followup_command_spec.py`
5. `/root/logoscope/ai-service/ai/langchain_runtime/prompts.py`
6. `/root/logoscope/ai-service/ai/langchain_runtime/service.py`
7. `/root/logoscope/exec-service/api/execute.py`
8. `/root/logoscope/exec-service/core/policy.py`
9. `/root/logoscope/exec-service/core/runtime_service.py`
