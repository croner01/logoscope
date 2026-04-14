# Runtime Skill Selection Governance Design

## 背景

当前 runtime skill 选择把两类目标混在了一起：

1. 给 prompt / catalog 展示“可能相关”的候选技能
2. 在 runtime / planning 中“自动注入”诊断步骤

这两类目标的容错要求不同。候选发现可以更宽松，但自动注入必须更保守，否则普通 `service` 场景也可能因为 `component_type` 加分被带入专项 skill。

## 根因

根因不是单一 prompt，而是评分与执行门槛耦合：

- `match_score` 同时叠加了 `pattern_score` 与 `component_bonus`
- runtime API 的 skill 自动选择使用 `threshold=0.1`
- planning 节点也使用低门槛规则匹配

这意味着只要某个 skill 的 `applicable_components` 与上下文组件对上，即使没有直接故障信号，也可能进入自动执行路径。

## 设计目标

- 保留 prompt/catalog 的宽松候选发现能力
- 收紧 runtime/planning 的自动 skill 注入
- 让“为什么选中某个 skill”可解释、可追踪
- 不重写整套 matcher，不影响已有显性强信号场景

## 方案

### 1. 拆分候选发现与自动注入

- `match_skills_by_rules()` 继续服务于 catalog / prompt 注入
- 新增 `extract_auto_selected_skills()` 仅用于 runtime 与 planning 自动执行

### 2. 自动注入必须满足直接证据

自动注入要求同时满足：

- `total_score >= 0.35`
- `pattern_hits > 0`

也就是说，组件匹配只能作为辅助加分，不能单独触发专项 skill 自动执行。

### 3. 保留评分明细

在 skill 基类中新增匹配明细结构，至少记录：

- `pattern_hits`
- `pattern_score`
- `component_bonus`
- `total_score`

这样后续若再次出现误选，可以直接定位是“正则太宽”还是“组件加分过度”。

### 4. 让运行时策略可见

runtime summary 中写入：

- `selected_skills`
- `skill_step_count`
- `skill_selection_policy`

用于回答“为什么这次自动执行了这些 skill”。

## 不做的事

这轮不做以下事情：

- 不把所有 matcher 阈值统一抬高
- 不改 prompt catalog 的候选展示策略
- 不重新设计 builtin skill 体系
- 不引入新的 LLM 判定链路

原因很简单：这轮目标是先收口误自动执行，而不是重构整套诊断框架。

## 预期结果

- 普通 `service` / 普通 `time window` 场景不再自动注入 observability 专项 skill
- 缺锚点、慢查询、连接拒绝等真实强信号场景仍能命中对应 skill
- runtime 事件和 summary 对 skill 选择策略更可解释
