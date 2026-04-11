# AI Agent 命令粘连治理执行计划（V1）

## 1. 文档信息
- 版本：v1
- 状态：Execution Draft
- 更新时间：2026-03-30
- 关联设计：`docs/design/ai-agent-command-glue-architecture-v1.zh-CN.md`

## 2. 执行目标（两周）
1. 将 `no_executable_query_candidates` 场景从“停在提示”提升到“可前推执行模板 + 可回填结构化参数”。
2. 将命令粘连导致的 `semantic_incomplete` 占比显著下降。
3. 保持安全边界不退化（无自由文本绕过、无未授权写入）。

## 3. 范围与非范围

## 3.1 In Scope
1. `ai-service`：`followup_command_spec`、`followup_planning_helpers`、`followup_orchestration_helpers`。
2. `exec-service`：`policy` 分类与 whitelist 结果可观测。
3. `frontend`：Runtime Lab 的 reason/fix-hint/模板命令可视化与复制。
4. 指标与告警：粘连原因、修复成功率、转化率。

## 3.2 Out of Scope
1. 新命令执行后端（如新增 SSH 执行器类型）。
2. 非追问链路（主分析初次问答）的大规模重构。
3. 权限模型重设计（OPA 策略大改）。

## 4. 角色与分工（建议）
1. Runtime Backend Owner（AI Service）：动作编译/修复/ReAct。
2. Exec Owner（Exec Service）：precheck/policy/whitelist/审计。
3. Frontend Owner：Runtime Lab 展示与交互。
4. QA Owner：回归矩阵、稳定性基线、灰度验收。
5. SRE Owner：指标落盘、告警阈值、灰度与回滚。

## 5. 工作流拆解（Workstreams）

## WS-A：编译器与修复能力增强（关键路径）

### A1. 统一粘连 reason 分组映射
- 目标：保留旧 reason 兼容，新增内部聚合分组。
- 涉及文件：
  - `/root/logoscope/ai-service/ai/followup_command_spec.py`
  - `/root/logoscope/ai-service/ai/followup_planning_helpers.py`
- 交付：`reason_group` 映射函数 + 单测。
- DoD：
  1. 现有 reason 不变；
  2. 新增 grouping 可用于指标标签；
  3. 单测覆盖 >= 8 个 reason。

### A2. 扩展自修复覆盖面
- 目标：提升 `glued_command_tokens`、`glued_sql_tokens` 的可修复率。
- 涉及文件：
  - `/root/logoscope/ai-service/ai/followup_command_spec.py`
  - `/root/logoscope/ai-service/tests/test_followup_command_spec.py`
- 交付：新增修复规则（kubectl/sql）与回归样本。
- DoD：
  1. 新增样本全部通过；
  2. 错误修复后能复编译通过；
  3. 不引入高危命令放行。

### A3. 模板命令自动结构化草稿
- 目标：把模板命令转换成 `command_spec` 草稿，减少人工回填。
- 涉及文件：
  - `/root/logoscope/ai-service/ai/followup_planning_helpers.py`
  - `/root/logoscope/ai-service/ai/followup_orchestration_helpers.py`
- 交付：`template -> command_spec` 推断器。
- DoD：
  1. 至少覆盖 `kubectl logs/get/top` 和 `psql -c` 基础模板；
  2. 推断失败时可回退到当前逻辑；
  3. 有明确 reason，不 silent fail。

## WS-B：ReAct 重规划前推

### B1. 无候选命令场景统一进入 replan
- 目标：避免“plan=5 observed=0 replan=false”的误收敛。
- 涉及文件：
  - `/root/logoscope/ai-service/ai/followup_planning_helpers.py`
- 交付：replan 条件统一化 + 统计字段。
- DoD：
  1. `executable=0 + query_like>0` 必须 `replan=true`；
  2. `react_loop.plan` 包含 `spec_blocked_actions`。

### B2. thought summary 输出可执行下一步
- 目标：总结必须“再前进一步”。
- 涉及文件：
  - `/root/logoscope/ai-service/ai/followup_orchestration_helpers.py`
- 交付：无候选时附模板命令 + 回填说明。
- DoD：
  1. summary detail 含“建议先补全并执行”；
  2. 回归测试覆盖。

## WS-C：前端可操作性增强

### C1. reason/fix-hint 展示
- 目标：用户能看懂“为什么不可执行”。
- 涉及文件（建议）：
  - `/root/logoscope/frontend/src/features/ai-runtime/utils/runtimeView.ts`
  - `/root/logoscope/frontend/src/features/ai-runtime/utils/runtimeTranscript.ts`
  - `/root/logoscope/frontend/src/pages/AIRuntimePlayground.tsx`
- 交付：按 reason_group 的 UI 标签与修复建议面板。
- DoD：
  1. 语义不完整动作可见 reason；
  2. 支持一键复制模板命令；
  3. 默认不暴露敏感字段。

### C2. 模板命令回填入口
- 目标：支持从模板快速提交下一轮输入。
- 交付：`Use Template as Input` 交互。
- DoD：
  1. 一键注入输入框；
  2. 保留历史上下文；
  3. 不覆盖用户已编辑文本（提示二次确认）。

## WS-D：可观测性与告警

### D1. 指标埋点
- 涉及服务：`ai-service`、`exec-service`
- 指标：
  1. `ai_followup_action_spec_compile_failed_total{reason}`
  2. `ai_followup_glue_repair_success_total{reason}`
  3. `ai_followup_no_executable_query_candidates_total`
  4. `ai_followup_semantic_incomplete_total{reason}`
- DoD：
  1. 本地与测试环境可采集；
  2. 指标标签可控，避免高基数。

### D2. 告警规则与看板
- 交付：Prometheus 告警 + Grafana 面板（或现有监控等价方案）。
- DoD：
  1. 三条核心告警上线；
  2. 告警文案含 runbook 链接。

## WS-E：测试与回归

### E1. 单测矩阵
- 目标：reason 检测、修复、replan 逻辑全覆盖。
- 涉及：
  - `/root/logoscope/ai-service/tests/test_followup_command_spec.py`
  - `/root/logoscope/ai-service/tests/test_followup_planning_helpers.py`
  - `/root/logoscope/ai-service/tests/test_followup_exec_streaming.py`

### E2. 集成回归
- 目标：followup -> precheck -> execute -> replan 全链路。
- 涉及：
  - `/root/logoscope/scripts/ai-runtime-v2-regression-check.sh`
  - `/root/logoscope/reports/ai-runtime-v2-regression/`

### E3. 前端关键路径
- 目标：Runtime Lab 页面事件与状态一致。
- 涉及：
  - `/root/logoscope/frontend/scripts/aiAgentRuntime.test.mjs`

## 6. 里程碑与排期（10 个工作日）

### Day 1-2
1. A1 reason 分组映射。
2. B1 replan 条件收敛。
3. E1 补单测骨架。

### Day 3-4
1. A2 编译器修复增强。
2. B2 summary 前推。
3. E1 完整单测。

### Day 5
1. C1 reason/fix-hint 前端展示。
2. D1 指标埋点联调。

### Day 6-7
1. A3 模板命令结构化草稿。
2. C2 模板回填入口。
3. E2 集成回归首轮。

### Day 8
1. D2 告警与面板。
2. E3 前端关键路径验证。

### Day 9
1. 预发布灰度（10% 流量或单租户）。
2. 对比基线指标。

### Day 10
1. 全量发布。
2. 发布复盘与阈值微调。

## 7. PR 切片建议（可直接开）
1. PR-1 `ai-service`: reason group + metrics labels。
2. PR-2 `ai-service`: glued command/sql repair enhancement。
3. PR-3 `ai-service`: no-exec replan hardening + template forward。
4. PR-4 `frontend`: reason/fix-hint/template actions。
5. PR-5 `exec-service`: precheck observability harmonization。
6. PR-6 `scripts/tests`: regression matrix + CI hooks。

## 8. Issue 模板（建议）

### 模板 A：功能改造
- 标题：`[AI-Runtime][Glue] <模块> <目标>`
- 描述字段：
  1. 背景（具体失败样例）
  2. 目标（可量化）
  3. 设计约束（安全/兼容）
  4. 变更文件
  5. 验收标准
  6. 回滚策略

### 模板 B：回归缺陷
- 标题：`[AI-Runtime][Regression] <reason> in <flow>`
- 描述字段：
  1. 复现输入
  2. 期望行为
  3. 实际行为
  4. run_id / 事件序列
  5. 影响范围

## 9. 验收口径（上线门槛）
1. `no_executable_query_candidates_rate` 较基线下降 >= 20%。
2. `semantic_incomplete_total{glue*}` 较基线下降 >= 25%。
3. `first_round_auto_exec_rate` 提升 >= 15%。
4. 安全回归：
   - 未出现绕过 `command_spec` 的执行路径；
   - 写命令仍需确认/提权。

## 10. 灰度与回滚

### 10.1 灰度顺序
1. Dev 环境（全量）
2. Staging（全量）
3. Prod 单租户/单命名空间
4. Prod 全量

### 10.2 回滚开关（建议新增）
1. `AI_FOLLOWUP_GLUE_AUTO_REPAIR_ENABLED`
2. `AI_FOLLOWUP_COMMAND_TEMPLATE_HINTS_ENABLED`
3. `AI_FOLLOWUP_TEMPLATE_AUTOSPEC_ENABLED`

### 10.3 回滚策略
1. 先关 autospec；
2. 再关 auto-repair；
3. 保留现有 precheck + policy + approval 边界。

## 11. 依赖与阻塞项
1. 前端展示依赖后端 reason/fix-hint 字段稳定。
2. 指标告警依赖监控命名约定与采集链路。
3. 灰度需 SRE 提供按租户/命名空间路由能力。

## 12. 会议建议（30 分钟）
1. 前 10 分钟：过目标与状态机。
2. 中 10 分钟：逐条确认 PR 切片与 owner。
3. 后 10 分钟：确认上线闸门与回滚开关。

## 13. 关联文件
1. `/root/logoscope/docs/design/ai-agent-command-glue-architecture-v1.zh-CN.md`
2. `/root/logoscope/ai-service/ai/followup_command_spec.py`
3. `/root/logoscope/ai-service/ai/followup_planning_helpers.py`
4. `/root/logoscope/ai-service/ai/followup_orchestration_helpers.py`
5. `/root/logoscope/exec-service/api/execute.py`
6. `/root/logoscope/exec-service/core/policy.py`
7. `/root/logoscope/frontend/src/pages/AIRuntimePlayground.tsx`
