# AI Agent Runtime V4 评审会 Checklist（中文版）

> 适用范围：V4 蓝图评审会（架构、接口、策略、迁移）  
> 关联文档：  
> - `docs/design/ai-agent-runtime-v4-blueprint.zh-CN.md`  
> - `docs/design/ai-agent-runtime-v4-task-breakdown.zh-CN.md`  
> - `docs/design/ai-agent-runtime-v4-api-contract.zh-CN.md`  
> - `docs/design/ai-agent-runtime-v4-opa-baseline.zh-CN.md`

---

## 1. 会议目标（必须当场拍板）

1. 确认 V4 架构：`Temporal 外环 + LangGraph 内环`
2. 确认执行红线：第一阶段删除 local fallback
3. 确认策略红线：OPA 单一裁决、fail-closed
4. 确认治理红线：写命令三条件 gate
5. 确认 API v2 模型：thread-run-action
6. 确认里程碑顺序与负责人

---

## 2. 会议角色

1. 主持人：技术负责人（你）
2. 架构 owner：AI runtime owner
3. 执行面 owner：exec-service owner
4. 策略 owner：OPA/安全 owner
5. 前端 owner：runtime UI owner
6. QA owner：测试/回归 owner

---

## 3. 90 分钟议程（建议）

1. 0-10 分钟：目标与约束确认
2. 10-30 分钟：V4 总体架构评审
3. 30-45 分钟：API v2 合同评审
4. 45-60 分钟：OPA 策略基线评审
5. 60-75 分钟：迁移与回滚策略评审
6. 75-90 分钟：任务分配与里程碑冻结

---

## 4. 架构评审检查项

1. 是否明确 Temporal 是 run 生命周期唯一权威
2. 是否明确 LangGraph 只维护推理子图状态
3. 是否确认未知目标默认 `manual_required`
4. 是否确认执行链不允许 local fallback
5. 是否确认所有写命令必须审批+策略+合同三重 gate

通过标准：全部同意，且无“后续再讨论”的关键悬置项。

---

## 5. API v2 评审检查项

1. 是否确认 `thread/run/action/approval/policy_decision/event` 六类对象
2. 是否确认 `events/stream` 为前端唯一事实来源
3. 是否确认 `after_seq` 断点续传语义
4. 是否确认审批、中断、用户补充输入接口
5. 是否确认 API v1 仅短期并存（只读兼容）

通过标准：接口路径、状态机、错误码分层全部冻结。

---

## 6. OPA 策略评审检查项

1. 是否确认 deny-first 策略顺序
2. 是否确认写命令不可能直接 allow
3. 是否确认策略异常 fail-closed
4. 是否确认影子模式切主阈值
5. 是否确认 `decision_id` 与 `run_id` 绑定审计

通过标准：输入输出 schema 冻结，v1 策略矩阵冻结。

---

## 7. 迁移与回滚评审检查项

1. Milestone 顺序是否接受（A->B->C->D/E->F/G）
2. 是否接受停服重构窗口
3. 回滚是否允许恢复不合规路径（答案必须否）
4. 是否确认前端切换策略与 feature flag 方案
5. 是否确认数据迁移与审计保序策略

通过标准：回滚策略明确且不突破安全红线。

---

## 8. 风险清单（会议需逐项确认）

1. 双层编排状态漂移风险
2. OPA 误杀/误放行风险
3. 沙箱执行器资源瓶颈风险
4. API v2 切换期间前端状态不一致风险
5. 合同门禁误阻断风险

每项都要有 owner 和缓解动作。

---

## 9. 会议输出模板（会后 24h 内）

1. 《评审结论》：通过/有条件通过/驳回
2. 《冻结项清单》：不可变更项
3. 《待补项清单》：补充材料与截止时间
4. 《责任人清单》：Track owner 与 DRI
5. 《里程碑日期》：M1-M5 目标日期

---

## 10. 一票否决项

以下任一项未通过，评审结论必须为“有条件通过”或“驳回”：

1. local fallback 未删除
2. 写命令 gate 未锁定
3. OPA 非单一裁决源
4. `decision_id` 无法关联 `run_id`
5. 未知目标不走 manual_required

