import { buildRuntimeAnalysisContext, type RuntimeAnalysisMode } from './runtimeAnalysisMode.js';

const toObject = (value: unknown): Record<string, unknown> => (
  value && typeof value === 'object' ? { ...value as Record<string, unknown> } : {}
);

const toNormalizedString = (value: unknown): string => {
  if (value === undefined || value === null) {
    return '';
  }
  return String(value).trim();
};

const pickFirstString = (values: Array<unknown>): string => {
  for (const candidate of values) {
    const normalized = toNormalizedString(candidate);
    if (normalized) {
      return normalized;
    }
  }
  return '';
};

const applyCanonicalStringField = (
  context: Record<string, unknown>,
  key: string,
  value: string,
) => {
  if (value) {
    context[key] = value;
    return;
  }
  if (
    Object.prototype.hasOwnProperty.call(context, key)
    && typeof context[key] === 'string'
    && !String(context[key]).trim()
  ) {
    delete context[key];
  }
};

export function buildRuntimeFollowUpContext(params: {
  analysisSessionId?: string | null;
  analysisType: 'log' | 'trace';
  serviceName?: string | null;
  inputText: string;
  question?: string | null;
  llmInfo?: Record<string, unknown> | null;
  result?: unknown;
  detectedTraceId?: string | null;
  detectedRequestId?: string | null;
  sourceLogTimestamp?: string | null;
  sourceTraceId?: string | null;
  sourceRequestId?: string | null;
  followupRelatedLogs?: unknown[] | null;
  followupRelatedLogCount?: number | null;
  followupRelatedMeta?: Record<string, unknown> | null;
}): Record<string, unknown> {
  const normalizedSessionId = toNormalizedString(params.analysisSessionId);
  const normalizedInputText = toNormalizedString(params.inputText);
  const normalizedQuestion = toNormalizedString(params.question);

  const llmInfo = params.llmInfo && typeof params.llmInfo === 'object'
    ? { ...params.llmInfo }
    : {};
  const resultPayload = toObject(params.result);
  const agentPayload = toObject(resultPayload.agent);
  const followupMeta = toObject(params.followupRelatedMeta);

  const requestIdFromResult = pickFirstString([
    resultPayload.request_id,
    resultPayload.requestId,
    agentPayload.request_id,
    agentPayload.requestId,
  ]);
  const traceIdFromResult = pickFirstString([
    resultPayload.trace_id,
    resultPayload.traceId,
    agentPayload.trace_id,
    agentPayload.traceId,
  ]);

  const requestIdFromMeta = pickFirstString([
    followupMeta.followup_related_request_id,
    followupMeta.request_id,
  ]);
  const traceIdFromMeta = pickFirstString([
    followupMeta.followup_related_trace_id,
    followupMeta.trace_id,
  ]);

  const resolvedTraceId = pickFirstString([
    traceIdFromResult,
    traceIdFromMeta,
    params.detectedTraceId,
    params.sourceTraceId,
  ]);
  const resolvedRequestId = pickFirstString([
    requestIdFromResult,
    requestIdFromMeta,
    params.detectedRequestId,
    params.sourceRequestId,
  ]);

  const runtimeAnalysisType: RuntimeAnalysisMode = params.analysisType === 'trace'
    ? 'trace'
    : 'log';
  const analysisContext = buildRuntimeAnalysisContext({
    analysisType: runtimeAnalysisType,
    traceId: resolvedTraceId,
    serviceName: params.serviceName,
    baseContext: {
      agent_mode: 'request_flow',
      session_id: normalizedSessionId || undefined,
      analysis_session_id: normalizedSessionId || undefined,
      input_text: normalizedInputText || undefined,
      question: normalizedQuestion || undefined,
      llm_info: llmInfo,
      result: params.result,
    },
  });

  const context = { ...analysisContext };
  if (Object.keys(followupMeta).length > 0) {
    Object.assign(context, followupMeta);
  }

  const anchorTimestamp = pickFirstString([
    followupMeta.related_log_anchor_timestamp,
    followupMeta.followup_related_anchor_utc,
    params.sourceLogTimestamp,
  ]);
  applyCanonicalStringField(context, 'related_log_anchor_timestamp', anchorTimestamp);

  const windowStart = pickFirstString([
    followupMeta.request_flow_window_start,
    followupMeta.followup_related_start_time,
    followupMeta.evidence_window_start,
  ]);
  applyCanonicalStringField(context, 'request_flow_window_start', windowStart);

  const windowEnd = pickFirstString([
    followupMeta.request_flow_window_end,
    followupMeta.followup_related_end_time,
    followupMeta.evidence_window_end,
  ]);
  applyCanonicalStringField(context, 'request_flow_window_end', windowEnd);

  applyCanonicalStringField(context, 'request_id', resolvedRequestId);

  const normalizedSourceLogTimestamp = toNormalizedString(params.sourceLogTimestamp);
  if (normalizedSourceLogTimestamp) {
    context.source_log_timestamp = normalizedSourceLogTimestamp;
  }

  const normalizedSourceTraceId = toNormalizedString(params.sourceTraceId);
  if (normalizedSourceTraceId) {
    context.source_trace_id = normalizedSourceTraceId;
  }

  const normalizedSourceRequestId = toNormalizedString(params.sourceRequestId);
  if (normalizedSourceRequestId) {
    context.source_request_id = normalizedSourceRequestId;
  }

  const explicitRelatedLogCount = (
    typeof params.followupRelatedLogCount === 'number'
    && Number.isFinite(params.followupRelatedLogCount)
  )
    ? params.followupRelatedLogCount
    : undefined;
  if (Array.isArray(params.followupRelatedLogs) && params.followupRelatedLogs.length > 0) {
    context.followup_related_logs = params.followupRelatedLogs;
    context.followup_related_log_count = explicitRelatedLogCount ?? params.followupRelatedLogs.length;
  } else if (explicitRelatedLogCount !== undefined) {
    context.followup_related_log_count = explicitRelatedLogCount;
  }

  return context;
}
