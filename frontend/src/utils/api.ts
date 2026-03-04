/**
 * API 客户端 - 参考 Datadog 前端设计
 * 统一的 API 调用、错误处理、请求拦截
 */
import axios, { AxiosInstance, type AxiosRequestConfig, type AxiosResponse } from 'axios';
import { parseLogMessage } from './logMessage';

// API 基础配置
// 开发环境：使用 Vite 代理（相对路径）
// 生产环境：使用相对路径，由 nginx 代理
const API_BASE_URL = import.meta.env.VITE_API_URL || import.meta.env.VITE_API_BASE_URL || '';
const API_PREFIX = '/api/v1';
const API_TIMEOUT_MS = Number(import.meta.env.VITE_API_TIMEOUT_MS || 60000);
const LOGS_API_TIMEOUT_MS = Number(import.meta.env.VITE_LOGS_API_TIMEOUT_MS || 90000);

/**
 * 安全地将数据转换为数组
 * 防止 map is not a function 错误
 */
function ensureArray(data: any): any[] {
  if (Array.isArray(data)) {
    return data;
  }
  if (data && typeof data === 'object' && Array.isArray(data.data)) {
    return data.data;
  }
  if (data && typeof data === 'object' && Array.isArray(data.metrics)) {
    return data.metrics;
  }
  if (data && typeof data === 'object' && Array.isArray(data.traces)) {
    return data.traces;
  }
  if (data && typeof data === 'object' && Array.isArray(data.events)) {
    return data.events;
  }
  return [];
}

// 数据模型类型定义

/**
 * 事件数据模型
 */
export interface Event {
  id: string;
  timestamp: string;
  service_name: string;
  pod_name: string;
  namespace: string;
  level: 'TRACE' | 'DEBUG' | 'INFO' | 'WARN' | 'ERROR' | 'FATAL';
  message: string;
  attributes: Record<string, any>;
  trace_id?: string;
  span_id?: string;
  node_name?: string;
  container_name?: string;
  labels?: Record<string, string>;
}

/**
 * 指标数据模型
 */
export interface Metric {
  service_name: string;
  metric_name: string;
  value: number;
  timestamp: string;
  labels: Record<string, string>;
}

/**
 * 追踪数据模型
 */
export interface Trace {
  trace_id: string;
  service_name: string;
  operation_name: string;
  start_time: string;
  duration_ms: number;
  status_code: 'STATUS_CODE_OK' | 'STATUS_CODE_ERROR' | 'STATUS_CODE_UNSET';
}

/**
 * Trace-Lite 推断片段
 */
export interface TraceLiteFragment {
  fragment_id: string;
  source_service: string;
  target_service: string;
  inference_method: 'request_id' | 'time_window';
  confidence: number;
  confidence_explain: string;
  sample_size: number;
  request_ids: string[];
  trace_ids: string[];
  evidence_chain: Array<Record<string, any>>;
  first_seen?: string | null;
  last_seen?: string | null;
}

/**
 * 拓扑节点
 */
export interface TopologyNode {
  id: string;
  label: string;
  type: 'service' | 'database' | 'cache' | 'external';
  node_key?: string;
  service?: {
    namespace: string;
    name: string;
    env: string;
  };
  evidence_type?: 'observed' | 'inferred';
  coverage?: number;
  quality_score?: number;
  metrics: {
    log_count?: number;
    trace_count?: number;
    error_count?: number;
    avg_duration?: number;
    confidence?: number;
    evidence_type?: 'observed' | 'inferred';
    coverage?: number;
    quality_score?: number;
    service_name?: string;
    service_namespace?: string;
    env?: string;
  };
}

/**
 * 拓扑边
 */
export interface TopologyEdge {
  id: string;
  source: string;
  target: string;
  label: string;
  type: 'sync' | 'async' | 'calls';
  edge_key?: string;
  protocol?: string;
  endpoint_pattern?: string;
  evidence_type?: 'observed' | 'inferred';
  coverage?: number;
  quality_score?: number;
  p95?: number;
  p99?: number;
  timeout_rate?: number;
  metrics: {
    call_count?: number | null;
    confidence?: number;
    data_source?: string;
    data_sources?: string[];
    reason?: string;
    confidence_boost?: number;
    avg_latency?: number;
    error_rate?: number;
    evidence_type?: 'observed' | 'inferred';
    coverage?: number;
    quality_score?: number;
    p95?: number;
    p99?: number;
    timeout_rate?: number;
    retries?: number;
    pending?: number;
    dlq?: number;
    protocol?: string;
    endpoint_pattern?: string;
  };
}

/**
 * 拓扑图数据
 */
export interface TopologyGraph {
  nodes: TopologyNode[];
  edges: TopologyEdge[];
  metadata: {
    data_sources: string[];
    time_window: string;
    node_count: number;
    edge_count: number;
    avg_confidence: number;
    inference_mode?: 'rule' | 'hybrid_score' | string;
    inference_quality?: Record<string, any>;
    source_breakdown: Record<string, any>;
    generated_at: string;
    contract_version?: string;
    quality_version?: string;
  };
}

/**
 * 告警规则
 */
export interface AlertRule {
  id: string;
  name: string;
  description: string;
  metric_name: string;
  service_name: string | null;
  condition: 'gt' | 'lt' | 'eq' | 'gte' | 'lte';
  threshold: number;
  duration: number;
  severity: 'critical' | 'warning' | 'info';
  enabled: boolean;
  labels: Record<string, string>;
  min_occurrence_count?: number;
  notification_enabled?: boolean;
  notification_channels?: string[];
  notification_cooldown_seconds?: number;
  created_at: string;
  updated_at: string;
}

/**
 * 告警事件
 */
export interface AlertEvent {
  id: string;
  rule_id: string;
  rule_name: string;
  metric_name: string;
  service_name: string;
  current_value: number;
  threshold: number;
  condition: string;
  severity: 'critical' | 'warning' | 'info';
  message: string;
  fired_at: string;
  status: 'pending' | 'firing' | 'acknowledged' | 'silenced' | 'resolved';
  resolved_at?: string;
  first_triggered_at?: string;
  last_triggered_at?: string;
  acknowledged_at?: string;
  silenced_until?: string;
  occurrence_count?: number;
  last_notified_at?: string;
  notification_count?: number;
  updated_at?: string;
}

export interface AlertEventsResponse {
  total: number;
  events: AlertEvent[];
  limit: number;
  cursor: string | null;
  has_more: boolean;
  next_cursor: string | null;
}

export interface AlertRuleTemplate {
  id: string;
  name: string;
  description: string;
  metric_name: string;
  condition: 'gt' | 'lt' | 'eq' | 'gte' | 'lte';
  threshold: number;
  duration: number;
  severity: 'critical' | 'warning' | 'info';
  labels: Record<string, string>;
}

export interface AlertNotification {
  id: string;
  event_id: string;
  rule_id: string;
  rule_name: string;
  service_name: string;
  severity: 'critical' | 'warning' | 'info';
  event_status: 'pending' | 'firing' | 'acknowledged' | 'silenced' | 'resolved' | string;
  channel: 'inapp' | 'webhook' | string;
  delivery_status: 'ok' | 'failed' | 'skipped' | 'unknown' | string;
  detail: string;
  created_at: string;
}

/**
 * M4 价值 KPI 指标
 */
export interface ValueKpiMetrics {
  mttd_minutes: number;
  mttr_minutes: number;
  trace_log_correlation_rate: number;
  topology_coverage_rate: number;
  release_regression_pass_rate: number;
}

export interface ValueKpiResponse {
  status: string;
  time_window: string;
  metrics: ValueKpiMetrics;
  incident_summary: {
    incident_count: number;
  };
  release_gate_summary: {
    total: number;
    passed: number;
    failed: number;
    bypassed: number;
    pass_rate: number;
    last_result?: Record<string, any> | null;
  };
  generated_at: string;
}

/**
 * 日志 Pattern 聚合结果
 */
export interface LogPattern {
  pattern: string;
  pattern_hash: string;
  count: number;
  level: string;
  first_seen: string;
  last_seen: string;
  samples: Event[];
  variables: string[];
  variable_examples: Record<string, string[]>;
  service_names: string[];
}

/**
 * 日志聚合查询结果
 */
export interface AggregatedLogsResult {
  patterns: LogPattern[];
  total_logs: number;
  total_patterns: number;
  aggregated_count: number;
  aggregation_ratio: number;
}

/**
 * 日志查询参数
 */
export interface LogsQueryParams {
  limit?: number;
  service_name?: string;
  service_names?: string[] | string;
  trace_id?: string;
  pod_name?: string;
  level?: string;
  levels?: string[] | string;
  start_time?: string;
  end_time?: string;
  exclude_health_check?: boolean;
  search?: string;
  source_service?: string;
  target_service?: string;
  time_window?: string;
  cursor?: string;
  anchor_time?: string;
}

export interface LogsQueryResult {
  events: Event[];
  total: number;
  has_more: boolean;
  next_cursor: string | null;
  anchor_time: string | null;
}

export interface LogsFacetBucket {
  value: string;
  count: number;
}

export interface LogsFacetResult {
  services: LogsFacetBucket[];
  levels: LogsFacetBucket[];
  context?: Record<string, any>;
  generated_at?: string;
}

export interface LogsStatsResult {
  total: number;
  byService: Record<string, number>;
  byLevel: Record<string, number>;
}

export interface LogsFacetQueryParams extends LogsQueryParams {
  limit_services?: number;
  limit_levels?: number;
}

/**
 * 日志聚合查询参数
 */
export interface AggregatedLogsParams extends LogsQueryParams {
  min_pattern_count?: number;
  max_patterns?: number;
  max_samples?: number;
}

/**
 * API 客户端类
 */
export class APIClient {
  private client: AxiosInstance;
  private inflightGetRequests = new Map<string, Promise<AxiosResponse<any>>>();

  constructor() {
    this.client = axios.create({
      baseURL: API_BASE_URL,
      timeout: API_TIMEOUT_MS,
      headers: {
        'Content-Type': 'application/json',
      },
    });

    // 请求拦截器
    this.client.interceptors.request.use(
      (config) => {
        // 可以在这里添加认证信息
        // config.headers.Authorization = `Bearer ${token}`;
        return config;
      },
      (error) => {
        return Promise.reject(error);
      }
    );

    // 响应拦截器
    this.client.interceptors.response.use(
      (response) => response,
      (error) => {
        // 统一错误处理
        console.error('API Error:', error);
        return Promise.reject(error);
      }
    );
  }

  private stableSerialize(value: unknown): string {
    const seen = new WeakSet<object>();

    const walk = (input: unknown): string => {
      if (input === null) {
        return 'null';
      }
      if (input === undefined) {
        return 'undefined';
      }

      const inputType = typeof input;
      if (inputType === 'string') {
        return JSON.stringify(input);
      }
      if (inputType === 'number' || inputType === 'boolean') {
        return String(input);
      }
      if (inputType === 'bigint') {
        return JSON.stringify(String(input));
      }
      if (inputType !== 'object') {
        return JSON.stringify(String(input));
      }

      if (input instanceof Date) {
        return JSON.stringify(input.toISOString());
      }

      if (Array.isArray(input)) {
        return `[${input.map((item) => walk(item)).join(',')}]`;
      }

      if (seen.has(input as object)) {
        return '"[Circular]"';
      }
      seen.add(input as object);

      const entries = Object.entries(input as Record<string, unknown>)
        .filter(([, itemValue]) => itemValue !== undefined)
        .sort(([keyA], [keyB]) => keyA.localeCompare(keyB))
        .map(([itemKey, itemValue]) => `${JSON.stringify(itemKey)}:${walk(itemValue)}`);
      return `{${entries.join(',')}}`;
    };

    return walk(value);
  }

  private buildInflightGetKey(url: string, config?: AxiosRequestConfig): string {
    const paramsKey = this.stableSerialize(config?.params ?? null);
    const timeoutKey = Number(config?.timeout ?? API_TIMEOUT_MS);
    const responseTypeKey = String(config?.responseType || 'json');
    return `${url}|${paramsKey}|${timeoutKey}|${responseTypeKey}`;
  }

  private async getWithInflightDedupe<T = any>(
    url: string,
    config?: AxiosRequestConfig
  ): Promise<AxiosResponse<T>> {
    const key = this.buildInflightGetKey(url, config);
    const existing = this.inflightGetRequests.get(key);
    if (existing) {
      return existing as Promise<AxiosResponse<T>>;
    }

    const request = this.client
      .get<T>(url, config)
      .finally(() => {
        this.inflightGetRequests.delete(key);
      });
    this.inflightGetRequests.set(key, request as Promise<AxiosResponse<any>>);
    return request;
  }

  /**
   * 健康检查
   */
  async health(): Promise<{ status: string; service: string; version: string }> {
    const response = await this.client.get('/health');
    return response.data;
  }

  /**
   * 获取事件列表
   */
  async getEvents(params?: LogsQueryParams): Promise<LogsQueryResult> {
    const response = await this.getWithInflightDedupe(`${API_PREFIX}/logs`, {
      params,
      timeout: LOGS_API_TIMEOUT_MS,
    });
    const data = ensureArray(response.data);
    const events = data.map((item: any) => this.transformEvent(item));
    return {
      events,
      total: response.data?.total ?? response.data?.count ?? events.length,
      has_more: Boolean(response.data?.has_more),
      next_cursor: response.data?.next_cursor || null,
      anchor_time: response.data?.anchor_time || null,
    };
  }

  /**
   * 获取日志筛选 Facet（服务、级别）
   */
  async getLogFacets(params?: LogsFacetQueryParams): Promise<LogsFacetResult> {
    const response = await this.getWithInflightDedupe(`${API_PREFIX}/logs/facets`, {
      params,
      timeout: LOGS_API_TIMEOUT_MS,
    });
    const servicesRaw = Array.isArray(response.data?.services) ? response.data.services : [];
    const levelsRaw = Array.isArray(response.data?.levels) ? response.data.levels : [];
    return {
      services: servicesRaw
        .map((item: any) => ({
          value: String(item?.value || '').trim(),
          count: Number(item?.count || 0),
        }))
        .filter((item: LogsFacetBucket) => Boolean(item.value)),
      levels: levelsRaw
        .map((item: any) => ({
          value: String(item?.value || '').trim().toUpperCase(),
          count: Number(item?.count || 0),
        }))
        .filter((item: LogsFacetBucket) => Boolean(item.value)),
      context: response.data?.context || {},
      generated_at: response.data?.generated_at,
    };
  }

  /**
   * 获取拓扑链路问题日志预览
   */
  async getTopologyEdgeLogPreview(params: {
    source_service: string;
    target_service: string;
    time_window?: string;
    limit?: number;
    exclude_health_check?: boolean;
  }): Promise<{ data: Event[]; count: number; limit: number; context?: Record<string, any> }> {
    const response = await this.getWithInflightDedupe(`${API_PREFIX}/logs/preview/topology-edge`, { params });
    const raw = ensureArray(response.data);
    const events = raw.map((item: any) => this.transformEvent(item));
    return {
      data: events,
      count: response.data?.count ?? events.length,
      limit: response.data?.limit ?? params.limit ?? 20,
      context: response.data?.context || {},
    };
  }

  /**
   * 获取聚合日志 (智能 Pattern 聚合)
   */
  async getAggregatedLogs(params?: AggregatedLogsParams): Promise<AggregatedLogsResult> {
    const response = await this.getWithInflightDedupe(`${API_PREFIX}/logs/aggregated`, {
      params,
      timeout: LOGS_API_TIMEOUT_MS,
    });
    const data = response.data;
    
    if (data.patterns && Array.isArray(data.patterns)) {
      data.patterns = data.patterns.map((pattern: any) => ({
        ...pattern,
        samples: (pattern.samples || []).map((item: any) => this.transformEvent(item))
      }));
    }
    
    return data;
  }

  private hashText(input: string): string {
    let hash = 2166136261;
    for (let i = 0; i < input.length; i += 1) {
      hash ^= input.charCodeAt(i);
      hash += (hash << 1) + (hash << 4) + (hash << 7) + (hash << 8) + (hash << 24);
    }
    return (hash >>> 0).toString(16).padStart(8, '0');
  }

  private buildFallbackEventId(item: any, message: string): string {
    const seed = [
      String(item?.timestamp || ''),
      String(item?.service_name || ''),
      String(item?.pod_name || ''),
      String(item?.namespace || ''),
      String(item?.level || ''),
      message,
    ].join('|');
    return `evt-${this.hashText(seed)}`;
  }

  /**
   * 转换后端事件数据到前端格式
   */
  private transformEvent(item: any): Event {
    const parsedMessage = parseLogMessage(item.message || '');
    let labels: Record<string, string> = {};
    if (item.labels) {
      if (typeof item.labels === 'string') {
        try {
          labels = JSON.parse(item.labels);
        } catch {
          labels = {};
        }
      } else if (typeof item.labels === 'object') {
        labels = item.labels;
      }
    }

    const existingAttributes = this.parseAttributes(item.attributes);
    const existingK8s = this.parseAttributes(existingAttributes.k8s);
    const stableId = String(item?.id || '').trim() || this.buildFallbackEventId(item, parsedMessage.message);
    
    return {
      id: stableId,
      timestamp: item.timestamp,
      service_name: item.service_name || 'unknown',
      pod_name: item.pod_name || 'unknown',
      namespace: item.namespace || 'unknown',
      level: this.normalizeLevel(item.level || 'INFO', parsedMessage.message),
      message: parsedMessage.message,
      attributes: {
        ...existingAttributes,
        k8s: {
          ...existingK8s,
          node: item.node_name,
          container_name: item.container_name,
          labels: labels,
        },
        trace_id: item.trace_id,
        span_id: item.span_id,
        log_meta: parsedMessage.meta,
      },
      trace_id: item.trace_id,
      span_id: item.span_id,
      node_name: item.node_name,
      container_name: item.container_name,
      labels: labels,
    };
  }

  /**
   * 标准化日志级别
   */
  private normalizeLevel(
    level: string,
    message?: string,
  ): 'TRACE' | 'DEBUG' | 'INFO' | 'WARN' | 'ERROR' | 'FATAL' {
    const resolveStrictLevel = (value: string): 'TRACE' | 'DEBUG' | 'INFO' | 'WARN' | 'ERROR' | 'FATAL' | '' => {
      const text = String(value || '').trim();
      if (!text) {
        return '';
      }
      const upper = text.toUpperCase();
      if (upper === 'WARNING') {
        return 'WARN';
      }
      if (['TRACE', 'DEBUG', 'INFO', 'WARN', 'ERROR', 'FATAL'].includes(upper)) {
        return upper as 'TRACE' | 'DEBUG' | 'INFO' | 'WARN' | 'ERROR' | 'FATAL';
      }
      const prefixMatched = text.match(
        /^\[?(TRACE|DEBUG|INFO|WARN(?:ING)?|ERROR|FATAL)\]?(?:\s+|:|-)/i
      ) || text.match(
        /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\s+(TRACE|DEBUG|INFO|WARN(?:ING)?|ERROR|FATAL)\b/i
      );
      const resolved = String(prefixMatched?.[1] || '').toUpperCase();
      if (resolved === 'WARNING') {
        return 'WARN';
      }
      if (['TRACE', 'DEBUG', 'INFO', 'WARN', 'ERROR', 'FATAL'].includes(resolved)) {
        return resolved as 'TRACE' | 'DEBUG' | 'INFO' | 'WARN' | 'ERROR' | 'FATAL';
      }
      return '';
    };

    const raw = String(level || '').trim();
    const upper = raw.toUpperCase();
    const levelResolved = resolveStrictLevel(raw);
    if (levelResolved && levelResolved !== 'INFO') {
      return levelResolved;
    }
    const messageResolved = resolveStrictLevel(String(message || '').trim());
    if (messageResolved && (!levelResolved || levelResolved === 'INFO')) {
      return messageResolved;
    }
    if (levelResolved) {
      return levelResolved;
    }
    if (upper === 'WARNING') {
      return 'WARN';
    }
    return 'INFO';
  }

  private parseAttributes(value: unknown): Record<string, any> {
    if (!value) {
      return {};
    }

    if (typeof value === 'string') {
      try {
        const parsed = JSON.parse(value);
        if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
          return parsed as Record<string, any>;
        }
      } catch {
        return {};
      }
      return {};
    }

    if (typeof value === 'object' && !Array.isArray(value)) {
      return value as Record<string, any>;
    }

    return {};
  }

  private transformContextLogItem(item: any): any {
    if (!item || typeof item !== 'object') {
      return item;
    }

    const parsedMessage = parseLogMessage(item.message || '');
    const rawMeta = this.parseAttributes(item.log_meta);

    return {
      ...item,
      message: parsedMessage.message,
      log_meta: {
        ...rawMeta,
        ...parsedMessage.meta,
      },
    };
  }

  private transformLogContextResult(data: any): any {
    if (!data || typeof data !== 'object') {
      return data;
    }

    const before = ensureArray(data.before).map((item: any) => this.transformContextLogItem(item));
    const after = ensureArray(data.after).map((item: any) => this.transformContextLogItem(item));
    const listData = ensureArray(data.data).map((item: any) => this.transformContextLogItem(item));
    const current = data.current ? this.transformContextLogItem(data.current) : data.current;

    return {
      ...data,
      before,
      after,
      data: listData,
      current,
    };
  }

  /**
   * 获取日志统计
   */
  async getLogsStats(params?: { time_window?: string }): Promise<LogsStatsResult> {
    try {
      const response = await this.getWithInflightDedupe(`${API_PREFIX}/logs/stats`, { params });
      const byServiceRaw = response.data?.byService && typeof response.data.byService === 'object' ? response.data.byService : {};
      const byLevelRaw = response.data?.byLevel && typeof response.data.byLevel === 'object' ? response.data.byLevel : {};
      const byService = Object.entries(byServiceRaw).reduce((acc, [key, value]) => {
        const name = String(key || '').trim();
        if (!name) {
          return acc;
        }
        acc[name] = Number(value || 0);
        return acc;
      }, {} as Record<string, number>);
      const byLevel = Object.entries(byLevelRaw).reduce((acc, [key, value]) => {
        const level = String(key || '').trim().toUpperCase() || 'OTHER';
        acc[level] = Number(value || 0);
        return acc;
      }, {} as Record<string, number>);

      return {
        total: Number(response.data?.total || 0),
        byService,
        byLevel,
      };
    } catch (error: any) {
      console.warn('Logs stats API error, returning empty data:', error?.response?.data || error?.message);
      return {
        total: 0,
        byService: {},
        byLevel: {},
      };
    }
  }

  /**
   * 获取指标列表
   */
  async getMetrics(params?: { limit?: number; service_name?: string; metric_name?: string }): Promise<{ metrics: Metric[] }> {
    const response = await this.getWithInflightDedupe(`${API_PREFIX}/metrics`, { params });
    // 转换后端数据格式到前端期望的格式
    const data = ensureArray(response.data);
    const metrics = data.map((item: any) => ({
      service_name: item.service_name || 'unknown',
      metric_name: item.metric_name,
      value: item.value,
      timestamp: item.timestamp,
      labels: item.labels || {},
    }));
    return { metrics };
  }

  /**
   * 获取指标统计
   */
  async getMetricStats(): Promise<Record<string, any>> {
    try {
      const response = await this.getWithInflightDedupe(`${API_PREFIX}/metrics/stats`);
      return response.data || { total: 0, byService: {}, byMetricName: {} };
    } catch (error: any) {
      // 如果统计 API 失败，返回空统计数据
      console.warn('Metric stats API error, returning empty data:', error?.response?.data || error?.message);
      return {
        total: 0,
        byService: {},
        byMetricName: {},
      };
    }
  }

  /**
   * 获取追踪列表
   */
  async getTraces(params?: { limit?: number; service_name?: string; trace_id?: string }): Promise<{ traces: Trace[]; total: number }> {
    const response = await this.getWithInflightDedupe(`${API_PREFIX}/traces`, { params });
    // 转换后端数据格式到前端期望的格式
    const data = ensureArray(response.data);
    const traces = data.map((item: any) => ({
      trace_id: item.trace_id,
      service_name: item.service_name || 'unknown',
      operation_name: item.operation_name,
      start_time: item.start_time_str || item.start_time,
      duration_ms: this.resolveTraceDurationMs(item),
      status_code: this.normalizeStatusCode(item.status),
    }));
    return { traces, total: response.data.count || traces.length };
  }

  /**
   * 获取追踪的所有 spans
   */
  async getTraceSpans(traceId: string): Promise<any[]> {
    const response = await this.getWithInflightDedupe(`${API_PREFIX}/traces/${traceId}/spans`);
    const data = ensureArray(response.data);
    return data.map((item: any) => ({
      ...item,
      start_time: item.start_time_str || item.start_time,
      duration_ms: this.resolveTraceDurationMs(item),
      status: this.normalizeStatusCode(item.status),
      tags: item.tags || item.attributes || item.attrs || {},
    }));
  }

  /**
   * 标准化状态码
   */
  private normalizeStatusCode(status: string): 'STATUS_CODE_OK' | 'STATUS_CODE_ERROR' | 'STATUS_CODE_UNSET' {
    const upper = (status || '').toUpperCase().replace('STATUS_CODE_', '');
    if (['OK', 'ERROR', 'UNSET'].includes(upper)) {
      return `STATUS_CODE_${upper}` as 'STATUS_CODE_OK' | 'STATUS_CODE_ERROR' | 'STATUS_CODE_UNSET';
    }
    return 'STATUS_CODE_UNSET';
  }

  /**
   * 解析 Trace 时延（毫秒）
   * 兼容不同后端字段命名，避免前端展示为 0。
   */
  private resolveTraceDurationMs(item: any): number {
    const parsePositive = (value: unknown, scale = 1): number => {
      const parsed = Number(value);
      if (Number.isFinite(parsed) && parsed > 0) {
        return parsed * scale;
      }
      return 0;
    };

    const directCandidates = [
      item?.duration_ms,
      item?.durationMs,
      item?.trace_duration_ms,
      item?.trace_duration,
      item?.latency_ms,
      item?.latency,
      item?.duration,
      item?.elapsed_ms,
      item?.span_duration_ms,
    ];
    for (const candidate of directCandidates) {
      const parsed = parsePositive(candidate);
      if (parsed > 0) {
        return parsed;
      }
    }

    const directUsCandidates = [item?.duration_us, item?.latency_us, item?.elapsed_us, item?.span_duration_us];
    for (const candidate of directUsCandidates) {
      const parsed = parsePositive(candidate, 1 / 1000);
      if (parsed > 0) {
        return parsed;
      }
    }

    const directNsCandidates = [item?.duration_ns, item?.latency_ns, item?.elapsed_ns, item?.span_duration_ns];
    for (const candidate of directNsCandidates) {
      const parsed = parsePositive(candidate, 1 / 1_000_000);
      if (parsed > 0) {
        return parsed;
      }
    }

    const bag = item?.tags || item?.attributes || item?.attrs || {};
    const taggedCandidates = [
      bag?.duration_ms,
      bag?.['span.duration_ms'],
      bag?.latency_ms,
      bag?.duration,
      bag?.elapsed_ms,
    ];
    for (const candidate of taggedCandidates) {
      const parsed = parsePositive(candidate);
      if (parsed > 0) {
        return parsed;
      }
    }

    const usCandidates = [bag?.duration_us, bag?.['span.duration_us'], bag?.latency_us, bag?.elapsed_us];
    for (const candidate of usCandidates) {
      const parsed = parsePositive(candidate, 1 / 1000);
      if (parsed > 0) {
        return parsed;
      }
    }

    const nsCandidates = [bag?.duration_ns, bag?.['span.duration_ns'], bag?.latency_ns, bag?.elapsed_ns];
    for (const candidate of nsCandidates) {
      const parsed = parsePositive(candidate, 1 / 1_000_000);
      if (parsed > 0) {
        return parsed;
      }
    }

    const startNsCandidates = [
      item?.start_time_unix_nano,
      item?.start_unix_nano,
      item?.start_ns,
      bag?.start_time_unix_nano,
      bag?.start_unix_nano,
      bag?.start_ns,
    ];
    const endNsCandidates = [
      item?.end_time_unix_nano,
      item?.end_unix_nano,
      item?.end_ns,
      bag?.end_time_unix_nano,
      bag?.end_unix_nano,
      bag?.end_ns,
    ];
    for (const startRaw of startNsCandidates) {
      for (const endRaw of endNsCandidates) {
        const startNs = Number(startRaw);
        const endNs = Number(endRaw);
        if (Number.isFinite(startNs) && Number.isFinite(endNs) && endNs > startNs) {
          return (endNs - startNs) / 1_000_000;
        }
      }
    }

    return 0;
  }

  /**
   * 获取追踪统计
   */
  async getTraceStats(): Promise<Record<string, any>> {
    try {
      const response = await this.getWithInflightDedupe(`${API_PREFIX}/traces/stats`);
      return response.data;
    } catch (error: any) {
      console.warn('Trace stats API error, returning empty data:', error?.response?.data || error?.message);
      return {
        total: 0,
        avg_duration: 0,
        p99_duration: 0,
        error_rate: 0,
      };
    }
  }

  /**
   * 获取 Trace-Lite 推断调用片段
   */
  async getTraceLiteInferred(params?: {
    time_window?: string;
    source_service?: string;
    target_service?: string;
    namespace?: string;
    limit?: number;
  }): Promise<{ data: TraceLiteFragment[]; count: number; stats?: Record<string, any> }> {
    const response = await this.getWithInflightDedupe(`${API_PREFIX}/trace-lite/inferred`, { params });
    return response.data || { data: [], count: 0, stats: {} };
  }

  /**
   * 获取推断质量指标
   */
  async getInferenceQuality(params?: { time_window?: string }): Promise<Record<string, any>> {
    const response = await this.getWithInflightDedupe(`${API_PREFIX}/quality/inference`, { params });
    return response.data || { status: 'error', metrics: {} };
  }

  /**
   * 获取推断质量告警
   */
  async getInferenceQualityAlerts(params?: {
    time_window?: string;
    min_coverage?: number;
    max_inferred_ratio?: number;
    max_false_positive_rate?: number;
  }): Promise<Record<string, any>> {
    const response = await this.getWithInflightDedupe(`${API_PREFIX}/quality/inference/alerts`, { params });
    return response.data || { status: 'error', alerts: [] };
  }

  /**
   * 设置推断质量告警抑制
   */
  async setInferenceAlertSuppression(metric: string, enabled: boolean): Promise<Record<string, any>> {
    const response = await this.client.post(`${API_PREFIX}/quality/inference/alerts/suppress`, null, {
      params: { metric, enabled },
    });
    return response.data || { status: 'error' };
  }

  /**
   * 获取价值指标看板（M4）
   */
  async getValueKpi(params?: { time_window?: string }): Promise<ValueKpiResponse> {
    const response = await this.getWithInflightDedupe(`${API_PREFIX}/value/kpi`, { params });
    return response.data || {
      status: 'error',
      time_window: params?.time_window || '7 DAY',
      metrics: {
        mttd_minutes: 0,
        mttr_minutes: 0,
        trace_log_correlation_rate: 0,
        topology_coverage_rate: 0,
        release_regression_pass_rate: 0,
      },
      incident_summary: { incident_count: 0 },
      release_gate_summary: { total: 0, passed: 0, failed: 0, bypassed: 0, pass_rate: 0 },
      generated_at: new Date().toISOString(),
    };
  }

  /**
   * 导出价值指标周报 CSV（M4）
   */
  async exportValueKpiWeekly(params?: { weeks?: number }): Promise<string> {
    const response = await this.client.get(`${API_PREFIX}/value/kpi/weekly-export`, {
      params,
      responseType: 'text',
    });
    return response.data || '';
  }

  /**
   * 获取拓扑图
   */
  async getTopology(params?: { limit?: number; namespace?: string; source?: string }): Promise<TopologyGraph> {
    const response = await this.getWithInflightDedupe(`${API_PREFIX}/graph/topology`, { params });
    return response.data;
  }

  /**
   * 获取混合拓扑
   */
  async getHybridTopology(params?: {
    time_window?: string;
    namespace?: string;
    confidence_threshold?: number;
    inference_mode?: 'rule' | 'hybrid_score';
    force_refresh?: boolean;
    message_target_enabled?: boolean;
    message_target_patterns?: string;
    message_target_min_support?: number;
    message_target_max_per_log?: number;
  }): Promise<TopologyGraph> {
    try {
      const response = await this.getWithInflightDedupe(`${API_PREFIX}/topology/hybrid`, { params });
      return response.data;
    } catch (error: any) {
      // 如果拓扑 API 失败，返回空数据而不是抛出错误
      console.warn('Topology API error, returning empty data:', error?.response?.data || error?.message);
      return {
        nodes: [],
        edges: [],
        metadata: {
          data_sources: [],
          time_window: params?.time_window || '1 HOUR',
          node_count: 0,
          edge_count: 0,
          avg_confidence: 0,
          source_breakdown: {},
          generated_at: new Date().toISOString(),
        },
      };
    }
  }

  /**
   * 获取拓扑统计
   */
  async getTopologyStats(params?: { time_window?: string }): Promise<Record<string, any>> {
    try {
      const response = await this.getWithInflightDedupe(`${API_PREFIX}/topology/stats`, { params });
      return response.data;
    } catch (error: any) {
      console.warn('Topology stats API error, returning empty data:', error?.response?.data || error?.message);
      return {
        total_nodes: 0,
        total_edges: 0,
        avg_confidence: 0,
      };
    }
  }

  /**
   * 获取告警规则
   */
  async getAlertRules(): Promise<{ total: number; rules: AlertRule[] }> {
    const response = await this.client.get(`${API_PREFIX}/alerts/rules`);
    return response.data;
  }

  /**
   * 获取告警规则模板
   */
  async getAlertRuleTemplates(): Promise<{ total: number; templates: AlertRuleTemplate[] }> {
    const response = await this.client.get(`${API_PREFIX}/alerts/rule-templates`);
    const data = response.data || {};
    return {
      total: Number(data.total || 0),
      templates: Array.isArray(data.templates) ? data.templates : [],
    };
  }

  /**
   * 创建告警规则
   */
  async createAlertRule(rule: Omit<AlertRule, 'id' | 'created_at' | 'updated_at'>): Promise<{ status: string; rule: AlertRule }> {
    const response = await this.client.post(`${API_PREFIX}/alerts/rules`, rule);
    return response.data;
  }

  /**
   * 基于模板创建告警规则
   */
  async createAlertRuleFromTemplate(payload: {
    template_id: string;
    name?: string;
    description?: string;
    service_name?: string;
    threshold?: number;
    duration?: number;
    severity?: 'critical' | 'warning' | 'info';
    labels?: Record<string, string>;
    min_occurrence_count?: number;
    notification_enabled?: boolean;
    notification_channels?: string[];
    notification_cooldown_seconds?: number;
  }): Promise<{ status: string; rule: AlertRule }> {
    const response = await this.client.post(`${API_PREFIX}/alerts/rules/from-template`, payload);
    return response.data;
  }

  /**
   * 更新告警规则
   */
  async updateAlertRule(ruleId: string, rule: Partial<AlertRule>): Promise<{ status: string; rule: AlertRule }> {
    try {
      const response = await this.client.patch(`${API_PREFIX}/alerts/rules/${ruleId}`, rule);
      return response.data;
    } catch (error: any) {
      const status = Number(error?.response?.status || 0);
      // 向后兼容仅支持 PUT 的旧后端
      if (status === 404 || status === 405) {
        const fallbackResponse = await this.client.put(`${API_PREFIX}/alerts/rules/${ruleId}`, rule);
        return fallbackResponse.data;
      }
      throw error;
    }
  }

  /**
   * 删除告警规则
   */
  async deleteAlertRule(ruleId: string): Promise<{ status: string; message: string }> {
    const response = await this.client.delete(`${API_PREFIX}/alerts/rules/${ruleId}`);
    return response.data;
  }

  /**
   * 获取告警事件
   */
  async getAlertEvents(params?: { limit?: number; status?: string; severity?: string; cursor?: string; service_name?: string; search?: string }): Promise<AlertEventsResponse> {
    const response = await this.getWithInflightDedupe(`${API_PREFIX}/alerts/events`, { params });
    const data = response.data || {};
    return {
      total: Number(data.total || 0),
      events: Array.isArray(data.events) ? data.events : [],
      limit: Number(data.limit || params?.limit || 100),
      cursor: data.cursor || null,
      has_more: Boolean(data.has_more),
      next_cursor: data.next_cursor || null,
    };
  }

  /**
   * 获取告警统计
   */
  async getAlertStats(): Promise<Record<string, any>> {
    const response = await this.client.get(`${API_PREFIX}/alerts/stats`);
    return response.data;
  }

  /**
   * 获取告警通知记录
   */
  async getAlertNotifications(params?: {
    limit?: number;
    channel?: string;
    delivery_status?: string;
    event_id?: string;
  }): Promise<{ total: number; notifications: AlertNotification[] }> {
    const response = await this.client.get(`${API_PREFIX}/alerts/notifications`, { params });
    const data = response.data || {};
    return {
      total: Number(data.total || 0),
      notifications: Array.isArray(data.notifications) ? data.notifications : [],
    };
  }

  /**
   * 手动触发一次告警规则评估
   */
  async evaluateAlertRules(): Promise<{ status: string; evaluated_rules: number; triggered_alerts: number; resolved_alerts: number }> {
    const response = await this.client.post(`${API_PREFIX}/alerts/evaluate`);
    return response.data;
  }

  /**
   * 确认告警事件
   */
  async acknowledgeAlertEvent(eventId: string): Promise<{ status: string; event: AlertEvent }> {
    const response = await this.client.post(`${API_PREFIX}/alerts/events/${eventId}/ack`);
    return response.data;
  }

  /**
   * 静默告警事件
   */
  async silenceAlertEvent(eventId: string, durationSeconds = 3600): Promise<{ status: string; event: AlertEvent; duration_seconds: number }> {
    const response = await this.client.post(`${API_PREFIX}/alerts/events/${eventId}/silence`, null, {
      params: { duration_seconds: durationSeconds },
    });
    return response.data;
  }

  /**
   * 手工关闭告警事件
   */
  async resolveAlertEvent(eventId: string, reason?: string): Promise<{ status: string; event: AlertEvent; updated: boolean }> {
    const response = await this.client.post(`${API_PREFIX}/alerts/events/${eventId}/resolve`, null, {
      params: { reason },
    });
    return response.data;
  }

  /**
   * 获取标签发现结果
   */
  async discoverLabels(params?: { limit?: number }): Promise<Record<string, any>> {
    const response = await this.getWithInflightDedupe(`${API_PREFIX}/labels/discover`, { params });
    return response.data;
  }

  /**
   * 获取标签建议
   */
  async getLabelSuggestions(params?: { service_name?: string }): Promise<Record<string, any>> {
    const response = await this.getWithInflightDedupe(`${API_PREFIX}/labels/suggestions`, { params });
    return response.data;
  }

  /**
   * 发送 OTLP 日志
   */
  async sendOTLPLogs(logs: any): Promise<{ status: string; processed: number }> {
    const response = await this.client.post('/v1/logs', logs);
    return response.data;
  }

  /**
   * 发送 OTLP 指标
   */
  async sendOTLPMetrics(metrics: any): Promise<{ status: string; processed: number }> {
    const response = await this.client.post('/v1/metrics', metrics);
    return response.data;
  }

  /**
   * 发送 OTLP 追踪
   */
  async sendOTLPTraces(traces: any): Promise<{ status: string; processed: number }> {
    const response = await this.client.post('/v1/traces', traces);
    return response.data;
  }

  /**
   * 获取日志上下文（优先 log_id，支持 trace_id 或 pod_name + timestamp 回退）
   */
  async getLogContext(params: { 
    log_id?: string;
    trace_id?: string; 
    pod_name?: string; 
    namespace?: string;
    timestamp?: string; 
    before_count?: number; 
    after_count?: number; 
    limit?: number 
  }): Promise<{ 
    log_id?: string;
    trace_id?: string; 
    pod_name?: string;
    namespace?: string;
    timestamp?: string;
    data?: any[]; 
    before?: any[];
    after?: any[];
    current?: any;
    count?: number; 
    before_count?: number;
    after_count?: number;
    limit?: number 
  }> {
    const response = await this.getWithInflightDedupe(`${API_PREFIX}/logs/context`, { params });
    return this.transformLogContextResult(response.data);
  }

  /**
   * AI 分析日志
   */
  async analyzeLog(event: Event): Promise<{
    overview?: {
      problem: string;
      severity: string;
      description: string;
      confidence: number;
    };
    rootCauses?: Array<{
      title: string;
      description: string;
    }>;
    solutions?: Array<{
      title: string;
      description: string;
      steps: string[];
    }>;
    similarCases?: Array<{
      title: string;
      description: string;
    }>;
    session_id?: string;
  }> {
    const requestData = {
      id: event.id,
      timestamp: event.timestamp,
      entity: { name: event.service_name },
      event: {
        level: event.level.toLowerCase(),
        raw: event.message
      },
      context: event.attributes || {}
    };
    const response = await this.client.post(`${API_PREFIX}/ai/analyze-log`, requestData);
    return response.data;
  }

  /**
   * AI 分析日志 (使用 LLM 大模型)
   */
  async analyzeLogLLM(params: {
    log_content: string;
    service_name?: string;
    context?: Record<string, any>;
    use_llm?: boolean;
  }): Promise<{
    overview?: {
      problem: string;
      severity: string;
      description: string;
      confidence: number;
    };
    rootCauses?: Array<{
      title: string;
      description: string;
    }>;
    solutions?: Array<{
      title: string;
      description: string;
      steps: string[];
    }>;
    similarCases?: Array<{
      title: string;
      description: string;
    }>;
    analysis_method?: string;
    model?: string;
    cached?: boolean;
    latency_ms?: number;
    session_id?: string;
  }> {
    const response = await this.client.post(`${API_PREFIX}/ai/analyze-log-llm`, params);
    return response.data;
  }

  /**
   * AI 分析追踪
   */
  async analyzeTrace(data: { trace_id: string; service_name?: string }): Promise<{
    overview?: {
      problem: string;
      severity: string;
      description: string;
      confidence: number;
    };
    rootCauses?: Array<{
      title: string;
      description: string;
    }>;
    solutions?: Array<{
      title: string;
      description: string;
      steps: string[];
    }>;
    similarCases?: Array<{
      title: string;
      description: string;
    }>;
    session_id?: string;
  }> {
    const traceId = (data.trace_id || '').trim();
    const payload = {
      trace_id: traceId,
      service_name: data.service_name,
    };
    const response = await this.client.post(`${API_PREFIX}/ai/analyze-trace`, payload);
    return response.data;
  }

  /**
   * AI 分析追踪 (使用 LLM 大模型)
   */
  async analyzeTraceLLM(params: { trace_id: string; service_name?: string }): Promise<{
    overview?: {
      problem: string;
      severity: string;
      description: string;
      confidence: number;
    };
    rootCauses?: Array<{
      title: string;
      description: string;
    }>;
    solutions?: Array<{
      title: string;
      description: string;
      steps: string[];
    }>;
    similarCases?: Array<{
      title: string;
      description: string;
    }>;
    analysis_method?: string;
    model?: string;
    cached?: boolean;
    latency_ms?: number;
    error?: string;
    session_id?: string;
  }> {
    const response = await this.client.post(`${API_PREFIX}/ai/analyze-trace-llm`, params);
    return response.data;
  }

  /**
   * 查找相似知识条目
   */
  async findSimilarCases(params: {
    log_content: string;
    service_name?: string;
    problem_type?: string;
    context?: Record<string, any>;
    limit?: number;
  }): Promise<{
    cases: Array<{
      id: string;
      problem_type: string;
      severity: string;
      summary: string;
      service_name: string;
      root_causes: string[];
      solutions: Array<{ title: string; description: string; steps: string[] }>;
      resolved: boolean;
      resolution: string;
      tags: string[];
      similarity_score: number;
      matched_features: string[];
      relevance_reason: string;
      content_update_history_count?: number;
      content_update_history_recent?: Array<{
        event_id?: string;
        event_type?: string;
        version?: number;
        editor?: string;
        changed_fields?: string[];
        changes?: Record<string, { before?: any; after?: any }>;
        requested_fields?: string[];
        unchanged_requested_fields?: string[];
        no_effective_change_reason?: string;
        effective_save_mode?: string;
        sync_status?: string;
        sync_error_code?: string;
        note?: string;
        source?: string;
        updated_at?: string;
      }>;
    }>;
    total: number;
  }> {
    const response = await this.client.post(`${API_PREFIX}/ai/similar-cases`, params);
    return response.data;
  }

  /**
   * 保存知识条目到知识库
   */
  async saveCase(params: {
    problem_type: string;
    severity: string;
    summary: string;
    log_content: string;
    service_name?: string;
    root_causes?: string[];
    solutions?: Array<{ title: string; description: string; steps: string[] }>;
    context?: Record<string, any>;
    llm_provider?: string;
    llm_model?: string;
    llm_metadata?: Record<string, any>;
    source?: string;
    tags?: string[];
    save_mode?: 'local_only' | 'local_and_remote';
    remote_enabled?: boolean;
  }): Promise<{
    id: string;
    message: string;
    created_at: string;
    effective_save_mode?: 'local_only' | 'local_and_remote';
    sync_status?: string;
    external_doc_id?: string;
    sync_error?: string;
    sync_error_code?: string;
    outbox_id?: string;
  }> {
    const response = await this.client.post(`${API_PREFIX}/ai/cases`, params);
    return response.data;
  }

  /**
   * 知识库 Provider 运行状态
   */
  async getKBProviderStatus(): Promise<{
    mode: string;
    provider?: string;
    remote_configured?: boolean;
    remote_available?: boolean;
    message?: string;
    cached?: boolean;
    outbox_queue_total?: number;
    outbox_failed?: number;
    outbox_failed_retry_attempts?: number;
    outbox_worker_running?: boolean;
  }> {
    const response = await this.client.get(`${API_PREFIX}/ai/kb/providers/status`);
    return response.data;
  }

  /**
   * 获取远端知识库运行时状态
   */
  async getKBRuntimeStatus(): Promise<Record<string, any>> {
    const response = await this.client.get(`${API_PREFIX}/ai/kb/runtime`);
    return response.data;
  }

  /**
   * 校验远端知识库运行时参数
   */
  async validateKBRuntimeConfig(params: {
    provider?: string;
    base_url?: string;
    api_key?: string;
    timeout_seconds?: number;
    health_path?: string;
    search_path?: string;
    upsert_path?: string;
    outbox_enabled?: boolean;
    outbox_poll_seconds?: number;
    outbox_max_attempts?: number;
    clear_api_key?: boolean;
    persist_to_deployment?: boolean;
    extra?: Record<string, any>;
  }): Promise<Record<string, any>> {
    const response = await this.client.post(`${API_PREFIX}/ai/kb/runtime/validate`, params);
    return response.data;
  }

  /**
   * 更新远端知识库运行时参数
   */
  async updateKBRuntimeConfig(params: {
    provider?: string;
    base_url?: string;
    api_key?: string;
    timeout_seconds?: number;
    health_path?: string;
    search_path?: string;
    upsert_path?: string;
    outbox_enabled?: boolean;
    outbox_poll_seconds?: number;
    outbox_max_attempts?: number;
    clear_api_key?: boolean;
    persist_to_deployment?: boolean;
    extra?: Record<string, any>;
  }): Promise<Record<string, any>> {
    const response = await this.client.post(`${API_PREFIX}/ai/kb/runtime/update`, params);
    return response.data;
  }

  /**
   * 远端同步 Outbox 状态
   */
  async getKBOutboxStatus(): Promise<{
    enabled: boolean;
    worker_running: boolean;
    queue_total: number;
    pending: number;
    failed: number;
    processing: number;
    poll_seconds: number;
    max_attempts: number;
    items: Array<{
      outbox_id: string;
      case_id: string;
      status: string;
      attempts: number;
      max_attempts: number;
      next_retry_at: number;
      last_error?: string;
      last_error_code?: string;
    }>;
    failed_retry_attempts?: number;
    failed_by_code?: Record<string, number>;
  }> {
    const response = await this.client.get(`${API_PREFIX}/ai/kb/outbox/status`);
    return response.data;
  }

  /**
   * 知识库运行时选项解析
   */
  async resolveKBRuntimeOptions(params: {
    remote_enabled: boolean;
    retrieval_mode: 'local' | 'hybrid';
    save_mode: 'local_only' | 'local_and_remote';
  }): Promise<{
    effective_retrieval_mode: 'local' | 'hybrid';
    effective_save_mode: 'local_only' | 'local_and_remote';
    remote_available: boolean;
    provider_name?: string;
    message?: string;
  }> {
    const response = await this.client.post(`${API_PREFIX}/ai/kb/runtime/options`, params);
    return response.data;
  }

  /**
   * 统一知识库检索
   */
  async searchKB(params: {
    query: string;
    service_name?: string;
    problem_type?: string;
    top_k?: number;
    retrieval_mode?: 'local' | 'hybrid';
    include_draft?: boolean;
  }): Promise<{
    cases: Array<{
      id: string;
      summary: string;
      problem_type: string;
      service_name?: string;
      similarity_score: number;
      source_backend: 'local' | 'external';
      resolution?: string;
      verification_result?: 'pass' | 'fail';
    }>;
    total: number;
    effective_mode: 'local' | 'hybrid';
    sources: { local: number; external: number };
    message?: string;
    warning_code?: string;
  }> {
    const response = await this.client.post(`${API_PREFIX}/ai/kb/search`, params);
    return response.data;
  }

  /**
   * 从分析会话生成知识草稿
   */
  async buildKBFromAnalysisSession(params: {
    analysis_session_id: string;
    include_followup?: boolean;
    history?: Array<{ role: 'user' | 'assistant'; content: string; timestamp?: string; message_id?: string; metadata?: Record<string, any> }>;
    use_llm?: boolean;
    save_mode?: 'local_only' | 'local_and_remote';
    remote_enabled?: boolean;
  }): Promise<{
    draft_case: Record<string, any>;
    missing_required_fields: string[];
    confidence: number;
    draft_method?: 'llm' | 'rule-based';
    llm_enabled?: boolean;
    llm_requested?: boolean;
    llm_fallback_reason?: string;
    save_mode_effective?: 'local_only' | 'local_and_remote';
  }> {
    const response = await this.client.post(`${API_PREFIX}/ai/kb/from-analysis-session`, params);
    return response.data;
  }

  /**
   * 更新人工修复步骤
   */
  async updateCaseManualRemediation(
    caseId: string,
    params: {
      manual_remediation_steps: string[];
      verification_result: 'pass' | 'fail';
      verification_notes: string;
      final_resolution?: string;
      save_mode?: 'local_only' | 'local_and_remote';
      remote_enabled?: boolean;
    }
  ): Promise<{
    status: string;
    case_id: string;
    knowledge_version: number;
    sync_status: string;
    effective_save_mode?: 'local_only' | 'local_and_remote';
    outbox_id?: string;
    sync_error_code?: string;
    remediation_history_count?: number;
    message?: string;
  }> {
    const response = await this.client.patch(
      `${API_PREFIX}/ai/cases/${encodeURIComponent(caseId)}/manual-remediation`,
      params
    );
    return response.data;
  }

  /**
   * 获取案例列表
   */
  async getCases(params?: {
    problem_type?: string;
    service_name?: string;
    limit?: number;
  }): Promise<{
    cases: Array<{
      id: string;
      problem_type: string;
      severity: string;
      summary: string;
      service_name: string;
      resolved: boolean;
      resolution?: string;
      tags: string[];
      created_at: string;
      updated_at?: string;
      resolved_at?: string;
      source?: string;
      llm_provider?: string;
      llm_model?: string;
      case_status?: string;
      knowledge_version?: number;
      verification_result?: 'pass' | 'fail';
      verification_notes?: string;
      manual_remediation_steps?: string[];
      sync_status?: string;
      external_doc_id?: string;
      sync_error?: string;
      sync_error_code?: string;
      last_editor?: string;
      content_update_history_count?: number;
      remediation_history?: Array<{
        version?: number;
        updated_at?: string;
        editor?: string;
        manual_remediation_steps?: string[];
        verification_result?: 'pass' | 'fail' | string;
        verification_notes?: string;
        final_resolution?: string;
        sync_status?: string;
        effective_save_mode?: 'local_only' | 'local_and_remote' | string;
      }>;
    }>;
    total: number;
  }> {
    const response = await this.client.get(`${API_PREFIX}/ai/cases`, { params });
    return response.data;
  }

  /**
   * 获取案例详情
   */
  async getCaseDetail(caseId: string): Promise<{
    id: string;
    problem_type: string;
    severity: string;
    summary: string;
    log_content: string;
    service_name: string;
    root_causes: string[];
    solutions: Array<{ title?: string; description?: string; steps?: string[] }>;
    context: Record<string, any>;
    resolved: boolean;
    resolution?: string;
    tags: string[];
    created_at: string;
    updated_at?: string;
    resolved_at?: string;
    llm_provider?: string;
    llm_model?: string;
    llm_metadata?: Record<string, any>;
    source?: string;
    case_status?: string;
    knowledge_version?: number;
    manual_remediation_steps?: string[];
    verification_result?: 'pass' | 'fail';
    verification_notes?: string;
    analysis_summary?: string;
    sync_status?: string;
    external_doc_id?: string;
    sync_error?: string;
    sync_error_code?: string;
    last_editor?: string;
    content_update_history?: Array<{
      event_id?: string;
      event_type?: string;
      version?: number;
      updated_at?: string;
      editor?: string;
      changed_fields?: string[];
      changes?: Record<string, { before?: any; after?: any }>;
      requested_fields?: string[];
      unchanged_requested_fields?: string[];
      no_effective_change_reason?: string;
      effective_save_mode?: 'local_only' | 'local_and_remote' | string;
      sync_status?: string;
      sync_error_code?: string;
      note?: string;
      source?: string;
    }>;
    content_update_history_count?: number;
    remediation_history?: Array<{
      version?: number;
      updated_at?: string;
      editor?: string;
      manual_remediation_steps?: string[];
      verification_result?: 'pass' | 'fail' | string;
      verification_notes?: string;
      final_resolution?: string;
      sync_status?: string;
      effective_save_mode?: 'local_only' | 'local_and_remote' | string;
    }>;
    analysis_result?: {
      overview?: { problem: string; severity: string; description: string; confidence: number };
      rootCauses?: Array<{ title: string; description: string }>;
      solutions?: Array<{ title: string; description: string; steps: string[] }>;
      similarCases?: Array<{ title: string; description: string }>;
      analysis_method?: string;
      model?: string;
      cached?: boolean;
      latency_ms?: number;
      error?: string;
    };
  }> {
    const response = await this.client.get(`${API_PREFIX}/ai/cases/${encodeURIComponent(caseId)}`);
    return response.data;
  }

  /**
   * 更新知识库内容（摘要/根因/方案等）
   */
  async updateCaseContent(
    caseId: string,
    params: {
      problem_type?: string;
      severity?: string;
      summary?: string;
      service_name?: string;
      root_causes?: string[];
      solutions?: Array<{ title?: string; description?: string; steps?: string[] }>;
      solutions_text?: string;
      analysis_summary?: string;
      resolution?: string;
      tags?: string[];
      save_mode?: 'local_only' | 'local_and_remote';
      remote_enabled?: boolean;
    }
  ): Promise<{
    status: string;
    case_id: string;
    knowledge_version: number;
    effective_save_mode?: 'local_only' | 'local_and_remote';
    sync_status?: string;
    external_doc_id?: string;
    sync_error?: string;
    sync_error_code?: string;
    outbox_id?: string;
    updated_at?: string;
    last_editor?: string;
    analysis_summary?: string;
    updated_fields?: string[];
    requested_fields?: string[];
    unchanged_requested_fields?: string[];
    no_effective_change_reason?: string;
    history_entry?: Record<string, any>;
    content_update_history_count?: number;
    friendly_message?: string;
    message?: string;
  }> {
    const response = await this.client.patch(`${API_PREFIX}/ai/cases/${encodeURIComponent(caseId)}`, params);
    return response.data;
  }

  /**
   * 优化知识库解决建议文本（LLM + 规则回退）
   */
  async optimizeKBSolutionContent(params: {
    content: string;
    summary?: string;
    service_name?: string;
    problem_type?: string;
    severity?: string;
    use_llm?: boolean;
  }): Promise<{
    optimized_text: string;
    method: 'llm' | 'rule-based' | string;
    applied_style: string;
    llm_enabled?: boolean;
    llm_requested?: boolean;
    llm_fallback_reason?: string;
  }> {
    const response = await this.client.post(`${API_PREFIX}/ai/kb/solutions/optimize`, params);
    return response.data;
  }

  /**
   * 获取 AI 会话历史列表
   */
  async getAIHistory(params?: {
    limit?: number;
    offset?: number;
    analysis_type?: 'log' | 'trace';
    service_name?: string;
    q?: string;
    include_archived?: boolean;
    pinned_first?: boolean;
  }): Promise<{
    sessions: Array<{
      session_id: string;
      analysis_type: 'log' | 'trace' | string;
      title?: string;
      service_name: string;
      trace_id?: string;
      summary: string;
      summary_text?: string;
      analysis_method?: string;
      llm_model?: string;
      llm_provider?: string;
      source?: string;
      status?: string;
      created_at: string;
      updated_at: string;
      is_pinned?: boolean;
      is_archived?: boolean;
      message_count?: number;
    }>;
    total: number;
    total_all?: number;
    limit?: number;
    offset?: number;
    has_more?: boolean;
  }> {
    const response = await this.client.get(`${API_PREFIX}/ai/history`, { params });
    return response.data;
  }

  /**
   * 获取 AI 会话历史详情（请求 + 结果 + 追问消息）
   */
  async getAIHistoryDetail(sessionId: string): Promise<{
    session_id: string;
    analysis_type: 'log' | 'trace' | string;
    title?: string;
    service_name: string;
    trace_id?: string;
    input_text: string;
    context?: Record<string, any>;
    result?: {
      overview?: { problem: string; severity: string; description: string; confidence: number };
      rootCauses?: Array<{ title: string; description: string }>;
      solutions?: Array<{ title: string; description: string; steps: string[] }>;
      similarCases?: Array<{ title: string; description: string }>;
      analysis_method?: string;
      model?: string;
      cached?: boolean;
      latency_ms?: number;
      error?: string;
      session_id?: string;
    };
    summary?: string;
    summary_text?: string;
    analysis_method?: string;
    llm_model?: string;
    llm_provider?: string;
    source?: string;
    status?: string;
    created_at: string;
    updated_at: string;
    is_pinned?: boolean;
    is_archived?: boolean;
    message_count?: number;
    context_pills?: Array<{ key: string; value: string }>;
    messages: Array<{
      message_id?: string;
      role: 'user' | 'assistant';
      content: string;
      timestamp?: string;
      metadata?: Record<string, any>;
    }>;
  }> {
    const response = await this.client.get(`${API_PREFIX}/ai/history/${encodeURIComponent(sessionId)}`);
    return response.data;
  }

  /**
   * 删除案例
   */
  async deleteCase(caseId: string): Promise<{ status: string; id: string; message: string }> {
    const response = await this.client.delete(`${API_PREFIX}/ai/cases/${encodeURIComponent(caseId)}`);
    return response.data;
  }

  /**
   * 标记案例已解决
   */
  async resolveCase(caseId: string, resolution: string): Promise<{
    status: string;
    id: string;
    resolved: boolean;
    resolution: string;
    resolved_at: string;
    message: string;
  }> {
    const response = await this.client.patch(`${API_PREFIX}/ai/cases/${encodeURIComponent(caseId)}/resolve`, {
      resolution,
    });
    return response.data;
  }

  /**
   * 更新 AI 历史会话（重命名/Pin/归档）
   */
  async updateAIHistorySession(
    sessionId: string,
    params: { title?: string; is_pinned?: boolean; is_archived?: boolean; status?: string }
  ): Promise<{
    status: string;
    session_id: string;
    title?: string;
    is_pinned?: boolean;
    is_archived?: boolean;
    state?: string;
    updated_at?: string;
  }> {
    const response = await this.client.patch(`${API_PREFIX}/ai/history/${encodeURIComponent(sessionId)}`, params);
    return response.data;
  }

  /**
   * 删除 AI 历史会话
   */
  async deleteAIHistorySession(sessionId: string): Promise<{
    status: string;
    session_id: string;
    message: string;
  }> {
    const response = await this.client.delete(`${API_PREFIX}/ai/history/${encodeURIComponent(sessionId)}`);
    return response.data;
  }

  /**
   * 将某条追问回答转换为动作草案
   */
  async createFollowUpAction(
    sessionId: string,
    messageId: string,
    params: { action_type: 'ticket' | 'runbook' | 'alert_suppression'; title?: string; extra?: Record<string, any> }
  ): Promise<{
    status: string;
    session_id: string;
    message_id: string;
    action_id: string;
    action: {
      action_type: string;
      title: string;
      payload: Record<string, any>;
    };
  }> {
    const response = await this.client.post(
      `${API_PREFIX}/ai/history/${encodeURIComponent(sessionId)}/messages/${encodeURIComponent(messageId)}/actions`,
      params
    );
    return response.data;
  }

  /**
   * 删除会话中的单条追问消息
   */
  async deleteFollowUpMessage(
    sessionId: string,
    messageId: string,
  ): Promise<{
    status: string;
    session_id: string;
    message_id: string;
    remaining_message_count: number;
    message: string;
  }> {
    const response = await this.client.delete(
      `${API_PREFIX}/ai/history/${encodeURIComponent(sessionId)}/messages/${encodeURIComponent(messageId)}`
    );
    return response.data;
  }

  /**
   * AI 追问（支持上下文会话）
   */
  async followUpAnalysis(params: {
    question: string;
    analysis_session_id?: string;
    conversation_id?: string;
    use_llm?: boolean;
    analysis_context?: Record<string, any>;
    history?: Array<{ role: 'user' | 'assistant'; content: string; timestamp?: string; message_id?: string; metadata?: Record<string, any> }>;
    reset?: boolean;
  }): Promise<{
    analysis_session_id: string;
    conversation_id: string;
    analysis_method: string;
    llm_enabled: boolean;
    llm_requested?: boolean;
    answer: string;
    history: Array<{
      message_id?: string;
      role: 'user' | 'assistant';
      content: string;
      timestamp?: string;
      metadata?: Record<string, any>;
    }>;
    references?: Array<{ id: string; type: string; title: string; snippet: string }>;
    context_pills?: Array<{ key: string; value: string }>;
    history_compacted?: boolean;
    conversation_summary?: string;
    token_budget?: number;
    token_estimate?: number;
    token_remaining?: number;
    token_warning?: boolean;
    llm_timeout_fallback?: boolean;
  }> {
    const configuredTimeoutMs = Number((import.meta as any)?.env?.VITE_AI_FOLLOWUP_TIMEOUT_MS);
    const timeoutMs = Number.isFinite(configuredTimeoutMs) && configuredTimeoutMs > 0
      ? configuredTimeoutMs
      : 180000;
    const response = await this.client.post(`${API_PREFIX}/ai/follow-up`, params, { timeout: timeoutMs });
    return response.data;
  }

  /**
   * 获取 LLM 运行时状态（预留本地 LLM 接入接口）
   */
  async getLLMRuntimeStatus(): Promise<Record<string, any>> {
    const response = await this.client.get(`${API_PREFIX}/ai/llm/runtime`);
    return response.data;
  }

  /**
   * 校验 LLM 运行时参数（预留接口）
   */
  async validateLLMRuntimeConfig(params: {
    provider?: string;
    model?: string;
    api_base?: string;
    api_key?: string;
    local_model_path?: string;
    clear_api_key?: boolean;
    extra?: Record<string, any>;
  }): Promise<Record<string, any>> {
    const response = await this.client.post(`${API_PREFIX}/ai/llm/runtime/validate`, params);
    return response.data;
  }

  /**
   * 更新 LLM 运行时参数（默认同时尝试持久化到部署文件）
   */
  async updateLLMRuntimeConfig(params: {
    provider?: string;
    model?: string;
    api_base?: string;
    api_key?: string;
    local_model_path?: string;
    clear_api_key?: boolean;
    persist_to_deployment?: boolean;
    extra?: Record<string, any>;
  }): Promise<Record<string, any>> {
    const response = await this.client.post(`${API_PREFIX}/ai/llm/runtime/update`, params);
    return response.data;
  }

  /**
   * 创建拓扑快照
   */
  async createTopologySnapshot(params: { time_window?: string; namespace?: string }): Promise<{ id: string; created_at: string }> {
    const response = await this.client.post(`${API_PREFIX}/topology/snapshots`, params);
    return response.data;
  }

  /**
   * 获取拓扑快照列表
   */
  async getTopologySnapshots(params?: { from_time?: string; to_time?: string }): Promise<{ snapshots: Array<{ id: string; created_at: string; node_count: number; edge_count: number }> }> {
    const response = await this.client.get(`${API_PREFIX}/topology/snapshots`, { params });
    return response.data;
  }

  /**
   * 清除缓存
   */
  async clearCache(pattern?: string): Promise<{ status: string; cleared: number }> {
    try {
      const response = await this.client.delete(`${API_PREFIX}/cache`, { params: { pattern } });
      return response.data;
    } catch (error: any) {
      const status = Number(error?.response?.status || 0);
      // 向后兼容旧后端：仅支持 POST /cache/clear
      if (status === 404 || status === 405) {
        const fallbackResponse = await this.client.post(`${API_PREFIX}/cache/clear`, null, { params: { pattern } });
        return fallbackResponse.data;
      }
      throw error;
    }
  }

  /**
   * 获取缓存统计
   */
  async getCacheStats(): Promise<Record<string, any>> {
    const response = await this.client.get(`${API_PREFIX}/cache/stats`);
    return response.data;
  }

  /**
   * 获取去重统计
   */
  async getDeduplicationStats(): Promise<Record<string, any>> {
    const response = await this.client.get(`${API_PREFIX}/deduplication/stats`);
    return response.data;
  }

  /**
   * 清除去重缓存
   */
  async clearDeduplicationCache(): Promise<{ status: string; message?: string }> {
    const response = await this.client.post(`${API_PREFIX}/deduplication/clear-cache`);
    return response.data || { status: 'ok' };
  }
}

/**
 * 创建 API 客户端实例
 */
export const apiClient = new APIClient();

/**
 * 导出常用 API 方法的快捷方式
 */
export const api = {
  health: () => apiClient.health(),
  getEvents: (params?: LogsQueryParams) => apiClient.getEvents(params),
  getLogsStats: (params?: { time_window?: string }) => apiClient.getLogsStats(params),
  getLogFacets: (params?: LogsFacetQueryParams) => apiClient.getLogFacets(params),
  getTopologyEdgeLogPreview: (params: { source_service: string; target_service: string; time_window?: string; limit?: number; exclude_health_check?: boolean }) =>
    apiClient.getTopologyEdgeLogPreview(params),
  getAggregatedLogs: (params?: AggregatedLogsParams) => apiClient.getAggregatedLogs(params),
  getMetrics: (params?: any) => apiClient.getMetrics(params),
  getMetricStats: () => apiClient.getMetricStats(),
  getTraces: (params?: any) => apiClient.getTraces(params),
  getTraceStats: () => apiClient.getTraceStats(),
  getTraceSpans: (traceId: string) => apiClient.getTraceSpans(traceId),
  getTraceLiteInferred: (params?: any) => apiClient.getTraceLiteInferred(params),
  getInferenceQuality: (params?: any) => apiClient.getInferenceQuality(params),
  getInferenceQualityAlerts: (params?: any) => apiClient.getInferenceQualityAlerts(params),
  setInferenceAlertSuppression: (metric: string, enabled: boolean) => apiClient.setInferenceAlertSuppression(metric, enabled),
  getValueKpi: (params?: any) => apiClient.getValueKpi(params),
  exportValueKpiWeekly: (params?: any) => apiClient.exportValueKpiWeekly(params),
  getTopology: (params?: any) => apiClient.getTopology(params),
  getHybridTopology: (params?: any) => apiClient.getHybridTopology(params),
  getTopologyStats: (params?: any) => apiClient.getTopologyStats(params),
  getAlertRules: () => apiClient.getAlertRules(),
  getAlertRuleTemplates: () => apiClient.getAlertRuleTemplates(),
  createAlertRule: (rule: any) => apiClient.createAlertRule(rule),
  createAlertRuleFromTemplate: (payload: any) => apiClient.createAlertRuleFromTemplate(payload),
  updateAlertRule: (ruleId: string, rule: any) => apiClient.updateAlertRule(ruleId, rule),
  deleteAlertRule: (ruleId: string) => apiClient.deleteAlertRule(ruleId),
  getAlertEvents: (params?: any) => apiClient.getAlertEvents(params),
  getAlertNotifications: (params?: any) => apiClient.getAlertNotifications(params),
  getAlertStats: () => apiClient.getAlertStats(),
  evaluateAlertRules: () => apiClient.evaluateAlertRules(),
  acknowledgeAlertEvent: (eventId: string) => apiClient.acknowledgeAlertEvent(eventId),
  silenceAlertEvent: (eventId: string, durationSeconds?: number) => apiClient.silenceAlertEvent(eventId, durationSeconds),
  resolveAlertEvent: (eventId: string, reason?: string) => apiClient.resolveAlertEvent(eventId, reason),
  discoverLabels: (params?: any) => apiClient.discoverLabels(params),
  getLabelSuggestions: (params?: any) => apiClient.getLabelSuggestions(params),
  sendOTLPLogs: (logs: any) => apiClient.sendOTLPLogs(logs),
  sendOTLPMetrics: (metrics: any) => apiClient.sendOTLPMetrics(metrics),
  sendOTLPTraces: (traces: any) => apiClient.sendOTLPTraces(traces),
  getLogContext: (params: any) => apiClient.getLogContext(params),
  analyzeLog: (data: any) => apiClient.analyzeLog(data),
  analyzeLogLLM: (params: any) => apiClient.analyzeLogLLM(params),
  analyzeTrace: (data: any) => apiClient.analyzeTrace(data),
  analyzeTraceLLM: (params: any) => apiClient.analyzeTraceLLM(params),
  findSimilarCases: (params: any) => apiClient.findSimilarCases(params),
  saveCase: (params: any) => apiClient.saveCase(params),
  getKBProviderStatus: () => apiClient.getKBProviderStatus(),
  getKBRuntimeStatus: () => apiClient.getKBRuntimeStatus(),
  validateKBRuntimeConfig: (params: any) => apiClient.validateKBRuntimeConfig(params),
  updateKBRuntimeConfig: (params: any) => apiClient.updateKBRuntimeConfig(params),
  getKBOutboxStatus: () => apiClient.getKBOutboxStatus(),
  resolveKBRuntimeOptions: (params: any) => apiClient.resolveKBRuntimeOptions(params),
  searchKB: (params: any) => apiClient.searchKB(params),
  buildKBFromAnalysisSession: (params: any) => apiClient.buildKBFromAnalysisSession(params),
  optimizeKBSolutionContent: (params: any) => apiClient.optimizeKBSolutionContent(params),
  updateCaseContent: (caseId: string, params: any) => apiClient.updateCaseContent(caseId, params),
  updateCaseManualRemediation: (caseId: string, params: any) => apiClient.updateCaseManualRemediation(caseId, params),
  getCases: (params?: any) => apiClient.getCases(params),
  getCaseDetail: (caseId: string) => apiClient.getCaseDetail(caseId),
  getAIHistory: (params?: any) => apiClient.getAIHistory(params),
  getAIHistoryDetail: (sessionId: string) => apiClient.getAIHistoryDetail(sessionId),
  updateAIHistorySession: (sessionId: string, params: any) => apiClient.updateAIHistorySession(sessionId, params),
  deleteAIHistorySession: (sessionId: string) => apiClient.deleteAIHistorySession(sessionId),
  createFollowUpAction: (sessionId: string, messageId: string, params: any) => apiClient.createFollowUpAction(sessionId, messageId, params),
  deleteFollowUpMessage: (sessionId: string, messageId: string) => apiClient.deleteFollowUpMessage(sessionId, messageId),
  deleteCase: (caseId: string) => apiClient.deleteCase(caseId),
  resolveCase: (caseId: string, resolution: string) => apiClient.resolveCase(caseId, resolution),
  followUpAnalysis: (params: any) => apiClient.followUpAnalysis(params),
  getLLMRuntimeStatus: () => apiClient.getLLMRuntimeStatus(),
  validateLLMRuntimeConfig: (params: any) => apiClient.validateLLMRuntimeConfig(params),
  updateLLMRuntimeConfig: (params: any) => apiClient.updateLLMRuntimeConfig(params),
  createTopologySnapshot: (params: any) => apiClient.createTopologySnapshot(params),
  getTopologySnapshots: (params?: any) => apiClient.getTopologySnapshots(params),
  clearCache: (pattern?: string) => apiClient.clearCache(pattern),
  getCacheStats: () => apiClient.getCacheStats(),
  getDeduplicationStats: () => apiClient.getDeduplicationStats(),
  clearDeduplicationCache: () => apiClient.clearDeduplicationCache(),
};

export default api;
