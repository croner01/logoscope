# ai-service 模块需求符合性检查（日志 AI 分析 Agent）

日期：2026-04-19（UTC）
范围：仅检查 `ai-service/` 后端实现，不含前端页面交互细节。

## 结论（先说重点）

- **总体结论：部分满足，无法判定完全满足。**
- 已具备：日志/链路分析入口、自动关联拉取、技能匹配、命令执行分级（低风险自动/高风险审批）、会话历史、相似案例复用、本地+远端知识库联动。
- 主要缺口：
  1) **缺少“手动上下文前后 N 条日志”模式的明确接口契约**；
  2) **缺少“关联日志缺失后继续/重拉”的显式校验与交互结果字段**；
  3) **未看到强制加载 OpenStack/MariaDB/Linux 专项技能包**（目前以内置 K8s/ClickHouse/网络/资源等技能为主）；
  4) **角色 Prompt 未严格落到“资深运维架构师（10+年，OpenStack+K8s+MariaDB+Linux）”**。

---

## 逐条需求核对

### 一、功能入口与基础触发逻辑

1. 入口触发（日志列表选中后点击 AI 分析并携带完整原始字段）
   - `AnalyzeLogRequest` 与 `LLMAnalyzeRequest` 支持携带日志主体与 `context`，可承载 `request_id/trace_id` 等字段。状态：**后端可承载，前端跳转逻辑不在 ai-service 范围内**。

2. 日志数据拉取两模式
   - 自动关联模式：`RequestFlowAgent` 会提取 `request_id/trace_id`，并在时间窗内聚合关联日志与 trace spans。状态：**部分满足（自动模式存在）**。
   - 手动选择模式（前后 N 条，默认前10后10）：未发现明确的“before/after 条数”API参数与分支逻辑。状态：**不满足/未实现**。

3. 数据校验（缺失关联日志时“继续分析/重新拉取”）
   - 看到工具执行与结果汇总，但未发现“关联缺失提示 + 用户选择继续/重拉”的明确状态机或 API 字段。状态：**不满足/未实现**。

### 二、AI分析核心配置与能力支撑

1. 角色与 Prompt（资深运维架构师）
   - 现有 prompt 偏“日志分析专家/SRE”，但未见固定“10年以上运维架构师 + OpenStack/K8s/MariaDB/Linux”角色模板。状态：**部分满足**。

2. 专用 Skills 强制加载
   - 已有技能注册与内置技能自动加载机制。
   - 但内置集合以 `k8s_pod/clickhouse/network/resource/observability` 为主，未见明确 `OpenStack/MariaDB/Linux` 三套专用技能实现并强制加载。状态：**部分满足（技能机制有，技能覆盖不全）**。

3. Prompt 补充规则（按日志类型注入场景提示）
   - 已有 skill catalog 注入与项目知识注入，但未见按“OpenStack/K8s/MariaDB 日志类型”做明确 if/else 场景 Prompt 增强规则。状态：**部分满足**。

### 三、AI分析流程与输出要求

1. 第一步：日志运转流程分析
   - `LLMService.analyze_log` 要求先输出 data_flow；`RequestFlowAgent` 可构造路径、证据、异常节点。状态：**满足**。

2. 第二步：故障原因分析（直接原因/根本原因、影响范围、等级、扩散）
   - 有 `root_causes` 与严重级别字段，但“直接原因 vs 根本原因”结构未强制、“影响范围/扩散路径”未形成固定输出契约。状态：**部分满足**。

3. 第三步：修复方案与命令执行
   - 有动作命令分类、风险等级、审批链路（approve/input/interrupt），并对危险命令做限制。状态：**核心能力满足**。
   - 审批记录落历史会话/消息元数据链路具备。状态：**基本满足**。

4. 第四步：对话延续分析
   - 存在 follow-up 与流式 follow-up API、会话历史与上下文拼接逻辑。状态：**满足**。

### 四、知识库管理与历史记录

1. 本地知识库存储
   - 会话与消息支持落 ClickHouse；案例库也有本地存储结构。状态：**满足**。

2. 历史记录管理与检索
   - 支持 history 列表/详情/更新/删除及基础筛选排序。
   - 但“按故障类型、request-id”等专门检索键未见明确独立字段过滤（更多依赖全文 search/context）。状态：**部分满足**。

3. 知识库复用
   - 相似案例推荐机制存在。状态：**满足**。

4. 远端知识库联动（RAGFlow）
   - 已有 RAGFlow/Generic REST provider、检索与 upsert、outbox 重试机制。状态：**满足**。

---

## 建议的最短闭环改造（按优先级）

1. **先补接口契约（高优先）**
   - 在 `LLMAnalyzeRequest.context` 或新增字段中固化：
     - `pull_mode: manual_context | auto_correlation`
     - `manual_before: int=10`
     - `manual_after: int=10`
     - `allow_partial: bool`

2. **补“缺失关联校验”统一返回结构（高优先）**
   - 统一返回：
     - `integrity.missing_components[]`
     - `integrity.partial=true/false`
     - `integrity.next_action=continue|repull_required`

3. **补齐技能包（高优先）**
   - 新增并注册：`openstack_diagnostics`、`mariadb_diagnostics`、`linux_system_diagnostics`。
   - 在 prompt 注入层增加“按日志类型强制技能优先级”。

4. **补角色 Prompt 与输出 schema（中优先）**
   - 固化“资深运维架构师”角色系统提示词。
   - 输出 schema 增加：`direct_causes[]`、`root_causes[]`、`impact_scope`、`fault_level`、`blast_radius`。

5. **补历史检索索引（中优先）**
   - 在 session/context 结构里显式冗余 `request_id` 字段，并在 history 列表接口提供专门 filter 参数。

