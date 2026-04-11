export interface RuntimeStreamGuardParams {
  streamToken: number;
  currentToken: number;
  streamRunId: string;
  activeRunId?: string | null;
}

const normalizeToken = (value: unknown): number => {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed < 0) {
    return 0;
  }
  return Math.floor(parsed);
};

const normalizeRunId = (value: unknown): string => (
  typeof value === 'string' ? value.trim() : value === null || value === undefined ? '' : String(value).trim()
);

export const createNextRuntimeStreamToken = (currentToken: number): number => (
  normalizeToken(currentToken) + 1
);

export const shouldHandleRuntimeStreamMutation = (params: RuntimeStreamGuardParams): boolean => {
  const streamRunId = normalizeRunId(params.streamRunId);
  const activeRunId = normalizeRunId(params.activeRunId);
  if (!streamRunId || !activeRunId) {
    return false;
  }
  return streamRunId === activeRunId && normalizeToken(params.streamToken) === normalizeToken(params.currentToken);
};
