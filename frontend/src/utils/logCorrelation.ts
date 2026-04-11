import type { Event } from './api';

const REQUEST_ID_CANDIDATE_KEYS = ['correlation_request_id', 'request_id', 'x_request_id'] as const;
const TRACE_ID_CANDIDATE_KEYS = ['correlation_trace_id', 'trace_id'] as const;

function normalizeValue(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function pushUnique(target: string[], seen: Set<string>, value: unknown): void {
  const normalized = normalizeValue(value);
  if (!normalized || seen.has(normalized)) {
    return;
  }
  seen.add(normalized);
  target.push(normalized);
}

function readNestedObjectValue(source: unknown, path: readonly string[]): unknown {
  let current: unknown = source;
  for (const segment of path) {
    if (!current || typeof current !== 'object' || Array.isArray(current)) {
      return undefined;
    }
    current = (current as Record<string, unknown>)[segment];
  }
  return current;
}

export function extractEventRequestIds(event?: Partial<Event> | null): string[] {
  const values: string[] = [];
  const seen = new Set<string>();
  const attributes = event?.attributes;

  for (const key of REQUEST_ID_CANDIDATE_KEYS) {
    pushUnique(values, seen, attributes?.[key]);
  }
  pushUnique(values, seen, readNestedObjectValue(attributes, ['request', 'id']));
  pushUnique(values, seen, readNestedObjectValue(attributes, ['http', 'request_id']));
  pushUnique(values, seen, readNestedObjectValue(attributes, ['trace', 'request_id']));

  return values;
}

export function extractEventTraceIds(event?: Partial<Event> | null): string[] {
  const values: string[] = [];
  const seen = new Set<string>();
  const attributes = event?.attributes;

  pushUnique(values, seen, event?.trace_id);
  for (const key of TRACE_ID_CANDIDATE_KEYS) {
    pushUnique(values, seen, attributes?.[key]);
  }
  pushUnique(values, seen, readNestedObjectValue(attributes, ['trace', 'id']));

  return values;
}
