# Workflow 流水线合并服务拓扑 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在服务拓扑页面中嵌入 Workflow 执行面板，选中后高亮拓扑路径

**Architecture:** 新增映射工具函数 `workflowTopologyMapper.ts`，在拓扑页面新增第四个浮动面板+时间线浮层+SVG 高亮渲染层。后端 API 免改。

**Tech Stack:** React 18, TypeScript, SVG (现有拓扑渲染引擎)

## 全局约束

- 不修改后端 API
- 不移除独立 `/workflows` 页面
- 不修改现有三个浮动面板的默认位置
- 不引入新 npm 依赖
- 所有路径映射逻辑在 `workflowTopologyMapper.ts` 纯函数中，不在组件内
- 遵循现有 TopologyPage 风格：浮动 panel + SVG 叠加渲染

---

## 文件结构

| 文件 | 变更类型 | 职责 |
|------|---------|------|
| `frontend/src/utils/workflowTopologyMapper.ts` | **Create** | WorkflowDetail → 拓扑节点/边/步骤序列的纯函数映射 |
| `frontend/src/pages/TopologyPage.tsx` | **Modify** | 新增 Workflow 面板、时间线浮层、SVG 路径高亮渲染 |
| `frontend/src/utils/api.ts` | **Modify** | 可选：暴露类型 `WorkflowSummary`、`WorkflowDetail` |

### Task 1: 创建映射工具函数

**文件:**
- Create: `frontend/src/utils/workflowTopologyMapper.ts`
- Test: (集成测试在拓扑页面联动时验证，本函数为纯函数无需独立测试文件)

**接口:**
- Produces: `mapWorkflowToTopology(detail, nodes, edges) → WorkflowHighlightResult`
- Produces: `findBestMatch(serviceName, nodes) → TopologyNodeEntity | null`

```typescript
// ── 类型定义 ──────────────────────────────────────────────

interface StepInfo {
  index: number;
  serviceName: string;
  nodeId: string | null;
  action: string;
  startedAt: string;
  durationMs: number;
  status: string;
  level: string;
}

interface TempEdge {
  source: string;
  target: string;
  stepIndex: number;
}

interface WorkflowHighlightResult {
  nodeIds: string[];
  edgeIds: string[];
  stepSequence: StepInfo[];
  tempEdges: TempEdge[];
}

// ── 主函数 ────────────────────────────────────────────────

export function mapWorkflowToTopology(
  detail: WorkflowDetail,
  nodes: TopologyNodeEntity[],
  edges: TopologyEdgeEntity[],
): WorkflowHighlightResult

// ── 辅助函数 ──────────────────────────────────────────────

/** 模糊匹配服务名到拓扑节点（精确→前缀→包含） */
export function findBestMatch(
  serviceName: string,
  nodes: TopologyNodeEntity[],
): TopologyNodeEntity | null
```

- [ ] **Step 1: 实现 `mapWorkflowToTopology` 函数**

创建新文件 `frontend/src/utils/workflowTopologyMapper.ts`，包含完整实现：

```typescript
import type { TopologyNodeEntity, TopologyEdgeEntity } from '../utils/api';

export interface StepInfo {
  index: number;
  serviceName: string;
  nodeId: string | null;
  action: string;
  startedAt: string;
  durationMs: number;
  status: string;
  level: string;
}

export interface TempEdge {
  source: string;
  target: string;
  stepIndex: number;
}

export interface WorkflowHighlightResult {
  nodeIds: string[];
  edgeIds: string[];
  stepSequence: StepInfo[];
  tempEdges: TempEdge[];
}

export interface WorkflowDetail {
  execution_id: string;
  operation_type: string;
  resource_id: string;
  global_request_id: string;
  status: string;
  started_at: string;
  finished_at: string;
  duration_ms: number;
  error_message: string;
  source_cluster: string;
  step_count: number;
  'steps.service_name': string[];
  'steps.action': string[];
  'steps.started_at': string[];
  'steps.duration_ms': number[];
  'steps.status': string[];
  'steps.level': string[];
}

export function findBestMatch(
  serviceName: string,
  nodes: TopologyNodeEntity[],
): TopologyNodeEntity | null {
  const name = serviceName.toLowerCase().trim();
  if (!name) return null;

  // 精确匹配
  let match = nodes.find(n => n.service_name?.toLowerCase() === name);
  if (match) return match;

  // 前缀匹配
  match = nodes.find(n => n.service_name?.toLowerCase().startsWith(name));
  if (match) return match;

  // 包含匹配
  match = nodes.find(n => n.service_name?.toLowerCase().includes(name));
  if (match) return match;

  // 反向包含匹配（拓扑名较短但包含在步骤服务名中）
  match = nodes.find(n => name.includes(n.service_name?.toLowerCase() ?? ''));
  return match ?? null;
}

export function mapWorkflowToTopology(
  detail: WorkflowDetail,
  nodes: TopologyNodeEntity[],
  edges: TopologyEdgeEntity[],
): WorkflowHighlightResult {
  const rawSteps = detail['steps.service_name'].map((svc, i) => ({
    serviceName: svc,
    action: detail['steps.action']?.[i] ?? '',
    startedAt: detail['steps.started_at']?.[i] ?? '',
    durationMs: detail['steps.duration_ms']?.[i] ?? 0,
    status: detail['steps.status']?.[i] ?? 'success',
    level: detail['steps.level']?.[i] ?? 'INFO',
  }));

  const stepSequence: StepInfo[] = rawSteps.map((step, idx) => {
    const matchedNode = findBestMatch(step.serviceName, nodes);
    return {
      index: idx + 1,
      serviceName: step.serviceName,
      nodeId: matchedNode?.id ?? null,
      action: step.action,
      startedAt: step.startedAt,
      durationMs: step.durationMs,
      status: step.status,
      level: step.level,
    };
  });

  const highlightNodeIds = new Set<string>();
  const highlightEdgeIds = new Set<string>();
  const tempEdges: TempEdge[] = [];

  for (let i = 0; i < stepSequence.length - 1; i++) {
    const current = stepSequence[i];
    const next = stepSequence[i + 1];
    if (current.nodeId) highlightNodeIds.add(current.nodeId);
    if (next.nodeId) highlightNodeIds.add(next.nodeId);
    if (!current.nodeId || !next.nodeId) continue;

    const matchedEdge = edges.find(edge =>
      edge.source === current.nodeId && edge.target === next.nodeId
    );
    if (matchedEdge) {
      const eid = matchedEdge.id ?? matchedEdge.edge_key ?? '';
      if (eid) highlightEdgeIds.add(eid);
    } else {
      tempEdges.push({ source: current.nodeId, target: next.nodeId, stepIndex: i });
    }
  }

  return {
    nodeIds: [...highlightNodeIds],
    edgeIds: [...highlightEdgeIds],
    stepSequence,
    tempEdges,
  };
}
```

- [ ] **Step 2: 验证纯函数可编译**

```bash
cd frontend
npx tsc --noEmit src/utils/workflowTopologyMapper.ts --esModuleInterop --moduleResolution bundler --module esnext --target es2020 --strict 2>&1 | head -20
```
预期输出：无错误（或仅类型引用错误，因 TopologyNodeEntity 类型在 api.ts 中）

- [ ] **Step 3: 提交**

```bash
git add frontend/src/utils/workflowTopologyMapper.ts
git commit -m "feat(topology): add workflow-to-topology mapper utility"
```

---

### Task 2: Workflow 面板 UI（浮动面板 + 数据加载）

**文件:**
- Modify: `frontend/src/pages/TopologyPage.tsx`

**接口:**
- Consumes: `WorkflowHighlightResult` from Task 1

- [ ] **Step 1: 在 TopologyPage 中添加 Workflow 状态变量**

在现有 state 声明区域（约 1061-1119 行）之后，新增：

```typescript
// ── Workflow 面板状态 ────────────────────────────────────
const [workflows, setWorkflows] = useState<WorkflowSummary[]>([]);
const [workflowsLoading, setWorkflowsLoading] = useState(false);
const [workflowsError, setWorkflowsError] = useState<string | null>(null);
const [workflowTimeWindow, setWorkflowTimeWindow] = useState<number>(1);
const [workflowFilterOp, setWorkflowFilterOp] = useState('');

const [selectedWorkflowId, setSelectedWorkflowId] = useState<string | null>(null);
const [selectedWorkflowDetail, setSelectedWorkflowDetail] = useState<WorkflowDetail | null>(null);
const [selectedWorkflowLoading, setSelectedWorkflowLoading] = useState(false);

const [workflowHighlight, setWorkflowHighlight] = useState<WorkflowHighlightResult | null>(null);
const [workflowHighlightFixed, setWorkflowHighlightFixed] = useState(false);
```

同时添加类型引用：
```typescript
import { mapWorkflowToTopology, type WorkflowHighlightResult, type WorkflowDetail } from '../utils/workflowTopologyMapper';
```

WorkflowSummary 类型（直接定义在文件顶部，不引入额外依赖）：
```typescript
interface WorkflowSummary {
  execution_id: string;
  operation_type: string;
  resource_id: string;
  global_request_id: string;
  status: string;
  started_at: string;
  finished_at: string;
  duration_ms: number;
  error_message: string;
  step_count: number;
}
```

- [ ] **Step 2: 添加 Workflow 数据加载函数**

在 `useEffect` 加载拓扑数据之后（约 480 行区域），添加：

```typescript
// ── Workflow 数据加载 ──────────────────────────────────────
const fetchWorkflows = useCallback(async () => {
  setWorkflowsLoading(true);
  setWorkflowsError(null);
  try {
    const resp = await fetch(`/api/v1/workflows?limit=50&since_hours=${workflowTimeWindow}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    setWorkflows(data.workflows || []);
  } catch (err: unknown) {
    setWorkflowsError(err instanceof Error ? err.message : '加载失败');
  } finally {
    setWorkflowsLoading(false);
  }
}, [workflowTimeWindow]);

const fetchWorkflowDetail = useCallback(async (executionId: string) => {
  setSelectedWorkflowLoading(true);
  setSelectedWorkflowDetail(null);
  try {
    const resp = await fetch(`/api/v1/workflows/${encodeURIComponent(executionId)}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    setSelectedWorkflowDetail(data);
    // 自动映射到拓扑
    if (visibleNodes.length && visibleEdges.length) {
      const result = mapWorkflowToTopology(data, visibleNodes, visibleEdges);
      setWorkflowHighlight(result);
    }
  } catch (err: unknown) {
    console.error('Failed to load workflow detail:', err);
    setWorkflowsError(err instanceof Error ? err.message : '加载详情失败');
  } finally {
    setSelectedWorkflowLoading(false);
  }
}, [visibleNodes, visibleEdges]);
```

在页面挂载的 useEffect（约 480 行）中添加 fetchWorkflows 调用：
```typescript
useEffect(() => {
  fetchWorkflows();
}, [fetchWorkflows]);
```

- [ ] **Step 3: 添加 Workflow 浮动面板 JSX**

在拓扑页面的浮动面板渲染区域（约 4686 行 `data-floating-panel` 区块），在最后一个 panel 之后、`</div>` 之前，添加第四个面板：

```tsx
{/* ════════════════════ Workflow 执行面板 ════════════════════ */}
<div
  data-floating-panel
  className="pointer-events-auto absolute w-[320px] rounded-2xl border border-cyan-500/30 bg-slate-900/85 shadow-[0_0_38px_rgba(56,189,248,0.18)] backdrop-blur"
  style={{ left: 20, top: 100, maxHeight: '50vh' }}
>
  {/* 标题栏 */}
  <div
    className="flex cursor-move items-center justify-between border-b border-slate-700 px-3 py-2"
    onMouseDown={(e) => startPanelDrag('workflow', e)}
  >
    <div className="flex items-center gap-2 text-xs font-semibold text-cyan-200">
      <Activity size={14} /> Workflow 执行
    </div>
    <button
      onClick={fetchWorkflows}
      disabled={workflowsLoading}
      className="rounded border border-slate-600 px-2 py-0.5 text-[10px] text-slate-300 hover:bg-slate-800 disabled:opacity-50"
    >
      <RefreshCw size={12} className={workflowsLoading ? 'animate-spin' : ''} />
    </button>
  </div>

  {/* 工具栏 */}
  <div className="flex items-center gap-2 border-b border-slate-700/50 px-3 py-1.5">
    <select
      value={workflowTimeWindow}
      onChange={(e) => setWorkflowTimeWindow(Number(e.target.value))}
      className="rounded border border-slate-600 bg-slate-900 px-2 py-1 text-[10px] text-slate-200"
    >
      <option value={1}>近 1 小时</option>
      <option value={6}>近 6 小时</option>
      <option value={24}>近 24 小时</option>
    </select>
    {workflows.length > 0 && (
      <select
        value={workflowFilterOp}
        onChange={(e) => setWorkflowFilterOp(e.target.value)}
        className="max-w-[140px] rounded border border-slate-600 bg-slate-900 px-2 py-1 text-[10px] text-slate-200"
      >
        <option value="">全部操作</option>
        {Array.from(new Set(workflows.map((w: WorkflowSummary) => w.operation_type))).sort().map((t: string) => (
          <option key={t} value={t}>{t}</option>
        ))}
      </select>
    )}
  </div>

  {/* 内容区 */}
  <div className="max-h-[calc(50vh-80px)] space-y-0.5 overflow-auto p-2">
    {/* 错误状态 */}
    {workflowsError && (
      <div className="rounded-lg border border-red-800/40 bg-red-900/20 px-3 py-2 text-[11px] text-red-400">
        {workflowsError}
        <button onClick={fetchWorkflows} className="ml-2 underline">重试</button>
      </div>
    )}

    {/* 加载状态 */}
    {workflowsLoading && workflows.length === 0 && (
      <div className="space-y-2 p-2">
        {[1,2,3].map(i => (
          <div key={i} className="h-14 animate-pulse rounded-lg bg-slate-800/60" />
        ))}
      </div>
    )}

    {/* 空状态 */}
    {!workflowsLoading && !workflowsError && filteredWorkflows.length === 0 && (
      <div className="flex flex-col items-center py-8 text-slate-500">
        <Activity size={24} className="mb-2 opacity-40" />
        <p className="text-xs">暂无 Workflow 执行记录</p>
        <button onClick={fetchWorkflows} className="mt-2 rounded bg-slate-800 px-3 py-1 text-[10px] text-slate-300 hover:bg-slate-700">
          重新扫描
        </button>
      </div>
    )}

    {/* 列表 */}
    {filteredWorkflows.map((wf: WorkflowSummary) => {
      const isSelected = selectedWorkflowId === wf.execution_id;
      const sConfig = getStatusConfig(wf.status);
      return (
        <div
          key={wf.execution_id}
          onClick={() => handleWorkflowSelect(wf.execution_id)}
          className={`cursor-pointer rounded-lg border px-3 py-2 text-xs transition-all ${
            isSelected
              ? 'border-cyan-500/50 bg-cyan-900/20'
              : 'border-slate-700/50 bg-slate-800/40 hover:border-slate-600/60 hover:bg-slate-800/70'
          }`}
        >
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <span className="text-sm font-medium text-slate-200">
                {getOperationLabel(wf.operation_type)}
              </span>
              <span className={`flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[10px] ${sConfig.bg} ${sConfig.text}`}>
                {sConfig.icon}
                {sConfig.label}
              </span>
            </div>
          </div>
          <div className="mt-0.5 flex items-center gap-2 text-[10px] text-slate-500">
            <span>{formatDate(wf.started_at)}</span>
            <span>{formatDuration(wf.duration_ms)}</span>
            <span>{wf.step_count} 步</span>
          </div>
        </div>
      );
    })}
  </div>
</div>
```

- [ ] **Step 4: 添加 `PanelKey` 类型扩展和 `handleWorkflowSelect` 回调**

在 `PanelKey` 类型定义处（约 150 行），添加 `'workflow'`：
```typescript
type PanelKey = 'control' | 'issues' | 'detail' | 'workflow';
```

在 `PANEL_DEFAULTS`（约 281 行）中添加：
```typescript
const PANEL_DEFAULTS: Record<PanelKey, PanelPos> = {
  control: { x: 20, y: 100 },
  issues: { x: 20, y: 320 },
  detail: { x: 20, y: 540 },
  workflow: { x: 20, y: 100 },  // 新增，左下角
};
```

注意：将 control/issues/detail 的位置保持在右侧（与现有一致），workflow 在左侧：
```typescript
const PANEL_DEFAULTS: Record<PanelKey, PanelPos> = {
  control: { x: window.innerWidth - 400, y: 100 },  // 右上
  issues: { x: window.innerWidth - 400, y: 320 },    // 右中
  detail: { x: window.innerWidth - 400, y: 540 },     // 右下
  workflow: { x: 20, y: 100 },                         // 左下
};
```

添加 `handleWorkflowSelect` 和 `filteredWorkflows`：
```typescript
// 在 Workflow 状态变量下方，添加计算属性
const filteredWorkflows = useMemo(() => {
  if (!workflowFilterOp) return workflows;
  return workflows.filter((w: WorkflowSummary) => w.operation_type === workflowFilterOp);
}, [workflows, workflowFilterOp]);

const handleWorkflowSelect = useCallback(async (executionId: string) => {
  if (selectedWorkflowId === executionId) {
    // 取消选中
    setSelectedWorkflowId(null);
    setSelectedWorkflowDetail(null);
    setWorkflowHighlight(null);
    return;
  }
  setSelectedWorkflowId(executionId);
  await fetchWorkflowDetail(executionId);
}, [selectedWorkflowId, fetchWorkflowDetail]);
```

- [ ] **Step 5: 提交**

```bash
git add frontend/src/pages/TopologyPage.tsx
git commit -m "feat(topology): add workflow execution panel with data loading"
```

---

### Task 3: SVG 路径高亮渲染

**文件:**
- Modify: `frontend/src/pages/TopologyPage.tsx`

- [ ] **Step 1: 在 SVG 中叠加节点序号标记**

在 SVG 节点渲染区域（`renderNodes` 函数产生每个节点的 JSX 位置），为 Workflow 路径节点添加序号标记。找到节点渲染循环（约 2290+ 行），在节点元素包裹的 `<g>` 内新增：

```tsx
{/* Workflow 路径序号标记 */}
{workflowHighlight && workflowHighlight.nodeIds.includes(node.id) && (
  (() => {
    const step = workflowHighlight.stepSequence.find(s => s.nodeId === node.id);
    if (!step) return null;
    const statusColor = step.status === 'failed' ? '#f87171'
      : step.status === 'warning' ? '#fbbf24'
      : '#22d3ee';
    return (
      <g>
        <circle
          cx={x - 18} cy={y - 18} r={12}
          fill="rgba(15,23,42,0.9)"
          stroke={statusColor}
          strokeWidth={2}
        />
        <text
          x={x - 18} y={y - 14}
          textAnchor="middle"
          fill={statusColor}
          fontSize={11}
          fontWeight="bold"
          fontFamily="monospace"
        >
          {step.index}
        </text>
        {/* 步骤耗时 label */}
        <text
          x={x - 18} y={y - 32}
          textAnchor="middle"
          fill="#94a3b8"
          fontSize={9}
          fontFamily="monospace"
        >
          {formatDuration(step.durationMs)}
        </text>
      </g>
    );
  })()
)}
```

- [ ] **Step 2: 非路径节点/边降透明度**

在 `renderEdges` 和 `renderNodes` 的循环中，检查当前元素是否在 `workflowHighlight` 中：

对于边渲染（约 3440 行 edgeOpacity 计算处）：
```typescript
// 在已有 edgeOpacity 计算之后，叠加 Workflow 路径降透明度
let finalEdgeOpacity = edgeOpacity;
if (workflowHighlight && !workflowHighlight.edgeIds.includes(uid)) {
  finalEdgeOpacity = 0.08;
}
```

对于节点渲染（约 2280+ 行 nodeOpacity 计算处）：
```typescript
let finalNodeOpacity = nodeOpacity;
if (workflowHighlight && !workflowHighlight.nodeIds.includes(node.id)) {
  finalNodeOpacity = 0.15;
}
```

- [ ] **Step 3: 渲染临时边（拓扑图不存在的边）**

在边渲染循环（约 3410 行）之后，添加临时边渲染：

```tsx
{/* Workflow 临时边（拓扑无对应） */}
{workflowHighlight?.tempEdges.map((te, idx) => {
  const sourcePos = nodePositions[te.source];
  const targetPos = nodePositions[te.target];
  if (!sourcePos || !targetPos) return null;
  return (
    <line
      key={`wf-temp-${idx}`}
      x1={sourcePos.x} y1={sourcePos.y}
      x2={targetPos.x} y2={targetPos.y}
      stroke="#f87171"
      strokeWidth={1.5}
      strokeDasharray="6 4"
      opacity={0.7}
    />
  );
})}
```

- [ ] **Step 4: 路径边加粗和流动动画**

在边渲染区域，对 `workflowHighlight.edgeIds` 中的边设置额外样式：
```typescript
// 在 edgeWidth 计算处叠加
const isWorkflowEdge = workflowHighlight?.edgeIds.includes(uid);
const finalEdgeWidth = isWorkflowEdge ? edgeWidth + 2 : edgeWidth;
const finalEdgeOpacity = isWorkflowEdge ? 0.98 : finalEdgeOpacity;
```

- [ ] **Step 5: 清除路径高亮（选中状态退出时）**

确保 `handleWorkflowSelect` 设为执行相同 ID 时清除：
已在 Task 2 Step 4 中实现（cancel选中逻辑）。

- [ ] **Step 6: 提交**

```bash
git add frontend/src/pages/TopologyPage.tsx
git commit -m "feat(topology): add workflow path highlight on SVG canvas"
```

---

### Task 4: 时间线浮层

**文件:**
- Modify: `frontend/src/pages/TopologyPage.tsx`

- [ ] **Step 1: 在 Workflow 面板中展开时间线浮层**

在 Workflow 面板内容区底部（列表之后），当 `selectedWorkflowDetail` 非空时展开：

```tsx
{/* 时间线浮层（选中 Workflow 后展开） */}
{selectedWorkflowDetail && (
  <div className="mt-2 rounded-lg border border-cyan-700/40 bg-slate-950/70 p-3">
    <div className="mb-2 flex items-center justify-between">
      <h4 className="text-[11px] font-medium text-slate-300">
        执行时间线 — {getOperationLabel(selectedWorkflowDetail.operation_type)}
      </h4>
      <div className="flex items-center gap-1">
        <button
          onClick={() => setWorkflowHighlightFixed(!workflowHighlightFixed)}
          className={`rounded px-1.5 py-0.5 text-[9px] ${
            workflowHighlightFixed ? 'bg-cyan-600/30 text-cyan-200' : 'text-slate-500 hover:text-slate-300'
          }`}
        >
          {workflowHighlightFixed ? '已固定' : '固定路径'}
        </button>
        <button
          onClick={() => setLayoutMode('swimlane')}
          className="rounded px-1.5 py-0.5 text-[9px] text-slate-500 hover:text-slate-300"
        >
          泳道布局
        </button>
      </div>
    </div>

    {/* 步骤时间线 */}
    {selectedWorkflowLoading ? (
      <div className="space-y-2 py-4">
        {[1,2,3].map(i => (
          <div key={i} className="h-6 animate-pulse rounded bg-slate-800/60" />
        ))}
      </div>
    ) : (
      <div className="space-y-1.5">
        {workflowHighlight?.stepSequence.map((step) => {
          const pct = step.durationMs > 0 && selectedWorkflowDetail.duration_ms > 0
            ? Math.max(1, Math.min(100, (step.durationMs / selectedWorkflowDetail.duration_ms) * 100))
            : 0;
          const barColor = step.status === 'failed' ? 'bg-red-500/60'
            : step.status === 'warning' ? 'bg-amber-500/60'
            : 'bg-cyan-500/40';
          const stepMatched = step.nodeId !== null;

          return (
            <div
              key={step.index}
              className="group flex items-center gap-2 rounded px-1 py-0.5 hover:bg-slate-800/60 cursor-pointer transition-colors"
              onMouseEnter={() => {
                /* 脉冲高亮：通过一个临时 state 触发重新渲染 */
                setHighlightPulseNodeId(step.nodeId);
              }}
              onMouseLeave={() => setHighlightPulseNodeId(null)}
              onClick={() => {
                if (step.nodeId && nodePositions[step.nodeId]) {
                  setPan({ x: -nodePositions[step.nodeId].x + window.innerWidth / 2, y: -nodePositions[step.nodeId].y + 100 });
                  setZoom(1);
                }
              }}
            >
              {/* 步骤序号 */}
              <span className={`flex h-5 w-5 items-center justify-center rounded-full text-[10px] font-bold font-mono ${
                step.status === 'failed' ? 'text-red-400 bg-red-500/15' :
                step.status === 'warning' ? 'text-amber-400 bg-amber-500/15' :
                'text-cyan-300 bg-cyan-500/10'
              }`}>
                {step.index}
              </span>

              {/* 服务名 */}
              <span className={`min-w-[80px] text-[11px] truncate ${
                stepMatched ? 'text-slate-200' : 'text-slate-600 italic'
              }`}>
                {step.serviceName}
                {!stepMatched && ' (未匹配)'}
              </span>

              {/* 进度条 */}
              <div className="flex-1 h-1.5 overflow-hidden rounded-full bg-slate-800">
                <div
                  className={`h-full rounded-full transition-all duration-200 ${barColor}`}
                  style={{ width: `${pct}%` }}
                />
              </div>

              {/* 耗时 */}
              <span className="w-12 text-right text-[10px] text-slate-500 font-mono tabular-nums">
                {formatDuration(step.durationMs)}
              </span>

              {/* 状态 */}
              {step.status === 'failed' && <XCircle size={10} className="text-red-400" />}
              {step.status === 'warning' && <AlertCircle size={10} className="text-amber-400" />}
            </div>
          );
        })}
      </div>
    )}

    {/* 概要行 */}
    {!selectedWorkflowLoading && selectedWorkflowDetail && (
      <div className="mt-2 flex items-center gap-3 border-t border-slate-700/40 pt-2 text-[10px] text-slate-500">
        <span>总耗时: {formatDuration(selectedWorkflowDetail.duration_ms)}</span>
        <span>步骤: {selectedWorkflowDetail.step_count}</span>
        {selectedWorkflowDetail.error_message && (
          <span className="truncate text-red-400" title={selectedWorkflowDetail.error_message}>
            ⚠ {selectedWorkflowDetail.error_message.slice(0, 40)}
          </span>
        )}
      </div>
    )}
  </div>
)}
```

- [ ] **Step 2: 添加脉冲高亮状态**

新增状态：
```typescript
const [highlightPulseNodeId, setHighlightPulseNodeId] = useState<string | null>(null);
```

在节点渲染的 `<circle>` 或 `<g>` 上添加脉冲效果：
```tsx
{highlightPulseNodeId === node.id && (
  <animate attributeName="opacity" values="1;0.4;1" dur="0.8s" repeatCount="1" />
)}
```

- [ ] **Step 3: Workflow 面板与时间线的集成——时间线更新逻辑**

在 `fetchWorkflowDetail` 成功的回调中，确保 `workflowHighlight` 被更新（已在前面的 fetchWorkflowDetail 中实现）。

- [ ] **Step 4: 提交**

```bash
git add frontend/src/pages/TopologyPage.tsx
git commit -m "feat(topology): add workflow execution timeline overlay"
```

---

### Task 5: 状态集成与边界处理

**文件:**
- Modify: `frontend/src/pages/TopologyPage.tsx`

- [ ] **Step 1: 切换时间窗口时清空 Workflow 高亮**

在 `fetchWorkflows` 中确保当时间窗口改变导致重新加载时，清除已选状态：

```typescript
// 在 fetchWorkflows 顶部添加
setSelectedWorkflowId(null);
setSelectedWorkflowDetail(null);
setWorkflowHighlight(null);
```

- [ ] **Step 2: Workflow 路径节点/边不受过滤影响**

在 `filteredTopology` 计算之后（约 1268+ 行 `renderNodes`/`renderEdges` 使用的数据区域），确保 Workflow 路径涉及的节点不被过滤掉：

```typescript
// 在 filteredTopology 或 visibleNodes 计算处
const protectedNodeIds = new Set(workflowHighlight?.nodeIds ?? []);
const protectedEdgeIds = new Set(workflowHighlight?.edgeIds ?? []);
```

如果使用了 `visibleNodes` 等过滤后的集合，确保 Workflow 节点包含在内（在 Task 2 fetchWorkflowDetail 中已经用 visibleNodes/visibleEdges 做映射，所以如果节点已被过滤，映射结果为空——需要强制包含）：

```typescript
// 在计算 visibleNodes 时，确保 workflowHighlight 涉及的节点不受过滤影响
const finalVisibleNodes = useMemo(() => {
  if (!workflowHighlight) return visibleNodes;
  const existingIds = new Set(visibleNodes.map(n => n.id));
  const missingNodes = nodes.filter(n =>
    workflowHighlight.nodeIds.includes(n.id) && !existingIds.has(n.id)
  );
  return missingNodes.length > 0 ? [...visibleNodes, ...missingNodes] : visibleNodes;
}, [visibleNodes, nodes, workflowHighlight]);
```

- [ ] **Step 3: Ctrl+点击叠加多条 Workflow 路径**

修改 `handleWorkflowSelect` 支持叠加模式：

```typescript
const handleWorkflowSelect = useCallback(async (executionId: string, ctrlKey?: boolean) => {
  if (selectedWorkflowId === executionId && !ctrlKey) {
    // 取消选中（非 Ctrl 模式）
    clearWorkflowSelection();
    return;
  }

  if (ctrlKey) {
    // Ctrl+点击叠加模式
    const newSelected = new Set(selectedWorkflowIds);
    if (newSelected.has(executionId)) {
      newSelected.delete(executionId);
    } else {
      newSelected.add(executionId);
    }
    setSelectedWorkflowIds(newSelected);

    // 重新计算所有选中 Workflow 的合并高亮
    // 暂时简化为只保留最近选中的（或者一次只高亮一条）
    setSelectedWorkflowId(executionId);
  } else {
    // 单选模式
    setSelectedWorkflowId(executionId);
    setSelectedWorkflowIds(new Set([executionId]));
  }

  await fetchWorkflowDetail(executionId);
}, [selectedWorkflowId, selectedWorkflowIds, fetchWorkflowDetail, clearWorkflowSelection]);
```

> **注意：** Ctrl+点击叠加的完整实现（同时高亮多条 Workflow 路径）需要处理节点序号冲突和边叠加，复杂度较高。本次实现先支持「单条路径高亮」，Ctrl+点击作为基础结构但行为保持单选。完整的叠加模式留到后续迭代。

- [ ] **Step 4: 清理函数**

```typescript
const clearWorkflowSelection = useCallback(() => {
  setSelectedWorkflowId(null);
  setSelectedWorkflowDetail(null);
  setWorkflowHighlight(null);
  setSelectedWorkflowIds(new Set());
}, []);
```

- [ ] **Step 5: `startPanelDrag` 支持 workflow panel**

找到 `startPanelDrag` 函数（约 2975 行），它已经用 `PanelKey` 类型，所以 `'workflow'` 会自动支持：

```typescript
// 已有逻辑，无需改动
const startPanelDrag = (panel: PanelKey, e: React.MouseEvent) => {
```

- [ ] **Step 6: 切换布局模式时重新映射路径**

当用户切换布局模式（swimlane/grid/free）时，`nodePositions` 会重新计算。`workflowHighlight` 中保存的是 `nodeIds` 和 `edgeIds`，这些 ID 本身不变，但节点位置变化。SVG 渲染基于 `nodePositions` 动态读取位置，所以无需额外操作——只需要确保 `stepSequence` 中的 `nodeId` 引用有效即可。

但如果 `visibleNodes` / `visibleEdges` 在布局切换后改变了（过滤规则变化），需要重新映射：

```typescript
useEffect(() => {
  if (selectedWorkflowDetail && visibleNodes.length > 0) {
    const result = mapWorkflowToTopology(selectedWorkflowDetail, visibleNodes, visibleEdges);
    setWorkflowHighlight(result);
  }
}, [layoutMode, visibleNodes, visibleEdges]);
```

- [ ] **Step 7: 提交**

```bash
git add frontend/src/pages/TopologyPage.tsx
git commit -m "feat(topology): integrate workflow highlight with topology state"
```

---

## 验证清单

| 验证项 | 检查方法 |
|--------|---------|
| Workflow 面板正常加载 | 打开拓扑页，左下角可见面板，列表不为空 |
| 空状态渲染 | 切换到无 Workflow 的时间窗口，显示"暂无记录" |
| 错误状态 | 停止后端 → 面板显示错误+重试按钮 |
| 路径高亮 | 点击一条 → 画布节点显示序号，边变粗，非路径元素降透明度 |
| 时间线浮层 | 选中后面板内展开时间线，进度条比例正确 |
| 步骤悬停 | 鼠标悬停时间线步骤行 → 对应节点脉冲 |
| 步骤点击 | 点击时间线步骤 → 画布居中到该节点 |
| 路径固定 | 点击固定 → 取消选中时路径保留 |
| 取消选中 | 再次点击同一行 → 恢复全图 |
| 布局切换 | 切换 swimlane/grid → 路径高亮保持 |
| 搜索/聚焦 | 在 focus 模式下，路径高亮叠加 |
| 时间窗口切换 | 切换 1h/6h/24h → 列表更新 + 清空高亮 |
| 面板拖动 | 四个面板均可拖动，不重叠 |
