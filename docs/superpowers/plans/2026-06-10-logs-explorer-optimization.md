# Logs Explorer 筛选与加载优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 优化 Logs Explorer 筛选交互，消除点击筛选时的冗余 API 请求和加载闪烁

**Architecture:** 前端侧纯优化，不改后端。核心思路：(1) 筛选变更加防抖，合并快速连点 (2) 保留旧数据直到新数据到达 (3) Facet 统计与筛选解耦

**Tech Stack:** React (hooks), TypeScript

**设计文档:** `docs/superpowers/specs/2026-06-10-logs-explorer-optimization-design.md`

---

## 文件结构

| 文件 | 操作 | 职责 |
|------|------|------|
| `frontend/src/hooks/useDebounce.ts` | 新建 | 通用防抖 hook，用于延迟筛选参数变化 |
| `frontend/src/pages/LogsExplorer.tsx` | 修改 | 核心改动：防抖、数据保留、facet 解耦 |

不需要改动 `useApi.ts`，所有逻辑在 LogsExplorer 层面处理。

---

### Task 1: 新建 useDebounce hook

**Files:**
- Create: `frontend/src/hooks/useDebounce.ts`

- [ ] **Step 1: 创建文件**

```typescript
/**
 * 通用防抖 hook
 * 在 delay 毫秒内如果 value 发生变化，则重新计时
 */
import { useState, useEffect } from 'react';

export function useDebounce<T>(value: T, delay: number): T {
  const [debouncedValue, setDebouncedValue] = useState<T>(value);

  useEffect(() => {
    const timer = setTimeout(() => {
      setDebouncedValue(value);
    }, delay);

    return () => clearTimeout(timer);
  }, [value, delay]);

  return debouncedValue;
}
```

- [ ] **Step 2: 验证语法**

Run: `cd /root/logoscope/frontend && npx tsc --noEmit src/hooks/useDebounce.ts`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
git add frontend/src/hooks/useDebounce.ts
git commit -m "feat: add useDebounce hook for filter optimization"
```

---

### Task 2: 给 filter 参数加 debounce

**Files:**
- Modify: `frontend/src/pages/LogsExplorer.tsx` (添加 import 和 debounce 逻辑)

当前问题：`apiParams` / `aggregatedParams` / `facetParams` 三个 `useMemo` 的依赖数组包含了所有筛选状态（`selectedLevels`, `selectedServices`, `selectedNamespaces` 等），任一状态变化都会立即触发所有三个 API 请求。

- [ ] **Step 1: 将三个 params useMemo 改为基于 debounced 版本**

在 LogsExplorer 组件中，现有代码 L15 的 import 下新增：

```typescript
import { useDebounce } from '../hooks/useDebounce';
```

在现有 filter state 声明之后（约 L758-766），新增 debounce 参数：

```typescript
// 筛选参数防抖：300ms 内合并多次变化
const debouncedSelectedLevels = useDebounce(selectedLevels, 300);
const debouncedSelectedServices = useDebounce(selectedServices, 300);
const debouncedSelectedNamespaces = useDebounce(selectedNamespaces, 300);
const debouncedSelectedContainers = useDebounce(selectedContainers, 300);
const debouncedTraceIdFilter = useDebounce(traceIdFilter, 300);
const debouncedRequestIdFilter = useDebounce(requestIdFilter, 300);
const debouncedPodNameFilter = useDebounce(podNameFilter, 300);
const debouncedStartTime = useDebounce(startTime, 300);
const debouncedEndTime = useDebounce(endTime, 300);
const debouncedExcludeHealthCheck = useDebounce(excludeHealthCheck, 300);
```

然后将 `apiParams` / `aggregatedParams` / `facetParams` 的依赖数组中的状态变量替换为对应的 `debounced` 版本：

**apiParams useMemo 依赖变更（L970）：**

```typescript
// 之前:
}, [selectedLevels, selectedServices, selectedNamespaces, selectedContainers, traceIdFilter, 
    correlationTraceIds, requestIdFilter, correlationRequestIds, podNameFilter, debouncedSearchQuery, 
    startTime, endTime, excludeHealthCheck, anchorTime, topologyJumpContext, effectiveDefaultTimeWindow]);

// 之后:
}, [debouncedSelectedLevels, debouncedSelectedServices, debouncedSelectedNamespaces, 
    debouncedSelectedContainers, debouncedTraceIdFilter, correlationTraceIds, 
    debouncedRequestIdFilter, correlationRequestIds, debouncedPodNameFilter, debouncedSearchQuery, 
    debouncedStartTime, debouncedEndTime, debouncedExcludeHealthCheck, anchorTime, 
    topologyJumpContext, effectiveDefaultTimeWindow]);
```

**facetParams useMemo 依赖变更（L1071）：**

同样将 `selectedLevels` → `debouncedSelectedLevels`，`selectedServices` → `debouncedSelectedServices` 等。

**aggregatedParams useMemo 依赖变更（L1009-1027）：**

同样将筛选状态替换为 debounced 版本。

**注意**：`correlationTraceIds` / `correlationRequestIds` / `anchorTime` / `topologyJumpContext` / `effectiveDefaultTimeWindow` 这些参数从 URL 参数来，不是用户频繁点击的筛选按钮触发的，所以**不需要** debounce。

- [ ] **Step 2: 更新 excludeHealthCheck 的实时日志 filter 依赖**

`realtimeFilters`（L1106）中也需要更新，但 useMemo 的依赖数组改为用 debounced 版本：

```typescript
const realtimeFilters = useMemo(() => ({
    service_name: debouncedSelectedServices.length === 1 ? debouncedSelectedServices[0] : undefined,
    namespace: debouncedSelectedNamespaces.length === 1 ? debouncedSelectedNamespaces[0] : undefined,
    container_name: debouncedSelectedContainers.length === 1 ? debouncedSelectedContainers[0] : undefined,
    level: debouncedSelectedLevels.length === 1 ? debouncedSelectedLevels[0] : undefined,
    exclude_health_check: debouncedExcludeHealthCheck,
}), [debouncedSelectedServices, debouncedSelectedNamespaces, debouncedSelectedContainers, 
    debouncedSelectedLevels, debouncedExcludeHealthCheck]);
```

- [ ] **Step 3: 验证编译**

Run: `cd /root/logoscope/frontend && npm run typecheck`
Expected: No type errors (useDebounce 的泛型类型会正确推导)

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/LogsExplorer.tsx
git commit -m "perf: add debounce to filter params to reduce redundant API calls"
```

---

### Task 3: 保留旧数据，消除 loading 闪烁

**Files:**
- Modify: `frontend/src/pages/LogsExplorer.tsx`

当前问题：筛选变化后 `useEvents` 返回 `loading: true`，虽然 `createApiHook` **不**清空 data，但状态刷新过程中：
1. `loading` 变为 `true`
2. 某些代码路径（如 `shouldAutoExpand` / `isInitialSyncPending` 等）可能引起 UI 抖动
3. 新数据到达后 `pagedEvents` 被替换，列表内容跳变

- [ ] **Step 1: 修改 displayEvents，在 loading 时保留旧数据**

在当前 `const allEvents = useMemo(...)`（L1127-1142）之后，新增一个保留旧数据的 display layer：

```typescript
// 加载时保留旧数据：用 useRef 保存上一次成功加载的事件列表
const stableEventsRef = useRef<LogEvent[]>([]);

useEffect(() => {
  if (!loading && data && data.events && data.events.length > 0) {
    stableEventsRef.current = data.events;
  }
}, [loading, data]);

// 用于展示的事件列表：加载中时取 ref 中的旧数据，新数据到达后用新数据
const displayEvents = useMemo<LogEvent[]>(() => {
  if (!loading && data?.events) {
    return data.events;
  }
  // 加载中：使用 ref 中保存的旧数据
  return stableEventsRef.current;
}, [loading, data]);
```

- [ ] **Step 2: 将 `allEvents` 的 fallback 指向 `displayEvents`**

修改 `allEvents` useMemo 中的 `staticEvents`：

```typescript
const allEvents = useMemo(() => {
    // 改为使用 displayEvents 替代 pagedEvents，确保加载时保留旧数据
    const staticEvents = displayEvents.length > 0 ? displayEvents : pagedEvents;
    // ... 其余不变
}, [realtimeMode, realtimeLogs, displayEvents, pagedEvents]);
```

- [ ] **Step 3: 清理 data 初始加载的 useEffect**

现有 L1074-1083 的 useEffect 中：

```typescript
useEffect(() => {
    if (!data) return;
    setPagedEvents(data.events || []);
    setNextCursor(data.next_cursor || null);
    setAnchorTime(data.anchor_time || null);
    setLoadedPageCount((data.events || []).length > 0 ? 1 : 0);
    dataUpdatedAfterAutoExpandRef.current = true;
}, [data]);
```

这个 useEffect 在 `displayEvents` + `stableEventsRef` 的机制下，可以简化为不再需要更新 `pagedEvents`，因为 displayEvents 已经直接从 data 读取。但为了保持与 `loadMoreLogs` 的兼容（`loadMoreLogs` 手动调用 `setPagedEvents`），保留这个 useEffect 不变，只将展示层数据源切到 `displayEvents`。

- [ ] **Step 4: 验证编译**

Run: `cd /root/logoscope/frontend && npm run typecheck`
Expected: No errors

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/LogsExplorer.tsx
git commit -m "perf: retain old log data during loading to eliminate flash"
```

---

### Task 4: Facet 请求与筛选解耦

**Files:**
- Modify: `frontend/src/pages/LogsExplorer.tsx`

当前问题：`facetParams` 的依赖包含了所有筛选状态，这意味着用户点击级别/服务筛选时，面板统计也会重新请求。实际上 facet 统计只与时间窗口和搜索关键词相关。

- [ ] **Step 1: 分离 facete 依赖**

将 `facetParams` 的依赖从**所有筛选**改为只依赖**影响数据窗口的参数**：

```typescript
const facetParams = useMemo(() => {
    const params: LogsFacetQueryParams = {};
    // 只传递时间窗口相关的参数，不传递 level/service/namespace 筛选
    if (debouncedStartTime) params.start_time = debouncedStartTime;
    if (debouncedEndTime) params.end_time = debouncedEndTime;
    if (debouncedExcludeHealthCheck) params.exclude_health_check = true;
    if (debouncedSearchQuery) params.search = debouncedSearchQuery;
    // 时间窗口兜底
    if (!debouncedStartTime && !debouncedEndTime) {
      if (topologyJumpContext?.timeWindow) {
        params.time_window = topologyJumpContext.timeWindow;
      } else {
        params.time_window = effectiveDefaultTimeWindow;
      }
    }
    params.limit_services = 300;
    params.limit_namespaces = 300;
    params.limit_levels = 20;
    return params;
}, [
    debouncedStartTime,
    debouncedEndTime,
    debouncedExcludeHealthCheck,
    debouncedSearchQuery,
    topologyJumpContext,
    effectiveDefaultTimeWindow,
]);
```

**不传递** `level`, `levels`, `service_name`, `service_names`, `namespace`, `namespaces`, `container_name` 等筛选条件到 facet 接口。

这样 facet 统计反映的是**时间窗口内的全量数据分布**，而不是"当前筛选后的子集分布"。用户无论选了什么级别/服务，面板始终显示该时间窗口内所有级别/服务的真实计数。

- [ ] **Step 2: 兼容现有 facet 消费逻辑**

检查现有 `useEffect`（L1247-1336）中对 `facetsData.services/facetsData.levels/facetsData.namespaces` 的使用。该逻辑已经包含了 fallback 机制（当 facet data 为空时使用已加载事件的本地统计）：

```typescript
// L1273-1283: 当 facet 为空或统计不足时，用本地 fallback
if (facetServiceTotal > 0 || fallbackServiceTotal <= 0) {
    setAvailableServices(facetsData!.services.map((item) => item.value));
    setServiceCountMap(nextServiceCounts);
} else {
    setAvailableServices(Object.keys(fallbackCounts.services).sort());
    setServiceCountMap(fallbackCounts.services);
}
```

因为我们现在让 facet 返回时间窗口内的全量数据，`facetServiceTotal` 会更大更准确，fallback 路径触发更少。这是正优化。

- [ ] **Step 3: 验证编译**

Run: `cd /root/logoscope/frontend && npm run typecheck`
Expected: No errors

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/LogsExplorer.tsx
git commit -m "perf: decouple facet requests from filter changes, facet now only depends on time window"
```

---

### Task 5: 验证优化效果

- [ ] **Step 1: 运行 TypeScript 编译检查**

```bash
cd /root/logoscope/frontend && npm run typecheck
```
Expected: No type errors

- [ ] **Step 2: 运行 ESLint**

```bash
cd /root/logoscope/frontend && npm run lint
```
Expected: No warnings

- [ ] **Step 3: 确认改动不破坏现有逻辑**

检查的关键路径：
- URL 参数初始化（L844-911）：不受影响，因为 debounce 仅影响 filter 状态变化后的请求，不阻断初始设置
- `loadMoreLogs` 分页加载（L1639-1677）：不受影响，因为该函数直接调用 `api.getEvents`，不经过 `useEvents`
- WebSocket 实时日志（L1099-1116）：`realtimeFilters` 使用了 debounced 版本，实时订阅变更有 300ms 延迟，可以接受（快速切换筛选时反而避免频繁重连）
- Pattern 聚合视图（L973-1033）：`aggregatedParams` 同样加了 debounce，与 stream 视图行为一致

- [ ] **Step 4: 构建验证**

```bash
cd /root/logoscope/frontend && npm run build
```
Expected: Build succeeds

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/LogsExplorer.tsx
git commit -m "chore: finalize logs explorer optimization and verify build"
```

---

## 验证标准（Post-implementation）

| 验证项 | 预期 |
|--------|------|
| 点击级别筛选 | 列表**立即**按前端过滤展示，后端后台加载，无 loading 闪烁 |
| 快速连点 3 个级别 | 只有 1 次后端请求（防抖合并） |
| 点击时间"应用"按钮 | 触发后端请求（时间窗口变化） |
| 筛选面板计数 | 不随级别/服务筛选变化抖动 |
| facet API 调用频率 | 用户切级别时不再触发 facet 请求 |
| Pattern 视图 | 与 stream 视图一致的筛选行为 |
| 实时日志模式 | 切换筛选后约 300ms 订阅更新 |

## 未涵盖的后续优化（超出本计划范围）

- `createApiHook` 的通用 data/loading 分离改造（影响所有 hook，需单独评估）
- 前端缓存（react-query / SWR）引入（架构级变更）
- 虚拟列表性能优化（已由 VirtualLogList 处理）
