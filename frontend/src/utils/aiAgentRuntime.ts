/**
 * AI agent runtime protocol helpers and shared types.
 */

export type AgentRunStatus =
  | 'queued'
  | 'running'
  | 'waiting_approval'
  | 'waiting_user_input'
  | 'blocked'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | string;

export type AIRuntimeEventVisibility = 'default' | 'debug' | string;

export type AgentRuntimeEventType =
  | 'run_started'
  | 'run_status_changed'
  | 'message_started'
  | 'reasoning_summary_delta'
  | 'reasoning_step'
  | 'tool_call_started'
  | 'tool_call_progress'
  | 'tool_call_output_delta'
  | 'tool_call_finished'
  | 'tool_call_skipped_duplicate'
  | 'approval_required'
  | 'approval_resolved'
  | 'action_waiting_approval'
  | 'action_waiting_user_input'
  | 'action_resumed'
  | 'action_replanned'
  | 'approval_timeout'
  | 'assistant_delta'
  | 'assistant_message_finalized'
  | 'run_finished'
  | 'run_failed'
  | 'run_cancelled'
  | 'run_interrupted'
  | 'error'
  | string;

export interface AgentRunSnapshot {
  run_id: string;
  session_id: string;
  conversation_id?: string;
  analysis_type: string;
  engine: string;
  runtime_version: string;
  user_message_id: string;
  assistant_message_id: string;
  service_name?: string;
  trace_id?: string;
  status: AgentRunStatus;
  question: string;
  input_json?: Record<string, unknown>;
  context_json?: Record<string, unknown>;
  summary_json?: Record<string, unknown>;
  error_code?: string;
  error_detail?: string;
  created_at?: string;
  updated_at?: string;
  ended_at?: string | null;
}

export interface AgentRunEventEnvelope {
  event_id?: string;
  run_id: string;
  seq: number;
  event_type: AgentRuntimeEventType;
  created_at?: string;
  payload: Record<string, unknown>;
}

export interface AgentRunEventsResponse {
  run_id: string;
  next_after_seq: number;
  events: AgentRunEventEnvelope[];
}

export interface AgentRunCreateRequest {
  session_id?: string;
  question: string;
  analysis_context?: Record<string, unknown>;
  runtime_options?: Record<string, unknown>;
  idempotency_key?: string;
  client_deadline_ms?: number;
  pipeline_steps?: Array<Record<string, unknown>>;
}

export interface AgentRunApproveRequest {
  approval_id: string;
  decision?: 'approved' | 'rejected' | string;
  comment?: string;
  confirmed?: boolean;
  elevated?: boolean;
}

export interface AgentRunCommandRequest {
  action_id?: string;
  step_id?: string;
  command: string;
  command_spec?: Record<string, unknown>;
  purpose: string;
  title?: string;
  tool_name?: string;
  confirmed?: boolean;
  elevated?: boolean;
  approval_token?: string;
  client_deadline_ms?: number;
  timeout_seconds?: number;
}

export interface AgentRunInputRequest {
  text: string;
  source?: string;
}

export interface AgentRunInterruptRequest {
  reason?: string;
}

export interface AgentRunStreamEventPayload {
  event: AgentRuntimeEventType;
  data: AgentRunEventEnvelope | Record<string, unknown>;
}

export interface ParsedSSEBlock {
  event: string;
  data: Record<string, unknown>;
}

export const parseAgentRuntimeEventBlock = (block: string): ParsedSSEBlock | null => {
  const text = String(block || '').trim();
  if (!text) {
    return null;
  }
  const lines = text.split(/\r?\n/);
  let eventName = 'message';
  const dataLines: string[] = [];
  lines.forEach((line) => {
    if (line.startsWith('event:')) {
      eventName = line.slice(6).trim() || 'message';
      return;
    }
    if (line.startsWith('data:')) {
      dataLines.push(line.slice(5).trimStart());
    }
  });
  if (!dataLines.length) {
    return null;
  }
  const dataText = dataLines.join('\n');
  try {
    const payload = JSON.parse(dataText);
    return {
      event: eventName,
      data: payload && typeof payload === 'object' ? payload as Record<string, unknown> : {},
    };
  } catch (_error) {
    return {
      event: eventName,
      data: {},
    };
  }
};

export const takeNextSSEEventBlock = (buffer: string): { block: string | null; rest: string } => {
  const text = String(buffer || '');
  const separator = /\r?\n\r?\n/.exec(text);
  if (!separator || typeof separator.index !== 'number') {
    return { block: null, rest: text };
  }
  return {
    block: text.slice(0, separator.index).trim(),
    rest: text.slice(separator.index + separator[0].length),
  };
};

export const normalizeAgentRunEventEnvelope = (
  value: unknown,
): AgentRunEventEnvelope | null => {
  if (!value || typeof value !== 'object') {
    return null;
  }
  const payload = value as Record<string, unknown>;
  const runId = String(payload.run_id || '').trim();
  const eventType = String(payload.event_type || '').trim();
  const seq = Number(payload.seq);
  if (!runId || !eventType || !Number.isFinite(seq)) {
    return null;
  }
  return {
    event_id: String(payload.event_id || '').trim() || undefined,
    run_id: runId,
    seq: Math.max(0, Math.floor(seq)),
    event_type: eventType,
    created_at: String(payload.created_at || '').trim() || undefined,
    payload: payload.payload && typeof payload.payload === 'object'
      ? payload.payload as Record<string, unknown>
      : {},
  };
};
