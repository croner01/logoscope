/**
 * Correlation id extraction helpers used by runtime analysis flows.
 */

const asRecord = (value: unknown): Record<string, unknown> => (
  value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
);

const resolveDotPathValue = (input: Record<string, unknown>, path: string): unknown => {
  const segments = String(path || '').split('.').filter(Boolean);
  let current: unknown = input;
  for (const segment of segments) {
    if (!current || typeof current !== 'object' || Array.isArray(current)) {
      return undefined;
    }
    current = asRecord(current)[segment];
  }
  return current;
};

export const extractTraceIdFromRecord = (record?: Record<string, unknown> | null): string => {
  if (!record || typeof record !== 'object') {
    return '';
  }
  const keys = [
    'trace_id',
    'trace.id',
    'traceId',
    'trace-id',
    'trace',
    'otel.trace_id',
  ];

  for (const key of keys) {
    const direct = record[key];
    if (direct !== undefined && direct !== null) {
      const normalized = String(direct).trim();
      if (normalized) {
        return normalized;
      }
    }
    if (key.includes('.')) {
      const nested = resolveDotPathValue(record, key);
      if (nested !== undefined && nested !== null) {
        const normalized = String(nested).trim();
        if (normalized) {
          return normalized;
        }
      }
    }
  }
  return '';
};

export const extractTraceId = (value: string): string => {
  const text = String(value || '').trim();
  if (!text) {
    return '';
  }

  if (text.startsWith('{')) {
    try {
      const parsed = JSON.parse(text);
      const parsedTraceId = extractTraceIdFromRecord(asRecord(parsed));
      if (parsedTraceId) {
        return parsedTraceId;
      }
    } catch {
      // Fall through to raw-text extraction.
    }
  }

  const inlineMatch = text.match(/(?:trace(?:[_-]?id)?|trace\.id)\s*[:=]\s*([a-zA-Z0-9_-]{8,})/i);
  if (inlineMatch?.[1]) {
    return inlineMatch[1].trim();
  }

  if (/^[a-zA-Z0-9_-]{8,}$/.test(text)) {
    return text;
  }

  return '';
};

export const extractRequestIdFromRecord = (record?: Record<string, unknown> | null): string => {
  if (!record || typeof record !== 'object') {
    return '';
  }
  const keys = [
    'request_id',
    'request.id',
    'requestId',
    'req_id',
    'x-request-id',
    'x_request_id',
    'http.request_id',
    'trace.request_id',
  ];

  for (const key of keys) {
    const direct = record[key];
    if (direct !== undefined && direct !== null) {
      const normalized = String(direct).trim();
      if (normalized) {
        return normalized;
      }
    }
    if (key.includes('.')) {
      const nested = resolveDotPathValue(record, key);
      if (nested !== undefined && nested !== null) {
        const normalized = String(nested).trim();
        if (normalized) {
          return normalized;
        }
      }
    }
  }
  return '';
};

export const extractRequestId = (value: string): string => {
  const text = String(value || '').trim();
  if (!text) {
    return '';
  }

  if (text.startsWith('{')) {
    try {
      const parsed = JSON.parse(text);
      const parsedRequestId = extractRequestIdFromRecord(asRecord(parsed));
      if (parsedRequestId) {
        return parsedRequestId;
      }
    } catch {
      // Fall through to raw-text extraction.
    }
  }

  const explicitMatch = text.match(
    /(?:request[_-]?id|req[_-]?id|x-request-id)\s*[:=]\s*([a-zA-Z0-9._:-]{6,})/i,
  );
  if (explicitMatch?.[1]) {
    return explicitMatch[1].trim();
  }

  const reqPrefixMatch = text.match(/\b(req-[a-zA-Z0-9._:-]{3,})\b/i);
  if (reqPrefixMatch?.[1]) {
    return reqPrefixMatch[1].trim();
  }

  return '';
};
