/**
 * Runtime follow-up context normalization helpers.
 */

import {
  buildRuntimeAnalysisContext,
  type RuntimeAnalysisMode,
} from './runtimeAnalysisMode.js';

const normalizeText = (value: unknown): string => String(value ?? '').trim();

const asRecord = (value: unknown): Record<string, unknown> => (
  value && typeof value === 'object' && !Array.isArray(value)
    ? { ...(value as Record<string, unknown>) }
    : {}
);

const compactRecord = (record: Record<string, unknown>): Record<string, unknown> => (
  Object.fromEntries(
    Object.entries(record).filter(([, value]) => {
      if (value === undefined || value === null) {
        return false;
      }
      if (typeof value === 'string') {
        return value.trim().length > 0;
      }
      return true;
    }),
  )
);

const firstText = (...values: unknown[]): string => {
  for (const value of values) {
    const normalized = normalizeText(value);
    if (normalized) {
      return normalized;
    }
  }
  return '';
};

const normalizeCount = (value: unknown): number | undefined => {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return undefined;
  }
  return Math.max(0, Math.floor(parsed));
};

export function buildRuntimeFollowUpContext(params: {
  analysisSessionId?: string | null;
  analysisType: RuntimeAnalysisMode;
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
  followup_related_anchor_utc?: string | null;
  followup_related_start_time?: string | null;
  followup_related_end_time?: string | null;
  evidence_window_start?: string | null;
  evidence_window_end?: string | null;
  followupRelatedAnchorUtc?: string | null;
  followupRelatedStartTime?: string | null;
  followupRelatedEndTime?: string | null;
  evidenceWindowStart?: string | null;
  evidenceWindowEnd?: string | null;
  followupRelatedLogs?: unknown[] | null;
  followupRelatedLogCount?: number | null;
  followupRelatedMeta?: Record<string, unknown> | null;
}): Record<string, unknown> {
  const followupRelatedMeta = compactRecord(asRecord(params.followupRelatedMeta));
  const resultRecord = compactRecord(asRecord(params.result));
  const agentRecord = compactRecord(asRecord(resultRecord.agent));
  const normalizedSessionId = firstText(params.analysisSessionId);
  const normalizedSourceLogTimestamp = firstText(
    params.sourceLogTimestamp,
    followupRelatedMeta.source_log_timestamp,
  );
  const canonicalTraceId = firstText(
    params.detectedTraceId,
    params.sourceTraceId,
    resultRecord.trace_id,
    resultRecord.traceId,
    agentRecord.trace_id,
    agentRecord.traceId,
    followupRelatedMeta.trace_id,
    followupRelatedMeta.followup_related_trace_id,
  );
  const canonicalRequestId = firstText(
    params.detectedRequestId,
    params.sourceRequestId,
    resultRecord.request_id,
    resultRecord.requestId,
    agentRecord.request_id,
    agentRecord.requestId,
    followupRelatedMeta.request_id,
    followupRelatedMeta.followup_related_request_id,
  );
  const relatedLogAnchorTimestamp = firstText(
    params.followup_related_anchor_utc,
    params.followupRelatedAnchorUtc,
    normalizedSourceLogTimestamp,
    followupRelatedMeta.related_log_anchor_timestamp,
    followupRelatedMeta.followup_related_anchor_utc,
  );
  const requestFlowWindowStart = firstText(
    resultRecord.request_flow_window_start,
    params.followup_related_start_time,
    params.followupRelatedStartTime,
    params.evidence_window_start,
    params.evidenceWindowStart,
    followupRelatedMeta.request_flow_window_start,
    followupRelatedMeta.followup_related_start_time,
    followupRelatedMeta.evidence_window_start,
  );
  const requestFlowWindowEnd = firstText(
    resultRecord.request_flow_window_end,
    params.followup_related_end_time,
    params.followupRelatedEndTime,
    params.evidence_window_end,
    params.evidenceWindowEnd,
    followupRelatedMeta.request_flow_window_end,
    followupRelatedMeta.followup_related_end_time,
    followupRelatedMeta.evidence_window_end,
  );
  const evidenceWindowStart = firstText(
    params.evidence_window_start,
    params.evidenceWindowStart,
    resultRecord.request_flow_window_start,
    followupRelatedMeta.evidence_window_start,
  );
  const evidenceWindowEnd = firstText(
    params.evidence_window_end,
    params.evidenceWindowEnd,
    resultRecord.request_flow_window_end,
    followupRelatedMeta.evidence_window_end,
  );

  const baseContext: Record<string, unknown> = {
    ...followupRelatedMeta,
    session_id: normalizedSessionId || undefined,
    analysis_session_id: normalizedSessionId || undefined,
    input_text: String(params.inputText ?? ''),
    question: firstText(params.question) || undefined,
    llm_info: params.llmInfo && typeof params.llmInfo === 'object'
      ? params.llmInfo
      : undefined,
    result: params.result === undefined || params.result === null ? undefined : params.result,
    agent_mode: 'request_flow',
    source_log_timestamp: normalizedSourceLogTimestamp || undefined,
    source_trace_id: firstText(params.sourceTraceId) || undefined,
    source_request_id: firstText(params.sourceRequestId) || undefined,
    followup_related_anchor_utc: firstText(
      params.followup_related_anchor_utc,
      params.followupRelatedAnchorUtc,
      followupRelatedMeta.followup_related_anchor_utc,
    ) || undefined,
    followup_related_start_time: firstText(
      params.followup_related_start_time,
      params.followupRelatedStartTime,
      followupRelatedMeta.followup_related_start_time,
    ) || undefined,
    followup_related_end_time: firstText(
      params.followup_related_end_time,
      params.followupRelatedEndTime,
      followupRelatedMeta.followup_related_end_time,
    ) || undefined,
    evidence_window_start: evidenceWindowStart || undefined,
    evidence_window_end: evidenceWindowEnd || undefined,
    related_log_anchor_timestamp: relatedLogAnchorTimestamp || undefined,
    request_flow_window_start: requestFlowWindowStart || undefined,
    request_flow_window_end: requestFlowWindowEnd || undefined,
    request_id: canonicalRequestId || undefined,
  };

  if (Array.isArray(params.followupRelatedLogs) && params.followupRelatedLogs.length > 0) {
    baseContext.followup_related_logs = params.followupRelatedLogs;
    baseContext.followup_related_log_count = normalizeCount(params.followupRelatedLogCount)
      ?? params.followupRelatedLogs.length;
  } else {
    baseContext.followup_related_log_count = normalizeCount(params.followupRelatedLogCount);
  }

  const context = buildRuntimeAnalysisContext({
    analysisType: params.analysisType,
    traceId: canonicalTraceId,
    serviceName: params.serviceName,
    baseContext,
  });

  if (canonicalTraceId) {
    context.trace_id = canonicalTraceId;
  }

  return Object.fromEntries(
    Object.entries(context).filter(([, value]) => value !== undefined),
  );
}
