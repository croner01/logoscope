# AI 运行时对话输出整洁化方案

日期: 2026-06-12

## 问题

AI 诊断分析的对话消息内容不完整、结构混乱。用户在对话框中期望看到完整的排查思路、执行命令、执行输出，但实际上：

- 排查思路（thought）折叠在独立的"思考过程"面板中，不在消息正文
- 执行命令（command）存在 metadata 中，消息正文只放 `command: xxx` 文本
- 执行输出（stdout）最终展示在消息气泡中只有 **240 字符**，其余折叠在 CommandOutputPanel
- 状态标签（`running`、`exit_code: 0`、`duration_ms`）等"噪声信息"混在正文里

## 当前状态（代码快照）

### 输出截断链路（三层截断）

```
exec-service runtime_service.py:21
  MAX_OUTPUT_CHARS = max(512, int(os.getenv("EXEC_COMMAND_MAX_OUTPUT_CHARS", "12000")))
  → _append_output() 在 12000 处截断，追加 ...<truncated>...

ToolAdapter tools.py:145-146
  stdout=_as_str(run_data.get("stdout", ""))[:10000],
  stderr=_as_str(run_data.get("stderr", ""))[:2000],
  → 比 exec-service 的 12000 还少了 2000

前端 runtimeMessages.ts:353
  out.substring(0, 240)
  → 消息正文只展示 240 字符，完整输出在 CommandOutputPanel
```

### 消息内容组装

```python
# api/ai.py:2099-2105
runtime_service.finalize_assistant_message(
    run_id,
    content=str(result.get("answer") or ""),      # ← 仅 LLM 回答文本
    metadata=assistant_metadata,                    # ← action_observations 在 metadata 里
)
```

### 前端渲染

```typescript
// AIAnalysis.tsx:6661-6667
msg.role === 'assistant'
  ? renderFollowUpRichContent(msg.content, ...)   // ← 消息正文（纯文本/结构化）
  : <div>msg.content</div>

// 下方独立面板（用户不想看到的"噪声"）:
// - 思考过程面板 (thought timeline)
// - 操作按钮面板 (action buttons)
// - CommandOutputPanel (command runs)
```

## 设计

### 原则

1. **后端存储数据，前端负责展示** — 不改数据持久层格式
2. **最小改动** — 三处后端配置微调 + 两处前端展示逻辑
3. **渐进兼容** — 历史消息不受影响，新消息自动获得完整展示

### 改动总览

```
┌─────────────────────────────────────────────────────┐
│ 后端（3处改动，配置级 + 查询补丁）                      │
│   compiler.py        → ClickHouse 查询自动补 LIMIT    │
│   runtime_service.py → MAX_OUTPUT_CHARS 放大          │
│   tools.py           → stdout/stderr 截断对齐         │
├─────────────────────────────────────────────────────┤
│ 前端（2处改动，纯展示层）                                │
│   runtimeMessages.ts → 去掉 240 truncation            │
│   AIAnalysis.tsx     → 展示 action_observations       │
│                      → 隐藏冗余面板                    │
└─────────────────────────────────────────────────────┘
```

## 详细改动

### 1. 后端 — Compiler 自动 LIMIT（compiler.py）

**位置**: `ai/command/compiler.py` 在 `compile_command()` 的 `ToolType.CLICKHOUSE_QUERY` 分支（约 line 272-287）

**改动**: 在 `_normalize_clickhouse_query()` 之后，用正则检查 SQL 是否已有 LIMIT 子句。如果无 LIMIT 且非聚合查询（不含 `GROUP BY`），自动追加 `LIMIT 1000`。

```python
# compiler.py: 新增 _ensure_clickhouse_limit()
def _ensure_clickhouse_limit(query: str) -> str:
    """Auto-append LIMIT 1000 to SELECT queries without one."""
    upper = query.strip().upper()
    if not upper.startswith("SELECT"):
        return query
    # Skip if already has LIMIT / LIMIT n BY / GROUP BY (aggregate)
    if re.search(r"\bLIMIT\s+\d", upper):
        return query
    if re.search(r"\bGROUP\s+BY\b", upper):
        return query
    return query.rstrip().rstrip(";") + " LIMIT 1000"
```

**目的**: 防止 LLM 生成的无 LIMIT 查询返回海量行撑爆输出缓冲区，同时确保输出完整不截断。

### 2. 后端 — 输出截断放大 + 对齐

#### 2a. exec-service runtime_service.py:21

```python
MAX_OUTPUT_CHARS = max(512, int(os.getenv("EXEC_COMMAND_MAX_OUTPUT_CHARS", "12000")))
                                                            ↓
MAX_OUTPUT_CHARS = max(512, int(os.getenv("EXEC_COMMAND_MAX_OUTPUT_CHARS", "100000")))
```

默认值从 12000 → 100000，允许通过环境变量覆盖。

#### 2b. ToolAdapter tools.py:145-146

```python
stdout=_as_str(run_data.get("stdout", ""))[:10000],
stderr=_as_str(run_data.get("stderr", ""))[:2000],
                               ↓
stdout=_as_str(run_data.get("stdout", ""))[:100000],
stderr=_as_str(run_data.get("stderr", ""))[:10000],
```

- stdout 从 10000 对齐到 100000（与 exec-service 一致）
- stderr 从 2000 放宽到 10000（错误栈信息可能较长）

### 3. 前端 — runtimeMessages.ts 去掉 240 截断

**位置**: `buildRuntimeAnalysisFollowUpMessage` 和 `buildRuntimeFollowUpMessage`

**改动**:

```typescript
// 当前:
content = `命令已执行\n${cmd}${out ? `\n→ ${out.substring(0, 240)}` : ''}${err ? `\n⚠ ${err.substring(0, 240)}` : ''}`;

// 改为:
const cmd = latestCommandRun.command || '';
const cmdStatus = String(latestCommandRun.status || '').trim().toLowerCase();
const out = String(latestCommandRun.stdout || '').trim();
const err = String(latestCommandRun.stderr || '').trim();
const exitCode = latestCommandRun.exitCode;
const parts: string[] = ['```bash', cmd, '```'];
if (out) {
  parts.push('', '**输出:**', '```', out, '```');
}
if (err) {
  parts.push('', '**错误:**', '```', err, '```');
}
if (typeof exitCode === 'number') {
  parts.push(`> exit code: ${exitCode}`);
}
content = parts.join('\n');
```

同时在 `formatCommandExecutionMessage` 中去掉 `exit_code: ${exitCode}`、`duration_ms: ${durationMs}` 等噪声字段。

### 4. 前端 — AIAnalysis.tsx 展示逻辑

**位置**: 消息渲染循环（约 line 6648-6750）

**改动**:

1. **metadata 有 action_observations 时直接渲染到正文**: 在气泡 content 下方，检查 `messageObservations`。如果有值且不是空数组，追加命令输出区块：

```tsx
{messageObservations.length > 0 && !streamLoading && (
  <div className="mt-3 space-y-2 border-t border-slate-200 pt-2">
    {messageObservations.map((obs, i) => (
      <div key={`obs-${i}`} className="...">
        <div className="text-xs font-mono text-slate-800">$ {obs.command}</div>
        {obs.stdout && <pre className="...">{obs.stdout}</pre>}
        {obs.stderr && <pre className="...">{obs.stderr}</pre>}
      </div>
    ))}
  </div>
)}
```

2. **隐藏冗余面板**: 当 `messageObservations.length > 0` 时（有诊断命令执行结果），隐藏独立的"思考过程"折叠面板和"操作按钮"面板。条件为 `messageObservations.length > 0`，因为这个字段只出现在自动化诊断产生的消息中，普通用户消息不会有此字段。

3. **格式统一**: `formatCommandExecutionMessage` 输出改为 markdown 风格，去掉状态标签噪声：

```
$ kubectl exec clickhouse ... -- clickhouse-client --query "SELECT ..."

**stdout:**
```
结果行1
结果行2
```

> exit: 0
```

## 数据流对比

### 改动前

```
LLM 回答 → content = "分析结果文本"
                                         前端气泡:
执行命令 → metadata.action_observations    "分析结果文本"
执行输出 → metadata.action_observations    ──────────────────
                                         ↓ 思考过程（折叠面板）
                                         ↓ 操作按钮（动作列表）
                                         ↓ CommandOutputPanel
```

### 改动后

```
LLM 回答 → content = "分析结果文本"
                                         前端气泡:
执行命令 → metadata 保留                    "分析结果文本"
执行输出 → metadata 保留                    [命令输出区块]
                                         ──────────────────
                                         （无多余面板）
```

## 边界情况

| 场景 | 处理方式 |
|------|---------|
| 历史消息已有关联的 action_observations | 前端检查 metadata，有新数据就展示，没有就保持原样 |
| 消息正在流式输出中 | stream_loading=true 时不展示命令输出区块，等 finalized 后再渲染 |
| 输出 > 100000 字符 | exec-service 截断 + 追加 `<...truncated...>` 标记，前端原样展示 |
| 仅执行一条命令 vs 多条 | iterations 中的多条命令按顺序展示，每条独立 code block |
| 用户手动执行的命令（非自动诊断） | 普通 follow-up 消息，保持现有渲染不变（无 command_execution 标记） |

## 测试策略

### 后端测试

| 测试点 | 方法 |
|--------|------|
| compiler LIMIT 自动追加 | 单元测试覆盖：已有 LIMIT 不追加、无 LIMIT 追加、GROUP BY 不追加 |
| MAX_OUTPUT_CHARS 默认值 | 集成测试验证 exec-service 返回 >= 100000 字符的输出 |
| ToolAdapter stdout 对齐 | 集成测试验证 stdout 100000、stderr 10000 |

### 前端测试

| 测试点 | 方法 |
|--------|------|
| action_observations 渲染 | 构建含多条 command run 的 mock state，验证气泡内正确渲染 code block |
| 无 command_execution 标记的消息 | 验证不隐藏现有面板 |
| 超大输出渲染 | mock stdout=80000 字符，验证渲染不卡顿、codeblock 完整 |
| 历史消息兼容 | mock 不含 action_observations 的 metadata，验证原样展示 |

## 检查清单

- [ ] compiler.py: `_ensure_clickhouse_limit()` 函数 + 单元测试
- [ ] runtime_service.py: `EXEC_COMMAND_MAX_OUTPUT_CHARS` 默认值 12000→100000
- [ ] tools.py: stdout/stderr 截断对齐
- [ ] runtimeMessages.ts: 去掉 240 truncation，噪声字段
- [ ] AIAnalysis.tsx: 展示 action_observations、隐藏冗余面板
- [ ] 前端单元测试覆盖新展示逻辑
- [ ] 回归测试：确认不影响非诊断消息
