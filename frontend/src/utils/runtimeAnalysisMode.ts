export type RuntimeAnalysisMode = 'log' | 'trace';

export interface RuntimeAnalysisModeResolution {
  resolvedType: RuntimeAnalysisMode;
  downgraded: boolean;
  reason?: string;
}

export function resolveRuntimeAnalysisMode(params: {
  analysisType: RuntimeAnalysisMode;
  traceId?: string | null;
}): RuntimeAnalysisModeResolution {
  const normalizedType = params.analysisType === 'trace' ? 'trace' : 'log';
  if (normalizedType === 'trace') {
    const traceIdText = String(params.traceId || '').trim();
    if (!traceIdText) {
      return {
        resolvedType: 'log',
        downgraded: true,
        reason: 'trace_id_missing',
      };
    }
  }
  return {
    resolvedType: normalizedType,
    downgraded: normalizedType !== params.analysisType,
  };
}

export function buildRuntimeAnalysisContext(params: {
  analysisType: RuntimeAnalysisMode;
  traceId?: string | null;
  serviceName?: string | null;
  baseContext?: Record<string, unknown>;
}): Record<string, unknown> {
  const baseContext = params.baseContext && typeof params.baseContext === 'object'
    ? { ...params.baseContext }
    : {};
  const normalizedTraceId = String(params.traceId || '').trim();
  const normalizedServiceName = String(params.serviceName || '').trim();
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
  } else if (resolved.resolvedType === 'trace' && normalizedTraceId) {
    baseContext.trace_id = normalizedTraceId;
  }

  if (normalizedServiceName) {
    baseContext.service_name = normalizedServiceName;
  }

  return Object.fromEntries(
    Object.entries(baseContext).filter(([, value]) => value !== undefined),
  );
}
