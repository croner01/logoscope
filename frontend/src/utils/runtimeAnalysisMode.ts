/**
 * Runtime analysis mode resolution and normalized context helpers.
 */

export type RuntimeAnalysisMode = 'log' | 'trace';

export interface RuntimeAnalysisModeResolution {
  resolvedType: RuntimeAnalysisMode;
  downgraded: boolean;
  reason?: 'trace_id_missing';
}

const normalizeText = (value: unknown): string => String(value ?? '').trim();

export const resolveRuntimeAnalysisMode = (params: {
  analysisType: RuntimeAnalysisMode;
  traceId?: string | null;
}): RuntimeAnalysisModeResolution => {
  const normalizedTraceId = normalizeText(params.traceId);
  if (params.analysisType === 'trace' && !normalizedTraceId) {
    return {
      resolvedType: 'log',
      downgraded: true,
      reason: 'trace_id_missing',
    };
  }
  return {
    resolvedType: params.analysisType,
    downgraded: false,
  };
};

export function buildRuntimeAnalysisContext(params: {
  analysisType: RuntimeAnalysisMode;
  traceId?: string | null;
  serviceName?: string | null;
  baseContext?: Record<string, unknown>;
}): Record<string, unknown> {
  const baseContext = params.baseContext && typeof params.baseContext === 'object'
    ? { ...params.baseContext }
    : {};
  const normalizedTraceId = normalizeText(params.traceId);
  const normalizedServiceName = normalizeText(params.serviceName);
  const resolved = resolveRuntimeAnalysisMode({
    analysisType: params.analysisType,
    traceId: normalizedTraceId,
  });

  baseContext.analysis_type = resolved.resolvedType;
  if (resolved.downgraded) {
    baseContext.analysis_type_original = params.analysisType;
    baseContext.analysis_type_downgraded = true;
    baseContext.analysis_type_downgrade_reason = resolved.reason;
    delete baseContext.trace_id;
  } else {
    delete baseContext.analysis_type_original;
    delete baseContext.analysis_type_downgraded;
    delete baseContext.analysis_type_downgrade_reason;
    if (normalizedTraceId) {
      baseContext.trace_id = normalizedTraceId;
    } else {
      delete baseContext.trace_id;
    }
  }

  if (normalizedServiceName) {
    baseContext.service_name = normalizedServiceName;
  }
  return Object.fromEntries(
    Object.entries(baseContext).filter(([, value]) => value !== undefined),
  );
}
