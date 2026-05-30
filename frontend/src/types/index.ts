/**
 * 类型定义文件 - 参考 Datadog 前端设计
 */

/**
 * 时间范围类型
 */
export type TimeRange = {
  start: string;
  end: string;
  window: string;
};

/**
 * 图表配置类型
 */
export type ChartConfig = {
  type: 'line' | 'bar' | 'pie' | 'gauge' | 'heatmap';
  title?: string;
  xAxis?: string;
  yAxis?: string;
  series?: string[];
};

/**
 * 筛选条件类型
 */
export type Filter = {
  key: string;
  value: string;
  operator: 'eq' | 'ne' | 'gt' | 'lt' | 'contains';
};

/**
 * 仪表盘卡片类型
 */
export type DashboardCard = {
  id: string;
  type: 'metric' | 'chart' | 'log' | 'trace' | 'topology';
  title: string;
  config: Record<string, unknown>;
  position: {
    x: number;
    y: number;
    width: number;
    height: number;
  };
};

/**
 * 仪表盘布局类型
 */
export type DashboardLayout = {
  id: string;
  name: string;
  cards: DashboardCard[];
};

/**
 * 服务状态类型
 */
export type ServiceStatus = 'healthy' | 'degraded' | 'critical' | 'unknown';

/**
 * 服务信息类型
 */
export type ServiceInfo = {
  name: string;
  status: ServiceStatus;
  metrics: {
    cpu: number;
    memory: number;
    requests: number;
    errors: number;
  };
};

/**
 * 日志查询参数类型
 */
export type LogQueryParams = {
  query: string;
  timeRange: TimeRange;
  filters: Filter[];
  sortBy: string;
  sortOrder: 'asc' | 'desc';
  limit: number;
};

/**
 * 指标查询参数类型
 */
export type MetricQueryParams = {
  metric: string;
  timeRange: TimeRange;
  groupBy: string[];
  aggregator: 'avg' | 'sum' | 'max' | 'min' | 'count';
};

/**
 * 追踪查询参数类型
 */
export type TraceQueryParams = {
  service: string;
  operation: string;
  timeRange: TimeRange;
  filters: Filter[];
  limit: number;
};

/**
 * 拓扑查询参数类型
 */
export type TopologyQueryParams = {
  timeRange: TimeRange;
  namespace: string;
  confidence: number;
};

/**
 * 告警查询参数类型
 */
export type AlertQueryParams = {
  status: 'firing' | 'resolved' | 'all';
  severity: 'critical' | 'warning' | 'info' | 'all';
  timeRange: TimeRange;
};

/**
 * 拓扑节点类型
 */
export type TopologyNode = {
  id: string;
  label: string;
  name: string;
  type: string;
  metrics: {
    log_count?: number;
    trace_count?: number;
    span_count?: number;
    error_count?: number;
    pod_count?: number;
    avg_duration?: number;
    error_rate?: number;
    last_seen?: string;
    confidence?: number;
    data_sources?: string[];
    data_source?: string;
    [key: string]: unknown;
  };
  [key: string]: unknown;
};

/**
 * 拓扑边类型
 */
export type TopologyEdge = {
  id: string;
  source: string;
  target: string;
  label: string;
  type: string;
  metrics: {
    call_count?: number | null;
    confidence?: number;
    data_source?: string;
    reason?: string;
    confidence_boost?: number;
    avg_latency?: number;
    error_rate?: number;
    [key: string]: unknown;
  };
};
