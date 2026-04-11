/**
 * AI agent runtime reducer and selectors.
 */

import type { AgentRunEventEnvelope, AgentRunSnapshot, AgentRunStatus } from './aiAgentRuntime';

export interface AgentRunState {
  hydrated: boolean;
  hydrating: boolean;
  streaming: boolean;
  streamError?: string;
  lastSeq: number;
  runMeta: RunMetaState | null;
  entities: RunEntityState;
}

export interface RunMetaState {
  runId: string;
  sessionId: string;
  status: AgentRunStatus;
  analysisType: string;
  engine: string;
  assistantMessageId: string;
  userMessageId?: string;
  serviceName?: string;
  traceId?: string;
  createdAt?: string;
  updatedAt?: string;
  endedAt?: string | null;
  currentPhase?: string;
  iteration?: number;
  summaryJson?: Record<string, unknown>;
}

export interface MessageEntity {
  messageId: string;
  role: 'user' | 'assistant';
  content: string;
  finalized: boolean;
  references: Array<Record<string, unknown>>;
  metadata?: Record<string, unknown>;
  createdAt?: string;
  updatedAt?: string;
}

export interface ReasoningStepEntity {
  stepId: string;
  phase: string;
  title: string;
  status: string;
  iteration?: number;
  summaryText: string;
  createdAt?: string;
  updatedAt?: string;
}

export interface ToolCallEntity {
  toolCallId: string;
  stepId?: string;
  toolName: string;
  title?: string;
  status: string;
  input?: Record<string, unknown>;
  summary?: Record<string, unknown>;
  commandRunId?: string;
  createdAt?: string;
  updatedAt?: string;
}

export interface CommandRunEntity {
  commandRunId: string;
  toolCallId?: string;
  actionId?: string;
  command: string;
  commandType?: string;
  riskLevel?: string;
  commandFamily?: string;
  approvalPolicy?: string;
  executorType?: string;
  executorProfile?: string;
  targetKind?: string;
  targetIdentity?: string;
  effectiveExecutorType?: string;
  effectiveExecutorProfile?: string;
  dispatchBackend?: string;
  dispatchMode?: string;
  dispatchReason?: string;
  targetClusterId?: string;
  targetNamespace?: string;
  targetNodeName?: string;
  resolvedTargetContext?: Record<string, unknown>;
  message?: string;
  status: string;
  stdout: string;
  stderr: string;
  outputTruncated?: boolean;
  exitCode?: number;
  timedOut?: boolean;
  createdAt?: string;
  updatedAt?: string;
  endedAt?: string | null;
}

export interface ApprovalEntity {
  approvalId: string;
  toolCallId?: string;
  actionId?: string;
  title?: string;
  command: string;
  purpose?: string;
  commandType?: string;
  riskLevel?: string;
  commandFamily?: string;
  approvalPolicy?: string;
  executorType?: string;
  executorProfile?: string;
  targetKind?: string;
  targetIdentity?: string;
  effectiveExecutorType?: string;
  effectiveExecutorProfile?: string;
  dispatchBackend?: string;
  dispatchMode?: string;
  dispatchReason?: string;
  requiresConfirmation: boolean;
  requiresElevation: boolean;
  status: string;
  message?: string;
  createdAt?: string;
  updatedAt?: string;
}

export interface RunEntityState {
  messagesById: Record<string, MessageEntity>;
  messageOrder: string[];
  stepsById: Record<string, ReasoningStepEntity>;
  stepOrder: string[];
  toolCallsById: Record<string, ToolCallEntity>;
  toolCallOrder: string[];
  commandRunsById: Record<string, CommandRunEntity>;
  commandRunOrder: string[];
  approvalsById: Record<string, ApprovalEntity>;
  approvalOrder: string[];
  events: AgentRunEventEnvelope[];
}

export type AgentRunReducerAction =
  | { type: 'hydrate_snapshot'; payload: { run: AgentRunSnapshot } }
  | { type: 'hydrate_events'; payload: { events: AgentRunEventEnvelope[] } }
  | { type: 'append_event'; payload: { event: AgentRunEventEnvelope } }
  | { type: 'set_streaming'; payload: { streaming: boolean } }
  | { type: 'set_stream_error'; payload: { error?: string } }
  | { type: 'reset'; payload?: { runId?: string } };

const emptyEntities = (): RunEntityState => ({
  messagesById: {},
  messageOrder: [],
  stepsById: {},
  stepOrder: [],
  toolCallsById: {},
  toolCallOrder: [],
  commandRunsById: {},
  commandRunOrder: [],
  approvalsById: {},
  approvalOrder: [],
  events: [],
});

export const createInitialAgentRunState = (): AgentRunState => ({
  hydrated: false,
  hydrating: false,
  streaming: false,
  streamError: undefined,
  lastSeq: 0,
  runMeta: null,
  entities: emptyEntities(),
});

const asText = (value: unknown): string => {
  if (typeof value === 'string') {
    return value;
  }
  if (value === null || value === undefined) {
    return '';
  }
  return String(value);
};

const ensureOrderedId = (order: string[], id: string): string[] => (
  order.includes(id) ? order : [...order, id]
);

const buildSyntheticCommandRunId = (toolCallId: string): string => `tool-${toolCallId}`;

const resolveCommandRunId = (
  payload: Record<string, unknown>,
  toolCallId: string,
  entities: RunEntityState,
): string => {
  const explicit = asText(payload.command_run_id);
  if (explicit) {
    return explicit;
  }
  const existingByTool = toolCallId ? asText(entities.toolCallsById[toolCallId]?.commandRunId) : '';
  if (existingByTool) {
    return existingByTool;
  }
  return toolCallId ? buildSyntheticCommandRunId(toolCallId) : '';
};

const mergeTerminalOutput = (params: {
  current: string;
  next: string;
  outputTruncated: boolean;
}): string => {
  const currentText = asText(params.current);
  const nextText = asText(params.next);
  if (!nextText) {
    return currentText;
  }
  if (!params.outputTruncated) {
    return nextText;
  }
  // Preserve streamed content when terminal payload is marked truncated.
  return currentText.length >= nextText.length ? currentText : nextText;
};

const applyRunSnapshot = (state: AgentRunState, run: AgentRunSnapshot): AgentRunState => ({
  ...state,
  hydrated: true,
  hydrating: false,
  runMeta: {
    runId: run.run_id,
    sessionId: run.session_id,
    status: run.status,
    analysisType: run.analysis_type,
    engine: run.engine,
    assistantMessageId: run.assistant_message_id,
    userMessageId: run.user_message_id || undefined,
    serviceName: run.service_name || undefined,
    traceId: run.trace_id || undefined,
    createdAt: run.created_at || undefined,
    updatedAt: run.updated_at || undefined,
    endedAt: run.ended_at,
    currentPhase: asText(run.summary_json?.current_phase) || undefined,
    iteration: Number.isFinite(Number(run.summary_json?.iteration))
      ? Number(run.summary_json?.iteration)
      : undefined,
    summaryJson: run.summary_json && typeof run.summary_json === 'object'
      ? run.summary_json
      : undefined,
  },
});

const appendEventEnvelope = (state: AgentRunState, event: AgentRunEventEnvelope): AgentRunState => {
  if (event.seq <= state.lastSeq) {
    return state;
  }

  let nextState = {
    ...state,
    hydrated: true,
    hydrating: false,
    streamError: undefined,
    lastSeq: event.seq,
    entities: {
      ...state.entities,
      events: [...state.entities.events, event],
    },
  };

  const payload = event.payload || {};
  const eventAt = event.created_at || undefined;

  if (event.event_type === 'run_started') {
    nextState = {
      ...nextState,
      runMeta: nextState.runMeta
        ? {
            ...nextState.runMeta,
            status: asText(payload.status) || nextState.runMeta.status,
            analysisType: asText(payload.analysis_type) || nextState.runMeta.analysisType,
            engine: asText(payload.engine) || nextState.runMeta.engine,
          }
        : nextState.runMeta,
    };
    return nextState;
  }

  if (event.event_type === 'run_status_changed') {
    nextState = {
      ...nextState,
      runMeta: nextState.runMeta
        ? {
            ...nextState.runMeta,
            status: asText(payload.status) || nextState.runMeta.status,
            currentPhase: asText(payload.current_phase) || nextState.runMeta.currentPhase,
            updatedAt: eventAt || nextState.runMeta.updatedAt,
          }
        : nextState.runMeta,
    };
    return nextState;
  }

  if (event.event_type === 'message_started') {
    const messageId = asText(payload.assistant_message_id);
    if (!messageId) {
      return nextState;
    }
    return {
      ...nextState,
      entities: {
        ...nextState.entities,
        messagesById: {
          ...nextState.entities.messagesById,
          [messageId]: {
            messageId,
            role: 'assistant',
            content: nextState.entities.messagesById[messageId]?.content || '',
            finalized: Boolean(nextState.entities.messagesById[messageId]?.finalized),
            references: nextState.entities.messagesById[messageId]?.references || [],
            metadata: nextState.entities.messagesById[messageId]?.metadata,
            createdAt: nextState.entities.messagesById[messageId]?.createdAt || eventAt,
            updatedAt: eventAt,
          },
        },
        messageOrder: ensureOrderedId(nextState.entities.messageOrder, messageId),
      },
      runMeta: nextState.runMeta
        ? {
            ...nextState.runMeta,
            assistantMessageId: messageId,
          }
        : nextState.runMeta,
    };
  }

  if (event.event_type === 'assistant_delta' || event.event_type === 'assistant_message_finalized') {
    const messageId = asText(payload.assistant_message_id || nextState.runMeta?.assistantMessageId);
    if (!messageId) {
      return nextState;
    }
    const current = nextState.entities.messagesById[messageId] || {
      messageId,
      role: 'assistant' as const,
      content: '',
      finalized: false,
      references: [],
      metadata: undefined,
      createdAt: eventAt,
      updatedAt: eventAt,
    };
    const contentDelta = event.event_type === 'assistant_delta'
      ? asText(payload.text)
      : asText(payload.content);
    return {
      ...nextState,
      entities: {
        ...nextState.entities,
        messagesById: {
          ...nextState.entities.messagesById,
          [messageId]: {
            ...current,
            content: event.event_type === 'assistant_delta'
              ? `${current.content}${contentDelta}`
              : (contentDelta || current.content),
            finalized: event.event_type === 'assistant_message_finalized' ? true : current.finalized,
            references: Array.isArray(payload.references)
              ? payload.references as Array<Record<string, unknown>>
              : current.references,
            metadata: event.event_type === 'assistant_message_finalized'
              ? (
                  payload.metadata && typeof payload.metadata === 'object'
                    ? payload.metadata as Record<string, unknown>
                    : current.metadata
                )
              : current.metadata,
            updatedAt: eventAt,
          },
        },
        messageOrder: ensureOrderedId(nextState.entities.messageOrder, messageId),
      },
    };
  }

  if (event.event_type === 'reasoning_step' || event.event_type === 'reasoning_summary_delta') {
    const stepId = asText(payload.step_id);
    if (!stepId) {
      return nextState;
    }
    const current = nextState.entities.stepsById[stepId] || {
      stepId,
      phase: asText(payload.phase),
      title: asText(payload.title),
      status: asText(payload.status) || 'pending',
      iteration: Number.isFinite(Number(payload.iteration)) ? Number(payload.iteration) : undefined,
      summaryText: '',
      createdAt: eventAt,
      updatedAt: eventAt,
    };
    return {
      ...nextState,
      entities: {
        ...nextState.entities,
        stepsById: {
          ...nextState.entities.stepsById,
          [stepId]: {
            ...current,
            phase: asText(payload.phase) || current.phase,
            title: asText(payload.title) || current.title,
            status: asText(payload.status) || current.status,
            iteration: Number.isFinite(Number(payload.iteration)) ? Number(payload.iteration) : current.iteration,
            summaryText: event.event_type === 'reasoning_summary_delta'
              ? `${current.summaryText}${asText(payload.text)}`
              : current.summaryText,
            updatedAt: eventAt,
          },
        },
        stepOrder: ensureOrderedId(nextState.entities.stepOrder, stepId),
      },
      runMeta: nextState.runMeta
        ? {
            ...nextState.runMeta,
            currentPhase: asText(payload.phase) || nextState.runMeta.currentPhase,
            iteration: Number.isFinite(Number(payload.iteration))
              ? Number(payload.iteration)
              : nextState.runMeta.iteration,
          }
        : nextState.runMeta,
    };
  }

  if (event.event_type === 'tool_call_finished' || event.event_type === 'tool_call_skipped_duplicate') {
    const toolCallId = asText(payload.tool_call_id);
    const nextEntities = { ...nextState.entities };
    const commandRunId = resolveCommandRunId(payload, toolCallId, nextEntities);
    if (toolCallId) {
      const currentToolCall = nextEntities.toolCallsById[toolCallId] || {
        toolCallId,
        toolName: asText(payload.tool_name),
        title: asText(payload.title) || undefined,
        status: asText(payload.status) || 'completed',
        createdAt: eventAt,
        updatedAt: eventAt,
      };
      nextEntities.toolCallsById = {
        ...nextEntities.toolCallsById,
        [toolCallId]: {
          ...currentToolCall,
          toolName: asText(payload.tool_name) || currentToolCall.toolName,
          title: asText(payload.title) || currentToolCall.title,
          status: asText(payload.status) || currentToolCall.status,
          commandRunId: commandRunId || currentToolCall.commandRunId,
          summary: { ...(currentToolCall.summary || {}), ...payload },
          updatedAt: eventAt,
        },
      };
      nextEntities.toolCallOrder = ensureOrderedId(nextEntities.toolCallOrder, toolCallId);
    }
    if (commandRunId) {
      const currentRun = nextEntities.commandRunsById[commandRunId] || {
        commandRunId,
        toolCallId: toolCallId || undefined,
        actionId: asText(payload.action_id) || undefined,
        command: asText(payload.command),
        commandType: asText(payload.command_type) || undefined,
        riskLevel: asText(payload.risk_level) || undefined,
        commandFamily: asText(payload.command_family) || undefined,
        approvalPolicy: asText(payload.approval_policy) || undefined,
        executorType: asText(payload.executor_type) || undefined,
        executorProfile: asText(payload.executor_profile) || undefined,
        targetKind: asText(payload.target_kind) || undefined,
        targetIdentity: asText(payload.target_identity) || undefined,
        effectiveExecutorType: asText(payload.effective_executor_type) || undefined,
        effectiveExecutorProfile: asText(payload.effective_executor_profile) || undefined,
        dispatchBackend: asText(payload.dispatch_backend) || undefined,
        dispatchMode: asText(payload.dispatch_mode) || undefined,
        dispatchReason: asText(payload.dispatch_reason) || undefined,
        targetClusterId: asText(payload.target_cluster_id) || undefined,
        targetNamespace: asText(payload.target_namespace) || undefined,
        targetNodeName: asText(payload.target_node_name) || undefined,
        resolvedTargetContext: (
          payload.resolved_target_context && typeof payload.resolved_target_context === 'object'
            ? payload.resolved_target_context as Record<string, unknown>
            : undefined
        ),
        message: asText(payload.message) || undefined,
        status: asText(payload.status) || 'completed',
        stdout: '',
        stderr: '',
        createdAt: eventAt,
        updatedAt: eventAt,
        endedAt: eventAt || null,
      };
      nextEntities.commandRunsById = {
        ...nextEntities.commandRunsById,
        [commandRunId]: {
          ...currentRun,
          toolCallId: toolCallId || currentRun.toolCallId,
          actionId: asText(payload.action_id) || currentRun.actionId,
          command: asText(payload.command) || currentRun.command,
          commandType: asText(payload.command_type) || currentRun.commandType,
          riskLevel: asText(payload.risk_level) || currentRun.riskLevel,
          commandFamily: asText(payload.command_family) || currentRun.commandFamily,
          approvalPolicy: asText(payload.approval_policy) || currentRun.approvalPolicy,
          executorType: asText(payload.executor_type) || currentRun.executorType,
          executorProfile: asText(payload.executor_profile) || currentRun.executorProfile,
          targetKind: asText(payload.target_kind) || currentRun.targetKind,
          targetIdentity: asText(payload.target_identity) || currentRun.targetIdentity,
          effectiveExecutorType: asText(payload.effective_executor_type) || currentRun.effectiveExecutorType,
          effectiveExecutorProfile: asText(payload.effective_executor_profile) || currentRun.effectiveExecutorProfile,
          dispatchBackend: asText(payload.dispatch_backend) || currentRun.dispatchBackend,
          dispatchMode: asText(payload.dispatch_mode) || currentRun.dispatchMode,
          dispatchReason: asText(payload.dispatch_reason) || currentRun.dispatchReason,
          targetClusterId: asText(payload.target_cluster_id) || currentRun.targetClusterId,
          targetNamespace: asText(payload.target_namespace) || currentRun.targetNamespace,
          targetNodeName: asText(payload.target_node_name) || currentRun.targetNodeName,
          resolvedTargetContext: (
            payload.resolved_target_context && typeof payload.resolved_target_context === 'object'
              ? payload.resolved_target_context as Record<string, unknown>
              : currentRun.resolvedTargetContext
          ),
          message: asText(payload.message) || currentRun.message,
          status: asText(payload.status) || currentRun.status,
          stdout: mergeTerminalOutput({
            current: currentRun.stdout,
            next: asText(payload.stdout),
            outputTruncated: Boolean(payload.output_truncated),
          }),
          stderr: mergeTerminalOutput({
            current: currentRun.stderr,
            next: asText(payload.stderr),
            outputTruncated: Boolean(payload.output_truncated),
          }),
          outputTruncated: Boolean(payload.output_truncated) || currentRun.outputTruncated,
          exitCode: Number.isFinite(Number(payload.exit_code)) ? Number(payload.exit_code) : currentRun.exitCode,
          timedOut: Boolean(payload.timed_out) || currentRun.timedOut,
          updatedAt: eventAt,
          endedAt: eventAt || currentRun.endedAt || null,
        },
      };
      nextEntities.commandRunOrder = ensureOrderedId(nextEntities.commandRunOrder, commandRunId);
    }
    return {
      ...nextState,
      entities: nextEntities,
    };
  }

  if (event.event_type === 'tool_call_started' || event.event_type === 'tool_call_progress') {
    const toolCallId = asText(payload.tool_call_id);
    if (!toolCallId) {
      return nextState;
    }
    const commandRunId = resolveCommandRunId(payload, toolCallId, nextState.entities);
    const current = nextState.entities.toolCallsById[toolCallId] || {
      toolCallId,
      toolName: asText(payload.tool_name),
      title: asText(payload.title) || undefined,
      status: asText(payload.status) || 'pending',
      stepId: asText(payload.step_id) || undefined,
      input: undefined,
      summary: undefined,
      commandRunId: commandRunId || undefined,
      createdAt: eventAt,
      updatedAt: eventAt,
    };
    const nextEntities: RunEntityState = {
      ...nextState.entities,
      toolCallsById: {
        ...nextState.entities.toolCallsById,
        [toolCallId]: {
          ...current,
          toolName: asText(payload.tool_name) || current.toolName,
          title: asText(payload.title) || current.title,
          status: asText(payload.status) || current.status,
          stepId: asText(payload.step_id) || current.stepId,
          summary: { ...(current.summary || {}), ...payload },
          commandRunId: commandRunId || current.commandRunId,
          updatedAt: eventAt,
        },
      },
      toolCallOrder: ensureOrderedId(nextState.entities.toolCallOrder, toolCallId),
      commandRunsById: nextState.entities.commandRunsById,
      commandRunOrder: nextState.entities.commandRunOrder,
      messagesById: nextState.entities.messagesById,
      messageOrder: nextState.entities.messageOrder,
      stepsById: nextState.entities.stepsById,
      stepOrder: nextState.entities.stepOrder,
      approvalsById: nextState.entities.approvalsById,
      approvalOrder: nextState.entities.approvalOrder,
      events: nextState.entities.events,
    };
    if (commandRunId) {
      const currentCommandRun = nextState.entities.commandRunsById[commandRunId] || {
        commandRunId,
        toolCallId,
        actionId: asText(payload.action_id) || undefined,
        command: asText(payload.command),
        commandType: asText(payload.command_type) || undefined,
        riskLevel: asText(payload.risk_level) || undefined,
        commandFamily: asText(payload.command_family) || undefined,
        approvalPolicy: asText(payload.approval_policy) || undefined,
        executorType: asText(payload.executor_type) || undefined,
        executorProfile: asText(payload.executor_profile) || undefined,
        targetKind: asText(payload.target_kind) || undefined,
        targetIdentity: asText(payload.target_identity) || undefined,
        effectiveExecutorType: asText(payload.effective_executor_type) || undefined,
        effectiveExecutorProfile: asText(payload.effective_executor_profile) || undefined,
        dispatchBackend: asText(payload.dispatch_backend) || undefined,
        dispatchMode: asText(payload.dispatch_mode) || undefined,
        dispatchReason: asText(payload.dispatch_reason) || undefined,
        targetClusterId: asText(payload.target_cluster_id) || undefined,
        targetNamespace: asText(payload.target_namespace) || undefined,
        targetNodeName: asText(payload.target_node_name) || undefined,
        resolvedTargetContext: (
          payload.resolved_target_context && typeof payload.resolved_target_context === 'object'
            ? payload.resolved_target_context as Record<string, unknown>
            : undefined
        ),
        message: asText(payload.message) || undefined,
        status: asText(payload.status) || 'running',
        stdout: '',
        stderr: '',
        createdAt: eventAt,
        updatedAt: eventAt,
        endedAt: null,
      };
      nextEntities.commandRunsById = {
        ...nextEntities.commandRunsById,
        [commandRunId]: {
          ...currentCommandRun,
          toolCallId,
          actionId: asText(payload.action_id) || currentCommandRun.actionId,
          command: asText(payload.command) || currentCommandRun.command,
          commandType: asText(payload.command_type) || currentCommandRun.commandType,
          riskLevel: asText(payload.risk_level) || currentCommandRun.riskLevel,
          commandFamily: asText(payload.command_family) || currentCommandRun.commandFamily,
          approvalPolicy: asText(payload.approval_policy) || currentCommandRun.approvalPolicy,
          executorType: asText(payload.executor_type) || currentCommandRun.executorType,
          executorProfile: asText(payload.executor_profile) || currentCommandRun.executorProfile,
          targetKind: asText(payload.target_kind) || currentCommandRun.targetKind,
          targetIdentity: asText(payload.target_identity) || currentCommandRun.targetIdentity,
          effectiveExecutorType: asText(payload.effective_executor_type) || currentCommandRun.effectiveExecutorType,
          effectiveExecutorProfile: asText(payload.effective_executor_profile) || currentCommandRun.effectiveExecutorProfile,
          dispatchBackend: asText(payload.dispatch_backend) || currentCommandRun.dispatchBackend,
          dispatchMode: asText(payload.dispatch_mode) || currentCommandRun.dispatchMode,
          dispatchReason: asText(payload.dispatch_reason) || currentCommandRun.dispatchReason,
          targetClusterId: asText(payload.target_cluster_id) || currentCommandRun.targetClusterId,
          targetNamespace: asText(payload.target_namespace) || currentCommandRun.targetNamespace,
          targetNodeName: asText(payload.target_node_name) || currentCommandRun.targetNodeName,
          resolvedTargetContext: (
            payload.resolved_target_context && typeof payload.resolved_target_context === 'object'
              ? payload.resolved_target_context as Record<string, unknown>
              : currentCommandRun.resolvedTargetContext
          ),
          message: asText(payload.message) || currentCommandRun.message,
          status: asText(payload.status) || currentCommandRun.status,
          updatedAt: eventAt,
        },
      };
      nextEntities.commandRunOrder = ensureOrderedId(nextEntities.commandRunOrder, commandRunId);
    }
    return {
      ...nextState,
      entities: nextEntities,
    };
  }

  if (event.event_type === 'tool_call_output_delta') {
    const toolCallId = asText(payload.tool_call_id);
    const commandRunId = resolveCommandRunId(payload, toolCallId, nextState.entities);
    if (!commandRunId) {
      return nextState;
    }
    const current = nextState.entities.commandRunsById[commandRunId] || {
      commandRunId,
      toolCallId: asText(payload.tool_call_id) || undefined,
      actionId: asText(payload.action_id) || undefined,
      command: asText(payload.command),
      commandType: asText(payload.command_type) || undefined,
      riskLevel: asText(payload.risk_level) || undefined,
      commandFamily: asText(payload.command_family) || undefined,
      approvalPolicy: asText(payload.approval_policy) || undefined,
      executorType: asText(payload.executor_type) || undefined,
      executorProfile: asText(payload.executor_profile) || undefined,
      targetKind: asText(payload.target_kind) || undefined,
      targetIdentity: asText(payload.target_identity) || undefined,
      effectiveExecutorType: asText(payload.effective_executor_type) || undefined,
      effectiveExecutorProfile: asText(payload.effective_executor_profile) || undefined,
      dispatchBackend: asText(payload.dispatch_backend) || undefined,
      dispatchMode: asText(payload.dispatch_mode) || undefined,
      dispatchReason: asText(payload.dispatch_reason) || undefined,
      targetClusterId: asText(payload.target_cluster_id) || undefined,
      targetNamespace: asText(payload.target_namespace) || undefined,
      targetNodeName: asText(payload.target_node_name) || undefined,
      resolvedTargetContext: (
        payload.resolved_target_context && typeof payload.resolved_target_context === 'object'
          ? payload.resolved_target_context as Record<string, unknown>
          : undefined
      ),
      message: asText(payload.message) || undefined,
      status: 'running',
      stdout: '',
      stderr: '',
      createdAt: eventAt,
      updatedAt: eventAt,
      endedAt: null,
    };
    const stream = asText(payload.stream || 'stdout').toLowerCase();
    const text = asText(payload.text);
    return {
      ...nextState,
      entities: {
        ...nextState.entities,
        commandRunsById: {
          ...nextState.entities.commandRunsById,
          [commandRunId]: {
            ...current,
            toolCallId: asText(payload.tool_call_id) || current.toolCallId,
            command: asText(payload.command) || current.command,
            commandType: asText(payload.command_type) || current.commandType,
            riskLevel: asText(payload.risk_level) || current.riskLevel,
            commandFamily: asText(payload.command_family) || current.commandFamily,
            approvalPolicy: asText(payload.approval_policy) || current.approvalPolicy,
            executorType: asText(payload.executor_type) || current.executorType,
            executorProfile: asText(payload.executor_profile) || current.executorProfile,
            targetKind: asText(payload.target_kind) || current.targetKind,
            targetIdentity: asText(payload.target_identity) || current.targetIdentity,
            effectiveExecutorType: asText(payload.effective_executor_type) || current.effectiveExecutorType,
            effectiveExecutorProfile: asText(payload.effective_executor_profile) || current.effectiveExecutorProfile,
            dispatchBackend: asText(payload.dispatch_backend) || current.dispatchBackend,
            dispatchMode: asText(payload.dispatch_mode) || current.dispatchMode,
            dispatchReason: asText(payload.dispatch_reason) || current.dispatchReason,
            targetClusterId: asText(payload.target_cluster_id) || current.targetClusterId,
            targetNamespace: asText(payload.target_namespace) || current.targetNamespace,
            targetNodeName: asText(payload.target_node_name) || current.targetNodeName,
            resolvedTargetContext: (
              payload.resolved_target_context && typeof payload.resolved_target_context === 'object'
                ? payload.resolved_target_context as Record<string, unknown>
                : current.resolvedTargetContext
            ),
            message: asText(payload.message) || current.message,
            status: 'running',
            stdout: stream === 'stdout' ? `${current.stdout}${text}` : current.stdout,
            stderr: stream === 'stderr' ? `${current.stderr}${text}` : current.stderr,
            outputTruncated: Boolean(payload.output_truncated) || current.outputTruncated,
            updatedAt: eventAt,
          },
        },
        commandRunOrder: ensureOrderedId(nextState.entities.commandRunOrder, commandRunId),
      },
    };
  }

  if (event.event_type === 'approval_required' || event.event_type === 'approval_resolved') {
    const approvalId = asText(payload.approval_id);
    if (!approvalId) {
      return nextState;
    }
    const current = nextState.entities.approvalsById[approvalId] || {
      approvalId,
      toolCallId: asText(payload.tool_call_id) || undefined,
      actionId: asText(payload.action_id) || undefined,
      title: asText(payload.title) || undefined,
      command: asText(payload.command),
      commandType: asText(payload.command_type) || undefined,
      riskLevel: asText(payload.risk_level) || undefined,
      commandFamily: asText(payload.command_family) || undefined,
      approvalPolicy: asText(payload.approval_policy) || undefined,
      executorType: asText(payload.executor_type) || undefined,
      executorProfile: asText(payload.executor_profile) || undefined,
      targetKind: asText(payload.target_kind) || undefined,
      targetIdentity: asText(payload.target_identity) || undefined,
      effectiveExecutorType: asText(payload.effective_executor_type) || undefined,
      effectiveExecutorProfile: asText(payload.effective_executor_profile) || undefined,
      dispatchBackend: asText(payload.dispatch_backend) || undefined,
      dispatchMode: asText(payload.dispatch_mode) || undefined,
      dispatchReason: asText(payload.dispatch_reason) || undefined,
      requiresConfirmation: Boolean(payload.requires_confirmation),
      requiresElevation: Boolean(payload.requires_elevation),
      status: event.event_type === 'approval_required' ? 'pending' : asText(payload.decision) || 'pending',
      message: asText(payload.reason || payload.comment) || undefined,
      createdAt: eventAt,
      updatedAt: eventAt,
    };
    return {
      ...nextState,
      entities: {
        ...nextState.entities,
        approvalsById: {
          ...nextState.entities.approvalsById,
          [approvalId]: {
            ...current,
            toolCallId: asText(payload.tool_call_id) || current.toolCallId,
            actionId: asText(payload.action_id) || current.actionId,
            title: asText(payload.title) || current.title,
            command: asText(payload.command) || current.command,
            purpose: asText(payload.purpose) || current.purpose,
            commandType: asText(payload.command_type) || current.commandType,
            riskLevel: asText(payload.risk_level) || current.riskLevel,
            commandFamily: asText(payload.command_family) || current.commandFamily,
            approvalPolicy: asText(payload.approval_policy) || current.approvalPolicy,
            executorType: asText(payload.executor_type) || current.executorType,
            executorProfile: asText(payload.executor_profile) || current.executorProfile,
            targetKind: asText(payload.target_kind) || current.targetKind,
            targetIdentity: asText(payload.target_identity) || current.targetIdentity,
            effectiveExecutorType: asText(payload.effective_executor_type) || current.effectiveExecutorType,
            effectiveExecutorProfile: asText(payload.effective_executor_profile) || current.effectiveExecutorProfile,
            dispatchBackend: asText(payload.dispatch_backend) || current.dispatchBackend,
            dispatchMode: asText(payload.dispatch_mode) || current.dispatchMode,
            dispatchReason: asText(payload.dispatch_reason) || current.dispatchReason,
            requiresConfirmation: Boolean(payload.requires_confirmation) || current.requiresConfirmation,
            requiresElevation: Boolean(payload.requires_elevation) || current.requiresElevation,
            status: event.event_type === 'approval_required'
              ? 'pending'
              : asText(payload.decision) || current.status,
            message: asText(payload.reason || payload.comment) || current.message,
            updatedAt: eventAt,
          },
        },
        approvalOrder: ensureOrderedId(nextState.entities.approvalOrder, approvalId),
      },
    };
  }

  if (event.event_type === 'run_finished' || event.event_type === 'run_failed' || event.event_type === 'run_cancelled') {
    const terminalStatus = event.event_type === 'run_finished'
      ? (asText(payload.status) || 'completed')
      : event.event_type === 'run_failed'
      ? 'failed'
      : 'cancelled';
    const currentSummary = (
      nextState.runMeta?.summaryJson && typeof nextState.runMeta.summaryJson === 'object'
        ? nextState.runMeta.summaryJson
        : {}
    ) as Record<string, unknown>;
    const mergedSummary = {
      ...currentSummary,
      ...(asText(payload.blocked_reason) ? { blocked_reason: asText(payload.blocked_reason) } : {}),
      ...(asText(payload.diagnosis_status) ? { diagnosis_status: asText(payload.diagnosis_status) } : {}),
      ...(asText(payload.fault_summary) ? { fault_summary: asText(payload.fault_summary) } : {}),
      ...(payload.gate_decision && typeof payload.gate_decision === 'object'
        ? { gate_decision: payload.gate_decision as Record<string, unknown> }
        : {}),
    };
    return {
      ...nextState,
      runMeta: nextState.runMeta
        ? {
            ...nextState.runMeta,
            status: terminalStatus,
            updatedAt: eventAt || nextState.runMeta.updatedAt,
            endedAt: eventAt || nextState.runMeta.endedAt,
            summaryJson: mergedSummary,
          }
        : nextState.runMeta,
    };
  }

  return nextState;
};

export const agentRunReducer = (
  state: AgentRunState,
  action: AgentRunReducerAction,
): AgentRunState => {
  switch (action.type) {
    case 'hydrate_snapshot':
      return applyRunSnapshot(
        {
          ...state,
          hydrating: false,
        },
        action.payload.run,
      );
    case 'hydrate_events': {
      const sorted = [...action.payload.events].sort((left, right) => left.seq - right.seq);
      return sorted.reduce<AgentRunState>((current, event) => appendEventEnvelope(current, event), {
        ...state,
        hydrating: false,
      });
    }
    case 'append_event':
      return appendEventEnvelope(state, action.payload.event);
    case 'set_streaming':
      return {
        ...state,
        streaming: action.payload.streaming,
      };
    case 'set_stream_error':
      return {
        ...state,
        streamError: action.payload.error,
      };
    case 'reset':
      return createInitialAgentRunState();
    default:
      return state;
  }
};

export const selectAssistantMessage = (state: AgentRunState): MessageEntity | null => {
  const messageId = state.runMeta?.assistantMessageId;
  if (!messageId) {
    return null;
  }
  return state.entities.messagesById[messageId] || null;
};

export const selectPendingApprovals = (state: AgentRunState): ApprovalEntity[] => (
  state.entities.approvalOrder
    .map((approvalId) => state.entities.approvalsById[approvalId])
    .filter((item): item is ApprovalEntity => Boolean(item))
    .filter((item) => item.status === 'pending')
);

export const selectCommandRuns = (state: AgentRunState): CommandRunEntity[] => (
  state.entities.commandRunOrder
    .map((commandRunId) => state.entities.commandRunsById[commandRunId])
    .filter((item): item is CommandRunEntity => Boolean(item))
);
