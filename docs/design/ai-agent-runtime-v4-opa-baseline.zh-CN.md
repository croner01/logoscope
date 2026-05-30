# AI Agent Runtime V4 OPA 策略基线（中文版）

> 关联文档：  
> - `docs/design/ai-agent-runtime-v4-blueprint.zh-CN.md`  
> - `docs/design/ai-agent-runtime-v4-task-breakdown.zh-CN.md`  
> - `docs/design/ai-agent-runtime-v4-api-contract.zh-CN.md`  
> 状态：Draft（评审版）

---

## 1. 文档目标

定义 V4 第一版 OPA 策略基线，明确：

1. 策略输入输出合同
2. 决策结果与执行动作映射
3. 第一批策略包范围
4. 回放测试样本与切主阈值

---

## 2. 策略定位

OPA 在 V4 中是策略唯一裁决源（PDP）。  
业务服务（PEP）只负责：

1. 组装输入
2. 调用 OPA
3. 执行 OPA 决策
4. 记录 `decision_id`

### 📌 Decision: policy/opa-single-authority
- **Value**: OPA 成为命令策略唯一裁决源，应用代码不保留平行最终裁决逻辑
- **Layer**: cross-cutting
- **Tags**: opa, pdp, policy-authority
- **Rationale**: 防止策略分叉与不可审计行为
- **Alternatives**: OPA 与代码双主裁决
- **Tradeoffs**: 需要严格治理策略发布流程，但一致性更高

### 🚫 Constraint: security
- **Rule**: 任何策略服务异常必须 fail-closed，不允许默认放行
- **Priority**: critical
- **Tags**: fail-closed, policy-availability

---

## 3. 策略输入合同（input）

## 3.1 顶层结构

```json
{
  "request_id": "req-001",
  "trace_id": "trace-001",
  "thread_id": "thr-001",
  "run_id": "run-001",
  "action_id": "act-001",
  "timestamp": "2026-03-23T11:00:00.000Z",
  "actor": {
    "type": "ai_agent",
    "id": "runtime-v4",
    "tenant": "default"
  },
  "command": {
    "raw": "kubectl get pods -n islap",
    "normalized": "kubectl get pods -n islap",
    "intent": "query",
    "family": "kubernetes",
    "risk_level": "low",
    "requires_write": false
  },
  "target": {
    "kind": "k8s_cluster",
    "identity": "namespace:islap",
    "registered": true,
    "capabilities": ["k8s.read", "k8s.logs"]
  },
  "executor": {
    "type": "sandbox_pod",
    "profile": "toolbox-k8s-readonly",
    "dispatch_ready": true
  },
  "contract": {
    "diagnosis_contract_complete": true,
    "missing_fields": []
  },
  "approval": {
    "requested": false,
    "confirmed": false,
    "elevated": false
  },
  "runtime_options": {
    "auto_exec_readonly": true
  }
}
```

## 3.2 必填字段

1. `run_id`
2. `action_id`
3. `command.normalized`
4. `target.kind`
5. `target.identity`
6. `executor.profile`
7. `contract.diagnosis_contract_complete`

---

## 4. 策略输出合同（result）

```json
{
  "decision": "allow",
  "decision_reason": "readonly whitelisted command",
  "approval_policy": "auto_execute",
  "requires_confirmation": false,
  "requires_elevation": false,
  "manual_required": false,
  "deny_code": "",
  "policy_version": "runtime-command-v1.0.0",
  "tags": ["kubernetes", "readonly"]
}
```

`decision` 枚举：

1. `allow`
2. `confirm`
3. `elevate`
4. `deny`
5. `manual_required`

### 映射规则

1. `allow` -> 可执行
2. `confirm` -> 进入审批确认
3. `elevate` -> 进入提权审批
4. `manual_required` -> 等待人工补充或人工执行
5. `deny` -> 直接拒绝

---

## 5. 第一版策略包范围

## 5.1 Package 划分建议

1. `runtime.command.v1`
2. `runtime.target.v1`
3. `runtime.contract.v1`
4. `runtime.approval.v1`
5. `runtime.override.v1`（紧急豁免，默认关闭）

## 5.2 策略优先级（从高到低）

1. 安全强约束（deny）
2. 目标治理（manual_required/deny）
3. 合同治理（deny/manual_required）
4. 审批策略（confirm/elevate）
5. 自动执行策略（allow）

### 📌 Decision: policy/evaluation-order
- **Value**: 策略采用“先拒绝后放行”顺序，先跑强约束再跑自动化规则
- **Layer**: cross-cutting
- **Tags**: policy-order, deny-first
- **Rationale**: 降低误放行概率
- **Alternatives**: 先判自动执行，再补拒绝条件
- **Tradeoffs**: 规则编写更严格，但安全性更高

---

## 6. 基线规则矩阵（v1）

## 6.1 只读命令

1. 目标已注册 + 能力匹配 + dispatch_ready + 合同完整  
结果：`allow` 或 `confirm`（由白名单模板命中决定）
2. 白名单未命中  
结果：`confirm`
3. 未知目标  
结果：`manual_required`

## 6.2 写命令

1. 任意写命令必须 `elevate`（进入审批）
2. 若 `contract_complete=false`  
结果：`deny`（`deny_code=CONTRACT_INCOMPLETE`）
3. 若目标未注册或能力缺失  
结果：`manual_required` 或 `deny`

## 6.3 执行器异常

1. `dispatch_ready=false`  
结果：`deny`（`deny_code=EXECUTOR_UNAVAILABLE`）

## 6.4 策略服务异常

1. OPA 调用失败  
结果：业务侧 fail-closed，返回 `policy_unavailable`

### 🚫 Constraint: security
- **Rule**: 写命令不得由策略直接返回 allow，即便 confirmed/elevated 参数为 true
- **Priority**: critical
- **Tags**: write-command, approval-chain

---

## 7. 参考 Rego 结构（示意）

```rego
package runtime.command.v1

default decision = {
  "decision": "deny",
  "decision_reason": "default deny",
  "approval_policy": "deny",
  "requires_confirmation": false,
  "requires_elevation": false,
  "manual_required": false,
  "deny_code": "DEFAULT_DENY"
}

decision = out {
  input.target.registered == false
  out := {
    "decision": "manual_required",
    "decision_reason": "target not registered",
    "approval_policy": "manual_required",
    "requires_confirmation": false,
    "requires_elevation": false,
    "manual_required": true,
    "deny_code": "TARGET_UNREGISTERED"
  }
}

decision = out {
  input.command.requires_write
  not input.contract.diagnosis_contract_complete
  out := {
    "decision": "deny",
    "decision_reason": "diagnosis_contract incomplete",
    "approval_policy": "deny",
    "requires_confirmation": false,
    "requires_elevation": false,
    "manual_required": false,
    "deny_code": "CONTRACT_INCOMPLETE"
  }
}
```

注：示意仅用于评审，不代表最终完整策略。

---

## 8. 影子模式与切主阈值

## 8.1 影子模式要求

1. 同一请求同时计算 `legacy_decision` 与 `opa_decision`
2. 不影响真实执行，先记录差异
3. 每日输出差异报告

## 8.2 切主阈值建议

1. 连续 7 天差异率低于 1%
2. 高风险命令分支差异率低于 0.1%
3. 无 P0 误放行

### 📌 Decision: policy/cutover-gate
- **Value**: OPA 切主需满足可量化差异阈值，不采用拍脑袋切换
- **Layer**: infrastructure
- **Tags**: cutover, shadow, risk-control
- **Rationale**: 策略误杀和误放行都属于高风险变更
- **Alternatives**: 直接切主
- **Tradeoffs**: 上线周期变长，但风险可控

---

## 9. 回放样本集（最小集）

1. 只读白名单命令（allow）
2. 只读非白名单命令（confirm）
3. 写命令合同完整（elevate）
4. 写命令合同缺失（deny）
5. 未注册目标（manual_required）
6. 执行器不可用（deny）
7. 审批 reject 后重试命令（replan 链路验证）
8. 高危未知命令（deny）

---

## 10. 审计字段要求

每次策略决策必须至少记录：

1. `decision_id`
2. `run_id`
3. `action_id`
4. `policy_package`
5. `policy_version`
6. `input_hash`
7. `decision`
8. `decision_reason`
9. `created_at`

---

## 11. 评审检查项

1. 是否同意 v1 策略矩阵
2. 是否同意 deny-first 顺序
3. 是否同意影子切主阈值
4. 是否同意紧急豁免包默认关闭
5. 是否同意审计字段最小集合

评审通过后输出：

1. `exec-service/policies/` 初始 Rego 实现
2. `exec-service/policies/tests/` 回放样本测试
3. OPA 部署与发布流程文档
