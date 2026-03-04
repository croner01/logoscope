/**
 * 日志消息格式化工具
 *
 * 兼容 OTel/Fluent Bit 常见包装格式：
 * {"log":"...","stream":"stderr","time":"..."}
 */

export interface ParsedLogMeta {
  wrapped: boolean;
  stream?: string;
  collector_time?: string;
  line_count: number;
}

export interface ParsedLogMessage {
  message: string;
  summary: string;
  meta: ParsedLogMeta;
}

const SUMMARY_LIMIT = 220;

function normalizeLineEndings(value: string): string {
  return value.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
}

function stripTrailingLineBreaks(value: string): string {
  return value.replace(/\n+$/g, '');
}

function summarizeMessage(value: string): string {
  const singleLine = value.replace(/\n+/g, ' ').replace(/\s+/g, ' ').trim();
  if (!singleLine) {
    return '';
  }
  if (singleLine.length <= SUMMARY_LIMIT) {
    return singleLine;
  }
  return `${singleLine.slice(0, SUMMARY_LIMIT - 3)}...`;
}

function tryParseEnvelope(raw: string): Record<string, unknown> | null {
  const trimmed = raw.trim();
  if (!trimmed.startsWith('{') || !trimmed.endsWith('}')) {
    return null;
  }

  try {
    const parsed = JSON.parse(trimmed);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
    if (typeof parsed === 'string') {
      const nested = JSON.parse(parsed);
      if (nested && typeof nested === 'object' && !Array.isArray(nested)) {
        return nested as Record<string, unknown>;
      }
    }
  } catch {
    return null;
  }

  return null;
}

export function parseLogMessage(raw: unknown): ParsedLogMessage {
  const source = typeof raw === 'string' ? raw : String(raw ?? '');
  const envelope = tryParseEnvelope(source);

  let message = source;
  let wrapped = false;
  let stream: string | undefined;
  let collectorTime: string | undefined;

  if (envelope) {
    const envelopeLog = typeof envelope.log === 'string' ? envelope.log : null;

    if (envelopeLog !== null) {
      message = envelopeLog;
      wrapped = true;
    }

    if (typeof envelope.stream === 'string') {
      stream = envelope.stream;
    }

    if (typeof envelope.time === 'string') {
      collectorTime = envelope.time;
    }
  }

  const normalized = stripTrailingLineBreaks(normalizeLineEndings(message));
  const lineCount = normalized ? normalized.split('\n').length : 0;

  return {
    message: normalized,
    summary: summarizeMessage(normalized),
    meta: {
      wrapped,
      stream,
      collector_time: collectorTime,
      line_count: lineCount,
    },
  };
}
