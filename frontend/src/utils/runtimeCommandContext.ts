/**
 * Shared runtime command context builder.
 */

import {
  buildRuntimeAnalysisContext,
  type RuntimeAnalysisMode,
} from './runtimeAnalysisMode.js';

const normalizeText = (value: unknown): string => String(value ?? '').trim();

export function buildRuntimeCommandAnalysisContext(params: {
  analysisType: RuntimeAnalysisMode;
  traceId?: string | null;
  requestId?: string | null;
  serviceName?: string | null;
  sourceMessageId?: string | null;
  sourceCommand?: string | null;
}): Record<string, unknown> {
  const normalizedRequestId = normalizeText(params.requestId);

  return buildRuntimeAnalysisContext({
    analysisType: params.analysisType,
    traceId: params.traceId,
    serviceName: params.serviceName,
    baseContext: {
      source_message_id: normalizeText(params.sourceMessageId) || undefined,
      source_command: normalizeText(params.sourceCommand) || undefined,
      request_id: normalizedRequestId || undefined,
      source_request_id: normalizedRequestId || undefined,
      agent_mode: 'followup_command_runtime',
    },
  });
}
