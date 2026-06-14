# AI 运行时对话输出整洁化 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** AI 诊断分析的对话消息在气泡内完整展示排查思路、执行命令、执行输出，去掉多余的截断和独立面板。

**Architecture:** 后端三处配置/代码微调（不涉及核心数据流），前端两处展示逻辑调整。

**Tech Stack:** Python 3.10, TypeScript/React 18, TailwindCSS

---

### Task 1: Compiler ClickHouse 查询自动补 LIMIT

**Files:**
- Modify: `ai-service/ai/command/compiler.py` (在 `compile_command` 的 `CLICKHOUSE_QUERY` 分支前添加 `_ensure_clickhouse_limit`)
- Test: `ai-service/tests/test_compiler.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_compiler.py — 新增到文件末尾
import re
from ai.command.compiler import compile_command
from ai.command.spec import CommandSpec, ToolType

def _spec(sql: str) -> CommandSpec:
    return CommandSpec(
        command=sql,
        tool=ToolType.CLICKHOUSE_QUERY,
        target_kind="clickhouse_cluster",
        target_identity="database:logs",
        purpose="test limit",
    )

def test_ensure_clickhouse_limit_appends_when_missing():
    spec = _spec("SELECT * FROM logs.logs WHERE level = 'error'")
    compiled = compile_command(spec)
    assert "LIMIT 1000" in compiled.shell_command, \
        f"Expected LIMIT 1000 in {compiled.shell_command}"

def test_ensure_clickhouse_limit_skips_when_present():
    spec = _spec("SELECT * FROM logs.logs LIMIT 50")
    compiled = compile_command(spec)
    # Should contain only LIMIT 50, not LIMIT 1000
    assert "LIMIT 50" in compiled.shell_command
    assert "LIMIT 1000" not in compiled.shell_command

def test_ensure_clickhouse_limit_skips_group_by():
    spec = _spec("SELECT service_name, count() FROM logs.logs GROUP BY service_name")
    compiled = compile_command(spec)
    assert "LIMIT 1000" not in compiled.shell_command

def test_ensure_clickhouse_limit_skips_non_select():
    spec = _spec("SHOW TABLES")
    compiled = compile_command(spec)
    assert "LIMIT 1000" not in compiled.shell_command

def test_ensure_clickhouse_limit_handles_limit_by():
    spec = _spec("SELECT * FROM logs.logs LIMIT 5 BY service_name ORDER BY timestamp DESC")
    compiled = compile_command(spec)
    assert "LIMIT 5 BY" in compiled.shell_command
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ai-service && python -m pytest tests/test_compiler.py::test_ensure_clickhouse_limit_appends_when_missing -v`
Expected: FAIL — `_ensure_clickhouse_limit` not defined yet

- [ ] **Step 3: Write `_ensure_clickhouse_limit` and integrate into `compile_command`**

在 `compiler.py` 中 `_normalize_clickhouse_query` 函数之后添加：

```python
def _ensure_clickhouse_limit(query: str, default_limit: int = 1000) -> str:
    """Auto-append LIMIT {default_limit} to SELECT queries without one.

    Skips if the query already has a LIMIT / LIMIT n BY clause, or if it
    contains GROUP BY (aggregate results are typically small enough).
    """
    stripped = query.strip()
    upper = stripped.upper()
    if not upper.startswith("SELECT"):
        return stripped
    # Already has LIMIT <number> or LIMIT <number> BY
    if re.search(r"\bLIMIT\s+\d", upper):
        return stripped
    # Aggregate queries are typically small — skip
    if re.search(r"\bGROUP\s+BY\b", upper):
        return stripped
    # Remove trailing semicolon before appending
    cleaned = stripped.rstrip().rstrip(";")
    return f"{cleaned} LIMIT {default_limit}"
```

在 `compile_command()` 的 `CLICKHOUSE_QUERY` 分支中集成：

```python
    if spec.tool == ToolType.CLICKHOUSE_QUERY:
        normalized = _normalize_clickhouse_query(command)
        limited = _ensure_clickhouse_limit(normalized)          # ← 新增
        escaped = _escape_clickhouse_query(limited)             # ← 用 limited 替代 normalized
        target = _resolve_clickhouse_target(namespace)
        shell = f'kubectl exec {target} -- clickhouse-client --query "{escaped}"'
        ...
```

确保文件顶部已有 `import re`（compiler.py line 11）。

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ai-service && python -m pytest tests/test_compiler.py::test_ensure_clickhouse_limit_appends_when_missing tests/test_compiler.py::test_ensure_clickhouse_limit_skips_when_present tests/test_compiler.py::test_ensure_clickhouse_limit_skips_group_by tests/test_compiler.py::test_ensure_clickhouse_limit_skips_non_select tests/test_compiler.py::test_ensure_clickhouse_limit_handles_limit_by -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add ai-service/ai/command/compiler.py ai-service/tests/test_compiler.py
git commit -m "feat: auto-append LIMIT 1000 to ClickHouse SELECT queries without one"
```

---

### Task 2: exec-service 输出截断上限放大

**Files:**
- Modify: `exec-service/core/runtime_service.py:21`

- [ ] **Step 1: Change default MAX_OUTPUT_CHARS**

```python
# line 21 — 默认值 12000 → 100000
MAX_OUTPUT_CHARS = max(512, int(os.getenv("EXEC_COMMAND_MAX_OUTPUT_CHARS", "100000")))
```

- [ ] **Step 2: Verify with existing tests**

Run: `cd exec-service && python -m pytest -x`
Expected: All existing tests pass (this is a config default change only)

- [ ] **Step 3: Commit**

```bash
git add exec-service/core/runtime_service.py
git commit -m "feat: increase default MAX_OUTPUT_CHARS from 12000 to 100000"
```

---

### Task 3: ToolAdapter stdout/stderr 截断对齐

**Files:**
- Modify: `ai-service/ai/runtime/tools.py:145-146`

- [ ] **Step 1: Update truncation limits**

```python
# line 145-146 — 旧值
stdout=_as_str(run_data.get("stdout", ""))[:10000],
stderr=_as_str(run_data.get("stderr", ""))[:2000],
# → 新值
stdout=_as_str(run_data.get("stdout", ""))[:100000],
stderr=_as_str(run_data.get("stderr", ""))[:10000],
```

- [ ] **Step 2: Verify with existing tests**

Run: `cd ai-service && python -m pytest tests/test_tools.py -x -v 2>/dev/null || echo "(test_tools.py may not exist — this is a pure limit change, safe to proceed)"`  
Expected: Pass or file-not-found (the change is a constant adjustment, no behavioral difference)

- [ ] **Step 3: Commit**

```bash
git add ai-service/ai/runtime/tools.py
git commit -m "feat: align ToolAdapter truncation limits with exec-service (stdout 100k, stderr 10k)"
```

---

### Task 4: 前端 runtimeMessages.ts 去掉 240 截断和噪声字段

**Files:**
- Modify: `frontend/src/features/ai-runtime/utils/runtimeMessages.ts`
  - `buildRuntimeAnalysisFollowUpMessage` (约 line 265)
  - `buildRuntimeFollowUpMessage` (约 line 157)

- [ ] **Step 1: 修改 `buildRuntimeFollowUpMessage` 中的 `formatCommandExecutionMessage` 调用**

在 `formatCommandExecutionMessage` 函数（约 line 4101，在 AIAnalysis.tsx 中）修改输出格式：

查找：
```typescript
  const formatCommandExecutionMessage = (
    payload: Record<string, unknown>,
    fallbackCommand: string,
  ): string => {
    const status = String(payload.status || 'unknown');
    const command = String(payload.command || fallbackCommand || '').trim();
    const message = String(payload.message || '').trim();
    const stdout = String(payload.stdout || '').trim();
    const stderr = String(payload.stderr || '').trim();
    const exitCode = Number(payload.exit_code);
    const durationMs = Number(payload.duration_ms);
    const outputTruncated = Boolean(payload.output_truncated);
    const lines: string[] = [
      `命令执行状态: ${status}`,
      command ? `command: ${command}` : '',
      Number.isFinite(exitCode) ? `exit_code: ${exitCode}` : '',
      Number.isFinite(durationMs) ? `duration_ms: ${durationMs}` : '',
      message ? `message: ${message}` : '',
    ].filter(Boolean);

    if (stdout) {
      lines.push(`stdout:\n${stdout}`);
    }
    if (stderr) {
      lines.push(`stderr:\n${stderr}`);
    }
    if (outputTruncated) {
      lines.push('note: 输出较长，已截断');
    }
    return lines.join('\n').trim();
  };
```

替换为：
```typescript
  const formatCommandExecutionMessage = (
    payload: Record<string, unknown>,
    fallbackCommand: string,
  ): string => {
    const command = String(payload.command || fallbackCommand || '').trim();
    const stdout = String(payload.stdout || '').trim();
    const stderr = String(payload.stderr || '').trim();
    const exitCode = Number(payload.exit_code);
    const parts: string[] = [];

    if (command) {
      parts.push('```bash', command, '```');
    }
    if (stdout) {
      parts.push('', '**stdout:**', '', '```', stdout, '```');
    }
    if (stderr) {
      parts.push('', '**stderr:**', '', '```', stderr, '```');
    }
    if (Number.isFinite(exitCode)) {
      parts.push('', `> exit: ${exitCode}`);
    }
    return parts.join('\n').trim();
  };
```

- [ ] **Step 2: 修改 `buildRuntimeAnalysisFollowUpMessage` 中去掉 240 截断**

查找约 line 352-353：
```typescript
        if (cmdStatus === 'completed' && (out || err)) {
          content = `命令已执行\n${cmd}${out ? `\n→ ${out.substring(0, 240)}` : ''}${err ? `\n⚠ ${err.substring(0, 240)}` : ''}`;
```

替换为：
```typescript
        if (cmdStatus === 'completed' && (out || err)) {
          const msgParts: string[] = [];
          if (cmd) msgParts.push('```bash', cmd, '```');
          if (out) msgParts.push('', '**stdout:**', '', '```', out, '```');
          if (err) msgParts.push('', '**stderr:**', '', '```', err, '```');
          if (typeof latestCommandRun.exitCode === 'number') {
            msgParts.push('', `> exit: ${latestCommandRun.exitCode}`);
          }
          content = msgParts.join('\n').trim();
```

- [ ] **Step 3: 验证编译**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无类型错误

- [ ] **Step 4: Commit**

```bash
git add frontend/src/features/ai-runtime/utils/runtimeMessages.ts frontend/src/pages/AIAnalysis.tsx
git commit -m "feat: remove 240-char truncation and noise fields from command output messages"
```

---

### Task 5: 前端 AIAnalysis.tsx 消息气泡内渲染命令输出

**Files:**
- Modify: `frontend/src/pages/AIAnalysis.tsx` (消息渲染循环，约 line 6648)

- [ ] **Step 1: 在气泡 content 下方添加 action_observations 渲染区块**

在 `renderFollowUpRichContent(msg.content, ...)` 的 `</div>` 关闭标签（约 line 6667）之后、时间戳（约 line 6678）之前，插入：

```tsx
                        </div>
                        {/* ── 命令输出区块（自动化诊断） ── */}
                        {msg.role === 'assistant' && messageObservations.length > 0 && !streamLoading && (
                          <div className="mt-2 space-y-2 border-t border-slate-100 pt-2">
                            {messageObservations.map((obs, obsIndex) => {
                              const obsPayload = obs as UnknownObject;
                              const obsCommand = String(obsPayload.command || '').trim();
                              const obsStdout = String(obsPayload.stdout || '').trim();
                              const obsStderr = String(obsPayload.stderr || '').trim();
                              const obsExitCode = Number(obsPayload.exit_code);
                              if (!obsCommand && !obsStdout && !obsStderr) return null;
                              return (
                                <div key={`obs-${index}-${obsIndex}`} className="rounded border border-slate-200 bg-slate-50 p-2">
                                  {obsCommand && (
                                    <pre className="text-[11px] font-mono text-slate-800 whitespace-pre-wrap overflow-auto max-h-32">
                                      {`$ ${obsCommand}`}
                                    </pre>
                                  )}
                                  {obsStdout && (
                                    <>
                                      <div className="mt-1 text-[10px] font-medium text-slate-500">stdout</div>
                                      <pre className="mt-0.5 text-[11px] font-mono text-emerald-800 whitespace-pre-wrap overflow-auto max-h-60 bg-white border border-slate-100 rounded p-1.5">
                                        {obsStdout}
                                      </pre>
                                    </>
                                  )}
                                  {obsStderr && (
                                    <>
                                      <div className="mt-1 text-[10px] font-medium text-rose-500">stderr</div>
                                      <pre className="mt-0.5 text-[11px] font-mono text-rose-700 whitespace-pre-wrap overflow-auto max-h-32 bg-white border border-rose-100 rounded p-1.5">
                                        {obsStderr}
                                      </pre>
                                    </>
                                  )}
                                  {Number.isFinite(obsExitCode) && (
                                    <div className="mt-1 text-[10px] text-slate-400">exit: {obsExitCode}</div>
                                  )}
                                </div>
                              );
                            })}
                          </div>
                        )}
```

**重要**: 在插入处确认 `messageObservations` 变量已在作用域中（约 line 6609-6611 定义）。

- [ ] **Step 2: 隐藏冗余面板**

查找"思考过程"面板的渲染条件（约 line 6683）：
```tsx
                      {/* 思考过程 */}
                      {msg.role === 'assistant' && messageThoughtTimeline.length > 0 && (
```

改为在有 action_observations 时跳过：
```tsx
                      {/* 思考过程（有自动化诊断结果时折叠到消息内容中，不重复展示） */}
                      {msg.role === 'assistant' && messageThoughtTimeline.length > 0 && messageObservations.length === 0 && (
```

查找"操作按钮"面板（约 line 6749，注释 `{/* 操作按钮 */}`）：
```tsx
                      {/* 操作按钮 */}
                      {msg.role === 'assistant' && !streamLoading && (
```

改为：
```tsx
                      {/* 操作按钮（有自动化诊断结果时隐藏） */}
                      {msg.role === 'assistant' && !streamLoading && messageObservations.length === 0 && (
```

- [ ] **Step 3: 验证编译 + lint**

Run:
```bash
cd frontend && npx tsc --noEmit && npm run lint -- --max-warnings 0
```
Expected: 无错误

- [ ] **Step 4: 自测渲染逻辑**

在 `frontend/src/pages/AIAnalysis.tsx` 中找到触发 `appendFollowUpAssistantMessage` 的地方，确认该函数在调用时设置了 `command_execution: true`（约 line 4193）。

如果 `buildRuntimeAnalysisFollowUpMessage` 返回的消息中也设置了 `command_execution: true` 标记（或在 metadata 中有 `action_observations`），则新渲染逻辑自动生效。

确认 `messageObservations` 变量在 line 6609-6611 的定义：
```typescript
const messageObservations = Array.isArray(messageMetadata?.action_observations)
  ? messageMetadata.action_observations as Array<Record<string, unknown>>
  : [];
```
确保 `messageMetadata` 有值（从 `msg.metadata` 获取，约 line 6581-6583）。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/AIAnalysis.tsx
git commit -m "feat: render command outputs in message bubble, hide redundant panels for diagnostic messages"
```

---

### Task 6: 回归测试

**Files:** 全部已修改文件

- [ ] **Step 1: 运行所有后端测试**

Run: `cd ai-service && python -m pytest -x --tb=short`
Expected: All tests pass (包括之前修复的 timeout 测试)

- [ ] **Step 2: 运行所有前端测试**

Run: `cd frontend && npm run typecheck && npm run lint -- --max-warnings 0`
Expected: 无类型错误、无 lint 警告

- [ ] **Step 3: 提交最终 commit**

```bash
git add -A
git commit -m "chore: post-implementation cleanup and verification"
```

---

## 执行选项

方案已保存至 `docs/superpowers/plans/2026-06-12-ai-runtime-clean-output.md`。

**两种执行方式：**
1. **Subagent-Driven（推荐）** — 每个 Task 派一个子 agent，独立执行+验证，并行度高
2. **Inline Execution** — 在当前会话中按 Task 顺序执行

选哪种？
