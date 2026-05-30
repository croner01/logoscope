import { selectAssistantMessage, selectCommandRuns, selectPendingApprovals, type AgentRunState } from '../../../utils/aiAgentRuntimeReducer';
import type { AgentRuntimeCommandSession } from '../types/command';

export interface RuntimeThoughtMessageItem {
  id?: string;
  phase?: string;
  status?: string;
  title?: string;
  detail?: string;
  timestamp?: string;
  iteration?: number;
}

export interface RuntimeFollowUpMessage {
  message_id: string;
  role: 'assistant';
  content: string;
  timestamp: string;
  metadata: Record<string, unknown>;
}

export interface RuntimeAnalysisSessionLike {
  runId: string;
  messageId: string;
  state: AgentRunState;
  sourceMessageId?: string;
  title: string;
  question?: string;
}

const asObject = (value: unknown): Record<string, unknown> => (
  value && typeof value === 'object' ? value as Record<string, unknown> : {}
);

const asText = (value: unknown): string => {
  if (typeof value === 'string') {
    return value;
  }
  if (value === null || value === undefined) {
    return '';
  }
  return String(value);
};

const isBusinessQuestionPayload = (payload: Record<string, unknown>): boolean => {
  const kind = asText(payload.kind).trim().toLowerCase();
  const questionKind = asText(payload.question_kind).trim().toLowerCase();
  const reason = asText(payload.reason || payload.message).trim().toLowerCase();
  return kind === 'business_question'
    || Boolean(questionKind)
    || reason.includes('sql_preflight_failed')
    || reason.includes('unknown_semantics')
    || reason.includes('semantic_incomplete')
    || reason.includes('command_spec')
    || reason.includes('diagnosis_contract');
};

const getPendingUserInput = (runtimeState: AgentRunState): {
  title?: string;
  prompt?: string;
  reason?: string;
  command?: string;
} | null => {
  const status = String(runtimeState.runMeta?.status || '').trim().toLowerCase();
  if (status !== 'waiting_user_input') {
    return null;
  }
  const events = runtimeState.entities.events;
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (String(event.event_type || '').trim().toLowerCase() !== 'action_waiting_user_input') {
      continue;
    }
    const payload = asObject(event.payload);
    return {
      title: asText(payload.title).trim() || undefined,
      prompt: asText(payload.prompt).trim() || '我还需要一个关键信息后继续排查。',
      reason: asText(payload.reason || payload.message).trim() || undefined,
      command: isBusinessQuestionPayload(payload) ? undefined : (asText(payload.command).trim() || undefined),
    };
  }
  return null;
};

export const buildRuntimeThoughtTimeline = (params: {
  runtimeState: AgentRunState;
  truncateText: (raw: unknown, maxLen?: number) => string;
  normalizePhase: (phase: unknown) => string;
  normalizeTimeline: (items: RuntimeThoughtMessageItem[]) => RuntimeThoughtMessageItem[];
  maxItems: number;
}): RuntimeThoughtMessageItem[] => {
  const thoughts: RuntimeThoughtMessageItem[] = params.runtimeState.entities.stepOrder
    .map((stepId) => params.runtimeState.entities.stepsById[stepId])
    .filter(Boolean)
    .map((step, index) => {
      const statusRaw = String(step.status || '').trim().toLowerCase();
      let status = 'info';
      if (statusRaw === 'completed') {
        status = 'success';
      } else if (statusRaw === 'failed') {
        status = 'error';
      } else if (statusRaw === 'pending') {
        status = 'warning';
      }
      return {
        id: `${step.stepId}-${index}`,
        phase: params.normalizePhase(step.phase || 'thought'),
        status,
        title: step.title || step.phase || '运行步骤',
        detail: String(step.summaryText || '').trim() || undefined,
        timestamp: step.updatedAt || step.createdAt,
        iteration: step.iteration,
      };
    });

  const pendingApproval = selectPendingApprovals(params.runtimeState).slice(-1)[0];
  if (pendingApproval) {
    thoughts.push({
      id: `approval-${pendingApproval.approvalId}`,
      phase: 'observation',
      status: 'warning',
      title: pendingApproval.command
        ? `待审批命令: ${params.truncateText(pendingApproval.command, 80)}`
        : '待审批命令',
      detail: String(pendingApproval.message || pendingApproval.title || '').trim() || undefined,
      timestamp: pendingApproval.updatedAt || pendingApproval.createdAt,
    });
  }

  const latestCommandRun = selectCommandRuns(params.runtimeState).slice(-1)[0];
  if (latestCommandRun) {
    const statusRaw = String(latestCommandRun.status || '').trim().toLowerCase();
    let status = 'info';
    if (statusRaw === 'completed') {
      status = 'success';
    } else if (statusRaw === 'failed' || statusRaw === 'cancelled') {
      status = 'error';
    }
    thoughts.push({
      id: `command-${latestCommandRun.commandRunId}`,
      phase: 'observation',
      status,
      title: latestCommandRun.command
        ? `命令执行: ${params.truncateText(latestCommandRun.command, 80)}`
        : '命令执行中',
      detail: params.truncateText(
        latestCommandRun.stderr || latestCommandRun.stdout || latestCommandRun.status || '',
        160,
      ) || undefined,
      timestamp: latestCommandRun.updatedAt || latestCommandRun.createdAt,
    });
  }

  return params.normalizeTimeline(thoughts).slice(-params.maxItems);
};

export const buildRuntimeFollowUpMessage = (params: {
  session: AgentRuntimeCommandSession;
  thoughtTimeline: RuntimeThoughtMessageItem[];
  formatCommandExecutionMessage: (payload: Record<string, unknown>, fallbackCommand: string) => string;
}): RuntimeFollowUpMessage => {
  const runtimeState = params.session.state;
  const assistantMessage = selectAssistantMessage(runtimeState);
  const commandRuns = selectCommandRuns(runtimeState);
  const pendingApprovals = selectPendingApprovals(runtimeState);
  const pendingUserInput = getPendingUserInput(runtimeState);
  const latestCommandRun = commandRuns.length > 0 ? commandRuns[commandRuns.length - 1] : null;

  const actionObservations = commandRuns.map((item) => ({
    tool_call_id: item.toolCallId,
    action_id: item.actionId,
    command_run_id: item.commandRunId,
    command: item.command,
    command_type: item.commandType,
    risk_level: item.riskLevel,
    status: item.status,
    stdout: item.stdout,
    stderr: item.stderr,
    exit_code: item.exitCode,
    timed_out: item.timedOut,
    output_truncated: item.outputTruncated,
    runtime_run_id: params.session.runId,
  }));
  const approvalPayloads = pendingApprovals.map((item) => ({
    approval_id: item.approvalId,
    action_id: item.actionId,
    command: item.command,
    command_type: item.commandType,
    risk_level: item.riskLevel,
    requires_confirmation: item.requiresConfirmation,
    requires_elevation: item.requiresElevation,
    status: item.status,
    message: item.message,
    title: item.title,
    runtime_run_id: params.session.runId,
    runtime_approval_id: item.approvalId,
    confirmation_ticket: item.approvalId,
  }));

  let content = String(assistantMessage?.content || '').trim();
  if (!content) {
    if (pendingUserInput) {
      content = [
        '[待确认关键信息]',
        pendingUserInput.command ? `command: ${pendingUserInput.command}` : '',
        pendingUserInput.prompt || pendingUserInput.reason || '请补充一个关键信息后继续排查。',
      ].filter(Boolean).join('\n');
    } else if (approvalPayloads.length > 0) {
      const pendingApproval = approvalPayloads[approvalPayloads.length - 1];
      content = [
        '[待审批]',
        pendingApproval.command ? `command: ${pendingApproval.command}` : '',
        pendingApproval.message ? `message: ${pendingApproval.message}` : '命令需要审批后继续执行。',
      ].filter(Boolean).join('\n');
    } else if (latestCommandRun) {
      content = params.formatCommandExecutionMessage(
        {
          ...latestCommandRun,
          status: latestCommandRun.status,
          command: latestCommandRun.command || params.session.command,
        },
        params.session.command,
      );
    } else {
      content = `命令执行准备中...\ncommand: ${params.session.command}`;
    }
  }

  const requiresElevation = params.session.commandType === 'repair';
  const messageId = String(assistantMessage?.messageId || params.session.messageId).trim();
  return {
    message_id: messageId,
    role: 'assistant',
    content,
    timestamp: assistantMessage?.updatedAt || assistantMessage?.createdAt || new Date().toISOString(),
    metadata: {
      command_execution: true,
      runtime_run_id: params.session.runId,
      runtime_last_seq: runtimeState.lastSeq,
      source_message_id: params.session.sourceMessageId,
      stream_loading: runtimeState.streaming,
      stream_stage: runtimeState.runMeta?.currentPhase || runtimeState.runMeta?.status,
      actions: [
        {
          id: params.session.actionId || `runtime-${params.session.runId}`,
          title: params.session.title,
          purpose: params.session.purpose,
          command: params.session.command,
          command_type: params.session.commandType,
          risk_level: params.session.riskLevel,
          executable: true,
          requires_confirmation: requiresElevation,
          requires_write_permission: requiresElevation,
          requires_elevation: requiresElevation,
        },
      ],
      action_observations: actionObservations,
      approval_required: approvalPayloads,
      thoughts: params.thoughtTimeline,
      stream_timeline: params.thoughtTimeline,
    },
  };
};

export const buildRuntimeAnalysisFollowUpMessage = (params: {
  session: RuntimeAnalysisSessionLike;
  thoughtTimeline: RuntimeThoughtMessageItem[];
}): RuntimeFollowUpMessage => {
  const runtimeState = params.session.state;
  const assistantMessage = selectAssistantMessage(runtimeState);
  const commandRuns = selectCommandRuns(runtimeState);
  const pendingApprovals = selectPendingApprovals(runtimeState);
  const pendingUserInput = getPendingUserInput(runtimeState);
  const assistantMetadata = (
    assistantMessage?.metadata && typeof assistantMessage.metadata === 'object'
      ? assistantMessage.metadata
      : {}
  ) as Record<string, unknown>;

  const actionObservations = commandRuns.map((item) => ({
    tool_call_id: item.toolCallId,
    action_id: item.actionId,
    command_run_id: item.commandRunId,
    command: item.command,
    command_type: item.commandType,
    risk_level: item.riskLevel,
    status: item.status,
    stdout: item.stdout,
    stderr: item.stderr,
    exit_code: item.exitCode,
    timed_out: item.timedOut,
    output_truncated: item.outputTruncated,
    runtime_run_id: params.session.runId,
  }));
  const approvalPayloads = pendingApprovals.map((item) => ({
    approval_id: item.approvalId,
    action_id: item.actionId,
    command: item.command,
    command_type: item.commandType,
    risk_level: item.riskLevel,
    requires_confirmation: item.requiresConfirmation,
    requires_elevation: item.requiresElevation,
    status: item.status,
    message: item.message,
    title: item.title,
    runtime_run_id: params.session.runId,
    runtime_approval_id: item.approvalId,
    confirmation_ticket: item.approvalId,
  }));

  let content = String(assistantMessage?.content || '').trim();
  if (!content) {
    if (pendingUserInput) {
      content = [
        '[待确认关键信息]',
        pendingUserInput.command ? `command: ${pendingUserInput.command}` : '',
        pendingUserInput.prompt || pendingUserInput.reason || '请补充一个关键信息后继续排查。',
      ].filter(Boolean).join('\n');
    } else {
      const pendingApproval = approvalPayloads[approvalPayloads.length - 1];
      if (pendingApproval) {
        content = [
          '[待审批]',
          pendingApproval.command ? `command: ${pendingApproval.command}` : '',
          pendingApproval.message ? `message: ${pendingApproval.message}` : '存在待审批动作，确认后可继续执行。',
        ].filter(Boolean).join('\n');
      } else {
        const currentPhase = String(runtimeState.runMeta?.currentPhase || runtimeState.runMeta?.status || '').trim();
        content = currentPhase
          ? `正在分析...\nphase: ${currentPhase}`
          : '正在分析...';
      }
    }
  }

  const mergedObservations = Array.isArray(assistantMetadata.action_observations)
    && assistantMetadata.action_observations.length > 0
    ? assistantMetadata.action_observations
    : actionObservations;
  const mergedApprovals = Array.isArray(assistantMetadata.approval_required)
    && assistantMetadata.approval_required.length > 0
    ? assistantMetadata.approval_required
    : approvalPayloads;
  const references = Array.isArray(assistantMessage?.references) ? assistantMessage.references : [];
  const messageId = String(assistantMessage?.messageId || params.session.messageId).trim();

  return {
    message_id: messageId,
    role: 'assistant',
    content,
    timestamp: assistantMessage?.updatedAt || assistantMessage?.createdAt || new Date().toISOString(),
    metadata: {
      ...assistantMetadata,
      references,
      runtime_run_id: params.session.runId,
      runtime_last_seq: runtimeState.lastSeq,
      source_message_id: params.session.sourceMessageId,
      stream_loading: runtimeState.streaming,
      stream_stage: runtimeState.runMeta?.currentPhase || runtimeState.runMeta?.status,
      action_observations: mergedObservations,
      approval_required: mergedApprovals,
      thoughts: params.thoughtTimeline,
      stream_timeline: params.thoughtTimeline,
    },
  };
};
