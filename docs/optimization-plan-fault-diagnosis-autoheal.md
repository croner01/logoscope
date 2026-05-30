# 故障诊断与自愈全流程优化方案

**文档版本**: 1.0  
**日期**: 2026-05-17  
**分支**: feature/openhands-runtime-v4-backend  
**作者**: AI 架构分析 & 优化设计

---

## 一、背景与问题诊断

### 1.1 现状分析

通过对 `feature/openhands-runtime-v4-backend` 分支代码的全面审查，发现以下主要问题：

#### Skills 规范问题
| 问题 | 涉及文件 | 严重级别 |
|------|---------|---------|
| `_as_str`/`_generic_exec`/`_clickhouse_query` 在10个文件中重复定义 | 全部 builtin skills | P1 |
| `kubectl -A logs` 命令语法错误（`-A` 不适用于 `logs` 子命令） | `openstack_diagnostics.py`, `observability_log_correlation_gap.py`, `observability_read_path_latency.py` | P0 |
| `kubectl describe --tail=50` 参数无效（`describe` 不支持 `--tail`） | `k8s_pod.py` | P1 |
| `trigger_patterns` 过宽（`\berror\b` 几乎匹配所有场景） | `clickhouse_log.py`, `runtime_diagnosis_orchestrator.py` | P2 |
| 不必要的 `depends_on` 串行化 | `mariadb_diagnostics.py` | P2 |

#### AI 执行失败重复问题
- **根因**：`observing` 节点仅记录 `exit_code == 0` 为成功，不分析失败原因
- **表现**：失败命令不写入 `executed_fingerprints`，下一轮仍会重复执行
- **影响**：消耗 `max_iterations` 配额，无效执行，诊断质量差
- **缺失**：无失败分类 → 无替代策略 → 无重新思考机制

#### 功能缺口
- 缺少日志运转流程分析（Phase 1）作为强制第一步
- 缺少跨组件横向日志关联能力（trace_id / request_id / 时间窗口兜底）
- 知识库修复方案结构不完整，无法支撑自动修复闭环
- 无人工验证→授权自动修复的完整流程

---

## 二、优化目标

```
日志摄入 → 流程分析 → 横向关联 → 精准定位 → 知识沉淀 → 人工验证 → 授权自愈
```

1. **Phase 1**：分析日志运转流程，提取调用链锚点
2. **Phase 2**：基于锚点横向拉取所有相关组件日志（trace_id → request_id → 时间窗兜底）
3. **Phase 3**：命令驱动精准定位故障点，失败后重新思考替代命令（不重复执行）
4. **Phase 4**：AI 生成修复方案，自动保存到知识库
5. **Phase 5**：人工手动操作，提交验证结果
6. **Phase 6**：人工授权后，相似故障自动修复

---

## 三、总体架构

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           故障诊断与自愈全流程                                 │
│                                                                             │
│  Phase 1: 日志摄入      Phase 2: 横向关联      Phase 3: 深度诊断              │
│  ┌─────────────┐        ┌──────────────┐      ┌──────────────────┐          │
│  │ 日志运转流   │──锚点──▶│ 跨组件关联   │──证据──▶│ 命令驱动定位    │          │
│  │ 程分析      │        │ 拉取         │      │ 故障点          │          │
│  └─────────────┘        └──────────────┘      └────────┬─────────┘          │
│                                                        │                    │
│  Phase 4: 知识沉淀      Phase 5: 人工验证      Phase 6: 授权自愈              │
│  ┌─────────────┐        ┌──────────────┐      ┌──────────────────┐          │
│  │ 修复方案    │──保存──▶│ 人工操作     │──验证──▶│ 相似故障自动修  │          │
│  │ 写入知识库  │        │ 验证结果     │      │ 复（授权后）    │          │
│  └─────────────┘        └──────────────┘      └──────────────────┘          │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 四、详细设计

### 4.1 Phase 1：日志运转流程分析

**新建文件**：`ai-service/ai/skills/builtin/log_flow_analyzer.py`

**核心逻辑**：
- 强制作为所有诊断的第一步（`priority=100`, `mandatory_first=True`）
- 从 ClickHouse 查询当前日志的完整调用链（按 trace_id/request_id/时间窗优先级）
- 获取服务拓扑快照，辅助识别调用链中的组件
- 输出 `data_flow` 结构，写入 `SkillContext`，供 Phase 2 使用

**`planning.py` 修改**：
```python
# 第一次 planning 强制注入 log_flow_analyzer
if state.iteration == 1 and "log_flow_analyzer" not in state.selected_skills:
    _inject_mandatory_first_skill(state)
    return state  # 先只执行流程分析
```

### 4.2 Phase 2：跨组件日志横向关联

**新建文件**：`ai-service/ai/skills/builtin/cross_component_correlation.py`

**锚点优先级策略**：
```
Level 1: trace_id 精确关联         → ClickHouse 精确匹配
Level 2: request_id 关联           → 标准 HTTP request_id
         os_request_id 关联        → OpenStack req-xxx 格式
Level 3: 时间窗口兜底              → ±5 分钟全组件异常日志
```

**组件自动识别**：
- 根据日志内容关键词自动识别 Nova/Neutron/Cinder/Glance/K8s/MariaDB/ClickHouse
- 为每个识别到的组件生成独立的日志拉取步骤（可并行）

**`SkillContext` 扩展字段**：
```python
request_id: str = ""
os_request_id: str = ""
log_timestamp: str = ""
correlation_anchor: str = ""
correlation_anchor_value: str = ""
related_components: List[str] = field(default_factory=list)
data_flow: List[Dict[str, Any]] = field(default_factory=list)
evidence_window_start: str = ""
evidence_window_end: str = ""
```

### 4.3 Phase 3：失败重思考机制

**失败分类表**（`observing.py` 新增）：

| 失败类型 | 关键词 | 替代策略 |
|---------|--------|---------|
| `resource_not_found` | no resources found, not found | `widen_search_scope` |
| `permission_denied` | forbidden, permission denied | `skip_or_use_alternative` |
| `command_syntax_error` | unknown flag, invalid argument | `fix_command_syntax` |
| `connection_failure` | connection refused, no route | `check_service_availability` |
| `resource_not_ready` | CrashLoopBackOff, pending | `wait_and_retry_with_describe` |
| `empty_output` | （无输出）| `try_alternative_query` |
| `timeout` | timed out, deadline exceeded | `reduce_scope_retry` |
| `unknown_failure` | 其他 | `llm_rethink` |

**数据流**：
```
observing → 失败分类 → evidence["failure_category"]
replan   → 读取失败证据 → 取消阻塞 actions → 写入 failure_hints
acting   → 跳过依赖失败步骤的 pending actions
planning → 读取 failure_hints → 生成替代命令 actions
service  → 记录失败 fingerprint → 下次返回 needs_rethink 而非重复执行
```

**`service.py` 关键修改**：
```python
# 失败命令也记录 fingerprint（携带失败分类）
summary_updates["failed_command_fingerprints"] = [...]
summary_updates["failed_command_contexts"] = {fingerprint: failure_category}

# 检查重复时，失败命令返回 needs_rethink
if command_fingerprint in failed_fps:
    return {"status": "needs_rethink", "failure_context": failure_ctx}
```

### 4.4 Phase 4：知识库修复方案保存

**`similar_cases.py` 新增数据结构**：

```python
@dataclass
class RemediationStep:
    order: int
    title: str
    command: str
    command_spec: Dict[str, Any]
    purpose: str
    risk_level: str           # low/medium/high
    requires_approval: bool
    rollback_command: str = ""
    timeout_seconds: int = 60
    verified_working: bool = False

@dataclass
class RemediationPlan:
    plan_id: str
    case_id: str
    fault_summary: str
    root_cause: str
    direct_cause: str
    impact_scope: str
    fault_level: str          # P0/P1/P2/P3
    remediation_steps: List[RemediationStep]
    verification_steps: List[str]
    rollback_steps: List[str]
    evidence_summary: str
    created_at: str
    created_by: str = "ai"
    verified: bool = False
    verified_at: str = ""
    verified_by: str = ""
    verification_notes: str = ""
    auto_fix_enabled: bool = False
    auto_fix_authorized_by: str = ""
    auto_fix_authorized_at: str = ""
    auto_fix_conditions: List[str] = field(default_factory=list)
    auto_fix_risk_level: str = "high"
    similarity_fingerprint: str = ""
```

**故障指纹生成**：
```python
def _build_fault_fingerprint(components, error_category, error_keywords, service):
    payload = {
        "components": sorted(components),
        "error_category": error_category,
        "keywords": sorted(error_keywords[:5]),
        "service": service,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
```

### 4.5 Phase 5：人工验证 API

**新增 API 端点**：

```
GET  /api/ai/remediation/pending-verification
     → 返回所有待验证的修复方案列表

POST /api/ai/remediation/{case_id}/verify
     请求体：{
       verified: bool,
       verification_notes: str,
       verified_steps: [{step_order, command, result, output_summary}],
       actual_root_cause: str,    # 可选，人工修正
       corrections: str           # 可选，步骤修正说明
     }
     → 更新 case.verified=True, knowledge_version++

POST /api/ai/remediation/{case_id}/authorize-auto-fix
     请求体：{
       authorized_by: str,
       authorization_notes: str,
       risk_level: str,           # 操作人降级确认
       auto_fix_conditions: [],   # 精确触发条件
       max_auto_fix_per_day: int,
       require_secondary_approval: bool
     }
     → 设置 auto_fix_enabled=True

GET  /api/ai/remediation/auto-fix-history
     → 返回自动修复执行历史和审计日志
```

### 4.6 Phase 6：授权自动修复

**新建文件**：`ai-service/ai/agent_runtime/auto_remediation.py`

**安全控制层次**：
1. 仅 `verified=True` 且 `auto_fix_enabled=True` 的 Case 才可触发
2. 执行前强制 fault_fingerprint + 条件双重匹配
3. 每日执行次数限制（`max_auto_fix_per_day`）
4. 可配置二次审批（`require_secondary_approval`）
5. 每步执行后验证结果，失败立即触发回滚
6. 自动修复不允许提权操作（`elevated=False`）
7. 完整审计日志记录

**执行流程**：
```
故障触发 → fingerprint 匹配 → 条件验证 → 次数检查
    → [可选] 二次审批 → 逐步执行 → 每步验证
    → 验证失败 → 自动回滚 → 告警
    → 验证成功 → 审计日志 → 完成
```

### 4.7 Skills 分离为 ConfigMap

**ConfigMap 结构**：
```
ai-skills-core       → 通用 skills（log_flow_analyzer, cross_component_correlation, k8s, network, resource）
ai-skills-openstack  → OpenStack 专项 skills（nova, neutron, cinder, glance）
ai-skills-db         → 数据库 skills（clickhouse, mariadb）
ai-skills-custom     → 用户自定义（最高优先级，可覆盖内置）
```

**热加载机制**：
- 环境变量 `AI_SKILLS_HOT_RELOAD=true` 开启
- 后台线程每 60 秒检查 ConfigMap 文件 mtime 变化
- 仅重新加载有变化的 skill 文件，不影响其他 skill
- ConfigMap skill 可覆盖同名内置 skill（支持 hotfix）

---

## 五、文件改动清单

| 文件路径 | 改动类型 | 主要内容 |
|---------|---------|---------|
| `ai-service/ai/skills/builtin/_helpers.py` | **新建** | 公共工具函数（`_as_str`, `_generic_exec`, `_clickhouse_query`） |
| `ai-service/ai/skills/builtin/log_flow_analyzer.py` | **新建** | Phase 1 日志运转流程分析 Skill |
| `ai-service/ai/skills/builtin/cross_component_correlation.py` | **新建** | Phase 2 跨组件横向关联 Skill |
| `ai-service/ai/skills/base.py` | 修改 | `SkillContext` 新增关联字段；`DiagnosticSkill` 新增 `priority` |
| `ai-service/ai/skills/builtin/__init__.py` | 修改 | 注册新 skill |
| `ai-service/ai/skills/builtin/k8s_pod.py` | 修复 | 移除 `--tail`；使用 `_helpers` |
| `ai-service/ai/skills/builtin/openstack_diagnostics.py` | 修复 | 修复 `kubectl -A logs` 语法错误 |
| `ai-service/ai/skills/builtin/observability_log_correlation_gap.py` | 修复 | 修复 `kubectl -A logs` 语法错误 |
| `ai-service/ai/skills/builtin/observability_read_path_latency.py` | 修复 | 修复 `kubectl -A logs` 语法错误 |
| `ai-service/ai/skills/builtin/network_check.py` | 修改 | 使用 `_helpers` |
| `ai-service/ai/skills/builtin/resource_usage.py` | 修改 | 使用 `_helpers` |
| `ai-service/ai/skills/builtin/clickhouse_log.py` | 修改 | 使用 `_helpers`；收紧 trigger_patterns |
| `ai-service/ai/skills/builtin/mariadb_diagnostics.py` | 修改 | 使用 `_helpers`；优化 `depends_on` |
| `ai-service/ai/skills/builtin/linux_system_diagnostics.py` | 修改 | 使用 `_helpers` |
| `ai-service/ai/skills/builtin/runtime_diagnosis_orchestrator.py` | 修改 | 使用 `_helpers`；收紧 trigger_patterns |
| `ai-service/ai/skills/configmap_loader.py` | **新建** | ConfigMap YAML 热加载器 |
| `ai-service/ai/runtime_v4/langgraph/nodes/planning.py` | 修改 | 强制注入 Phase 1/2；读取 failure_hints |
| `ai-service/ai/runtime_v4/langgraph/nodes/observing.py` | 修改 | 失败分类 `_classify_failure` |
| `ai-service/ai/runtime_v4/langgraph/nodes/replan.py` | 修改 | 失败分析；取消阻塞 actions；failure_hints |
| `ai-service/ai/runtime_v4/langgraph/nodes/acting.py` | 修改 | 跳过依赖失败步骤的 pending actions |
| `ai-service/ai/agent_runtime/service.py` | 修改 | 失败 fingerprint；needs_rethink；KB 保存 |
| `ai-service/ai/agent_runtime/auto_remediation.py` | **新建** | Phase 6 自动修复控制器 |
| `ai-service/ai/similar_cases.py` | 修改 | `RemediationPlan`、`FaultMatcher`、`mark_case_verified` |
| `k8s/configmaps/ai-skills-core.yaml` | **新建** | Core skills ConfigMap |
| `k8s/configmaps/ai-skills-openstack.yaml` | **新建** | OpenStack skills ConfigMap |
| `k8s/configmaps/ai-skills-db.yaml` | **新建** | DB skills ConfigMap |
| `k8s/configmaps/ai-skills-custom.yaml` | **新建** | 自定义 skills ConfigMap（空模板） |

---

## 六、完整数据流时序

```
用户点击"AI 分析"（携带 log_content + trace_id/request_id）
        │
        ▼
[Phase 1] log_flow_analyzer（强制第一步，priority=100）
  ├─ ClickHouse 查询完整调用链（trace_id → request_id → 时间窗）
  ├─ kubectl 获取服务拓扑快照
  ├─ 输出 data_flow（服务调用顺序图）
  └─ 写入 SkillContext.correlation_anchor + related_components
        │
        ▼
[Phase 2] cross_component_correlation（强制第二步，priority=90）
  ├─ Level 1: trace_id → ClickHouse 全链路精确查询
  ├─ Level 2: os_request_id → OpenStack req-xxx 关联查询
  ├─ Level 3: 各组件 kubectl logs（并行，±5min 时间窗）
  └─ Level 4: 时间窗口兜底聚合（所有组件 ERROR/WARN 统计）
        │
        ▼
[Phase 3] 专项诊断 Skills（基于证据动态选择）
  ├─ 执行命令 → observing 记录结果 + 失败分类
  ├─ 失败 → replan 生成 failure_hints
  │     → acting 跳过依赖失败的 actions
  │     → planning 生成替代命令（不重复执行）
  │     → service 记录失败 fingerprint → needs_rethink
  └─ 成功 → 累积 evidence，继续 replan
        │
        ▼
[Phase 4] run 完成 → AI 生成修复方案
  ├─ 构建 RemediationPlan（steps + verification + rollback）
  ├─ 生成 fault_fingerprint（components+category+keywords）
  └─ 自动保存到 KB（Case + RemediationPlan）
        │
        ▼
[Phase 5] 人工验证
  ├─ GET /api/ai/remediation/pending-verification
  ├─ 运维人员按步骤手动执行
  ├─ POST /verify（提交验证结果 + 修正）
  └─ KB 更新：verified=True, knowledge_version++
        │
        ▼
[Phase 6] 授权自动修复（显式人工授权，非默认开启）
  ├─ POST /authorize-auto-fix（设置条件 + 风险确认）
  │
  ▼ 后续相似故障触发
  ├─ fingerprint 匹配 + 条件验证
  ├─ 次数限制检查
  ├─ [可选] 二次审批
  ├─ 逐步执行（每步验证，失败即回滚）
  └─ 审计日志全记录
```

---

## 七、安全设计原则

1. **自动修复默认关闭**：`auto_fix_enabled=False`，需显式人工授权
2. **双重验证**：fingerprint 匹配 + 条件列表验证，两者都需通过
3. **每日限额**：`max_auto_fix_per_day` 防止自动修复风暴
4. **无提权执行**：自动修复固定 `elevated=False`，高风险操作必须走审批
5. **失败即回滚**：任意步骤失败立即终止并执行回滚
6. **完整审计**：所有自动修复操作记录 authorized_by、执行时间、结果
7. **知识版本控制**：`knowledge_version` 追踪每次人工修正，可追溯

---

## 八、开发实施顺序

1. **基础规范修复**（P0）：kubectl 语法错误 + 公共 helpers 提取
2. **SkillContext 扩展**（P0）：新增关联字段，向后兼容
3. **Phase 1/2 Skill 新建**（P1）：log_flow_analyzer + cross_component_correlation
4. **planning 节点修改**（P1）：强制注入 + priority 排序
5. **失败重思考机制**（P1）：observing + replan + acting + service 联动
6. **知识库扩展**（P1）：RemediationPlan + Phase 4 自动保存
7. **人工验证 API**（P2）：verify + authorize-auto-fix 端点
8. **自动修复控制器**（P2）：auto_remediation.py
9. **ConfigMap 分离**（P3）：configmap_loader + k8s YAML
