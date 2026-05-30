/**
 * API 数据获取 Hook - 参考 Datadog 前端设计
 * 统一的加载、错误、数据状态管理
 */
import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { api } from '../utils/api';
import type {
  AlertRule,
  Event,
  AggregatedLogsParams,
  LogsQueryParams,
  LogsFacetQueryParams,
  TopologyGraph,
} from '../utils/api';
import type { Span as TraceTimelineSpan } from '../components/traces/TraceTimeline';
import { parseLogMessage } from '../utils/logMessage';
import { isHealthCheckMessage } from '../utils/healthCheck';
import { resolveCanonicalServiceName } from '../utils/serviceName';

const TOPOLOGY_WS_RECONNECT_DELAY_MS = 2000;

/**
 * API 调用状态
 */
export interface ApiState<T> {
  data: T | null;
  loading: boolean;
  error: Error | null;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' ? (value as Record<string, unknown>) : {};
}

function stableSerialize(value: unknown): string {
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

/**
 * 通用 Hook 创建函数
 */
function createApiHook<T, P extends object | undefined>(
  apiFunc: (params: P) => Promise<T>,
  initialParams: P
): (params?: P) => ApiState<T> & { refetch: () => void } {
  return (params = initialParams) => {
    const [state, setState] = useState<ApiState<T>>({ data: null, loading: true, error: null });
    const paramsRef = useRef(params);
    const requestSeqRef = useRef(0);
    const paramsKey = useMemo(() => stableSerialize(params), [params]);

    useEffect(() => {
      paramsRef.current = params;
    }, [params, paramsKey]);

    const fetchData = useCallback(async () => {
      const requestSeq = ++requestSeqRef.current;

      try {
        setState(prev => ({ ...prev, loading: true, error: null }));
        const result = await apiFunc(paramsRef.current as P);
        if (requestSeq !== requestSeqRef.current) {
          return;
        }
        setState({ data: result, loading: false, error: null });
      } catch (error) {
        if (requestSeq !== requestSeqRef.current) {
          return;
        }
        setState(prev => ({ ...prev, loading: false, error: error as Error }));
      }
    }, []);

    useEffect(() => {
      fetchData();
    }, [fetchData, paramsKey]);

    return { ...state, refetch: fetchData };
  };
}

/**
 * 使用事件数据
 */
export const useEvents = createApiHook(
  (params) => api.getEvents(params),
  {} as LogsQueryParams
);

/**
 * 使用日志统计数据
 */
export const useLogsStats = createApiHook(
  (params) => api.getLogsStats(params),
  {} as { time_window?: string }
);

/**
 * 使用日志 Facet 统计（服务/级别）
 */
export const useLogFacets = createApiHook(
  (params) => api.getLogFacets(params),
  {} as LogsFacetQueryParams
);

/**
 * 使用聚合日志数据
 */
export const useAggregatedLogs = createApiHook(
  (params) => api.getAggregatedLogs(params),
  {} as AggregatedLogsParams
);

/**
 * 使用指标数据
 */
export const useMetrics = createApiHook(
  (params) => api.getMetrics(params),
  {} as { limit?: number; service_name?: string; metric_name?: string }
);

/**
 * 使用指标统计
 */
export function useMetricStats(): ApiState<Record<string, unknown>> & { refetch: () => void } {
  const [state, setState] = useState<ApiState<Record<string, unknown>>>({ data: null, loading: true, error: null });

  const fetchData = useCallback(async () => {
    try {
      setState(prev => ({ ...prev, loading: true, error: null }));
      const result = await api.getMetricStats();
      setState({ data: result, loading: false, error: null });
    } catch (error) {
      setState({ data: null, loading: false, error: error as Error });
    }
  }, []);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  return { ...state, refetch: fetchData };
}

/**
 * 使用追踪数据
 */
export const useTraces = createApiHook(
  (params) => api.getTraces(params),
  {} as {
    limit?: number;
    offset?: number;
    service_name?: string;
    trace_id?: string;
    start_time?: string;
    end_time?: string;
    time_window?: string;
  }
);

/**
 * 使用追踪的所有 spans
 */
export function useTraceSpans(traceId: string | null) {
  const [state, setState] = useState<{ data: TraceTimelineSpan[] | null; loading: boolean; error: Error | null }>({
    data: null,
    loading: false,
    error: null
  });

  useEffect(() => {
    if (!traceId) {
      setState({ data: null, loading: false, error: null });
      return;
    }

    const fetchData = async () => {
      try {
        setState(prev => ({ ...prev, loading: true, error: null }));
        const result = await api.getTraceSpans(traceId);
        setState({ data: result as TraceTimelineSpan[], loading: false, error: null });
      } catch (error) {
        setState({ data: null, loading: false, error: error as Error });
      }
    };

    fetchData();
  }, [traceId]);

  return state;
}

/**
 * 使用追踪统计
 */
export function useTraceStats(
  params: { time_window?: string; start_time?: string; end_time?: string } = {}
): ApiState<Record<string, unknown>> & { refetch: () => void } {
  const [state, setState] = useState<ApiState<Record<string, unknown>>>({ data: null, loading: true, error: null });
  const paramsRef = useRef(params);
  const paramsKey = useMemo(() => stableSerialize(params), [params]);

  const fetchData = useCallback(async () => {
    try {
      setState(prev => ({ ...prev, loading: true, error: null }));
      const result = await api.getTraceStats(paramsRef.current);
      setState({ data: result, loading: false, error: null });
    } catch (error) {
      setState({ data: null, loading: false, error: error as Error });
    }
  }, []);

  useEffect(() => {
    paramsRef.current = params;
  }, [params, paramsKey]);

  useEffect(() => {
    fetchData();
  }, [fetchData, paramsKey]);

  return { ...state, refetch: fetchData };
}

/**
 * 使用拓扑数据
 */
export const useTopology = createApiHook(
  (params) => api.getTopology(params),
  {} as { limit?: number; namespace?: string; source?: string }
);

/**
 * 使用 Trace-Lite 推断片段
 */
export const useTraceLiteInferred = createApiHook(
  (params) => api.getTraceLiteInferred(params),
  {} as { time_window?: string; source_service?: string; target_service?: string; namespace?: string; source_namespace?: string; target_namespace?: string; limit?: number }
);

/**
 * 使用拓扑链路问题日志预览
 */
export function useTopologyEdgeLogPreview(
  params: {
    source_service?: string;
    target_service?: string;
    namespace?: string;
    source_namespace?: string;
    target_namespace?: string;
    time_window?: string;
    anchor_time?: string;
    limit?: number;
    exclude_health_check?: boolean;
  } | null
): ApiState<{ data: Event[]; count: number; limit: number; context?: Record<string, unknown> }> & { refetch: () => void } {
  const [state, setState] = useState<ApiState<{ data: Event[]; count: number; limit: number; context?: Record<string, unknown> }>>({
    data: null,
    loading: false,
    error: null,
  });
  const paramsRef = useRef(params);

  useEffect(() => {
    paramsRef.current = params;
  }, [params]);

  const fetchData = useCallback(async () => {
    const current = paramsRef.current;
    if (!current?.source_service || !current?.target_service) {
      setState({ data: null, loading: false, error: null });
      return;
    }

    try {
      setState(prev => ({ ...prev, loading: true, error: null }));
      const result = await api.getTopologyEdgeLogPreview({
        source_service: current.source_service,
        target_service: current.target_service,
        namespace: current.namespace,
        source_namespace: current.source_namespace,
        target_namespace: current.target_namespace,
        time_window: current.time_window || '1 HOUR',
        anchor_time: current.anchor_time,
        limit: current.limit || 8,
        exclude_health_check: current.exclude_health_check ?? true,
      });
      setState({ data: result, loading: false, error: null });
    } catch (error) {
      setState({ data: null, loading: false, error: error as Error });
    }
  }, []);

  useEffect(() => {
    fetchData();
  }, [params, fetchData]);

  return { ...state, refetch: fetchData };
}

/**
 * 使用混合拓扑数据
 */
export const useHybridTopology = createApiHook(
  (params) => api.getHybridTopology(params),
  {} as {
    time_window?: string;
    namespace?: string;
    confidence_threshold?: number;
    inference_mode?: 'rule' | 'hybrid_score';
    force_refresh?: boolean;
    message_target_enabled?: boolean;
    message_target_patterns?: string;
    message_target_min_support?: number;
    message_target_max_per_log?: number;
  }
);

/**
 * 使用拓扑统计
 */
export const useTopologyStats = createApiHook(
  (params) => api.getTopologyStats(params),
  {} as { time_window?: string }
);

/**
 * 使用推断质量指标
 */
export const useInferenceQuality = createApiHook(
  (params) => api.getInferenceQuality(params),
  {} as { time_window?: string }
);

/**
 * 使用推断质量告警
 */
export const useInferenceQualityAlerts = createApiHook(
  (params) => api.getInferenceQualityAlerts(params),
  {} as { time_window?: string; min_coverage?: number; max_inferred_ratio?: number; max_false_positive_rate?: number }
);

/**
 * 使用价值 KPI 指标（M4）
 */
export const useValueKpi = createApiHook(
  (params) => api.getValueKpi(params),
  {} as { time_window?: string }
);

/**
 * 使用告警规则
 */
export function useAlertRules(): ApiState<{ total: number; rules: AlertRule[] }> & { refetch: () => void } {
  const [state, setState] = useState<ApiState<{ total: number; rules: AlertRule[] }>>({ data: null, loading: true, error: null });

  const fetchData = useCallback(async () => {
    try {
      setState(prev => ({ ...prev, loading: true, error: null }));
      const result = await api.getAlertRules();
      setState({ data: result, loading: false, error: null });
    } catch (error) {
      setState({ data: null, loading: false, error: error as Error });
    }
  }, []);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  return { ...state, refetch: fetchData };
}

/**
 * 使用告警事件
 */
export const useAlertEvents = createApiHook(
  (params) => api.getAlertEvents(params),
  {} as { limit?: number; status?: string; severity?: string; cursor?: string; service_name?: string; source_service?: string; target_service?: string; namespace?: string; search?: string; scope?: 'all' | 'edge' | 'service' }
);

/**
 * 使用告警规则模板
 */
export const useAlertRuleTemplates = createApiHook(
  () => api.getAlertRuleTemplates(),
  undefined
);

/**
 * 使用告警通知记录
 */
export const useAlertNotifications = createApiHook(
  (params) => api.getAlertNotifications(params),
  {} as { limit?: number; channel?: string; delivery_status?: string; event_id?: string }
);

/**
 * 使用告警统计
 */
export function useAlertStats(): ApiState<Record<string, unknown>> & { refetch: () => void } {
  const [state, setState] = useState<ApiState<Record<string, unknown>>>({ data: null, loading: true, error: null });

  const fetchData = useCallback(async () => {
    try {
      setState(prev => ({ ...prev, loading: true, error: null }));
      const result = await api.getAlertStats();
      setState({ data: result, loading: false, error: null });
    } catch (error) {
      setState({ data: null, loading: false, error: error as Error });
    }
  }, []);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  return { ...state, refetch: fetchData };
}

/**
 * 使用日志上下文
 */
type LogContextItem = {
  id?: string;
  timestamp?: string;
  level?: string;
  message?: string;
  service_name?: string;
  pod_name?: string;
  [key: string]: unknown;
};

type LogContextResponse = {
  log_id?: string;
  trace_id?: string;
  pod_name?: string;
  namespace?: string;
  container_name?: string;
  data?: LogContextItem[];
  count?: number;
  limit?: number;
  before?: LogContextItem[];
  after?: LogContextItem[];
  current?: LogContextItem;
  current_matches?: LogContextItem[];
  current_count?: number;
};

function normalizeLogContextParams(
  params: { log_id?: string; trace_id?: string; pod_name?: string; namespace?: string; container_name?: string; timestamp?: string; before_count?: number; after_count?: number; limit?: number } | null
): { log_id?: string; trace_id?: string; pod_name?: string; namespace?: string; container_name?: string; timestamp?: string; before_count?: number; after_count?: number; limit?: number } | null {
  if (!params) {
    return null;
  }
  const logId = String(params.log_id || '').trim();
  const traceId = String(params.trace_id || '').trim();
  const podName = String(params.pod_name || '').trim();
  const namespace = String(params.namespace || '').trim();
  const containerName = String(params.container_name || '').trim();
  const timestamp = String(params.timestamp || '').trim();
  return {
    log_id: logId || undefined,
    trace_id: traceId || undefined,
    pod_name: podName || undefined,
    namespace: namespace || undefined,
    container_name: containerName || undefined,
    timestamp: timestamp || undefined,
    before_count: params.before_count,
    after_count: params.after_count,
    limit: params.limit,
  };
}

export function useLogContext(params: { log_id?: string; trace_id?: string; pod_name?: string; namespace?: string; container_name?: string; timestamp?: string; before_count?: number; after_count?: number; limit?: number } | null): ApiState<LogContextResponse> & { refetch: () => void } {
  const [state, setState] = useState<ApiState<LogContextResponse>>({ data: null, loading: false, error: null });
  const paramsRef = useRef(params);

  useEffect(() => {
    paramsRef.current = normalizeLogContextParams(params);
  }, [params]);

  const fetchData = useCallback(async () => {
    if (!paramsRef.current) {
      setState({ data: null, loading: false, error: null });
      return;
    }

    // 支持三种模式：log_id 模式 / trace_id 模式 / pod_name + timestamp 模式
    const hasLogId = paramsRef.current.log_id;
    const hasTraceId = paramsRef.current.trace_id;
    const hasPodTimestamp = paramsRef.current.pod_name && paramsRef.current.timestamp;

    if (!hasLogId && !hasTraceId && !hasPodTimestamp) {
      setState({ data: null, loading: false, error: null });
      return;
    }

    try {
      setState(prev => ({ ...prev, loading: true, error: null }));
      const result = await api.getLogContext(paramsRef.current);
      setState({ data: result as LogContextResponse, loading: false, error: null });
    } catch (error) {
      setState({ data: null, loading: false, error: error as Error });
    }
  }, []);

  useEffect(() => {
    const normalized = normalizeLogContextParams(params);
    const hasLogId = normalized?.log_id;
    const hasTraceId = normalized?.trace_id;
    const hasPodTimestamp = normalized?.pod_name && normalized?.timestamp;
    if (hasLogId || hasTraceId || hasPodTimestamp) {
      fetchData();
    } else {
      // 当参数无效时清空数据
      setState({ data: null, loading: false, error: null });
    }
  }, [params, fetchData]);

  return { ...state, refetch: fetchData };
}

/**
 * 使用健康检查
 */
export function useHealth(): ApiState<{ status: string; service: string; version: string }> & { refetch: () => void } {
  const [state, setState] = useState<ApiState<{ status: string; service: string; version: string }>>({ data: null, loading: true, error: null });

  const fetchData = useCallback(async () => {
    try {
      setState(prev => ({ ...prev, loading: true, error: null }));
      const result = await api.health();
      setState({ data: result, loading: false, error: null });
    } catch (error) {
      setState({ data: null, loading: false, error: error as Error });
    }
  }, []);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  return { ...state, refetch: fetchData };
}

/**
 * AI 分析日志
 */
export interface AIAnalysisData {
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
}

export interface AnalyzeLogOptions {
  mode?: 'log' | 'trace';
  useLLM?: boolean;
  traceId?: string;
}

function extractTraceIdFromEvent(event: Event): string {
  const directTraceId = String(event.trace_id || '').trim();
  if (directTraceId) {
    return directTraceId;
  }

  const contextTraceId = String(event.attributes?.trace_id || event.attributes?.traceId || '').trim();
  if (contextTraceId) {
    return contextTraceId;
  }

  return '';
}

export function useAnalyzeLog() {
  const [state, setState] = useState<ApiState<AIAnalysisData>>({ data: null, loading: false, error: null });

  const analyze = useCallback(async (event: Event, options: AnalyzeLogOptions = {}) => {
    const mode = options.mode || 'log';
    const useLLM = options.useLLM ?? true;
    try {
      setState(prev => ({ ...prev, loading: true, error: null }));
      let result: AIAnalysisData;

      if (mode === 'trace') {
        const traceId = String(options.traceId || extractTraceIdFromEvent(event)).trim();
        if (!traceId) {
          throw new Error('当前日志缺少 trace_id，无法执行追踪分析');
        }

        if (useLLM) {
          result = await api.analyzeTraceLLM({
            trace_id: traceId,
            service_name: event.service_name || undefined,
          });
        } else {
          result = await api.analyzeTrace({
            trace_id: traceId,
            service_name: event.service_name || undefined,
          });
        }
      } else {
        result = await api.analyzeLogLLM({
          log_content: event.message || '',
          service_name: event.service_name || '',
          context: event.attributes || {},
          use_llm: useLLM,
        });
      }

      setState({ data: result, loading: false, error: null });
      return result;
    } catch (error) {
      setState({ data: null, loading: false, error: error as Error });
      throw error;
    }
  }, []);

  const reset = useCallback(() => {
    setState({ data: null, loading: false, error: null });
  }, []);

  return { ...state, analyze, reset };
}

/**
 * AI 分析追踪
 */
export function useAnalyzeTrace() {
  const [state, setState] = useState<ApiState<{
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
  }>>({ data: null, loading: false, error: null });

  const analyze = useCallback(async (data: { trace_id: string; service_name?: string }) => {
    try {
      setState(prev => ({ ...prev, loading: true, error: null }));
      const result = await api.analyzeTrace(data);
      setState({ data: result, loading: false, error: null });
      return result;
    } catch (error) {
      setState({ data: null, loading: false, error: error as Error });
      throw error;
    }
  }, []);

  const reset = useCallback(() => {
    setState({ data: null, loading: false, error: null });
  }, []);

  return { ...state, analyze, reset };
}


/**
 * WebSocket 实时日志流 Hook
 */
export interface RealtimeLog {
  id: string;
  timestamp: string;
  service_name: string;
  level: string;
  message: string;
  pod_name?: string;
  namespace?: string;
  attributes?: Record<string, unknown>;
  log_meta?: {
    wrapped: boolean;
    stream?: string;
    collector_time?: string;
    line_count: number;
  };
  [key: string]: unknown;
}

export interface UseRealtimeLogsOptions {
  enabled?: boolean;
  maxLogs?: number;
  filters?: {
    service_name?: string;
    namespace?: string;
    container_name?: string;
    level?: string;
    exclude_health_check?: boolean;
  };
  onLog?: (log: RealtimeLog) => void;
  onError?: (error: Error) => void;
}

function normalizeRealtimeLevel(rawLevel: unknown, message: unknown): 'TRACE' | 'DEBUG' | 'INFO' | 'WARN' | 'ERROR' | 'FATAL' {
  const resolveStrictLevel = (value: unknown): 'TRACE' | 'DEBUG' | 'INFO' | 'WARN' | 'ERROR' | 'FATAL' | '' => {
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

  const resolveStructuredLevel = (value: unknown): 'TRACE' | 'DEBUG' | 'INFO' | 'WARN' | 'ERROR' | 'FATAL' | '' => {
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

  const resolveWrappedLogLevel = (value: unknown): 'TRACE' | 'DEBUG' | 'INFO' | 'WARN' | 'ERROR' | 'FATAL' | '' => {
    const text = String(value || '').trim();
    if (!text || !text.startsWith('{')) {
      return '';
    }
    try {
      const parsed = JSON.parse(text);
      if (!parsed || typeof parsed !== 'object') {
        return '';
      }
      const nestedRaw = (parsed as Record<string, unknown>).log
        ?? (parsed as Record<string, unknown>).message
        ?? (parsed as Record<string, unknown>).msg;
      const nestedText = typeof nestedRaw === 'string' ? nestedRaw : '';
      if (!nestedText) {
        return '';
      }
      return resolveStrictLevel(nestedText) || resolveStructuredLevel(nestedText);
    } catch {
      return '';
    }
  };

  const rawLevelResolved = resolveStrictLevel(rawLevel);
  if (rawLevelResolved && rawLevelResolved !== 'INFO') {
    return rawLevelResolved;
  }

  const text = String(message || '').trim();
  const messageResolved = resolveStrictLevel(text) || resolveStructuredLevel(text) || resolveWrappedLogLevel(text);
  if (messageResolved && (!rawLevelResolved || rawLevelResolved === 'INFO')) {
    return messageResolved;
  }
  if (rawLevelResolved) {
    return rawLevelResolved;
  }
  if (!text) {
    return 'INFO';
  }

  // 仅在“日志结构可信位置”识别级别，避免 URL 参数 level=ERROR 被误判。
  const jsonLevelMatch = text.match(/"(?:level|severity|log_level)"\s*:\s*"?(TRACE|DEBUG|INFO|WARN(?:ING)?|ERROR|FATAL)"?/i);
  const leadingLevelMatch = text.match(/^\s*\[?(TRACE|DEBUG|INFO|WARN(?:ING)?|ERROR|FATAL)\]?(?:\s+|:|-)/i);
  const timestampedLevelMatch = text.match(/^\d{4}[-/]\d{2}[-/]\d{2}.{0,24}\b(TRACE|DEBUG|INFO|WARN(?:ING)?|ERROR|FATAL)\b/i);

  const resolved = jsonLevelMatch?.[1] || leadingLevelMatch?.[1] || timestampedLevelMatch?.[1] || '';
  const normalizedResolved = resolved.toUpperCase();
  if (normalizedResolved === 'WARNING') {
    return 'WARN';
  }
  if (['TRACE', 'DEBUG', 'INFO', 'WARN', 'ERROR', 'FATAL'].includes(normalizedResolved)) {
    return normalizedResolved as 'TRACE' | 'DEBUG' | 'INFO' | 'WARN' | 'ERROR' | 'FATAL';
  }
  return 'INFO';
}

function hashText(input: string): string {
  let hash = 2166136261;
  for (let i = 0; i < input.length; i++) {
    hash ^= input.charCodeAt(i);
    hash += (hash << 1) + (hash << 4) + (hash << 7) + (hash << 8) + (hash << 24);
  }
  return (hash >>> 0).toString(16).padStart(8, '0');
}

function parseRealtimeLogMeta(raw: unknown): Record<string, unknown> {
  if (!raw) {
    return {};
  }
  if (typeof raw === 'string') {
    try {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        return parsed as Record<string, unknown>;
      }
      return {};
    } catch {
      return {};
    }
  }
  if (typeof raw === 'object' && !Array.isArray(raw)) {
    return raw as Record<string, unknown>;
  }
  return {};
}

function buildRealtimeLogId(log: Partial<RealtimeLog>): string {
  const rawId = String(log.id || '').trim();
  const ts = String(log.timestamp || '');
  const service = resolveCanonicalServiceName(log.service_name, log.pod_name);
  const pod = String(log.pod_name || '');
  const namespace = String(log.namespace || '');
  const level = String(log.level || 'INFO');
  const traceId = String(log.trace_id || log.attributes?.trace_id || '');
  const message = String(log.message || '');
  return `${rawId}-${ts}-${service}-${pod}-${namespace}-${level}-${traceId}-${hashText(message)}`;
}

function resolveRealtimeLogPayload(message: unknown): RealtimeLog | null {
  const messageRecord = asRecord(message);
  if (messageRecord.type === 'log' && messageRecord.data && typeof messageRecord.data === 'object') {
    return messageRecord.data as RealtimeLog;
  }
  if (messageRecord.log && typeof messageRecord.log === 'object') {
    return messageRecord.log as RealtimeLog;
  }
  if (
    typeof messageRecord.message === 'string' &&
    messageRecord.timestamp &&
    (messageRecord.service_name || messageRecord.level || messageRecord.pod_name || messageRecord.trace_id || messageRecord.id)
  ) {
    return messageRecord as RealtimeLog;
  }
  return null;
}

export function useRealtimeLogs(options: UseRealtimeLogsOptions = {}) {
  const {
    enabled = true,
    maxLogs = 500,
    filters,
    onLog,
    onError
  } = options;

  const [logs, setLogs] = useState<RealtimeLog[]>([]);
  const [isConnected, setIsConnected] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reconnectAttemptsRef = useRef(0);

  const connect = useCallback(() => {
    if (!enabled || wsRef.current?.readyState === WebSocket.OPEN || wsRef.current?.readyState === WebSocket.CONNECTING) {
      return;
    }

    try {
      const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const wsHost = window.location.host;
      const wsUrl = `${wsProtocol}//${wsHost}/ws/logs`;

      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onopen = () => {
        console.log('[WebSocket] Connected to logs stream');
        setIsConnected(true);
        setError(null);
        reconnectAttemptsRef.current = 0;

        if (filters) {
          ws.send(JSON.stringify({
            action: 'subscribe',
            filters
          }));
        }
      };

      ws.onmessage = (event) => {
        try {
          const message = JSON.parse(event.data);
          const rawLog = resolveRealtimeLogPayload(message);

          if (rawLog) {
            const parsedMessage = parseLogMessage(rawLog.message || '');
            const resolvedLevel = normalizeRealtimeLevel(rawLog.level, parsedMessage.message);
            const resolvedServiceName = resolveCanonicalServiceName(rawLog.service_name, rawLog.pod_name);
            const rawLogMeta = parseRealtimeLogMeta(rawLog.log_meta);
            const newLog: RealtimeLog = {
              ...rawLog,
              id: buildRealtimeLogId(rawLog),
              service_name: resolvedServiceName,
              level: resolvedLevel,
              message: parsedMessage.message,
              log_meta: {
                ...parsedMessage.meta,
                ...rawLogMeta,
              },
            };

            if (
              filters?.service_name
              && resolveCanonicalServiceName(newLog.service_name, newLog.pod_name) !== resolveCanonicalServiceName(filters.service_name)
            ) {
              return;
            }
            if (
              filters?.namespace
              && String(newLog.namespace || '').trim() !== String(filters.namespace || '').trim()
            ) {
              return;
            }
            if (
              filters?.container_name
              && String(newLog.container_name || '').trim() !== String(filters.container_name || '').trim()
            ) {
              return;
            }
            if (filters?.level && newLog.level !== normalizeRealtimeLevel(filters.level, '')) {
              return;
            }
            if (filters?.exclude_health_check && isHealthCheckMessage(String(newLog.message || ''))) {
              return;
            }

            setLogs(prev => {
              const deduped = prev.filter((item) => item.id !== newLog.id);
              const updated = [newLog, ...deduped];
              return updated.slice(0, maxLogs);
            });

            onLog?.(newLog);
          } else if (message.type === 'ping') {
            ws.send(JSON.stringify({ action: 'ping' }));
          }
        } catch (e) {
          console.error('[WebSocket] Failed to parse message:', e);
        }
      };

      ws.onerror = (event) => {
        console.error('[WebSocket] Error:', event);
        const err = new Error('WebSocket connection error');
        setError(err);
        onError?.(err);
      };

      ws.onclose = (event) => {
        console.log('[WebSocket] Disconnected:', event.code, event.reason);
        setIsConnected(false);
        wsRef.current = null;

        if (enabled) {
          const delay = Math.min(1000 * Math.pow(2, reconnectAttemptsRef.current), 30000);
          console.log(`[WebSocket] Reconnecting in ${delay}ms (attempt ${reconnectAttemptsRef.current + 1})`);

          reconnectTimeoutRef.current = setTimeout(() => {
            reconnectAttemptsRef.current++;
            connect();
          }, delay);
        }
      };

    } catch (e) {
      const err = e as Error;
      console.error('[WebSocket] Failed to connect:', err);
      setError(err);
      onError?.(err);
    }
  }, [enabled, filters, maxLogs, onLog, onError]);

  const disconnect = useCallback(() => {
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }

    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }

    setIsConnected(false);
  }, []);

  const clearLogs = useCallback(() => {
    setLogs([]);
  }, []);

  useEffect(() => {
    if (enabled) {
      connect();
    } else {
      disconnect();
    }

    return () => {
      disconnect();
    };
  }, [enabled, connect, disconnect]);

  return {
    logs,
    isConnected,
    error,
    connect,
    disconnect,
    clearLogs,
  };
}


/**
 * WebSocket 实时拓扑更新 Hook
 */
export interface UseRealtimeTopologyOptions {
  enabled?: boolean;
  subscription?: {
    time_window?: string;
    namespace?: string | null;
    confidence_threshold?: number;
    inference_mode?: 'rule' | 'hybrid_score';
    message_target_enabled?: boolean;
    message_target_patterns?: string;
    message_target_min_support?: number;
    message_target_max_per_log?: number;
  };
  onUpdate?: (topology: TopologyGraph | null) => void;
  onError?: (error: Error) => void;
}

export function useRealtimeTopology(options: UseRealtimeTopologyOptions = {}) {
  const { enabled = true, subscription, onUpdate, onError } = options;

  const [topology, setTopology] = useState<TopologyGraph | null>(null);
  const [isConnected, setIsConnected] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const pendingTopologyRef = useRef<TopologyGraph | null>(null);
  const animationFrameRef = useRef<number | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const shouldReconnectRef = useRef(enabled);
  const subscriptionRef = useRef({
    time_window: '1 HOUR',
    namespace: null as string | null,
    confidence_threshold: 0.3,
    inference_mode: 'rule' as 'rule' | 'hybrid_score',
    message_target_enabled: true,
    message_target_patterns: 'url,kv,proxy,rpc',
    message_target_min_support: 2,
    message_target_max_per_log: 3,
  });

  const normalizedSubscription = useMemo(() => {
    const timeWindow = String(subscription?.time_window || '1 HOUR').trim() || '1 HOUR';
    const namespaceRaw = subscription?.namespace;
    const namespace = namespaceRaw && String(namespaceRaw).trim() ? String(namespaceRaw).trim() : null;
    const thresholdRaw = Number(subscription?.confidence_threshold ?? 0.3);
    const threshold = Number.isFinite(thresholdRaw) ? Math.max(0, Math.min(1, thresholdRaw)) : 0.3;
    const inferenceModeRaw = String(subscription?.inference_mode || 'rule').trim().toLowerCase();
    const inferenceMode: 'rule' | 'hybrid_score' = inferenceModeRaw === 'hybrid_score' ? 'hybrid_score' : 'rule';
    const messageTargetEnabled = subscription?.message_target_enabled ?? true;

    const patternText = String(subscription?.message_target_patterns || 'url,kv,proxy,rpc');
    const patternTokens = patternText
      .split(',')
      .map((item) => item.trim().toLowerCase())
      .filter(Boolean);
    const allowedPatterns = ['url', 'kv', 'proxy', 'rpc'];
    const filteredPatterns = Array.from(new Set(patternTokens.filter((item) => allowedPatterns.includes(item)))).sort();
    const messageTargetPatterns = (filteredPatterns.length ? filteredPatterns : ['url']).join(',');

    const minSupportRaw = Number(subscription?.message_target_min_support ?? 2);
    const messageTargetMinSupport = Number.isFinite(minSupportRaw) ? Math.max(1, Math.min(20, Math.round(minSupportRaw))) : 2;

    const maxPerLogRaw = Number(subscription?.message_target_max_per_log ?? 3);
    const messageTargetMaxPerLog = Number.isFinite(maxPerLogRaw) ? Math.max(1, Math.min(12, Math.round(maxPerLogRaw))) : 3;
    return {
      time_window: timeWindow,
      namespace,
      confidence_threshold: threshold,
      inference_mode: inferenceMode,
      message_target_enabled: Boolean(messageTargetEnabled),
      message_target_patterns: messageTargetPatterns,
      message_target_min_support: messageTargetMinSupport,
      message_target_max_per_log: messageTargetMaxPerLog,
    };
  }, [
    subscription?.time_window,
    subscription?.namespace,
    subscription?.confidence_threshold,
    subscription?.inference_mode,
    subscription?.message_target_enabled,
    subscription?.message_target_patterns,
    subscription?.message_target_min_support,
    subscription?.message_target_max_per_log,
  ]);

  useEffect(() => {
    subscriptionRef.current = normalizedSubscription;
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        action: 'subscribe',
        params: normalizedSubscription,
      }));
    }
  }, [normalizedSubscription]);

  const clearReconnectTimer = useCallback(() => {
    if (reconnectTimerRef.current !== null) {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
  }, []);

  const flushTopologyUpdate = useCallback(() => {
    animationFrameRef.current = null;
    const nextTopology = pendingTopologyRef.current;
    if (!nextTopology) {
      return;
    }
    pendingTopologyRef.current = null;
    setTopology(nextTopology);
    onUpdate?.(nextTopology);
  }, [onUpdate]);

  const connect = useCallback(() => {
    const readyState = wsRef.current?.readyState;
    if (!enabled || readyState === WebSocket.OPEN || readyState === WebSocket.CONNECTING) {
      return;
    }

    shouldReconnectRef.current = enabled;

    try {
      const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const wsHost = window.location.host;
      const wsUrl = `${wsProtocol}//${wsHost}/ws/topology`;

      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onopen = () => {
        console.log('[WebSocket] Connected to topology stream');
        clearReconnectTimer();
        setIsConnected(true);
        setError(null);

        // 首次连接时仅订阅，避免与服务端初始推送/轮询形成重复构建。
        ws.send(JSON.stringify({
          action: 'subscribe',
          params: subscriptionRef.current,
        }));
      };

      ws.onmessage = (event) => {
        try {
          const message = JSON.parse(event.data);

          if (message.type === 'topology_update' && message.data) {
            pendingTopologyRef.current = message.data as TopologyGraph;
            if (animationFrameRef.current === null) {
              animationFrameRef.current = window.requestAnimationFrame(flushTopologyUpdate);
            }
          } else if (message.type === 'ping') {
            ws.send(JSON.stringify({ action: 'pong' }));
          }
        } catch (e) {
          console.error('[WebSocket] Failed to parse topology message:', e);
        }
      };

      ws.onerror = (event) => {
        console.error('[WebSocket] Topology error:', event);
        const err = new Error('WebSocket topology connection error');
        setError(err);
        onError?.(err);
      };

      ws.onclose = () => {
        console.log('[WebSocket] Topology disconnected');
        if (animationFrameRef.current !== null) {
          window.cancelAnimationFrame(animationFrameRef.current);
          animationFrameRef.current = null;
        }
        pendingTopologyRef.current = null;
        setIsConnected(false);
        wsRef.current = null;
        if (shouldReconnectRef.current && reconnectTimerRef.current === null) {
          reconnectTimerRef.current = window.setTimeout(() => {
            reconnectTimerRef.current = null;
            if (shouldReconnectRef.current) {
              connect();
            }
          }, TOPOLOGY_WS_RECONNECT_DELAY_MS);
        }
      };

    } catch (e) {
      const err = e as Error;
      console.error('[WebSocket] Failed to connect to topology:', err);
      setError(err);
      onError?.(err);
    }
  }, [clearReconnectTimer, enabled, flushTopologyUpdate, onError]);

  const disconnect = useCallback(() => {
    shouldReconnectRef.current = false;
    clearReconnectTimer();
    if (animationFrameRef.current !== null) {
      window.cancelAnimationFrame(animationFrameRef.current);
      animationFrameRef.current = null;
    }
    pendingTopologyRef.current = null;
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    setIsConnected(false);
  }, [clearReconnectTimer]);

  useEffect(() => {
    shouldReconnectRef.current = enabled;
    if (enabled) {
      connect();
    } else {
      disconnect();
    }

    return () => {
      disconnect();
    };
  }, [enabled, connect, disconnect]);

  return {
    topology,
    isConnected,
    error,
    connect,
    disconnect,
  };
}

/**
 * 导出所有 Hook
 */
export default {
  useEvents,
  useLogsStats,
  useLogFacets,
  useAggregatedLogs,
  useMetrics,
  useMetricStats,
  useTraces,
  useTraceStats,
  useTraceSpans,
  useTraceLiteInferred,
  useTopology,
  useHybridTopology,
  useTopologyStats,
  useInferenceQuality,
  useInferenceQualityAlerts,
  useValueKpi,
  useAlertRules,
  useAlertEvents,
  useAlertRuleTemplates,
  useAlertNotifications,
  useAlertStats,
  useLogContext,
  useHealth,
  useAnalyzeLog,
  useAnalyzeTrace,
  useRealtimeLogs,
  useRealtimeTopology,
};
