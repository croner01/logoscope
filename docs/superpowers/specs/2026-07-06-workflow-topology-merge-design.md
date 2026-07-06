# Workflow 流水线与服务拓扑合并设计方案

## 概述

将独立在 `/workflows` 页面的 Cloud Workflow Timeline（OpenStack 操作执行记录）合并到服务拓扑页面中，使用户在查看服务依赖关系时能直观看到 Workflow 执行路径如何在拓扑节点间流动，无需切换页面。

## 背景

当前两个功能完全隔离：

- **服务拓扑** (`/topology`)：展示服务间依赖关系，已有 OpenStack tracing 机制（基于 `global_request_id` 的 observed 边）
- **Workflow 时间线** (`/workflows`)：展示从日志重建的 OpenStack 操作（CreateVM、DeleteVM 等），含多服务步骤序列

两者的天然关联：Workflow 的一次执行（如 CreateVM）经过的步骤序列（nova-api → nova-conductor → nova-compute → …）正是拓扑图中的节点和边。

## 设计目标

1. 用户能在拓扑图中**直接看到**当前时间窗口内的 Workflow 执行记录
2. 选中一条执行记录 → 拓扑图**高亮其完整路径**
3. 路径可视化包含**步骤序号、耗时、状态**
4. 不改变现有拓扑核心功能，互不干扰
5. 轻量集成，后端 API 免改造

## 架构概要

```
┌─────────────────────────────────────────────────────────────┐
│                    拓扑页面 (TopologyPage.tsx)                │
│                                                              │
│  ┌──────────┐  ┌─────────────────────────────────┐          │
│  │Workflow  │  │                                 │ ┌──────┐ │
│  │面板      │  │        拓扑 SVG 画布              │ │控制  │ │
│  │(左下)    │  │  ㉠ nova-api ──→ ㉡ conductor    │ │面板  │ │
│  │          │  │       ↓                           │ ├──────┤ │
│  │列表      │  │    ㉢ nova-compute                 │ │链路  │ │
│  │刷新      │  │      ↓        ↓                   │ │看板  │ │
│  │筛选      │  │ ㉣ cinder  ㉤ neutron              │ ├──────┤ │
│  │          │  │                                     │ │详情  │ │
│  └──────────┘  └─────────────────────────────────┘  │ │面板  │ │
│  ┌────────────────────────┐                          │ └──────┘ │
│  │ 执行时间线浮层 (右下)     │                          │          │
│  │ ① nova-api  ██── 3.2s  │                          │          │
│  │ ② conductor ░██─ 0.8s  │                          │          │
│  └────────────────────────┘                          │          │
└─────────────────────────────────────────────────────────────┘
```

### 组件层级

```
TopologyPage (新增状态 + 面板)
  ├── WorkflowPanel (新增浮动面板)
  │   ├── Toolbar: 刷新/时间窗口/筛选
  │   ├── WorkflowList: WorkflowSummary[] 列表
  │   └── WorkflowTimeline (选中后展开浮层)
  ├── TopologySvg (现有)
  │   └── workflowPathHighlight (新增高亮叠加层)
  ├── ControlPanel (现有可能调整位置)
  ├── IssuesPanel (现有)
  └── DetailPanel (现有)
```

## 数据流

### 加载流程

```
拓扑页面 mount
  ├─→ useHybridTopology()          ← 现有：拓扑图数据
  └─→ fetch(/api/v1/workflows?)    ← 新增：Workflow 列表
          ↓
      setWorkflows(data.workflows)
```

### 路径高亮流程

```
用户点击 Workflow 条目
  ↓
fetch(/api/v1/workflows/{id})
  ↓
getWorkflowDetail.steps[]
  ↓
workflowTopologyMapper(steps, visibleNodes, visibleEdges)
  ↓
返回 { nodeIds, edgeIds, stepSequence, tempEdges }
  ↓
setWorkflowHighlight(→ 画布渲染路径高亮)
```

### 数据模型

新增 `frontend/src/utils/workflowTopologyMapper.ts`：

```typescript
interface StepInfo {
  index: number;           // 步骤序号 1-based
  serviceName: string;     // 对应拓扑节点 service_name
  nodeId: string;          // 匹配到的拓扑节点 ID（可能为 null）
  action: string;          // 步骤动作
  startedAt: string;       // 开始时间
  durationMs: number;      // 耗时
  status: string;          // success / failed / warning
  level: string;           // 日志级别
}

interface TempEdge {
  source: string;          // 源节点 ID
  target: string;          // 目标节点 ID
  stepIndex: number;       // 对应步骤索引
}

interface WorkflowHighlightResult {
  nodeIds: string[];
  edgeIds: string[];   
  stepSequence: StepInfo[];
  tempEdges: TempEdge[];
}
```

## 面板设计细节

### 新增浮动面板「Workflow 执行」

**位置：** 画布左下角，320px 宽，最大高度 50vh，可滚动

**工具栏：**
- 刷新按钮（🔄）：重新获取 Workflow 列表
- 时间窗口选择：1小时 / 6小时 / 24小时
- 操作类型筛选：下拉选择（CreateVM / DeleteVM / LiveMigrate / …）

**列表条目：** 每个条目展示：
- 左侧：操作类型图标（复用现有 `OPERATION_ICONS` 映射）
- 中间：操作名称中文 + 状态标签（成功/失败）+ 摘要行（耗时 / 步数 / 资源ID）
- 右侧：选中态/路径预览指示
- 点击 = 切换选中，高亮路径
- Ctrl+点击 = 叠加选中

**空状态：** 虚线边框 + "当前时间窗口无 Workflow 执行记录"
**加载状态：** Skeleton loading（与列表条目等高等宽）
**错误状态：** 红色提示行 + 重试按钮

### 执行时间线浮层

**位置：** 画布右下角，选中 Workflow 后展开

**布局：** 每步骤一行，左侧序号圆点，中间服务名，右侧耗时进度条 + 耗时文字 + 状态图标

**交互：**
- 悬停步骤行 → 对应拓扑节点脉冲高亮
- 点击步骤行 → 拓扑图居中到该节点
- 总耗时/步骤数/错误数概要行
- 按钮：「固定路径」/「切换布局为 swimlane」

### 面板冲突处理

四个浮动面板的默认位置（left/top）不重叠：

| 面板 | 默认位置 |
|------|---------|
| 拓扑态势 (control) | right=20, top=100 |
| 链路情报 (issues) | right=20, top=320 |
| 详情 (detail) | right=20, top=540 |
| Workflow 执行 | left=20, top=100 |

所有面板可拖动重排，位置持久化到 localStorage。

## 路径高亮视觉规范

### 节点

| 状态 | 视觉 |
|------|------|
| 路径节点 | 边框加亮（2px cyan glow），右上角显示 **ⓝ** 序号标记 |
| 失败步骤节点 | 边框红色 glow，序号标记红色 |
| 警告步骤节点 | 边框琥珀色 glow，序号标记琥珀色 |
| 非路径节点 | 降低透明度至 0.15 |
| 匹配失败的步骤 | 节点不可交互，在时间线中灰色标记 |

### 边

| 类型 | 视觉 |
|------|------|
| 路径边（拓扑已有） | 加粗 2x + 流动小点动画 + 显示步骤耗时 label |
| 路径边（临时生成） | 红色虚线 + "Workflow 路径" label |
| 非路径边 | 降低透明度至 0.08 |

### 失败/警告路径标记

- 如果某个步骤状态为 failed，其对应节点用红色呼吸边框
- 边上的 label 改为显示错误信息摘要（截取前 40 字符）
- 时间线中对应行红底高亮

## 状态管理

### 新增状态变量

```typescript
// Workflow 面板数据
const [workflows, setWorkflows] = useState<WorkflowSummary[]>([]);
const [workflowsLoading, setWorkflowsLoading] = useState(false);
const [workflowsError, setWorkflowsError] = useState<string | null>(null);
const [workflowTimeWindow, setWorkflowTimeWindow] = useState<1 | 6 | 24>(1);
const [workflowFilter, setWorkflowFilter] = useState('');

// 选中 Workflow
const [selectedWorkflowId, setSelectedWorkflowId] = useState<string | null>(null);
const [selectedWorkflowDetail, setSelectedWorkflowDetail] = useState<WorkflowDetail | null>(null);
const [selectedWorkflowLoading, setSelectedWorkflowLoading] = useState(false);

// 路径高亮
const [workflowHighlight, setWorkflowHighlight] = useState<WorkflowHighlightResult | null>(null);
const [workflowHighlightFixed, setWorkflowHighlightFixed] = useState(false);
const [selectedWorkflowIds, setSelectedWorkflowIds] = useState<Set<string>>(new Set());
```

### 与现有状态的交互

| 拓扑操作 | 对 Workflow 高亮的影响 |
|----------|------------------------|
| 切换时间窗口 | 自动刷新 Workflow 列表，清空高亮 |
| 切换布局模式 | 重新计算路径映射，保留高亮 |
| 缩放/拖动画布 | 无影响，高亮保持 |
| 选中节点/边 | 保留 Workflow 高亮，详情面板共存 |
| 切换证据模式/降噪 | Workflow 路径涉及的节点/边强制可见 |
| 搜索/聚焦服务 | Workflow 高亮叠加在 focus 模式中 |
| 取消选中 Workflow | 恢复全图正常显示 |

### 状态覆盖矩阵

| Workflow 数据状态 | 面板显示 | 拓扑画布 |
|---|---|---|
| 加载中 | Skeleton | 不变 |
| 空列表 | 虚线边框空状态 | 不变 |
| 加载失败 | 红色错误提示+重试 | 不变 |
| 有数据·未选中 | 条目列表 | 不变 |
| 选中·详情加载中 | 条目 loading | 路径高亮保持 |
| 选中·详情到 | 列表+时间线 | 路径高亮生效 |
| 详情加载失败 | 面板报错 | 清除路径高亮 |

## 映射算法详情

文件：`frontend/src/utils/workflowTopologyMapper.ts`

```typescript
function mapWorkflowToTopology(
  detail: WorkflowDetail,
  nodes: TopologyNodeEntity[],
  edges: TopologyEdgeEntity[]
): WorkflowHighlightResult {
  // Step 1: 解析步骤数组
  const rawSteps = detail['steps.service_name'].map((svc, i) => ({
    serviceName: svc,
    action: detail['steps.action']?.[i] ?? '',
    startedAt: detail['steps.started_at']?.[i] ?? '',
    durationMs: detail['steps.duration_ms']?.[i] ?? 0,
    status: detail['steps.status']?.[i] ?? 'success',
    level: detail['steps.level']?.[i] ?? 'INFO',
  }));

  // Step 2: 为每个步骤匹配拓扑节点
  const nodeIndex = new Map<string, TopologyNodeEntity>();
  for (const node of nodes) {
    const key = node.service_name?.toLowerCase() ?? '';
    nodeIndex.set(key, node);
  }

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

  // Step 3: 构建路径边
  const highlightNodeIds = new Set<string>();
  const highlightEdgeIds = new Set<string>();
  const tempEdges: TempEdge[] = [];

  for (let i = 0; i < stepSequence.length - 1; i++) {
    const current = stepSequence[i];
    const next = stepSequence[i + 1];
    if (current.nodeId) highlightNodeIds.add(current.nodeId);
    if (next.nodeId) highlightNodeIds.add(next.nodeId);
    if (!current.nodeId || !next.nodeId) continue;

    // 在拓扑边中查找
    const matchedEdge = edges.find(edge =>
      edge.source === current.nodeId && edge.target === next.nodeId
    );
    if (matchedEdge) {
      highlightEdgeIds.add(matchedEdge.id ?? matchedEdge.edge_key ?? '');
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

### 节点匹配策略

```typescript
function findBestMatch(serviceName: string, nodes: TopologyNodeEntity[]): TopologyNodeEntity | null {
  const name = serviceName.toLowerCase();
  // 精确匹配
  let match = nodes.find(n => n.service_name?.toLowerCase() === name);
  if (match) return match;
  // 前缀匹配（拓扑名可能含版本号：nova-api → nova-api-2.1）
  match = nodes.find(n => n.service_name?.toLowerCase().startsWith(name));
  if (match) return match;
  // 包含匹配
  match = nodes.find(n => n.service_name?.toLowerCase().includes(name));
  return match ?? null;
}
```

## 文件变更清单

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `frontend/src/pages/TopologyPage.tsx` | 修改 | 新增 Workflow 状态变量、浮动面板、时间线浮层、路径渲染、加载逻辑 |
| `frontend/src/utils/workflowTopologyMapper.ts` | **新增** | Workflow → 拓扑映射纯函数 |
| `frontend/src/utils/api.ts` | 可选修改 | 可接入 useApi hook 模式 |
| `frontend/src/hooks/useApi.ts` | 可选新增 | 可考虑 useWorkflowList hook |
| (无后端改动) | — | 直接复用现有 `/api/v1/workflows` API |

## 实现顺序建议

1. **Phase 1 — 基础面板 + 数据加载**
   - 在 TopologyPage 中添加 Workflow 浮动面板（基础布局、可拖动）
   - 实现 `fetchWorkflows` 加载逻辑（复用列表 API）
   - 空/加载/错误状态

2. **Phase 2 — 路径映射 + 画布高亮**
   - 创建 `workflowTopologyMapper.ts`
   - 实现节点匹配和边映射
   - SVG 渲染层：节点序号标记、边高亮、降透明度的其他元素
   - 临时边（红色虚线）

3. **Phase 3 — 时间线浮层 + 交互**
   - 点击列表条目展开时间线浮层
   - 步骤行悬停→节点脉冲高亮
   - 点击→居中
   - 固定/取消固定

4. **Phase 4 — 状态集成 + 边界处理**
   - 与现有拓扑状态交互（聚焦、过滤、布局切换时的保持/清理）
   - Ctrl+点击叠加多条 Workflow
   - 步骤匹配失败的灰色展示

## 不做的事项

1. **不修改后端 API** — 现有 `/api/v1/workflows` 完全满足需求
2. **不移除独立 `/workflows` 页面** — 深度查看保留原页面
3. **不修改现有三个面板的默认位置** — 仅新增第四个面板
4. **不引入新依赖** — 复用现有 lucide-react 图标体系
5. **不修改拓扑核心渲染逻辑** — 仅叠加高亮层
