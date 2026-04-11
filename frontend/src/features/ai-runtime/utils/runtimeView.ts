import { selectAssistantMessage, selectCommandRuns, selectPendingApprovals, type AgentRunState } from '../../../utils/aiAgentRuntimeReducer.js';
import type { RuntimePanelRunView } from '../types/view.js';

type UnknownObject = Record<string, unknown>;

const asObject = (value: unknown): UnknownObject => (
  value && typeof value === 'object' ? value as UnknownObject : {}
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

const asOptionalText = (value: unknown): string | undefined => {
  const text = asText(value).trim();
  return text || undefined;
};

const asOptionalObject = (value: unknown): UnknownObject | undefined => (
  value && typeof value === 'object' ? value as UnknownObject : undefined
);

const normalizeCommandSpecRecovery = (value: unknown) => {
  const payload = asOptionalObject(value);
  if (!payload) {
    return undefined;
  }
  const fixCode = asOptionalText(payload.fix_code || payload.fixCode);
  const fixHint = asOptionalText(payload.fix_hint || payload.fixHint);
  const fixDetail = asOptionalText(payload.fix_detail || payload.fixDetail);
  const suggestedCommand = asOptionalText(payload.suggested_command || payload.suggestedCommand);
  const suggestedCommandSpec = (
    asOptionalObject(payload.suggested_command_spec)
    || asOptionalObject(payload.suggestedCommandSpec)
  );
  if (!fixCode && !fixHint && !fixDetail && !suggestedCommand && !suggestedCommandSpec) {
    return undefined;
  }
  return {
    fixCode,
    fixHint,
    fixDetail,
    suggestedCommand,
    suggestedCommandSpec,
  };
};

const isBusinessQuestionPayload = (payload: UnknownObject): boolean => {
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

const resolvePendingUserInput = (params: {
  runId: string;
  runtimeState: AgentRunState;
}) => {
  const runStatus = String(params.runtimeState.runMeta?.status || '').trim().toLowerCase();
  if (runStatus !== 'waiting_user_input') {
    return undefined;
  }
  const events = params.runtimeState.entities.events;
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (String(event.event_type || '').trim().toLowerCase() !== 'action_waiting_user_input') {
      continue;
    }
    const payload = asObject(event.payload);
    const actionId = asText(payload.action_id).trim();
    const command = asText(payload.command).trim();
    const title = asText(payload.title).trim() || '需要确认关键信息';
    const recovery = normalizeCommandSpecRecovery(asObject(asObject(payload.source_context).recovery));
    return {
      id: `${params.runId}:${actionId || event.seq}`,
      runtimeRunId: params.runId,
      actionId: actionId || undefined,
      title,
      prompt: asText(payload.prompt).trim() || '我还需要一个关键信息后继续排查。',
      reason: asText(payload.reason || payload.message).trim() || recovery?.fixHint || recovery?.fixDetail,
      command: isBusinessQuestionPayload(payload) ? undefined : (command || undefined),
      purpose: asText(payload.purpose).trim() || undefined,
      status: 'pending',
      updatedAt: asText(payload.requested_at || event.created_at).trim() || undefined,
      recovery,
    };
  }
  return undefined;
};

export interface RuntimeMessageLike {
  metadata?: Record<string, unknown>;
}

export interface RuntimeThoughtLike {
  id?: string;
  phase?: string;
  status?: string;
  title?: string;
  detail?: string;
  timestamp?: string;
  iteration?: number;
}

export interface RuntimeSessionLike {
  runId: string;
  messageId: string;
  state: AgentRunState;
  title: string;
}

export const isTerminalAgentRunStatus = (status: unknown): boolean => {
  const normalized = String(status || '').trim().toLowerCase();
  return normalized === 'blocked' || normalized === 'completed' || normalized === 'failed' || normalized === 'cancelled';
};

export const collectRuntimeRunIdsFromMessages = (messages: RuntimeMessageLike[]): string[] => {
  const runIds = new Set<string>();
  messages.forEach((message) => {
    const metadata = asObject(message.metadata);
    const directRunId = String(metadata.runtime_run_id || '').trim();
    if (directRunId) {
      runIds.add(directRunId);
    }
    const observations = Array.isArray(metadata.action_observations)
      ? metadata.action_observations
      : [];
    observations.forEach((item) => {
      const runId = String(asObject(item).runtime_run_id || '').trim();
      if (runId) {
        runIds.add(runId);
      }
    });
    const approvals = Array.isArray(metadata.approval_required)
      ? metadata.approval_required
      : [];
    approvals.forEach((item) => {
      const runId = String(asObject(item).runtime_run_id || '').trim();
      if (runId) {
        runIds.add(runId);
      }
    });
  });
  return Array.from(runIds);
};

export const buildRuntimePanelRuns = (params: {
  sessions: Record<string, RuntimeSessionLike>;
  buildThoughtTimeline: (runtimeState: AgentRunState) => RuntimeThoughtLike[];
  formatTimestamp: (value?: string) => string;
}): RuntimePanelRunView[] => (
  Object.values(params.sessions)
    .map((session) => {
      const assistantMessage = selectAssistantMessage(session.state);
      const commandRuns = selectCommandRuns(session.state);
      const approvals = selectPendingApprovals(session.state);
      const latestCommand = commandRuns.length > 0 ? commandRuns[commandRuns.length - 1] : null;
      const latestUpdatedAt = latestCommand?.updatedAt
        || assistantMessage?.updatedAt
        || session.state.runMeta?.updatedAt
        || session.state.runMeta?.createdAt;
      const pendingUserInput = resolvePendingUserInput({
        runId: session.runId,
        runtimeState: session.state,
      });
      return {
        runId: session.runId,
        title: session.title,
        status: String(session.state.runMeta?.status || 'running'),
        currentPhase: String(session.state.runMeta?.currentPhase || '').trim() || undefined,
        updatedAt: latestUpdatedAt,
        streaming: session.state.streaming,
        streamError: session.state.streamError,
        assistantMessage: String(assistantMessage?.content || '').trim() || undefined,
        commandRuns: commandRuns.map((item) => ({
          id: item.commandRunId,
          title: session.title,
          command: item.command,
          status: item.status,
          commandType: item.commandType,
          riskLevel: item.riskLevel,
          dispatchBackend: item.dispatchBackend,
          dispatchMode: item.dispatchMode,
          dispatchReason: item.dispatchReason,
          targetKind: item.targetKind,
          targetIdentity: item.targetIdentity,
          targetClusterId: item.targetClusterId,
          targetNamespace: item.targetNamespace,
          targetNodeName: item.targetNodeName,
          resolvedTargetContext: item.resolvedTargetContext,
          stdout: item.stdout,
          stderr: item.stderr,
          exitCode: item.exitCode,
          timedOut: item.timedOut,
          updatedAt: item.updatedAt || item.createdAt,
        })),
        approvals: approvals.map((item) => ({
          id: item.approvalId,
          runtimeRunId: session.runId,
          runtimeApprovalId: item.approvalId,
          title: String(item.title || session.title || item.command).trim() || item.command,
          command: item.command,
          message: item.message,
          status: item.status,
          commandType: item.commandType,
          riskLevel: item.riskLevel,
          requiresConfirmation: item.requiresConfirmation,
          requiresElevation: item.requiresElevation,
          messageId: session.messageId,
          actionId: item.actionId,
          confirmationTicket: item.approvalId,
        })),
        userInput: pendingUserInput,
        timeline: params.buildThoughtTimeline(session.state).map((item, index) => ({
          id: String(item.id || `${session.runId}-${index}`),
          phase: String(item.phase || 'system'),
          status: String(item.status || 'info'),
          title: String(item.title || '运行步骤'),
          detail: String(item.detail || '').trim() || undefined,
          timestamp: item.timestamp ? params.formatTimestamp(item.timestamp) : undefined,
          iteration: item.iteration,
        })),
      };
    })
    .sort((left, right) => {
      const leftTime = left.updatedAt ? new Date(left.updatedAt).getTime() : 0;
      const rightTime = right.updatedAt ? new Date(right.updatedAt).getTime() : 0;
      return rightTime - leftTime;
    })
);
