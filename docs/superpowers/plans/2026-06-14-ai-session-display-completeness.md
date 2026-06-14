# AI 会话展示完整性修复 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 解决 AI 诊断对话框中命令显示不全、子目标状态展示模糊、用户输入/LLM结论/命令执行三层信息混杂的问题，达到类似 Claude 的清晰对话体验。

**Root Cause:** 三个层面的断裂：
1. 后端 compaction 截断 (280/320 字符阈值过小)
2. 前端渲染条件苛刻（思考过程被隐藏、action 只显示 1 条、子目标收入 details）
3. 无三层结构分离（用户/LLM/命令混在单气泡中）

**Tech Stack:** Python 3.10, TypeScript/React 18, TailwindCSS

---

### Task 1: 后端 — 放大 metadata compaction 截断阈值

**Files:**
- Modify: `ai-service/ai/session_history.py` (修改 `_compact_action_observation` 和 `_compact_followup_action` 的截断常量)

**问题:** 助手消息的 `metadata_json` 中的 action_observation 字段（stdout_preview, stderr_preview, command）在写入 ClickHouse 前被截断到 280/320 字符，导致前端拿到不完整数据。

- [ ] **Step 1: 定位截断常量**

在 `session_history.py` 中找到 `_compact_action_observation` 方法，搜索 `stdout_preview` 赋值处。典型的截断值定义如下：

```python
# 当前截断值（约 1000 行附近）
STDOUT_PREVIEW_MAX = 280
STDERR_PREVIEW_MAX = 280
COMMAND_PREVIEW_MAX = 320
```

- [ ] **Step 2: 放大阈值**

将截断值从 280/320 提升到 2000：

```python
STDOUT_PREVIEW_MAX = 2000
STDERR_PREVIEW_MAX = 2000
COMMAND_PREVIEW_MAX = 2000
```

同时检查 `_compact_followup_action` 中是否有类似的 `action_detail` 截断值，一并放大。

- [ ] **Step 3: 验证不引入新问题**

确认改动不影响历史 session（仅影响新写入的数据）。旧 session 读取已有截断数据，不会回填——这是可接受的，增量改善。

---

### Task 2: 后端 — 解除「会话已完成但子目标未闭环」的状态不一致

**Files:**
- Modify: `ai-service/ai/followup_runtime_helpers.py`（或 legacy engine 的完成逻辑所在文件）

**问题:** Session DB status 标记为 `completed` 时，子目标（subgoal）状态可能还是 `pending`或 `in_progress`，用户看到「任务还在运行」但系统显示已完成。

- [ ] **Step 1: 定义子目标聚合状态**

在 backend API 层或 session store 中添加一个方法计算有效状态：

```python
def compute_effective_status(session_status: str, subgoals: List[dict]) -> str:
    """根据子目标状态计算会话的有效完成状态。"""
    if not subgoals:
        return session_status
    statuses = {sg.get("status", "") for sg in subgoals}
    if "in_progress" in statuses or "running" in statuses:
        return "running"
    if "pending" in statuses:
        return "partial"  # 部分完成
    if statuses == {"completed"}:
        return "completed"
    return session_status
```

- [ ] **Step 2: 在 API 层返回 effective_status**

修改 `GET /api/ai/history/{sessionId}` 的响应，在 session 对象和 message 的 metadata 中添加 `effective_status` 字段。

---

### Task 3: 前端 — 始终显示思考过程（修复条件隐藏）

**Files:**
- Modify: `frontend/src/pages/AIAnalysis.tsx`

**问题:** 第 6711 行的条件 `messageThoughtTimeline.length > 0 && messageObservations.length === 0` 导致只要有命令执行结果，LLM 的思考/规划/推理过程就全部不可见。

- [ ] **Step 1: 修改显示条件**

将原条件（line 6711）：
```typescript
{msg.role === 'assistant' && messageThoughtTimeline.length > 0 && messageObservations.length === 0 && (
```

改为无条件显示（有思考内容就显示，不管是否有 observations）：
```typescript
{msg.role === 'assistant' && messageThoughtTimeline.length > 0 && (
```

- [ ] **Step 2: 调整思考面板样式**

去掉 `messageObservations.length === 0` 条件后，思考面板和命令输出会同时出现。确认它们在视觉上协调——思考面板改为 `<details>` 默认折叠模式（避免气泡过长），但保留 <summary> 可见的迭代计数。

---

### Task 4: 前端 — 显示所有 Action（移除 slice(0,1)）

**Files:**
- Modify: `frontend/src/pages/AIAnalysis.tsx`

**问题:** 第 6910 行 `messageActions.slice(0, 1).map(...)` 导致有多个 action 时只显示第 1 个。

- [ ] **Step 1: 移除切片限制**

```typescript
// 修改前 (line 6910-6911):
{messageActions.slice(0, 1).map((action, actionIndex) => {

// 修改后:
{messageActions.map((action, actionIndex) => {
```

- [ ] **Step 2: 处理大量 action 的情况**

如果 action 条目较多（10+），面板会变得很长。添加 `max-h-80 overflow-auto` 样式限制垂直溢出，而非隐藏条目。

---

### Task 5: 前端 — 子目标和反思面板常驻可见

**Files:**
- Modify: `frontend/src/pages/AIAnalysis.tsx`

**问题:** 子目标（line 6858）和反思（line 6884）使用 `<details>` 元素，即使 `open` 属性存在，在视觉上仍是可折叠区域。pending 状态的子目标不够醒目。

- [ ] **Step 1: 子目标改为常驻面板**

将 `<details>`（line 6858-6882）改为直接渲染的 `<div>`。pending/in_progress 子目标使用红色/琥珀色背景高亮：

```typescript
// 修改前
<details className="..." open>
  <summary>子目标拆解</summary>
  ...
</details>

// 修改后
<div className="mt-1.5 w-full max-w-[85%] rounded border border-sky-200 bg-sky-50 p-2">
  <div className="text-[11px] font-medium text-sky-700 mb-1">子目标拆解</div>
  ...
</div>
```

- [ ] **Step 2: 反思改为常驻面板**

同样的方式将 `<details>`（line 6884-6904）改为 `<div>`。低置信度（< 0.8）时添加醒目指示。

- [ ] **Step 3: pending 子目标高亮**

对于 `status === 'pending'` 的子目标，显示琥珀色背景 + `⚠️` 图标 + "待完成" 标签，而非普通的灰色标签：

```typescript
{goal.status === 'pending' && (
  <span className="inline-flex items-center gap-1 text-[10px] text-amber-700 bg-amber-100 rounded-full px-2 py-0.5">
    ⚠️ 待完成
  </span>
)}
```

---

### Task 6: 前端 — 放大命令输出显示区域

**Files:**
- Modify: `frontend/src/pages/AIAnalysis.tsx`

**问题:** 命令和输出使用 `max-h-32`/`max-h-60` CSS 截断且字体仅 11px，可读性差。

- [ ] **Step 1: 放大命令预览区域**

修改 line 6683 的 `max-h-32` 为 `max-h-40`，同时移除 line 6682-6684 的 11px 限制：

```typescript
// 修改前
<pre className="text-[11px] font-mono text-slate-800 whitespace-pre-wrap overflow-auto max-h-32">

// 修改后  
<pre className="text-[12px] font-mono text-slate-800 whitespace-pre-wrap overflow-auto max-h-40">
```

- [ ] **Step 2: 放大 stdout/stderr 区域**

修改 line 6688 的 `max-h-60 text-[11px]` 为 `max-h-96 text-[12px]`，修改 line 6694 的 `max-h-32` 为 `max-h-40`。

- [ ] **Step 3: 添加折叠能力**

命令输出可能很长，添加一个点击展开/折叠的按钮：

```typescript
const [obsExpanded, setObsExpanded] = useState(false);
// ...
<pre className={`text-[12px] font-mono whitespace-pre-wrap overflow-auto ${obsExpanded ? 'max-h-[500px]' : 'max-h-[120px]'}`}>
  {obsStdout}
</pre>
{obsStdout.length > 500 && (
  <button onClick={() => setObsExpanded(!obsExpanded)} className="text-[10px] text-indigo-500 mt-0.5">
    {obsExpanded ? '收起' : '展开全部'}
  </button>
)}
```

---

### Task 7: 前端 — 放大 runtime transcript summary 截断

**Files:**
- Modify: `frontend/src/features/ai-runtime/utils/runtimeTranscript.ts`

**问题:** line 458 将 `detail` 截断到 120 字符作为 summary，丢失了大量推理细节。

- [ ] **Step 1: 放大截断阈值**

```typescript
// 修改前 (line 458):
summary: options.detail ? options.detail.slice(0, 120) : undefined,

// 修改后:
summary: options.detail ? options.detail.slice(0, 500) : undefined,
```

---

### Task 8: 前端 — 三层结构分离（高级改造）

**Files:**
- Modify: `frontend/src/pages/AIAnalysis.tsx`

**问题:** 整体 UI 缺少用户输入、LLM 结论、执行记录的分层，所有内容混在单气泡内。

这是一个较大的改造，建议在以上 Task 3-6 完成后评估效果后再决定是否需要独立执行。

- [ ] **Step 1: 设计三层布局**

```
┌─ [用户输入层] ──────────────────────────┐
│  双气泡: 用户蓝底 | AI 白底               │
│  LLM content 保留富文本 markdown 渲染       │
└────────────────────────────────────────────┘
┌─ [命令执行层] ──────────────────────────┐
│  所有 action + observation（完整列表）     │
│  思考过程 timeline（常驻可见）              │
│  子目标进度（常驻可见）                     │
│  反思/置信度（常驻可见）                   │
└────────────────────────────────────────────┘
```

- [ ] **Step 2: 重新组织渲染顺序**

当前渲染顺序（line 6641+）：
1. 气泡主体 (msg.content)
2. 流式加载指示器
3. 命令输出区块 (observations)
4. 时间戳
5. 思考过程 (thought timeline) ← 条件显示
6. 引用片段
7. 子目标 ← 可折叠
8. 反思 ← 可折叠
9. 执行计划 (actions) ← slice(0,1)

改造后渲染顺序：
1. **用户输入层**: 气泡主体 (content) + 时间戳
2. **执行层** (视觉上独立分组)：
   a. 子目标进度条（始终可见，pending 高亮）
   b. 执行计划（所有 action 完整列表）
   c. 思考过程 timeline（始终可见）
   d. 命令输出（observations，可折叠展开）
3. **总结层**:
   a. 反思 & 置信度（始终可见）
   b. 引用片段

---

### Task 9: 回归测试与验证

- [ ] **Step 1: 后端测试**

```bash
cd ai-service && pytest tests/test_session_history.py -x -v
```

- [ ] **Step 2: 前端类型检查**

```bash
cd frontend && npm run typecheck
```

- [ ] **Step 3: 前端 lint**

```bash
cd frontend && npm run lint
```

- [ ] **Step 4: 手动验证**

用已知 session（如 `ais-63019f3e874e4835`）验证：
1. 思考过程是否在命令输出旁同时可见
2. 所有 action 是否都渲染出来
3. pending 子目标是否有醒目标记
4. 命令输出字体是否改善
