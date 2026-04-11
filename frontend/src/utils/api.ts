/**
 * API 客户端 - 参考 Datadog 前端设计
 * 统一的 API 调用、错误处理、请求拦截
 */
import axios, { AxiosInstance, type AxiosRequestConfig, type AxiosResponse } from 'axios';
import {
  type AIRuntimeEventVisibility,
  type AgentRuntimeEventType,
  normalizeAgentRunEventEnvelope,
  parseAgentRuntimeEventBlock,
  takeNextSSEEventBlock,
  type AgentRunApproveRequest,
  type AgentRunCommandRequest,
  type AgentRunCreateRequest,
  type AgentRunEventEnvelope,
  type AgentRunEventsResponse,
  type AgentRunInputRequest,
  type AgentRunInterruptRequest,
  type AgentRunSnapshot,
  type AgentRunStreamEventPayload,
} from './aiAgentRuntime';
import { parseLogMessage } from './logMessage';
import { buildRuntimeCommandSpec, resolveRuntimeClientDeadlineMs } from './commandSpec';
import { resolveCanonicalServiceName } from './serviceName';

// API 基础配置
// 开发环境：使用 Vite 代理（相对路径）
// 生产环境：使用相对路径，由 nginx 代理
const API_BASE_URL = import.meta.env.VITE_API_URL || import.meta.env.VITE_API_BASE_URL || '';
const API_PREFIX = '/api/v1';
const API_TIMEOUT_MS = Number(import.meta.env.VITE_API_TIMEOUT_MS || 60000);
const AI_RUNTIME_API_TIMEOUT_MS = Number(import.meta.env.VITE_AI_RUNTIME_API_TIMEOUT_MS || 180000);
const AI_RUNTIME_API_TIMEOUT_MIN_MS = 10000;
const AI_RUNTIME_API_TIMEOUT_MAX_MS = 300000;
const AI_RUNTIME_EVENT_VISIBILITY_DEFAULT: AIRuntimeEventVisibility = 'default';
const LOGS_API_TIMEOUT_MS = Number(import.meta.env.VITE_LOGS_API_TIMEOUT_MS || 90000);

function resolveTimeoutMs(rawValue: LooseAny, fallbackMs: number): number {
  const parsed = Number(rawValue);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return fallbackMs;
  }
  return Math.floor(parsed);
}

function normalizeAIRuntimeEventVisibility(value: unknown): AIRuntimeEventVisibility {
  const normalized = String(value || '').trim().toLowerCase();
  if (normalized === 'debug') {
    return 'debug';
  }
  return 'default';
}

const AI_ANALYSIS_TIMEOUT_MS = resolveTimeoutMs(
  (import.meta as LooseAny)?.env?.VITE_AI_ANALYSIS_TIMEOUT_MS,
  180000,
);

type FollowUpHistoryMessage = {
  message_id?: string;
  role: 'user' | 'assistant';
  content: string;
  timestamp?: string;
  metadata?: Record<string, LooseAny>;
};

type FollowUpActionPayload = {
  id?: string;
  source?: string;
  priority?: number;
  title?: string;
  purpose?: string;
  question?: string;
  action_type?: 'query' | 'write' | 'manual' | string;
  command?: string;
  command_type?: 'query' | 'repair' | 'unknown' | string;
  risk_level?: 'low' | 'high' | string;
  executable?: boolean;
  requires_confirmation?: boolean;
  requires_write_permission?: boolean;
  requires_elevation?: boolean;
  reason?: string;
};

type FollowUpAnalysisResponsePayload = {
  analysis_session_id: string;
  conversation_id: string;
  analysis_method: string;
  llm_enabled: boolean;
  llm_requested?: boolean;
  answer: string;
  history: FollowUpHistoryMessage[];
  references?: Array<{ id: string; type: string; title: string; snippet: string }>;
  context_pills?: Array<{ key: string; value: string }>;
  history_compacted?: boolean;
  conversation_summary?: string;
  token_budget?: number;
  token_estimate?: number;
  token_remaining?: number;
  token_warning?: boolean;
  llm_timeout_fallback?: boolean;
  subgoals?: Array<{
    id: string;
    title: string;
    status: string;
    reason?: string;
    evidence?: string[];
    next_action?: string;
  }>;
  reflection?: {
    iterations?: number;
    completed_count?: number;
    total_count?: number;
    final_confidence?: number;
    gaps?: string[];
    next_actions?: string[];
    rounds?: Array<{
      iteration?: number;
      summary?: string;
      unresolved_subgoals?: string[];
      gaps?: string[];
      actions?: string[];
      confidence?: number;
    }>;
  };
  actions?: FollowUpActionPayload[];
  action_observations?: Array<Record<string, LooseAny>>;
  react_loop?: Record<string, LooseAny>;
  react_iterations?: Array<Record<string, LooseAny>>;
  thoughts?: Array<Record<string, LooseAny>>;
};

type FollowUpStreamEventPayload = {
  event: string;
  data: Record<string, LooseAny>;
};

export interface ExecExecutorStatusRow {
  executor_type: string;
  executor_profile: string;
  target_kind: string;
  target_identity: string;
  candidate_template_envs: string[];
  rollout_stage?: string;
  summary?: string;
  example_template?: string;
  dispatch_backend: string;
  dispatch_mode: string;
  dispatch_reason?: string;
  dispatch_template_env?: string;
  dispatch_requires_template: boolean;
  dispatch_ready: boolean;
  dispatch_degraded: boolean;
  effective_executor_type: string;
  effective_executor_profile: string;
}

export interface ExecExecutorStatusResponse {
  total: number;
  ready: number;
  rows: ExecExecutorStatusRow[];
  generated_at?: string;
}

/**
 * 安全地将数据转换为数组
 * 防止 map is not a function 错误
 */
function ensureArray(data: LooseAny): LooseAny[] {
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

const TIMEZONE_SUFFIX_REGEX = /(?:[zZ]|[+-]\d{2}:?\d{2})$/;
const NAIVE_DATETIME_REGEX = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?$/;

function normalizeAbsoluteTimeParam(rawValue: unknown): string | undefined {
  const text = String(rawValue ?? '').trim();
  if (!text) {
    return undefined;
  }

  // 对无时区时间按本地时间解析，再显式转成 UTC ISO，避免后端按 UTC 直解造成 +8 偏移。
  let candidate = text;
  if (!TIMEZONE_SUFFIX_REGEX.test(candidate) && NAIVE_DATETIME_REGEX.test(candidate)) {
    candidate = candidate.replace(' ', 'T');
  }

  const date = new Date(candidate);
  if (!Number.isNaN(date.getTime())) {
    return date.toISOString();
  }
  return text;
}


function normalizeText(value: unknown): string {
  return String(value ?? '').trim();
}

function transformTopologyGraph(raw: LooseAny): TopologyGraph {
  const rawNodes = ensureArray(raw?.nodes);
  const normalizedNodes = rawNodes.map((node: LooseAny) => {
    const metrics = node && typeof node.metrics === 'object' ? { ...node.metrics } : {};
    const service = node && typeof node.service === 'object' ? { ...node.service } : {};
    const serviceName = normalizeText(service.name || metrics.service_name || node.name || node.label || node.id || 'unknown');
    const serviceNamespace = normalizeText(service.namespace || metrics.service_namespace || metrics.namespace || node.namespace || '');
    const nodeKey = normalizeText(node.node_key || metrics.node_key || node.id || serviceName);
    return {
      ...node,
      id: nodeKey,
      node_key: nodeKey,
      label: normalizeText(node.label || serviceName || node.id || 'unknown'),
      name: normalizeText(node.name || serviceName || node.label || 'unknown'),
      namespace: serviceNamespace,
      service: {
        namespace: serviceNamespace,
        name: serviceName,
        env: normalizeText(service.env || metrics.env || 'prod'),
      },
      metrics: {
        ...metrics,
        node_key: nodeKey,
        service_name: serviceName,
        service_namespace: serviceNamespace,
        namespace: serviceNamespace || metrics.namespace,
      },
      legacy_id: normalizeText(node.legacy_id || metrics.legacy_id || node.id),
    };
  });

  const nodeIndex = new Map<string, LooseAny>();
  normalizedNodes.forEach((node: LooseAny) => {
    const keys = [node.id, node.node_key, node.legacy_id]
      .map((item: unknown) => normalizeText(item))
      .filter(Boolean);
    keys.forEach((key) => nodeIndex.set(key, node));
  });

  const rawEdges = ensureArray(raw?.edges);
  const normalizedEdges = rawEdges.map((edge: LooseAny) => {
    const metrics = edge && typeof edge.metrics === 'object' ? { ...edge.metrics } : {};
    const sourceKey = normalizeText(metrics.source_node_key || edge.source_node_key || edge.source);
    const targetKey = normalizeText(metrics.target_node_key || edge.target_node_key || edge.target);
    const sourceNode = nodeIndex.get(sourceKey);
    const targetNode = nodeIndex.get(targetKey);
    const sourceService = normalizeText(
      edge.source_service || metrics.source_service || sourceNode?.service?.name || sourceNode?.metrics?.service_name || edge.source,
    );
    const targetService = normalizeText(
      edge.target_service || metrics.target_service || targetNode?.service?.name || targetNode?.metrics?.service_name || edge.target,
    );
    const sourceNamespace = normalizeText(
      edge.source_namespace || metrics.source_namespace || sourceNode?.service?.namespace || sourceNode?.metrics?.service_namespace || '',
    );
    const targetNamespace = normalizeText(
      edge.target_namespace || metrics.target_namespace || targetNode?.service?.namespace || targetNode?.metrics?.service_namespace || '',
    );
    const edgeKey = normalizeText(edge.edge_key || metrics.edge_key || edge.id || `${sourceKey}->${targetKey}`);
    return {
      ...edge,
      id: edgeKey,
      edge_key: edgeKey,
      source: sourceKey,
      target: targetKey,
      source_service: sourceService,
      target_service: targetService,
      source_namespace: sourceNamespace,
      target_namespace: targetNamespace,
      source_node_key: sourceKey,
      target_node_key: targetKey,
      metrics: {
        ...metrics,
        edge_key: edgeKey,
        source_service: sourceService,
        target_service: targetService,
        source_namespace: sourceNamespace,
        target_namespace: targetNamespace,
        source_node_key: sourceKey,
        target_node_key: targetKey,
      },
    };
  });

  const metadata = raw && typeof raw.metadata === 'object' ? raw.metadata : {};
  return {
    nodes: normalizedNodes,
    edges: normalizedEdges,
    metadata: {
      data_sources: Array.isArray(metadata.data_sources) ? metadata.data_sources : [],
      time_window: normalizeText(metadata.time_window || '1 HOUR'),
      namespace: metadata.namespace ?? null,
      node_count: Number(metadata.node_count ?? normalizedNodes.length),
      edge_count: Number(metadata.edge_count ?? normalizedEdges.length),
      avg_confidence: Number(metadata.avg_confidence ?? 0),
      inference_mode: metadata.inference_mode,
      inference_quality: metadata.inference_quality,
      source_breakdown: metadata.source_breakdown || {},
      issue_summary: metadata.issue_summary,
      generated_at: normalizeText(metadata.generated_at || new Date().toISOString()),
      contract_version: metadata.contract_version,
      quality_version: metadata.quality_version,
    },
  };
}

function normalizeTimeRangeParams<T extends { start_time?: string; end_time?: string }>(params?: T): T | undefined {
  if (!params) {
    return params;
  }

  const normalizedStart = normalizeAbsoluteTimeParam(params.start_time);
  const normalizedEnd = normalizeAbsoluteTimeParam(params.end_time);
  if (normalizedStart === params.start_time && normalizedEnd === params.end_time) {
    return params;
  }

  const nextParams: T = { ...params };
  if (normalizedStart !== undefined) {
    nextParams.start_time = normalizedStart as T['start_time'];
  } else {
    delete nextParams.start_time;
  }
  if (normalizedEnd !== undefined) {
    nextParams.end_time = normalizedEnd as T['end_time'];
  } else {
    delete nextParams.end_time;
  }
  return nextParams;
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
  attributes: Record<string, LooseAny>;
  trace_id?: string;
  span_id?: string;
  node_name?: string;
  container_name?: string;
  container_id?: string;
  container_image?: string;
  pod_id?: string;
  host_ip?: string;
  labels?: Record<string, string>;
  log_meta?: {
    wrapped: boolean;
    stream?: string;
    collector_time?: string;
    line_count: number;
    merged?: boolean;
    truncated?: boolean;
    original_length?: number;
    parser_profile?: string;
    confidence?: number;
  };
  message_preview?: string;
  edge_side?: 'source' | 'target' | 'correlated';
  edge_match_kind?: 'source_mentions_target' | 'target_mentions_source' | 'dual_text' | 'source_service' | 'target_service' | 'correlated_text';
  correlation_kind?: 'seed' | 'expanded' | 'candidate';
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
  evidence_chain: Array<Record<string, LooseAny>>;
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
  source_service?: string;
  target_service?: string;
  source_namespace?: string;
  target_namespace?: string;
  source_node_key?: string;
  target_node_key?: string;
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
    source_service?: string;
    target_service?: string;
    source_namespace?: string;
    target_namespace?: string;
    source_node_key?: string;
    target_node_key?: string;
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
    namespace?: string | null;
    node_count: number;
    edge_count: number;
    avg_confidence: number;
    inference_mode?: 'rule' | 'hybrid_score' | string;
    inference_quality?: Record<string, LooseAny>;
    source_breakdown: Record<string, LooseAny>;
    issue_summary?: Record<string, unknown>;
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
  source_service?: string | null;
  target_service?: string | null;
  namespace?: string | null;
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
  source_service?: string;
  target_service?: string;
  namespace?: string;
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
  labels?: Record<string, string>;
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
  source_service?: string | null;
  target_service?: string | null;
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
  delivery_status: 'ok' | 'failed' | 'skipped' | 'LooseAny' | string;
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
    last_result?: Record<string, LooseAny> | null;
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
  namespace?: string;
  namespaces?: string[] | string;
  trace_id?: string;
  trace_ids?: string[] | string;
  correlation_mode?: 'and' | 'or' | string;
  request_id?: string;
  request_ids?: string[] | string;
  pod_name?: string;
  container_name?: string;
  level?: string;
  levels?: string[] | string;
  start_time?: string;
  end_time?: string;
  exclude_health_check?: boolean;
  search?: string;
  source_service?: string;
  target_service?: string;
  source_namespace?: string;
  target_namespace?: string;
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
  namespaces: LogsFacetBucket[];
  levels: LogsFacetBucket[];
  context?: Record<string, LooseAny>;
  generated_at?: string;
}

export interface LogsStatsResult {
  total: number;
  byService: Record<string, number>;
  byServiceErrors?: Record<string, number>;
  byLevel: Record<string, number>;
}

export interface LogsFacetQueryParams extends LogsQueryParams {
  limit_services?: number;
  limit_namespaces?: number;
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
  private inflightGetRequests = new Map<string, Promise<AxiosResponse<LooseAny>>>();
  private aiRuntimeThreadCache = new Map<string, string>();

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

  private stableSerialize(value: LooseAny): string {
    const seen = new WeakSet<object>();

    const walk = (input: LooseAny): string => {
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

      const entries = Object.entries(input as Record<string, LooseAny>)
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

  private readConversationIdFromRunCreateParams(params: AgentRunCreateRequest): string {
    const runtimeOptions = (params.runtime_options && typeof params.runtime_options === 'object')
      ? params.runtime_options as Record<string, unknown>
      : {};
    const analysisContext = (params.analysis_context && typeof params.analysis_context === 'object')
      ? params.analysis_context as Record<string, unknown>
      : {};
    const conversationId = runtimeOptions.conversation_id ?? analysisContext.conversation_id;
    if (typeof conversationId === 'string') {
      return conversationId.trim();
    }
    if (conversationId === null || conversationId === undefined) {
      return '';
    }
    return String(conversationId).trim();
  }

  private buildAIRuntimeThreadCacheKey(params: AgentRunCreateRequest): string {
    const sessionId = typeof params.session_id === 'string' ? params.session_id.trim() : '';
    const conversationId = this.readConversationIdFromRunCreateParams(params);
    return `${sessionId}::${conversationId}`;
  }

  private buildAIRuntimeRunIdempotencyKey(params: AgentRunCreateRequest, threadId: string): string {
    const explicit = typeof params.idempotency_key === 'string' ? params.idempotency_key.trim() : '';
    if (explicit) {
      return explicit.slice(0, 128);
    }
    const conversationId = this.readConversationIdFromRunCreateParams(params);
    const question = String(params.question || '').trim();
    const nonce = typeof globalThis.crypto?.randomUUID === 'function'
      ? globalThis.crypto.randomUUID()
      : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
    const seed = [threadId, conversationId, question, nonce].join('|');
    return `airun-${this.hashText(seed)}-${Date.now().toString(36)}`.slice(0, 128);
  }

  private parseAIRuntimeCreateRunRetryAfterMs(error: unknown): number {
    const status = Number((error as LooseAny)?.response?.status || 0);
    if (status !== 503) {
      return 0;
    }
    const detail = (error as LooseAny)?.response?.data?.detail;
    if (!detail || typeof detail !== 'object') {
      return 0;
    }
    const code = String((detail as Record<string, unknown>).code || '').trim();
    if (code !== 'runtime_outer_backend_unavailable') {
      return 0;
    }
    const retryAfterS = Number((detail as Record<string, unknown>).retry_after_s || 0);
    if (!Number.isFinite(retryAfterS) || retryAfterS <= 0) {
      return 0;
    }
    return Math.max(0, Math.floor(retryAfterS * 1000));
  }

  private resolveAIRuntimeCreateRunRetryDelaysMs(): number[] {
    const raw = String((import.meta as LooseAny)?.env?.VITE_AI_RUNTIME_CREATE_RUN_RETRY_DELAYS_MS || '300,800');
    const delays = raw
      .split(',')
      .map((segment) => Number(segment.trim()))
      .filter((item) => Number.isFinite(item) && item > 0)
      .map((item) => Math.min(Math.floor(item), 5000));
    if (!delays.length) {
      return [300, 800];
    }
    return delays.slice(0, 5);
  }

  private async sleepMs(ms: number): Promise<void> {
    const safeMs = Math.max(0, Math.floor(ms));
    if (safeMs <= 0) {
      return;
    }
    await new Promise<void>((resolve) => {
      globalThis.setTimeout(resolve, safeMs);
    });
  }

  private async ensureAIRuntimeThreadId(
    params: AgentRunCreateRequest,
    options?: { deadlineMs?: number; timeoutMs?: number },
  ): Promise<string> {
    const threadKey = this.buildAIRuntimeThreadCacheKey(params);
    const cachedThreadId = this.aiRuntimeThreadCache.get(threadKey);
    if (cachedThreadId) {
      return cachedThreadId;
    }
    const sessionId = typeof params.session_id === 'string' ? params.session_id.trim() : '';
    const conversationId = this.readConversationIdFromRunCreateParams(params);
    const payload = {
      session_id: sessionId,
      conversation_id: conversationId,
      title: String(params.question || '').trim() || 'AI Runtime Thread',
    };
    const requestTimeoutMs = Number.isFinite(Number(options?.timeoutMs)) && Number(options?.timeoutMs) > 0
      ? Number(options?.timeoutMs)
      : this.resolveAIRuntimeRequestTimeoutMs(options?.deadlineMs);
    const created = await this.client.post('/api/v2/threads', payload, {
      timeout: requestTimeoutMs,
    });
    const thread = (created.data && typeof created.data === 'object')
      ? (created.data as Record<string, unknown>).thread as Record<string, unknown>
      : undefined;
    const threadIdRaw = thread && typeof thread === 'object' ? thread.thread_id : '';
    const threadId = typeof threadIdRaw === 'string' ? threadIdRaw.trim() : String(threadIdRaw || '').trim();
    if (!threadId) {
      throw new Error('runtime v2 create thread returned empty thread_id');
    }
    this.aiRuntimeThreadCache.set(threadKey, threadId);
    return threadId;
  }

  private async getWithInflightDedupe<T = LooseAny>(
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
    this.inflightGetRequests.set(key, request as Promise<AxiosResponse<LooseAny>>);
    return request;
  }

  private resolveAIRuntimeRequestTimeoutMs(deadlineMs?: number, fallbackMs: number = AI_RUNTIME_API_TIMEOUT_MS): number {
    const fallback = Number.isFinite(Number(fallbackMs)) && Number(fallbackMs) > 0
      ? Number(fallbackMs)
      : AI_RUNTIME_API_TIMEOUT_MS;
    const safeFallback = Math.max(
      AI_RUNTIME_API_TIMEOUT_MIN_MS,
      Math.min(Math.floor(fallback), AI_RUNTIME_API_TIMEOUT_MAX_MS),
    );
    const parsedDeadline = Number(deadlineMs || 0);
    const nowMs = Date.now();
    if (Number.isFinite(parsedDeadline) && parsedDeadline > nowMs) {
      const remainingMs = Math.floor(parsedDeadline - nowMs);
      return Math.max(
        AI_RUNTIME_API_TIMEOUT_MIN_MS,
        Math.min(remainingMs, AI_RUNTIME_API_TIMEOUT_MAX_MS),
      );
    }
    return safeFallback;
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
    const normalizedParams = normalizeTimeRangeParams(params);
    const response = await this.getWithInflightDedupe(`${API_PREFIX}/logs`, {
      params: normalizedParams,
      timeout: LOGS_API_TIMEOUT_MS,
    });
    const data = ensureArray(response.data);
    const events = data.map((item: LooseAny) => this.transformEvent(item));
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
    const normalizedParams = normalizeTimeRangeParams(params);
    const response = await this.getWithInflightDedupe(`${API_PREFIX}/logs/facets`, {
      params: normalizedParams,
      timeout: LOGS_API_TIMEOUT_MS,
    });
    const servicesRaw = Array.isArray(response.data?.services) ? response.data.services : [];
    const namespacesRaw = Array.isArray(response.data?.namespaces) ? response.data.namespaces : [];
    const levelsRaw = Array.isArray(response.data?.levels) ? response.data.levels : [];
    return {
      services: servicesRaw
        .map((item: LooseAny) => ({
          value: String(item?.value || '').trim(),
          count: Number(item?.count || 0),
        }))
        .filter((item: LogsFacetBucket) => Boolean(item.value)),
      namespaces: namespacesRaw
        .map((item: LooseAny) => ({
          value: String(item?.value || '').trim(),
          count: Number(item?.count || 0),
        }))
        .filter((item: LogsFacetBucket) => Boolean(item.value)),
      levels: levelsRaw
        .map((item: LooseAny) => ({
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
    namespace?: string;
    source_namespace?: string;
    target_namespace?: string;
    time_window?: string;
    anchor_time?: string;
    limit?: number;
    exclude_health_check?: boolean;
  }): Promise<{ data: Event[]; count: number; limit: number; context?: Record<string, LooseAny> }> {
    const normalizedParams = {
      source_service: normalizeText(params.source_service),
      target_service: normalizeText(params.target_service),
      namespace: normalizeText(params.namespace) || undefined,
      source_namespace: normalizeText(params.source_namespace) || undefined,
      target_namespace: normalizeText(params.target_namespace) || undefined,
      time_window: normalizeText(params.time_window) || undefined,
      anchor_time: normalizeAbsoluteTimeParam(params.anchor_time),
      limit: params.limit,
      exclude_health_check: params.exclude_health_check,
    };
    const response = await this.getWithInflightDedupe(`${API_PREFIX}/logs/preview/topology-edge`, {
      params: normalizedParams,
      timeout: LOGS_API_TIMEOUT_MS,
    });
    const raw = ensureArray(response.data);
    const events = raw.map((item: LooseAny) => this.transformEvent(item));
    return {
      data: events,
      count: response.data?.count ?? events.length,
      limit: response.data?.limit ?? normalizedParams.limit ?? 20,
      context: response.data?.context || {},
    };
  }

  /**
   * 获取聚合日志 (智能 Pattern 聚合)
   */
  async getAggregatedLogs(params?: AggregatedLogsParams): Promise<AggregatedLogsResult> {
    const normalizedParams = normalizeTimeRangeParams(params);
    const response = await this.getWithInflightDedupe(`${API_PREFIX}/logs/aggregated`, {
      params: normalizedParams,
      timeout: LOGS_API_TIMEOUT_MS,
    });
    const data = response.data;
    
    if (data.patterns && Array.isArray(data.patterns)) {
      data.patterns = data.patterns.map((pattern: LooseAny) => ({
        ...pattern,
        samples: (pattern.samples || []).map((item: LooseAny) => this.transformEvent(item))
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

  private buildFallbackEventId(item: LooseAny, message: string): string {
    const canonicalServiceName = resolveCanonicalServiceName(item?.service_name, item?.pod_name);
    const seed = [
      String(item?.timestamp || ''),
      String(canonicalServiceName || ''),
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
  private transformEvent(item: LooseAny): Event {
    const parsedMessage = parseLogMessage(item.message || '');
    const resolvedServiceName = resolveCanonicalServiceName(item?.service_name, item?.pod_name);
    const labels = Object.entries(this.parseAttributes(item.labels)).reduce((acc, [rawKey, rawValue]) => {
      const key = String(rawKey || '').trim();
      if (!key) {
        return acc;
      }
      acc[key] = typeof rawValue === 'string' ? rawValue : String(rawValue ?? '');
      return acc;
    }, {} as Record<string, string>);

    const existingAttributes = this.parseAttributes(item.attributes);
    const existingK8s = this.parseAttributes(existingAttributes.k8s);
    const rawLogMeta = this.parseAttributes(item.log_meta);
    const mergedLogMeta = {
      ...parsedMessage.meta,
      ...rawLogMeta,
    };
    const stableId = String(item?.id || '').trim() || this.buildFallbackEventId(item, parsedMessage.message);
    
    return {
      id: stableId,
      timestamp: item.timestamp,
      service_name: resolvedServiceName,
      pod_name: item.pod_name || 'unknown',
      namespace: item.namespace || 'unknown',
      level: this.normalizeLevel(item.level || 'INFO', parsedMessage.message),
      message: parsedMessage.message,
      attributes: {
        ...existingAttributes,
        correlation_request_id: item.correlation_request_id || undefined,
        correlation_trace_id: item.correlation_trace_id || undefined,
        correlation_kind: item.correlation_kind || undefined,
        edge_match_kind: item.edge_match_kind || undefined,
        k8s: {
          ...existingK8s,
          node: item.node_name,
          host: item.node_name,
          host_ip: item.host_ip,
          container_name: item.container_name,
          container_id: item.container_id,
          container_image: item.container_image,
          pod_id: item.pod_id,
          labels: labels,
        },
        trace_id: item.trace_id,
        span_id: item.span_id,
        log_meta: mergedLogMeta,
      },
      trace_id: item.trace_id,
      span_id: item.span_id,
      node_name: item.node_name,
      container_name: item.container_name,
      container_id: item.container_id,
      container_image: item.container_image,
      pod_id: item.pod_id,
      host_ip: item.host_ip,
      labels: labels,
      log_meta: mergedLogMeta,
      message_preview: parsedMessage.summary || parsedMessage.message.split('\n')[0] || '',
      edge_side: item.edge_side || undefined,
      edge_match_kind: item.edge_match_kind || undefined,
      correlation_kind: item.correlation_kind || undefined,
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
        /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:\s+\d+)?\s+(TRACE|DEBUG|INFO|WARN(?:ING)?|ERROR|FATAL)\b/i
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

    const resolveStructuredLevel = (value: string): 'TRACE' | 'DEBUG' | 'INFO' | 'WARN' | 'ERROR' | 'FATAL' | '' => {
      const text = String(value || '').trim();
      if (!text) {
        return '';
      }
      const match = text.match(
        /(?:^|[\s,{])"?(?:level|log_level|severity|severity_text)"?\s*[:=]\s*"?(TRACE|DEBUG|INFO|WARN(?:ING)?|ERROR|FATAL)\b/i,
      );
      if (!match?.[1]) {
        return '';
      }
      const normalized = String(match[1]).toUpperCase();
      if (normalized === 'WARNING') {
        return 'WARN';
      }
      if (['TRACE', 'DEBUG', 'INFO', 'WARN', 'ERROR', 'FATAL'].includes(normalized)) {
        return normalized as 'TRACE' | 'DEBUG' | 'INFO' | 'WARN' | 'ERROR' | 'FATAL';
      }
      return '';
    };

    const resolveWrappedLogLevel = (value: string): 'TRACE' | 'DEBUG' | 'INFO' | 'WARN' | 'ERROR' | 'FATAL' | '' => {
      const text = String(value || '').trim();
      if (!text || !text.startsWith('{')) {
        return '';
      }
      try {
        const parsed = JSON.parse(text);
        if (!parsed || typeof parsed !== 'object') {
          return '';
        }
        const nestedRaw = (parsed as Record<string, LooseAny>).log
          ?? (parsed as Record<string, LooseAny>).message
          ?? (parsed as Record<string, LooseAny>).msg;
        const nestedText = typeof nestedRaw === 'string' ? nestedRaw : '';
        if (!nestedText) {
          return '';
        }
        return resolveStrictLevel(nestedText) || resolveStructuredLevel(nestedText);
      } catch {
        return '';
      }
    };

    const raw = String(level || '').trim();
    const upper = raw.toUpperCase();
    const levelResolved = resolveStrictLevel(raw);
    if (levelResolved && levelResolved !== 'INFO') {
      return levelResolved;
    }
    const messageText = String(message || '').trim();
    const messageResolved =
      resolveStrictLevel(messageText)
      || resolveStructuredLevel(messageText)
      || resolveWrappedLogLevel(messageText);
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

  private parseAttributes(value: LooseAny): Record<string, LooseAny> {
    if (!value) {
      return {};
    }

    if (typeof value === 'string') {
      try {
        const parsed = JSON.parse(value);
        if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
          return parsed as Record<string, LooseAny>;
        }
      } catch {
        return {};
      }
      return {};
    }

    if (typeof value === 'object' && !Array.isArray(value)) {
      return value as Record<string, LooseAny>;
    }

    return {};
  }

  private transformContextLogItem(item: LooseAny): LooseAny {
    if (!item || typeof item !== 'object') {
      return item;
    }

    const parsedMessage = parseLogMessage(item.message || '');
    const resolvedServiceName = resolveCanonicalServiceName(item?.service_name, item?.pod_name);
    const rawMeta = this.parseAttributes(item.log_meta);
    const stableId = String(item?.id || '').trim() || this.buildFallbackEventId(item, parsedMessage.message);

    return {
      ...item,
      id: stableId,
      service_name: resolvedServiceName,
      level: this.normalizeLevel(item.level || 'INFO', parsedMessage.message),
      message: parsedMessage.message,
      log_meta: {
        ...rawMeta,
        ...parsedMessage.meta,
      },
    };
  }

  private transformLogContextResult(data: LooseAny): LooseAny {
    if (!data || typeof data !== 'object') {
      return data;
    }

    const before = ensureArray(data.before).map((item: LooseAny) => this.transformContextLogItem(item));
    const after = ensureArray(data.after).map((item: LooseAny) => this.transformContextLogItem(item));
    const listData = ensureArray(data.data).map((item: LooseAny) => this.transformContextLogItem(item));
    const currentMatches = ensureArray(data.current_matches).map((item: LooseAny) => this.transformContextLogItem(item));
    const current = data.current ? this.transformContextLogItem(data.current) : data.current;

    return {
      ...data,
      before,
      after,
      data: listData,
      current_matches: currentMatches,
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
      const byServiceErrorsRaw = response.data?.byServiceErrors && typeof response.data.byServiceErrors === 'object'
        ? response.data.byServiceErrors
        : {};
      const byService = Object.entries(byServiceRaw).reduce((acc, [key, value]) => {
        const name = String(key || '').trim();
        if (!name) {
          return acc;
        }
        acc[name] = Number(value || 0);
        return acc;
      }, {} as Record<string, number>);
      const byServiceErrors = Object.entries(byServiceErrorsRaw).reduce((acc, [key, value]) => {
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
        byServiceErrors,
        byLevel,
      };
    } catch (error: LooseAny) {
      console.warn('Logs stats API error, returning empty data:', error?.response?.data || error?.message);
      return {
        total: 0,
        byService: {},
        byServiceErrors: {},
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
    const metrics = data.map((item: LooseAny) => ({
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
  async getMetricStats(): Promise<Record<string, LooseAny>> {
    try {
      const response = await this.getWithInflightDedupe(`${API_PREFIX}/metrics/stats`);
      return response.data || { total: 0, byService: {}, byMetricName: {} };
    } catch (error: LooseAny) {
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
  async getTraces(params?: {
    limit?: number;
    offset?: number;
    service_name?: string;
    trace_id?: string;
    start_time?: string;
    end_time?: string;
    time_window?: string;
  }): Promise<{
    traces: Trace[];
    total: number;
    count: number;
    limit: number;
    offset: number;
    has_more: boolean;
    next_offset: number | null;
  }> {
    const normalizedParams = normalizeTimeRangeParams(params);
    const response = await this.getWithInflightDedupe(`${API_PREFIX}/traces`, { params: normalizedParams });
    // 转换后端数据格式到前端期望的格式
    const data = ensureArray(response.data);
    const traces = data.map((item: LooseAny) => ({
      trace_id: item.trace_id,
      service_name: item.service_name || 'unknown',
      operation_name: item.operation_name,
      start_time: item.start_time_str || item.start_time,
      duration_ms: this.resolveTraceDurationMs(item),
      status_code: this.normalizeStatusCode(item.status),
    }));
    const total = Number(response.data?.total ?? response.data?.count ?? traces.length);
    const count = Number(response.data?.count ?? traces.length);
    const pageLimit = Number(response.data?.limit ?? params?.limit ?? count);
    const pageOffset = Number(response.data?.offset ?? params?.offset ?? 0);
    return {
      traces,
      total,
      count,
      limit: pageLimit,
      offset: pageOffset,
      has_more: Boolean(response.data?.has_more ?? pageOffset + count < total),
      next_offset: response.data?.next_offset ?? null,
    };
  }

  /**
   * 获取追踪的所有 spans
   */
  async getTraceSpans(traceId: string): Promise<LooseAny[]> {
    const response = await this.getWithInflightDedupe(`${API_PREFIX}/traces/${traceId}/spans`);
    const data = ensureArray(response.data);
    return data.map((item: LooseAny) => ({
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
  private resolveTraceDurationMs(item: LooseAny): number {
    const parsePositive = (value: LooseAny, scale = 1): number => {
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
  async getTraceStats(params?: { time_window?: string; start_time?: string; end_time?: string }): Promise<Record<string, LooseAny>> {
    try {
      const normalizedParams = normalizeTimeRangeParams(params);
      const response = await this.getWithInflightDedupe(`${API_PREFIX}/traces/stats`, { params: normalizedParams });
      return response.data;
    } catch (error: LooseAny) {
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
  }): Promise<{ data: TraceLiteFragment[]; count: number; stats?: Record<string, LooseAny> }> {
    const response = await this.getWithInflightDedupe(`${API_PREFIX}/trace-lite/inferred`, { params });
    return response.data || { data: [], count: 0, stats: {} };
  }

  /**
   * 获取推断质量指标
   */
  async getInferenceQuality(params?: { time_window?: string }): Promise<Record<string, LooseAny>> {
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
  }): Promise<Record<string, LooseAny>> {
    const response = await this.getWithInflightDedupe(`${API_PREFIX}/quality/inference/alerts`, { params });
    return response.data || { status: 'error', alerts: [] };
  }

  /**
   * 设置推断质量告警抑制
   */
  async setInferenceAlertSuppression(metric: string, enabled: boolean): Promise<Record<string, LooseAny>> {
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
    return transformTopologyGraph(response.data);
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
      return transformTopologyGraph(response.data);
    } catch (error: LooseAny) {
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
  async getTopologyStats(params?: { time_window?: string }): Promise<Record<string, LooseAny>> {
    try {
      const response = await this.getWithInflightDedupe(`${API_PREFIX}/topology/stats`, { params });
      return response.data;
    } catch (error: LooseAny) {
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
    source_service?: string;
    target_service?: string;
    namespace?: string;
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
    } catch (error: LooseAny) {
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
  async getAlertEvents(params?: { limit?: number; status?: string; severity?: string; cursor?: string; service_name?: string; source_service?: string; target_service?: string; namespace?: string; search?: string; scope?: 'all' | 'edge' | 'service' }): Promise<AlertEventsResponse> {
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
  async getAlertStats(): Promise<Record<string, LooseAny>> {
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
  async discoverLabels(params?: { limit?: number }): Promise<Record<string, LooseAny>> {
    const response = await this.getWithInflightDedupe(`${API_PREFIX}/labels/discover`, { params });
    return response.data;
  }

  /**
   * 获取标签建议
   */
  async getLabelSuggestions(params?: { service_name?: string }): Promise<Record<string, LooseAny>> {
    const response = await this.getWithInflightDedupe(`${API_PREFIX}/labels/suggestions`, { params });
    return response.data;
  }

  /**
   * 发送 OTLP 日志
   */
  async sendOTLPLogs(logs: LooseAny): Promise<{ status: string; processed: number }> {
    const response = await this.client.post('/v1/logs', logs);
    return response.data;
  }

  /**
   * 发送 OTLP 指标
   */
  async sendOTLPMetrics(metrics: LooseAny): Promise<{ status: string; processed: number }> {
    const response = await this.client.post('/v1/metrics', metrics);
    return response.data;
  }

  /**
   * 发送 OTLP 追踪
   */
  async sendOTLPTraces(traces: LooseAny): Promise<{ status: string; processed: number }> {
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
    container_name?: string;
    timestamp?: string; 
    before_count?: number; 
    after_count?: number; 
    limit?: number 
  }): Promise<{ 
    log_id?: string;
    trace_id?: string; 
    pod_name?: string;
    namespace?: string;
    container_name?: string;
    timestamp?: string;
    data?: LooseAny[]; 
    before?: LooseAny[];
    after?: LooseAny[];
    current?: LooseAny;
    current_matches?: LooseAny[];
    current_count?: number;
    count?: number; 
    before_count?: number;
    after_count?: number;
    limit?: number 
  }> {
    const normalizedTimestamp = normalizeAbsoluteTimeParam(params.timestamp);
    const normalizedParams = {
      log_id: normalizeText(params.log_id) || undefined,
      trace_id: normalizeText(params.trace_id) || undefined,
      pod_name: normalizeText(params.pod_name) || undefined,
      namespace: normalizeText(params.namespace) || undefined,
      container_name: normalizeText(params.container_name) || undefined,
      timestamp: normalizedTimestamp || undefined,
      before_count: params.before_count,
      after_count: params.after_count,
      limit: params.limit,
    };
    const response = await this.getWithInflightDedupe(`${API_PREFIX}/logs/context`, { params: normalizedParams });
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
    dataFlow?: {
      summary?: string;
      path?: Array<{
        step?: number;
        component?: string;
        operation?: string;
        status?: string;
        evidence?: string;
        from?: string;
        to?: string;
        latency_ms?: number;
      }>;
      evidence?: string[];
      confidence?: number;
    };
    rootCauses?: Array<{
      title: string;
      description: string;
    }>;
    handlingIdeas?: Array<{
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
    const response = await this.client.post(`${API_PREFIX}/ai/analyze-log`, requestData, {
      timeout: AI_ANALYSIS_TIMEOUT_MS,
    });
    return response.data;
  }

  /**
   * AI 分析日志 (使用 LLM 大模型)
   */
  async analyzeLogLLM(params: {
    log_content: string;
    service_name?: string;
    context?: Record<string, LooseAny>;
    use_llm?: boolean;
    enable_agent?: boolean;
    enable_web_search?: boolean;
  }): Promise<{
    overview?: {
      problem: string;
      severity: string;
      description: string;
      confidence: number;
    };
    dataFlow?: {
      summary?: string;
      path?: Array<{
        step?: number;
        component?: string;
        operation?: string;
        status?: string;
        evidence?: string;
        from?: string;
        to?: string;
        latency_ms?: number;
      }>;
      evidence?: string[];
      confidence?: number;
    };
    rootCauses?: Array<{
      title: string;
      description: string;
    }>;
    handlingIdeas?: Array<{
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
    const response = await this.client.post(`${API_PREFIX}/ai/analyze-log-llm`, params, {
      timeout: AI_ANALYSIS_TIMEOUT_MS,
    });
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
    dataFlow?: {
      summary?: string;
      path?: Array<{
        step?: number;
        component?: string;
        operation?: string;
        status?: string;
        evidence?: string;
        from?: string;
        to?: string;
        latency_ms?: number;
      }>;
      evidence?: string[];
      confidence?: number;
    };
    rootCauses?: Array<{
      title: string;
      description: string;
    }>;
    handlingIdeas?: Array<{
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
    const response = await this.client.post(`${API_PREFIX}/ai/analyze-trace`, payload, {
      timeout: AI_ANALYSIS_TIMEOUT_MS,
    });
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
    dataFlow?: {
      summary?: string;
      path?: Array<{
        step?: number;
        component?: string;
        operation?: string;
        status?: string;
        evidence?: string;
        from?: string;
        to?: string;
        latency_ms?: number;
      }>;
      evidence?: string[];
      confidence?: number;
    };
    rootCauses?: Array<{
      title: string;
      description: string;
    }>;
    handlingIdeas?: Array<{
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
    const response = await this.client.post(`${API_PREFIX}/ai/analyze-trace-llm`, params, {
      timeout: AI_ANALYSIS_TIMEOUT_MS,
    });
    return response.data;
  }

  /**
   * 查找相似知识条目
   */
  async findSimilarCases(params: {
    log_content: string;
    service_name?: string;
    problem_type?: string;
    context?: Record<string, LooseAny>;
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
        changes?: Record<string, { before?: LooseAny; after?: LooseAny }>;
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
    context?: Record<string, LooseAny>;
    llm_provider?: string;
    llm_model?: string;
    llm_metadata?: Record<string, LooseAny>;
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
  async getKBRuntimeStatus(): Promise<Record<string, LooseAny>> {
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
    extra?: Record<string, LooseAny>;
  }): Promise<Record<string, LooseAny>> {
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
    extra?: Record<string, LooseAny>;
  }): Promise<Record<string, LooseAny>> {
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
    retrieval_mode: 'local' | 'hybrid' | 'remote_only';
    save_mode: 'local_only' | 'local_and_remote';
  }): Promise<{
    effective_retrieval_mode: 'local' | 'hybrid' | 'remote_only';
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
    retrieval_mode?: 'local' | 'hybrid' | 'remote_only';
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
    effective_mode: 'local' | 'hybrid' | 'remote_only';
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
    history?: Array<{ role: 'user' | 'assistant'; content: string; timestamp?: string; message_id?: string; metadata?: Record<string, LooseAny> }>;
    use_llm?: boolean;
    save_mode?: 'local_only' | 'local_and_remote';
    remote_enabled?: boolean;
  }): Promise<{
    draft_case: Record<string, LooseAny>;
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
    context: Record<string, LooseAny>;
    resolved: boolean;
    resolution?: string;
    tags: string[];
    created_at: string;
    updated_at?: string;
    resolved_at?: string;
    llm_provider?: string;
    llm_model?: string;
    llm_metadata?: Record<string, LooseAny>;
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
      changes?: Record<string, { before?: LooseAny; after?: LooseAny }>;
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
    history_entry?: Record<string, LooseAny>;
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
    context?: Record<string, LooseAny>;
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
      metadata?: Record<string, LooseAny>;
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
    params: { action_type: 'ticket' | 'runbook' | 'alert_suppression'; title?: string; extra?: Record<string, LooseAny> }
  ): Promise<{
    status: string;
    session_id: string;
    message_id: string;
    action_id: string;
    action: {
      action_type: string;
      title: string;
      payload: Record<string, LooseAny>;
    };
  }> {
    const response = await this.client.post(
      `${API_PREFIX}/ai/history/${encodeURIComponent(sessionId)}/messages/${encodeURIComponent(messageId)}/actions`,
      params
    );
    return response.data;
  }

  /**
   * 执行追问回答中提取出的命令（含确认与权限提示）
   */
  async executeFollowUpCommand(
    sessionId: string,
    messageId: string,
    params: {
      command: string;
      purpose?: string;
      title?: string;
      confirmed?: boolean;
      elevated?: boolean;
      confirmation_ticket?: string;
      timeout_seconds?: number;
      client_deadline_ms?: number;
      command_spec?: Record<string, unknown>;
    }
  ): Promise<{
    status: 'confirmation_required' | 'elevation_required' | 'permission_required' | 'executed' | 'failed' | string;
    session_id: string;
    message_id: string;
    command: string;
    command_type?: 'query' | 'repair' | 'unknown' | string;
    risk_level?: 'low' | 'high' | string;
    requires_confirmation?: boolean;
    requires_write_permission?: boolean;
    requires_elevation?: boolean;
    confirmation_message?: string;
    confirmation_ticket?: string;
    ticket_expires_at?: string;
    ticket_ttl_seconds?: number;
    message?: string;
    exit_code?: number;
    duration_ms?: number;
    stdout?: string;
    stderr?: string;
    output_truncated?: boolean;
    timed_out?: boolean;
  }> {
    const safeCommand = String(params?.command || '').trim();
    const safeTimeout = Number.isFinite(Number(params?.timeout_seconds))
      ? Math.max(3, Math.min(180, Math.floor(Number(params?.timeout_seconds))))
      : 20;
    const normalizedClientDeadlineMs = Number(params?.client_deadline_ms || 0);
    const clientDeadlineMs = normalizedClientDeadlineMs > Date.now()
      ? normalizedClientDeadlineMs
      : resolveRuntimeClientDeadlineMs(AI_RUNTIME_API_TIMEOUT_MS);
    const requestTimeoutMs = this.resolveAIRuntimeRequestTimeoutMs(clientDeadlineMs);
    const safePayload = {
      ...params,
      command: safeCommand,
      timeout_seconds: safeTimeout,
      client_deadline_ms: clientDeadlineMs,
      command_spec: (
        params?.command_spec
        && typeof params.command_spec === 'object'
        && !Array.isArray(params.command_spec)
      )
        ? params.command_spec
        : buildRuntimeCommandSpec({
          command: safeCommand,
          purpose: String(params?.purpose || '').trim() || undefined,
          title: String(params?.title || '').trim() || undefined,
          timeoutSeconds: safeTimeout,
        }),
    };
    const response = await this.client.post(
      `${API_PREFIX}/ai/history/${encodeURIComponent(sessionId)}/messages/${encodeURIComponent(messageId)}/commands/execute`,
      safePayload,
      {
        timeout: requestTimeoutMs,
      },
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
    show_thought?: boolean;
    analysis_context?: Record<string, LooseAny>;
    history?: FollowUpHistoryMessage[];
    reset?: boolean;
  }): Promise<FollowUpAnalysisResponsePayload> {
    const configuredTimeoutMs = Number((import.meta as LooseAny)?.env?.VITE_AI_FOLLOWUP_TIMEOUT_MS);
    const timeoutMs = Number.isFinite(configuredTimeoutMs) && configuredTimeoutMs > 0
      ? configuredTimeoutMs
      : 180000;
    const response = await this.client.post(`${API_PREFIX}/ai/v2/follow-up`, params, { timeout: timeoutMs });
    return response.data;
  }

  async followUpAnalysisStream(
    params: {
      question: string;
      analysis_session_id?: string;
      conversation_id?: string;
      use_llm?: boolean;
      show_thought?: boolean;
      analysis_context?: Record<string, LooseAny>;
      history?: FollowUpHistoryMessage[];
      reset?: boolean;
    },
    options?: {
      onEvent?: (payload: FollowUpStreamEventPayload) => void;
    },
  ): Promise<FollowUpAnalysisResponsePayload> {
    // Legacy alias retained for compatibility; runtime stream is unified on v2 endpoint.
    return this.followUpAnalysisStreamV2(params, options);
  }

  async followUpAnalysisStreamV2(
    params: {
      question: string;
      analysis_session_id?: string;
      conversation_id?: string;
      use_llm?: boolean;
      show_thought?: boolean;
      analysis_context?: Record<string, LooseAny>;
      history?: FollowUpHistoryMessage[];
      reset?: boolean;
    },
    options?: {
      onEvent?: (payload: FollowUpStreamEventPayload) => void;
    },
  ): Promise<FollowUpAnalysisResponsePayload> {
    const configuredTimeoutMs = Number((import.meta as LooseAny)?.env?.VITE_AI_FOLLOWUP_TIMEOUT_MS);
    const timeoutMs = Number.isFinite(configuredTimeoutMs) && configuredTimeoutMs > 0
      ? configuredTimeoutMs
      : 180000;
    const controller = new AbortController();
    const timerId = window.setTimeout(() => controller.abort(), timeoutMs);
    const streamUrl = `${API_BASE_URL}${API_PREFIX}/ai/v2/follow-up/stream`;
    let response: Response;
    try {
      response = await fetch(streamUrl, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(params || {}),
        signal: controller.signal,
      });
    } catch (error) {
      window.clearTimeout(timerId);
      throw error;
    }
    if (!response.ok) {
      window.clearTimeout(timerId);
      let detail = `HTTP ${response.status}`;
      try {
        const payload = await response.json();
        const payloadDetail = payload && typeof payload === 'object' ? (payload as Record<string, LooseAny>).detail : '';
        if (typeof payloadDetail === 'string' && payloadDetail.trim()) {
          detail = payloadDetail.trim();
        }
      } catch (_err) {
        // ignore json parse failure
      }
      const err: LooseAny = new Error(detail);
      err.response = { status: response.status, data: { detail } };
      throw err;
    }
    if (!response.body) {
      window.clearTimeout(timerId);
      throw new Error('follow-up v2 stream has no response body');
    }

    const decoder = new TextDecoder('utf-8');
    const reader = response.body.getReader();
    const emit = (event: string, data: Record<string, LooseAny>) => {
      if (typeof options?.onEvent === 'function') {
        options.onEvent({ event, data });
      }
    };

    let buffer = '';
    let finalPayload: FollowUpAnalysisResponsePayload | null = null;
    const parseEventBlock = (block: string): { event: string; data: Record<string, LooseAny> } | null => {
      const lines = block.split(/\r?\n/);
      let eventName = 'message';
      const dataLines: string[] = [];
      lines.forEach((line) => {
        if (line.startsWith('event:')) {
          eventName = line.slice(6).trim() || 'message';
          return;
        }
        if (line.startsWith('data:')) {
          dataLines.push(line.slice(5).trimStart());
        }
      });
      if (!dataLines.length) {
        return null;
      }
      const dataText = dataLines.join('\n');
      try {
        const data = JSON.parse(dataText);
        return { event: eventName, data: data && typeof data === 'object' ? data : {} };
      } catch (_err) {
        return { event: eventName, data: {} };
      }
    };
    const takeNextEventBlock = (): string | null => {
      const separator = /\r?\n\r?\n/.exec(buffer);
      if (!separator || typeof separator.index !== 'number') {
        return null;
      }
      const rawBlock = buffer.slice(0, separator.index).trim();
      buffer = buffer.slice(separator.index + separator[0].length);
      return rawBlock;
    };
    const mapV2Event = (
      eventNameRaw: string,
      dataRaw: Record<string, LooseAny>,
    ): { event: string; data: Record<string, LooseAny> } => {
      const eventName = String(eventNameRaw || '').trim().toLowerCase();
      const data = dataRaw && typeof dataRaw === 'object' ? dataRaw : {};
      if (eventName === 'answer_delta') {
        return { event: 'token', data: { text: String(data.text || '') } };
      }
      if (eventName === 'thought_delta') {
        return { event: 'thought', data };
      }
      if (eventName === 'action_proposed') {
        return {
          event: 'action',
          data: {
            message_id: String(data.message_id || ''),
            actions: Array.isArray(data.actions) ? data.actions : [],
          },
        };
      }
      if (eventName === 'plan_started' || eventName === 'plan_updated') {
        const detail = (data.detail && typeof data.detail === 'object') ? data.detail as Record<string, LooseAny> : {};
        return {
          event: 'plan',
          data: {
            stage: String(data.stage || detail.stage || eventName),
            ...detail,
          },
        };
      }
      if (eventName === 'command_precheck_result') {
        const result = (data.result && typeof data.result === 'object') ? data.result as Record<string, LooseAny> : {};
        return {
          event: 'observation',
          data: {
            status: String(result.status || 'precheck'),
            command: String(data.command || result.command || ''),
            command_type: String(result.command_type || data.command_type || ''),
            risk_level: String(result.risk_level || ''),
            message: String(result.message || ''),
            message_id: String(data.message_id || ''),
            action_id: String(data.action_id || ''),
          },
        };
      }
      if (eventName === 'approval_required') {
        const precheck = (data.precheck && typeof data.precheck === 'object') ? data.precheck as Record<string, LooseAny> : {};
        return {
          event: 'approval_required',
          data: {
            status: String(precheck.status || 'elevation_required'),
            command: String(data.command || precheck.command || ''),
            message: String(precheck.message || '需要审批后执行'),
            message_id: String(data.message_id || ''),
            action_id: String(data.action_id || ''),
            command_type: String(precheck.command_type || data.command_type || ''),
            risk_level: String(precheck.risk_level || ''),
            requires_elevation: Boolean(precheck.requires_elevation),
            requires_confirmation: Boolean(precheck.requires_confirmation),
            confirmation_ticket: String(precheck.confirmation_ticket || ''),
          },
        };
      }
      if (eventName === 'command_observation') {
        const observation = (data.observation && typeof data.observation === 'object')
          ? data.observation as Record<string, LooseAny>
          : {};
        return { event: 'observation', data: observation };
      }
      if (eventName === 'replan') {
        return { event: 'replan', data: data.detail && typeof data.detail === 'object' ? data.detail as Record<string, LooseAny> : data };
      }
      if (eventName === 'final') {
        const maybeResult = (data.result && typeof data.result === 'object') ? data.result as Record<string, LooseAny> : data;
        return { event: 'final', data: maybeResult };
      }
      return { event: eventName, data };
    };
    const applyParsedEvent = (parsed: { event: string; data: Record<string, LooseAny> } | null) => {
      if (!parsed) {
        return;
      }
      const mapped = mapV2Event(parsed.event, parsed.data);
      emit(mapped.event, mapped.data);
      if (mapped.event === 'error') {
        const statusCode = Number(mapped.data.status_code || 500);
        const detail = String(mapped.data.detail || 'stream error');
        const err: LooseAny = new Error(detail);
        err.response = { status: statusCode, data: { detail } };
        throw err;
      }
      if (mapped.event === 'final') {
        finalPayload = mapped.data as FollowUpAnalysisResponsePayload;
      }
    };

    try {
      let streamDone = false;
      while (!streamDone) {
        const { done, value } = await reader.read();
        streamDone = done;
        buffer += decoder.decode(value || new Uint8Array(), { stream: !streamDone });
        let rawBlock = takeNextEventBlock();
        while (rawBlock !== null) {
          applyParsedEvent(parseEventBlock(rawBlock));
          rawBlock = takeNextEventBlock();
        }
      }
      const trailingBlock = buffer.trim();
      if (trailingBlock) {
        applyParsedEvent(parseEventBlock(trailingBlock));
      }
    } finally {
      window.clearTimeout(timerId);
      reader.releaseLock();
    }

    if (!finalPayload) {
      throw new Error('follow-up v2 stream ended without final payload');
    }
    return finalPayload;
  }

  async createAIRun(params: AgentRunCreateRequest): Promise<{ run: AgentRunSnapshot }> {
    const safeParams: AgentRunCreateRequest = {
      session_id: typeof params?.session_id === 'string' ? params.session_id : '',
      question: String(params?.question || ''),
      analysis_context: params?.analysis_context && typeof params.analysis_context === 'object'
        ? params.analysis_context
        : {},
      runtime_options: params?.runtime_options && typeof params.runtime_options === 'object'
        ? params.runtime_options
        : {},
      client_deadline_ms: Number.isFinite(Number(params?.client_deadline_ms))
        ? Number(params?.client_deadline_ms)
        : 0,
      pipeline_steps: Array.isArray(params?.pipeline_steps)
        ? params.pipeline_steps.filter((item) => item && typeof item === 'object')
        : [],
    };
    const normalizedClientDeadlineMs = Number(safeParams.client_deadline_ms || 0);
    const clientDeadlineMs = normalizedClientDeadlineMs > Date.now()
      ? normalizedClientDeadlineMs
      : resolveRuntimeClientDeadlineMs();
    const runtimeRequestTimeoutMs = this.resolveAIRuntimeRequestTimeoutMs(clientDeadlineMs);
    let threadId = await this.ensureAIRuntimeThreadId(safeParams, {
      deadlineMs: clientDeadlineMs,
      timeoutMs: runtimeRequestTimeoutMs,
    });
    const idempotencyKey = this.buildAIRuntimeRunIdempotencyKey(safeParams, threadId);
    const runPayload = {
      question: safeParams.question,
      analysis_context: safeParams.analysis_context || {},
      runtime_options: safeParams.runtime_options || {},
      idempotency_key: idempotencyKey,
      client_deadline_ms: clientDeadlineMs,
      pipeline_steps: safeParams.pipeline_steps || [],
    };
    const retryDelaysMs = this.resolveAIRuntimeCreateRunRetryDelaysMs();
    const maxAttempts = 1 + retryDelaysMs.length;
    const threadCacheKey = this.buildAIRuntimeThreadCacheKey(safeParams);
    let threadRecreated = false;

    for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
      try {
        const response = await this.client.post(
          `/api/v2/threads/${encodeURIComponent(threadId)}/runs`,
          runPayload,
          {
            timeout: runtimeRequestTimeoutMs,
          },
        );
        return response.data;
      } catch (error) {
        const status = Number((error as LooseAny)?.response?.status || 0);
        if (status === 404 && !threadRecreated) {
          this.aiRuntimeThreadCache.delete(threadCacheKey);
          threadId = await this.ensureAIRuntimeThreadId(safeParams, {
            deadlineMs: clientDeadlineMs,
            timeoutMs: runtimeRequestTimeoutMs,
          });
          threadRecreated = true;
          continue;
        }
        const retryAfterMs = this.parseAIRuntimeCreateRunRetryAfterMs(error);
        if (retryAfterMs <= 0 || attempt >= maxAttempts) {
          throw error;
        }
        const configuredDelay = retryDelaysMs[Math.min(attempt - 1, retryDelaysMs.length - 1)] || 0;
        await this.sleepMs(Math.max(configuredDelay, retryAfterMs));
      }
    }

    throw new Error('runtime v2 create run retry exhausted');
  }

  async getAIRun(runId: string): Promise<{ run: AgentRunSnapshot }> {
    const response = await this.client.get(`/api/v2/runs/${encodeURIComponent(runId)}`, {
      timeout: this.resolveAIRuntimeRequestTimeoutMs(),
    });
    return response.data;
  }

  async getAIRunEvents(
    runId: string,
    options?: { afterSeq?: number; limit?: number; visibility?: AIRuntimeEventVisibility },
  ): Promise<AgentRunEventsResponse> {
    const query = new URLSearchParams();
    if (Number.isFinite(Number(options?.afterSeq))) {
      query.set('after_seq', String(Math.max(0, Math.floor(Number(options?.afterSeq)))));
    }
    if (Number.isFinite(Number(options?.limit))) {
      query.set('limit', String(Math.max(1, Math.floor(Number(options?.limit)))));
    }
    query.set('visibility', normalizeAIRuntimeEventVisibility(options?.visibility || AI_RUNTIME_EVENT_VISIBILITY_DEFAULT));
    const suffix = query.toString() ? `?${query.toString()}` : '';
    const response = await this.client.get(`/api/v2/runs/${encodeURIComponent(runId)}/events${suffix}`, {
      timeout: this.resolveAIRuntimeRequestTimeoutMs(),
    });
    const payload = response.data && typeof response.data === 'object' ? response.data as Record<string, LooseAny> : {};
    return {
      run_id: String(payload.run_id || runId),
      next_after_seq: Number(payload.next_after_seq || 0),
      events: Array.isArray(payload.events)
        ? payload.events
          .map((item) => normalizeAgentRunEventEnvelope(item))
          .filter((item): item is AgentRunEventEnvelope => Boolean(item))
        : [],
    };
  }

  async streamAIRun(
    runId: string,
    options?: {
      afterSeq?: number;
      onEvent?: (payload: AgentRunStreamEventPayload) => void;
      signal?: AbortSignal;
      timeoutMs?: number;
      deadlineMs?: number;
      visibility?: AIRuntimeEventVisibility;
    },
  ): Promise<void> {
    const nowMs = Date.now();
    const deadlineMs = Number.isFinite(Number(options?.deadlineMs)) ? Number(options?.deadlineMs) : 0;
    const remainingByDeadline = deadlineMs > nowMs ? (deadlineMs - nowMs) : 0;
    const timeoutMs = Number.isFinite(Number(options?.timeoutMs)) && Number(options?.timeoutMs) > 0
      ? Number(options?.timeoutMs)
      : remainingByDeadline > 0
      ? remainingByDeadline
      : 180000;
    const streamUrl = new URL(`${API_BASE_URL}/api/v2/runs/${encodeURIComponent(runId)}/events/stream`, window.location.origin);
    if (Number.isFinite(Number(options?.afterSeq)) && Number(options?.afterSeq) > 0) {
      streamUrl.searchParams.set('after_seq', String(Math.floor(Number(options?.afterSeq))));
    }
    streamUrl.searchParams.set(
      'visibility',
      normalizeAIRuntimeEventVisibility(options?.visibility || AI_RUNTIME_EVENT_VISIBILITY_DEFAULT),
    );
    const controller = new AbortController();
    const timerId = window.setTimeout(() => controller.abort(), timeoutMs);
    const onAbort = () => controller.abort();
    if (options?.signal) {
      if (options.signal.aborted) {
        controller.abort();
      } else {
        options.signal.addEventListener('abort', onAbort, { once: true });
      }
    }

    let response: Response;
    try {
      response = await fetch(streamUrl.toString(), {
        method: 'GET',
        headers: {
          Accept: 'text/event-stream',
          ...(deadlineMs > 0 ? { 'X-Client-Deadline-Ms': String(deadlineMs) } : {}),
        },
        signal: controller.signal,
      });
    } catch (error) {
      window.clearTimeout(timerId);
      if (options?.signal) {
        options.signal.removeEventListener('abort', onAbort);
      }
      throw error;
    }

    if (!response.ok) {
      window.clearTimeout(timerId);
      if (options?.signal) {
        options.signal.removeEventListener('abort', onAbort);
      }
      let detail = `HTTP ${response.status}`;
      try {
        const payload = await response.json();
        const payloadDetail = payload && typeof payload === 'object' ? (payload as Record<string, LooseAny>).detail : '';
        if (typeof payloadDetail === 'string' && payloadDetail.trim()) {
          detail = payloadDetail.trim();
        }
      } catch (_error) {
        // ignore json parse failure
      }
      const err: LooseAny = new Error(detail);
      err.response = { status: response.status, data: { detail } };
      throw err;
    }
    if (!response.body) {
      window.clearTimeout(timerId);
      if (options?.signal) {
        options.signal.removeEventListener('abort', onAbort);
      }
      throw new Error('ai runtime stream has no response body');
    }

    const decoder = new TextDecoder('utf-8');
    const reader = response.body.getReader();
    let buffer = '';
    const emit = (event: AgentRuntimeEventType, data: AgentRunEventEnvelope | Record<string, unknown>) => {
      if (typeof options?.onEvent === 'function') {
        options.onEvent({ event, data });
      }
    };

    const applyParsedEvent = (parsed: ReturnType<typeof parseAgentRuntimeEventBlock>) => {
      if (!parsed) {
        return;
      }
      const normalizedEvent = normalizeAgentRunEventEnvelope(parsed.data);
      const eventName = String(parsed.event || '').trim() || 'message';
      emit(eventName, normalizedEvent || parsed.data);
      if (eventName === 'error') {
        const statusCode = Number((parsed.data as Record<string, unknown>).status_code || 500);
        const detail = String((parsed.data as Record<string, unknown>).detail || 'stream error');
        const err: LooseAny = new Error(detail);
        err.response = { status: statusCode, data: { detail } };
        throw err;
      }
    };

    try {
      let streamDone = false;
      while (!streamDone) {
        const { done, value } = await reader.read();
        streamDone = done;
        buffer += decoder.decode(value || new Uint8Array(), { stream: !streamDone });
        let next = takeNextSSEEventBlock(buffer);
        while (next.block !== null) {
          buffer = next.rest;
          applyParsedEvent(parseAgentRuntimeEventBlock(next.block));
          next = takeNextSSEEventBlock(buffer);
        }
      }
      const trailingBlock = buffer.trim();
      if (trailingBlock) {
        applyParsedEvent(parseAgentRuntimeEventBlock(trailingBlock));
      }
    } finally {
      window.clearTimeout(timerId);
      if (options?.signal) {
        options.signal.removeEventListener('abort', onAbort);
      }
      reader.releaseLock();
    }
  }

  async cancelAIRun(runId: string, params?: { reason?: string }): Promise<{ run: AgentRunSnapshot }> {
    const response = await this.client.post(
      `/api/v2/runs/${encodeURIComponent(runId)}/cancel`,
      params || {},
      {
        timeout: this.resolveAIRuntimeRequestTimeoutMs(),
      },
    );
    return response.data;
  }

  async interruptAIRun(
    runId: string,
    params?: AgentRunInterruptRequest,
  ): Promise<{ run: AgentRunSnapshot }> {
    const response = await this.client.post(
      `/api/v2/runs/${encodeURIComponent(runId)}/interrupt`,
      params || {},
      {
        timeout: this.resolveAIRuntimeRequestTimeoutMs(),
      },
    );
    return response.data;
  }

  async approveAIRun(
    runId: string,
    params: AgentRunApproveRequest,
  ): Promise<{ run: AgentRunSnapshot; approval?: Record<string, unknown>; command?: Record<string, unknown> }> {
    const approvalId = String(params?.approval_id || '').trim();
    if (!approvalId) {
      throw new Error('approval_id is required');
    }
    const response = await this.client.post(
      `/api/v2/runs/${encodeURIComponent(runId)}/approvals/${encodeURIComponent(approvalId)}/resolve`,
      {
        decision: params.decision,
        comment: params.comment,
        confirmed: params.confirmed,
        elevated: params.elevated,
      },
      {
        timeout: this.resolveAIRuntimeRequestTimeoutMs(),
      },
    );
    return response.data;
  }

  async executeAIRunCommand(
    runId: string,
    params: AgentRunCommandRequest,
  ): Promise<Record<string, unknown>> {
    const safeCommand = String(params?.command || '').trim();
    const safePurpose = String(params?.purpose || '').trim() || safeCommand;
    const safeTimeout = Number.isFinite(Number(params?.timeout_seconds))
      ? Math.max(3, Math.min(180, Math.floor(Number(params?.timeout_seconds))))
      : 20;
    const normalizedClientDeadlineMs = Number(params?.client_deadline_ms || 0);
    const clientDeadlineMs = normalizedClientDeadlineMs > Date.now()
      ? normalizedClientDeadlineMs
      : resolveRuntimeClientDeadlineMs(AI_RUNTIME_API_TIMEOUT_MS);
    const timeoutMs = this.resolveAIRuntimeRequestTimeoutMs(clientDeadlineMs);
    const safePayload = {
      ...params,
      command: safeCommand,
      purpose: safePurpose,
      timeout_seconds: safeTimeout,
      client_deadline_ms: clientDeadlineMs,
      command_spec: (
        params?.command_spec
        && typeof params.command_spec === 'object'
        && !Array.isArray(params.command_spec)
      )
        ? params.command_spec
        : buildRuntimeCommandSpec({
          command: safeCommand,
          purpose: safePurpose || undefined,
          title: String(params?.title || '').trim() || undefined,
          timeoutSeconds: safeTimeout,
          stepId: String(params?.step_id || '').trim() || undefined,
        }),
    };
    const response = await this.client.post(
      `/api/v2/runs/${encodeURIComponent(runId)}/actions/command`,
      safePayload,
      {
        timeout: timeoutMs,
      },
    );
    return response.data;
  }

  async continueAIRunWithInput(
    runId: string,
    params: AgentRunInputRequest,
  ): Promise<{ run: AgentRunSnapshot; user_input?: Record<string, unknown> }> {
    const response = await this.client.post(
      `/api/v2/runs/${encodeURIComponent(runId)}/input`,
      params,
      {
        timeout: this.resolveAIRuntimeRequestTimeoutMs(),
      },
    );
    return response.data;
  }

  async getExecExecutorStatus(): Promise<ExecExecutorStatusResponse> {
    const response = await this.client.get(`${API_PREFIX}/exec/executors`);
    const payload = response.data && typeof response.data === 'object' ? response.data as Record<string, LooseAny> : {};
    return {
      total: Number(payload.total || 0),
      ready: Number(payload.ready || 0),
      rows: Array.isArray(payload.rows)
        ? payload.rows.map((item) => ({
          executor_type: String(item?.executor_type || 'local_process'),
          executor_profile: String(item?.executor_profile || 'local-default'),
          target_kind: String(item?.target_kind || 'runtime_node'),
          target_identity: String(item?.target_identity || 'runtime:local'),
          candidate_template_envs: Array.isArray(item?.candidate_template_envs)
            ? item.candidate_template_envs.map((value: unknown) => String(value))
            : [],
          rollout_stage: item?.rollout_stage ? String(item.rollout_stage) : undefined,
          summary: item?.summary ? String(item.summary) : undefined,
          example_template: item?.example_template ? String(item.example_template) : undefined,
          dispatch_backend: String(item?.dispatch_backend || 'local_fallback'),
          dispatch_mode: String(item?.dispatch_mode || 'local_process'),
          dispatch_reason: item?.dispatch_reason ? String(item.dispatch_reason) : undefined,
          dispatch_template_env: item?.dispatch_template_env ? String(item.dispatch_template_env) : undefined,
          dispatch_requires_template: Boolean(item?.dispatch_requires_template),
          dispatch_ready: Boolean(item?.dispatch_ready),
          dispatch_degraded: Boolean(item?.dispatch_degraded),
          effective_executor_type: String(item?.effective_executor_type || 'local_process'),
          effective_executor_profile: String(item?.effective_executor_profile || 'local-fallback'),
        }))
        : [],
      generated_at: payload.generated_at ? String(payload.generated_at) : undefined,
    };
  }

  /**
   * 获取 LLM 运行时状态（预留本地 LLM 接入接口）
   */
  async getLLMRuntimeStatus(): Promise<Record<string, LooseAny>> {
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
    extra?: Record<string, LooseAny>;
  }): Promise<Record<string, LooseAny>> {
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
    extra?: Record<string, LooseAny>;
  }): Promise<Record<string, LooseAny>> {
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
    } catch (error: LooseAny) {
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
  async getCacheStats(): Promise<Record<string, LooseAny>> {
    const response = await this.client.get(`${API_PREFIX}/cache/stats`);
    return response.data;
  }

  /**
   * 获取去重统计
   */
  async getDeduplicationStats(): Promise<Record<string, LooseAny>> {
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
  getTopologyEdgeLogPreview: (params: {
    source_service: string;
    target_service: string;
    namespace?: string;
    source_namespace?: string;
    target_namespace?: string;
    time_window?: string;
    anchor_time?: string;
    limit?: number;
    exclude_health_check?: boolean;
  }) => apiClient.getTopologyEdgeLogPreview(params),
  getAggregatedLogs: (params?: AggregatedLogsParams) => apiClient.getAggregatedLogs(params),
  getMetrics: (params?: LooseAny) => apiClient.getMetrics(params),
  getMetricStats: () => apiClient.getMetricStats(),
  getTraces: (params?: LooseAny) => apiClient.getTraces(params),
  getTraceStats: (params?: LooseAny) => apiClient.getTraceStats(params),
  getTraceSpans: (traceId: string) => apiClient.getTraceSpans(traceId),
  getTraceLiteInferred: (params?: LooseAny) => apiClient.getTraceLiteInferred(params),
  getInferenceQuality: (params?: LooseAny) => apiClient.getInferenceQuality(params),
  getInferenceQualityAlerts: (params?: LooseAny) => apiClient.getInferenceQualityAlerts(params),
  setInferenceAlertSuppression: (metric: string, enabled: boolean) => apiClient.setInferenceAlertSuppression(metric, enabled),
  getValueKpi: (params?: LooseAny) => apiClient.getValueKpi(params),
  exportValueKpiWeekly: (params?: LooseAny) => apiClient.exportValueKpiWeekly(params),
  getTopology: (params?: LooseAny) => apiClient.getTopology(params),
  getHybridTopology: (params?: LooseAny) => apiClient.getHybridTopology(params),
  getTopologyStats: (params?: LooseAny) => apiClient.getTopologyStats(params),
  getAlertRules: () => apiClient.getAlertRules(),
  getAlertRuleTemplates: () => apiClient.getAlertRuleTemplates(),
  createAlertRule: (rule: LooseAny) => apiClient.createAlertRule(rule),
  createAlertRuleFromTemplate: (payload: LooseAny) => apiClient.createAlertRuleFromTemplate(payload),
  updateAlertRule: (ruleId: string, rule: LooseAny) => apiClient.updateAlertRule(ruleId, rule),
  deleteAlertRule: (ruleId: string) => apiClient.deleteAlertRule(ruleId),
  getAlertEvents: (params?: LooseAny) => apiClient.getAlertEvents(params),
  getAlertNotifications: (params?: LooseAny) => apiClient.getAlertNotifications(params),
  getAlertStats: () => apiClient.getAlertStats(),
  evaluateAlertRules: () => apiClient.evaluateAlertRules(),
  acknowledgeAlertEvent: (eventId: string) => apiClient.acknowledgeAlertEvent(eventId),
  silenceAlertEvent: (eventId: string, durationSeconds?: number) => apiClient.silenceAlertEvent(eventId, durationSeconds),
  resolveAlertEvent: (eventId: string, reason?: string) => apiClient.resolveAlertEvent(eventId, reason),
  discoverLabels: (params?: LooseAny) => apiClient.discoverLabels(params),
  getLabelSuggestions: (params?: LooseAny) => apiClient.getLabelSuggestions(params),
  sendOTLPLogs: (logs: LooseAny) => apiClient.sendOTLPLogs(logs),
  sendOTLPMetrics: (metrics: LooseAny) => apiClient.sendOTLPMetrics(metrics),
  sendOTLPTraces: (traces: LooseAny) => apiClient.sendOTLPTraces(traces),
  getLogContext: (params: LooseAny) => apiClient.getLogContext(params),
  analyzeLog: (data: LooseAny) => apiClient.analyzeLog(data),
  analyzeLogLLM: (params: LooseAny) => apiClient.analyzeLogLLM(params),
  analyzeTrace: (data: LooseAny) => apiClient.analyzeTrace(data),
  analyzeTraceLLM: (params: LooseAny) => apiClient.analyzeTraceLLM(params),
  findSimilarCases: (params: LooseAny) => apiClient.findSimilarCases(params),
  saveCase: (params: LooseAny) => apiClient.saveCase(params),
  getKBProviderStatus: () => apiClient.getKBProviderStatus(),
  getKBRuntimeStatus: () => apiClient.getKBRuntimeStatus(),
  validateKBRuntimeConfig: (params: LooseAny) => apiClient.validateKBRuntimeConfig(params),
  updateKBRuntimeConfig: (params: LooseAny) => apiClient.updateKBRuntimeConfig(params),
  getKBOutboxStatus: () => apiClient.getKBOutboxStatus(),
  resolveKBRuntimeOptions: (params: LooseAny) => apiClient.resolveKBRuntimeOptions(params),
  searchKB: (params: LooseAny) => apiClient.searchKB(params),
  buildKBFromAnalysisSession: (params: LooseAny) => apiClient.buildKBFromAnalysisSession(params),
  optimizeKBSolutionContent: (params: LooseAny) => apiClient.optimizeKBSolutionContent(params),
  updateCaseContent: (caseId: string, params: LooseAny) => apiClient.updateCaseContent(caseId, params),
  updateCaseManualRemediation: (caseId: string, params: LooseAny) => apiClient.updateCaseManualRemediation(caseId, params),
  getCases: (params?: LooseAny) => apiClient.getCases(params),
  getCaseDetail: (caseId: string) => apiClient.getCaseDetail(caseId),
  getAIHistory: (params?: LooseAny) => apiClient.getAIHistory(params),
  getAIHistoryDetail: (sessionId: string) => apiClient.getAIHistoryDetail(sessionId),
  updateAIHistorySession: (sessionId: string, params: LooseAny) => apiClient.updateAIHistorySession(sessionId, params),
  deleteAIHistorySession: (sessionId: string) => apiClient.deleteAIHistorySession(sessionId),
  createFollowUpAction: (sessionId: string, messageId: string, params: LooseAny) => apiClient.createFollowUpAction(sessionId, messageId, params),
  executeFollowUpCommand: (sessionId: string, messageId: string, params: LooseAny) => apiClient.executeFollowUpCommand(sessionId, messageId, params),
  deleteFollowUpMessage: (sessionId: string, messageId: string) => apiClient.deleteFollowUpMessage(sessionId, messageId),
  createAIRun: (params: AgentRunCreateRequest) => apiClient.createAIRun(params),
  getAIRun: (runId: string) => apiClient.getAIRun(runId),
  getAIRunEvents: (
    runId: string,
    options?: { afterSeq?: number; limit?: number; visibility?: AIRuntimeEventVisibility },
  ) => apiClient.getAIRunEvents(runId, options),
  streamAIRun: (
    runId: string,
    options?: {
      afterSeq?: number;
      onEvent?: (payload: AgentRunStreamEventPayload) => void;
      signal?: AbortSignal;
      timeoutMs?: number;
      deadlineMs?: number;
      visibility?: AIRuntimeEventVisibility;
    },
  ) => apiClient.streamAIRun(runId, options),
  cancelAIRun: (runId: string, params?: { reason?: string }) => apiClient.cancelAIRun(runId, params),
  interruptAIRun: (runId: string, params?: AgentRunInterruptRequest) => apiClient.interruptAIRun(runId, params),
  approveAIRun: (runId: string, params: AgentRunApproveRequest) => apiClient.approveAIRun(runId, params),
  executeAIRunCommand: (runId: string, params: AgentRunCommandRequest) => apiClient.executeAIRunCommand(runId, params),
  continueAIRunWithInput: (runId: string, params: AgentRunInputRequest) => apiClient.continueAIRunWithInput(runId, params),
  getExecExecutorStatus: () => apiClient.getExecExecutorStatus(),
  deleteCase: (caseId: string) => apiClient.deleteCase(caseId),
  resolveCase: (caseId: string, resolution: string) => apiClient.resolveCase(caseId, resolution),
  followUpAnalysis: (params: LooseAny) => apiClient.followUpAnalysis(params),
  followUpAnalysisStream: (params: LooseAny, options?: LooseAny) => apiClient.followUpAnalysisStream(params, options),
  followUpAnalysisStreamV2: (params: LooseAny, options?: LooseAny) => apiClient.followUpAnalysisStreamV2(params, options),
  getLLMRuntimeStatus: () => apiClient.getLLMRuntimeStatus(),
  validateLLMRuntimeConfig: (params: LooseAny) => apiClient.validateLLMRuntimeConfig(params),
  updateLLMRuntimeConfig: (params: LooseAny) => apiClient.updateLLMRuntimeConfig(params),
  createTopologySnapshot: (params: LooseAny) => apiClient.createTopologySnapshot(params),
  getTopologySnapshots: (params?: LooseAny) => apiClient.getTopologySnapshots(params),
  clearCache: (pattern?: string) => apiClient.clearCache(pattern),
  getCacheStats: () => apiClient.getCacheStats(),
  getDeduplicationStats: () => apiClient.getDeduplicationStats(),
  clearDeduplicationCache: () => apiClient.clearDeduplicationCache(),
};

export default api;
