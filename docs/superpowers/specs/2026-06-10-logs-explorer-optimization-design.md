# Logs Explorer 筛选与数据加载优化设计

## 背景

当前 Logs Explorer 页面默认加载 200 条日志，每次点击筛选条件（级别、服务、时间等）都会触发完整的后端 API 请求，导致：
1. **冗余请求**：一次筛选变化同时触发 `useEvents` / `useAggregatedLogs` / `useLogFacets` 三个请求
2. **加载闪烁**：`createApiHook` 在重请求时将 `loading` 置为 `true`，旧数据被覆盖，UI 出现空白闪烁
3. **数据失真**：200 条固定数量下，前端过滤可能忽略大量匹配日志（如实际有 5000 条 ERROR 但只加载了 200 条）

## 设计目标

1. 以**时间窗口**为默认数据边界，替代固定 200 条的限制
2. 筛选交互改为**后端查询但保留旧数据**，消除 loading 闪烁
3. 筛选变化加**防抖合并**，避免快速连点触发多个请求
4. Facet 数据（筛选面板统计）与主数据分离，减少不必要的 facet 请求

## 业界参考

| 流派 | 产品 | 核心设计 |
|------|------|---------|
| 流式加载型 | Datadog | 时间窗口 + 滚动无限加载，筛选触后端但保留旧数据 |
| 查询触发型 | Grafana Loki | 用户手动执行 LogQL 查询，筛选不自动触发 |
| 搜索驱动型 | Splunk | 搜索语法驱动，字段筛选追加到查询后重新执行 |

本方案采用 **Datadog 风格**的流式加载模式，兼顾实时性和数据完整性。

## 方案设计

### 1. 数据加载模型变更

```
当前:
  [静态] 加载 200 条 → 用户筛选 → 重新加载 200 条（覆盖旧数据）

优化后:
  [时间窗口] 初始加载 200 条 → 保留旧数据 → 筛选触发后台查询 → 新数据替换
```

#### 具体变化

| 维度 | 当前 | 优化后 |
|------|------|--------|
| 默认数据边界 | 固定 200 条 | 时间窗口（默认 30 分钟） |
| 初始加载 | 200 条 | 200 条（首次渲染速度不变） |
| 筛选触发 | 立即后端请求 + 清空旧数据 | 立即后端请求 + **保留旧数据显示** |
| 加载更多 | 手动/滚动触发 cursor 分页 | 同左（保持不变） |
| 新数据到达 | 替换旧数据 | 替换旧数据（transition 效果） |

### 2. 筛选交互优化

#### 级别/服务/Namespace 筛选

- 筛选变化时 `apiParams` 仍然携带 level/service 传到后端（保证数据完整性）
- **但 `useEvents` hook 不因 loading 而清空 data**
- 在 LogsExplorer 层面包装一层：`{ events: data?.events || pagedEvents, loading }`
- 用户侧看到的效果：数据立即按前端过滤结果变化，同时后台加载完整数据

#### 时间筛选

- 保持当前设计：弹出面板选择，确认后才触发 `setStartTime/setEndTime`
- 快捷按钮（最近 5m/15m/30m/1h 等）点击即触发，不需要额外确认

#### 防抖合并

- 对 `apiParams` 的变化加 300ms 防抖
- 用户快速连点多个级别（TRACE → DEBUG → INFO）时，只触发最后一次请求
- 搜索框保持现有 400ms 防抖不变

### 3. createApiHook 增强

```typescript
// 增强方案：不覆盖旧 data
createApiHook = (apiFunc, initialParams) => (params) => {
  const [state, setState] = useState({ data: null, loading: true, error: null });
  const [loading, setLoading] = useState(false);  // 拆分 loading 状态
  
  // data 只在首次加载或成功返回新数据时更新
  // loading 不影响已有 data 的展示
};
```

或在 LogsExplorer 层面直接处理：

```typescript
const displayEvents = useMemo(() => {
  // 有新数据时用新数据，否则保留旧数据
  return data?.events || pagedEvents;
}, [data, pagedEvents]);
```

### 4. Facet 请求优化

当前筛选面板的统计数据（服务计数、级别分布）跟随每次筛选变化重新请求，但实际上：

- **级别/服务/namespace 的计数**在同一个时间窗口内相对稳定
- 筛选面板的计数主要用于展示各选项的数据量，不必实时精确

优化方案：
- `useLogFacets` 仅依赖 `time_window` 和搜索关键词变化
- 级别/服务筛选变化时不触发 facet 重新请求
- 使用已加载数据的 `buildFallbackFacetCounts` 做本地计数（该逻辑已存在）
- 用户切时间窗口时刷新 facet

### 5. 数据流全景

```
用户点击"ERROR"级别
  │
  ├─▶ setSelectedLevels(["ERROR"])
  │     │
  │     ├─▶ applyClientFilters(allEvents) → 立即展示 ERROR 日志 ✅
  │     │
  │     └─▶ 300ms debounce 后
  │           │
  │           ├─▶ apiParams.level = "ERROR"
  │           ├─▶ useEvents 发起后端请求
  │           │     └─▶ 保留旧 pagedEvents（继续显示）
  │           ├─▶ useAggregatedLogs 发起请求（保留旧数据）
  │           └─▶ useLogFacets → 不触发（时间窗口没变）
  │
  └─▶ 后端返回
        ├─▶ data 更新 → displayEvents 切换为新数据
        └─▶ 用户看到完整 ERROR 数据
```

## 变更范围

### 需要修改的文件

| 文件 | 改动内容 |
|------|---------|
| `frontend/src/pages/LogsExplorer.tsx` | 核心改动：displayEvents 逻辑、防抖、apiParams 优化 |
| `frontend/src/hooks/useApi.ts` | `createApiHook` 的 data/loading 分离增强（可选） |
| 无需修改后端 | 所有优化在前端侧 |

### 不需改动的部分

- 后端 API 接口：完全不变
- 分页/光标机制：保留
- 实时 WebSocket：保留
- 排序：不变

## 风险与应对

| 风险 | 应对 |
|------|------|
| 防抖导致用户感知延迟 | 300ms 以内，几乎无感知；且前端 `applyClientFilters` 立即生效 |
| 保留旧数据显示新筛选结果不一致 | 旧数据 + 前端过滤已经能立即反映筛选状态，后台数据到达后自动替换 |
| 内存占用 | pagedEvents 保留旧数据可能增加内存，但单次查询最多 200 条 x 若干页 = 几千条，可控 |
| Pattern 视图兼容 | Pattern 聚合视图的筛选交互同步优化，保持一致的使用体验 |

## 验证标准

1. 点击级别筛选 → 前端列表**立即**显示过滤结果，**无 loading 闪烁**
2. 快速连点多个级别 → **只有一次**后端请求
3. 时间筛选点"应用"才触发请求
4. 筛选面板的服务/级别计数与时间窗口绑定，不随筛选变化抖动
5. 切换 Pattern 视图后，筛选行为与 stream 视图一致
