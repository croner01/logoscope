/**
 * AI runtime 临时功能验证页
 *
 * 目标是收敛到 AI IDE 风格的单列会话流：
 * 用户提问 -> AI 思考 -> 命令执行 / 审批 -> 继续思考 -> 最终回答。
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertCircle,
  BrainCircuit,
  Loader2,
  Play,
  RefreshCw,
  Search,
  Settings2,
  X,
} from 'lucide-react';

import RuntimeConversationCard from '../features/ai-runtime/components/RuntimeConversationCard';
import type { RuntimeApprovalEntry, RuntimeManualActionEntry, RuntimeTranscriptMessage } from '../features/ai-runtime/types/view';
import { buildRuntimeEventProjection } from '../features/ai-runtime/utils/runtimeEventProjection';
import { isTerminalAgentRunStatus } from '../features/ai-runtime/utils/runtimeView';
import { api } from '../utils/api';
import type { ExecExecutorStatusResponse, ExecExecutorStatusRow } from '../utils/api';
import {
  normalizeAgentRunEventEnvelope,
  type AgentRunEventEnvelope,
  type AgentRunSnapshot,
} from '../utils/aiAgentRuntime';
import {
  createNextRuntimeStreamToken,
  shouldHandleRuntimeStreamMutation,
} from '../utils/aiRuntimeStream';
import { reconcileAIRunState } from '../utils/aiRuntimeSync';
import {
  buildHistoryTurns as buildHistoryTurnSnapshots,
  filterRuntimeTurnsByThread,
  getConversationIdFromRun,
  isSameThreadIdentity,
  mergeHistoryTurnsWithRuntimeTurns,
  normalizeThreadIdentity,
  shouldHydrateHistoryForThreadSwitch,
  type AIHistoryTurnSnapshot,
  type ThreadIdentity,
} from '../utils/aiRuntimeThread';
import {
  agentRunReducer,
  createInitialAgentRunState,
  type AgentRunState,
} from '../utils/aiAgentRuntimeReducer';
import { buildRuntimeCommandSpec, resolveRuntimeClientDeadlineMs } from '../utils/commandSpec';
import { formatTime } from '../utils/formatters';
import {
  buildRuntimeAnalysisContext,
  resolveRuntimeAnalysisMode,
} from '../utils/runtimeAnalysisMode';

type UnknownObject = Record<string, unknown>;

interface PlaygroundSession {
  turnId: string;
  runId: string;
  messageId: string;
  state: AgentRunState;
  title: string;
  question?: string;
  sessionId: string;
  conversationId: string;
}

interface PlaygroundHistoryTurn {
  kind: 'history';
  turnId: string;
  question: string;
  answer: string;
  userMessageId?: string;
  assistantMessageId?: string;
  timestamp?: string;
}

interface PlaygroundRuntimeTurn {
  kind: 'runtime';
  turnId: string;
  session: PlaygroundSession;
}

type PlaygroundTurn = PlaygroundHistoryTurn | PlaygroundRuntimeTurn;

interface PlaygroundThread {
  sessionId: string;
  conversationId: string;
  turns: PlaygroundTurn[];
}

interface ExecutorStatusSummary {
  total: number;
  ready: number;
  degraded: number;
  generatedAt?: string;
  topReadyProfiles: string[];
  topDegradedProfiles: string[];
  rows: ExecExecutorStatusRow[];
}

interface PendingUserInputRequest {
  actionId?: string;
  title?: string;
  prompt?: string;
  reason?: string;
  command?: string;
  purpose?: string;
  requestedAt?: string;
}

interface BlockedInputRetryPayload {
  runId: string;
  text: string;
  source?: string;
}

const buildExecutorValuesSnippet = (row: ExecExecutorStatusRow): string => {
  const envName = row.dispatch_template_env || row.candidate_template_envs[0] || 'EXEC_EXECUTOR_TEMPLATE__EXAMPLE';
  const template = row.example_template || '<fill-me>';
  return [
    'components:',
    '  execService:',
    '    env:',
    `      ${envName}: ${JSON.stringify(template)}`,
  ].join('\n');
};

const asObject = (value: unknown): UnknownObject => (
  value && typeof value === 'object' ? value as UnknownObject : {}
);

const asOptionalObject = (value: unknown): UnknownObject | undefined => (
  value && typeof value === 'object' ? value as UnknownObject : undefined
);

const parseCommandSpecRecovery = (value: unknown): {
  fixHint?: string;
  fixDetail?: string;
  suggestedCommand?: string;
  suggestedCommandSpec?: UnknownObject;
} => {
  const payload = asObject(value);
  const fixHint = String(payload.fix_hint || payload.fixHint || '').trim() || undefined;
  const fixDetail = String(payload.fix_detail || payload.fixDetail || '').trim() || undefined;
  const suggestedCommand = String(payload.suggested_command || payload.suggestedCommand || '').trim() || undefined;
  const suggestedCommandSpec = (
    asOptionalObject(payload.suggested_command_spec)
    || asOptionalObject(payload.suggestedCommandSpec)
  );
  return {
    fixHint,
    fixDetail,
    suggestedCommand,
    suggestedCommandSpec,
  };
};

const buildRecoveryMessage = (params: {
  fallback: string;
  responsePayload: UnknownObject;
}): string => {
  const errorPayload = asObject(params.responsePayload.error);
  const recoveryPayload = asObject(errorPayload.recovery || params.responsePayload.recovery);
  const recovery = parseCommandSpecRecovery(recoveryPayload);
  const detailParts = [recovery.fixHint, recovery.fixDetail].filter(Boolean);
  if (detailParts.length > 0) {
    return detailParts.join(' ');
  }
  const responseMessage = String(
    errorPayload.message
    || errorPayload.detail
    || params.responsePayload.message
    || '',
  ).trim();
  return responseMessage || params.fallback;
};

const getErrorMessage = (error: unknown, fallback: string): string => {
  const responsePayload = asObject(asObject(error).response);
  const responseData = asObject(responsePayload.data);
  const detail = responseData.detail;
  if (typeof detail === 'string' && detail.trim()) {
    return detail.trim();
  }
  const detailObject = asObject(detail);
  const detailMessage = detailObject.message;
  if (typeof detailMessage === 'string' && detailMessage.trim()) {
    return detailMessage.trim();
  }
  const responseMessage = responseData.message;
  if (typeof responseMessage === 'string' && responseMessage.trim()) {
    return responseMessage.trim();
  }
  const message = asObject(error).message;
  if (typeof message === 'string' && message.trim()) {
    return message.trim();
  }
  return fallback;
};

const getErrorCode = (error: unknown): string => {
  const responsePayload = asObject(asObject(error).response);
  const responseData = asObject(responsePayload.data);
  const detail = asObject(responseData.detail);
  const rawCode = detail.code;
  if (typeof rawCode === 'string') {
    return rawCode.trim().toLowerCase();
  }
  return '';
};

const getThreadIdentityFromThread = (thread: PlaygroundThread): ThreadIdentity => (
  normalizeThreadIdentity({
    sessionId: thread.sessionId,
    conversationId: thread.conversationId,
  })
);

const getThreadIdentityFromSession = (session: PlaygroundSession): ThreadIdentity => (
  normalizeThreadIdentity({
    sessionId: session.sessionId,
    conversationId: session.conversationId,
  })
);

const buildSessionFromSnapshot = (run: AgentRunSnapshot, title?: string): PlaygroundSession => ({
  turnId: String(run.assistant_message_id || run.run_id).trim() || run.run_id,
  runId: run.run_id,
  messageId: run.assistant_message_id,
  state: agentRunReducer(createInitialAgentRunState(), {
    type: 'hydrate_snapshot',
    payload: { run },
  }),
  title: String(title || run.question || run.run_id).trim() || run.run_id,
  question: String(run.question || '').trim() || undefined,
  sessionId: String(run.session_id || '').trim(),
  conversationId: getConversationIdFromRun(run),
});

const buildLatestPendingApproval = (session: PlaygroundSession): RuntimeApprovalEntry | null => {
  const runStatus = String(session.state.runMeta?.status || '').trim().toLowerCase();
  if (runStatus !== 'waiting_approval') {
    return null;
  }
  const { approvalOrder, approvalsById } = session.state.entities;
  for (let index = approvalOrder.length - 1; index >= 0; index -= 1) {
    const approvalId = approvalOrder[index];
    const approval = approvalsById[approvalId];
    if (!approval || approval.status !== 'pending') {
      continue;
    }
    const timestamp = approval.updatedAt || approval.createdAt;
    return {
      id: approval.approvalId,
      runtimeRunId: session.runId,
      runtimeApprovalId: approval.approvalId,
      title: String(approval.title || approval.command).trim() || approval.command,
      command: approval.command,
      purpose: approval.purpose,
      message: approval.message,
      status: approval.status,
      commandType: approval.commandType,
      riskLevel: approval.riskLevel,
      commandFamily: approval.commandFamily,
      approvalPolicy: approval.approvalPolicy,
      executorType: approval.executorType,
      executorProfile: approval.executorProfile,
      targetKind: approval.targetKind,
      targetIdentity: approval.targetIdentity,
      effectiveExecutorType: approval.effectiveExecutorType,
      effectiveExecutorProfile: approval.effectiveExecutorProfile,
      dispatchBackend: approval.dispatchBackend,
      dispatchMode: approval.dispatchMode,
      dispatchReason: approval.dispatchReason,
      requiresConfirmation: approval.requiresConfirmation,
      requiresElevation: approval.requiresElevation,
      messageId: session.messageId,
      actionId: approval.actionId,
      confirmationTicket: approval.approvalId,
      updatedAt: timestamp,
    };
  }
  return null;
};

const hasPendingApproval = (
  thread: PlaygroundThread,
  approval: RuntimeApprovalEntry,
): boolean => (
  thread.turns.some((turn) => (
    turn.kind === 'runtime'
    && turn.session.runId === approval.runtimeRunId
    && String(turn.session.state.runMeta?.status || '').trim().toLowerCase() === 'waiting_approval'
    && turn.session.state.entities.approvalsById[approval.runtimeApprovalId]?.status === 'pending'
  ))
);

const buildLatestPendingManualAction = (
  thread: PlaygroundThread,
  messagesByTurnId: Record<string, RuntimeTranscriptMessage>,
): RuntimeManualActionEntry | null => {
  for (let index = thread.turns.length - 1; index >= 0; index -= 1) {
    const turn = thread.turns[index];
    if (turn.kind !== 'runtime') {
      continue;
    }
    if (String(turn.session.state.runMeta?.status || '').trim().toLowerCase() !== 'waiting_user_input') {
      continue;
    }
    const message = messagesByTurnId[turn.turnId];
    if (!message) {
      continue;
    }
    for (let blockIndex = message.blocks.length - 1; blockIndex >= 0; blockIndex -= 1) {
      const block = message.blocks[blockIndex];
      if (block.type === 'manual_action') {
        const command = String(block.action.command || '').trim();
        const commandType = String(block.action.commandType || '').trim().toLowerCase();
        if (!command || commandType === 'unknown') {
          continue;
        }
        return block.action;
      }
    }
  }
  return null;
};

const hasPendingManualAction = (
  thread: PlaygroundThread,
  action: RuntimeManualActionEntry,
  messagesByTurnId: Record<string, RuntimeTranscriptMessage>,
): boolean => (
  thread.turns.some((turn) => {
    if (turn.kind !== 'runtime') {
      return false;
    }
    const message = messagesByTurnId[turn.turnId];
    if (!message) {
      return false;
    }
    return message.blocks.some((block) => (
      block.type === 'manual_action'
      && block.action.id === action.id
    ));
  })
);

const isExecutableManualAction = (action: RuntimeManualActionEntry | null | undefined): boolean => {
  if (!action) {
    return false;
  }
  const command = String(action.command || '').trim();
  const commandType = String(action.commandType || '').trim().toLowerCase();
  if (!command) {
    return false;
  }
  if (commandType === 'unknown') {
    return false;
  }
  return true;
};

const buildPendingUserInputRequest = (session: PlaygroundSession): PendingUserInputRequest | null => {
  const runStatus = String(session.state.runMeta?.status || '').trim().toLowerCase();
  if (runStatus !== 'waiting_user_input') {
    return null;
  }
  const events = session.state.entities.events;
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event.event_type === 'action_waiting_user_input') {
      const payload = asObject(event.payload);
      return {
        actionId: String(payload.action_id || '').trim() || undefined,
        title: String(payload.title || '').trim() || undefined,
        prompt: String(payload.prompt || '').trim() || undefined,
        reason: String(payload.reason || '').trim() || undefined,
        command: String(payload.command || '').trim() || undefined,
        purpose: String(payload.purpose || '').trim() || undefined,
        requestedAt: String(payload.requested_at || event.created_at || '').trim() || undefined,
      };
    }
  }
  return null;
};

const buildApprovalDismissKey = (approval: RuntimeApprovalEntry): string => {
  const runId = String(approval.runtimeRunId || '').trim();
  const approvalId = String(approval.runtimeApprovalId || '').trim();
  const actionId = String(approval.actionId || '').trim();
  const command = String(approval.command || '').trim();
  const title = String(approval.title || '').trim();
  return [runId, approvalId || actionId || command || title].join('::');
};

const buildManualActionDismissKey = (action: RuntimeManualActionEntry): string => {
  const runId = String(action.runtimeRunId || '').trim();
  const actionId = String(action.actionId || '').trim();
  const command = String(action.command || '').trim();
  const title = String(action.title || '').trim();
  const fallbackId = String(action.id || '').trim();
  return [runId, actionId || command || title || fallbackId].join('::');
};

const toPlaygroundHistoryTurn = (turn: AIHistoryTurnSnapshot): PlaygroundHistoryTurn => ({
  kind: 'history',
  turnId: turn.turnId,
  question: turn.question,
  answer: turn.answer,
  userMessageId: turn.userMessageId,
  assistantMessageId: turn.assistantMessageId,
  timestamp: turn.timestamp,
});

const renderToneClassName = (value: string): string => {
  const normalized = String(value || '').trim().toLowerCase();
  if (normalized === 'waiting_approval' || normalized === 'waiting_user_input') {
    return 'border-amber-200 bg-amber-50 text-amber-800';
  }
  if (normalized === 'completed') {
    return 'border-emerald-200 bg-emerald-50 text-emerald-700';
  }
  if (normalized === 'blocked' || normalized === 'failed' || normalized === 'cancelled') {
    return 'border-rose-200 bg-rose-50 text-rose-700';
  }
  return 'border-sky-200 bg-sky-50 text-sky-700';
};

const shouldStopOnRunStatusChanged = (status: string, stopOnApproval: boolean): boolean => {
  const normalized = String(status || '').trim().toLowerCase();
  if (!normalized) {
    return false;
  }
  if (
    normalized === 'completed'
    || normalized === 'failed'
    || normalized === 'cancelled'
    || normalized === 'blocked'
  ) {
    return true;
  }
  if (normalized === 'waiting_user_input') {
    return true;
  }
  if (stopOnApproval && normalized === 'waiting_approval') {
    return true;
  }
  return false;
};

const buildRuntimeDowngradeNotice = (reason?: string): string | null => {
  if (reason === 'trace_id_missing') {
    return 'trace 模式未提供 Trace ID，本次运行已自动降级为 log 模式；如需按链路排查，请补充 Trace ID 后重试。';
  }
  return null;
};

const AIRuntimePlayground: React.FC = () => {
  const [question, setQuestion] = useState('请分析 checkout-service 最近频繁超时的原因，并在需要时继续执行只读排查命令。');
  const [serviceName, setServiceName] = useState('checkout-service');
  const [analysisType, setAnalysisType] = useState<'log' | 'trace'>('log');
  const [traceId, setTraceId] = useState('');
  const [sessionId, setSessionId] = useState('');
  const [conversationId, setConversationId] = useState('');
  const [existingRunId, setExistingRunId] = useState('');
  const [useLLM, setUseLLM] = useState(true);
  const [showThought, setShowThought] = useState(true);
  const [autoExecReadonly, setAutoExecReadonly] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [pageError, setPageError] = useState<string | null>(null);
  const [thread, setThread] = useState<PlaygroundThread>({
    sessionId: '',
    conversationId: '',
    turns: [],
  });
  const [activeTurnId, setActiveTurnId] = useState<string | null>(null);
  const [approvalDialog, setApprovalDialog] = useState<RuntimeApprovalEntry | null>(null);
  const [manualActionDialog, setManualActionDialog] = useState<RuntimeManualActionEntry | null>(null);
  const [approvalSubmitting, setApprovalSubmitting] = useState(false);
  const [pendingInputSubmitting, setPendingInputSubmitting] = useState(false);
  const [blockedInputRetryPayload, setBlockedInputRetryPayload] = useState<BlockedInputRetryPayload | null>(null);
  const [composerOffset, setComposerOffset] = useState(320);
  const [executorSummary, setExecutorSummary] = useState<ExecutorStatusSummary | null>(null);
  const [executorLoading, setExecutorLoading] = useState(false);

  const sessionsRef = useRef<Record<string, PlaygroundSession>>({});
  const activeRunIdRef = useRef<string | null>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const streamTokenRef = useRef(0);
  const dismissedApprovalKeysRef = useRef<Set<string>>(new Set());
  const dismissedManualActionKeysRef = useRef<Set<string>>(new Set());
  const threadRef = useRef<PlaygroundThread>({
    sessionId: '',
    conversationId: '',
    turns: [],
  });
  const interruptingRef = useRef(false);
  const bottomAnchorRef = useRef<HTMLDivElement | null>(null);
  const composerRef = useRef<HTMLDivElement | null>(null);

  const stopStreaming = useCallback((options?: { clearStreamingState?: boolean }) => {
    const controller = controllerRef.current;
    const stoppedRunId = activeRunIdRef.current;
    streamTokenRef.current = createNextRuntimeStreamToken(streamTokenRef.current);
    controllerRef.current = null;
    activeRunIdRef.current = null;
    if (controller) {
      controller.abort();
    }
    if (options?.clearStreamingState === false || !stoppedRunId) {
      return;
    }
    const session = sessionsRef.current[stoppedRunId];
    if (!session) {
      return;
    }
    session.state = agentRunReducer(session.state, {
      type: 'set_streaming',
      payload: { streaming: false },
    });
    setThread((current) => ({
      ...current,
      turns: current.turns.map((turn) => {
        if (turn.kind !== 'runtime' || turn.session.runId !== stoppedRunId) {
          return turn;
        }
        return {
          ...turn,
          session: { ...session },
        };
      }),
    }));
  }, []);

  useEffect(() => {
    threadRef.current = thread;
  }, [thread]);

  const syncIdentityInputs = useCallback((identity: ThreadIdentity) => {
    setSessionId(identity.sessionId);
    setConversationId(identity.conversationId);
  }, []);

  const upsertRuntimeSession = useCallback((session: PlaygroundSession) => {
    const nextIdentity = getThreadIdentityFromSession(session);
    const currentIdentity = getThreadIdentityFromThread(threadRef.current);
    const identityChanged = !isSameThreadIdentity(currentIdentity, nextIdentity);

    if (identityChanged) {
      stopStreaming({ clearStreamingState: false });
      sessionsRef.current = {};
      activeRunIdRef.current = null;
      dismissedApprovalKeysRef.current.clear();
      dismissedManualActionKeysRef.current.clear();
      setApprovalDialog(null);
      setManualActionDialog(null);
      setActiveTurnId(null);
    }

    sessionsRef.current[session.runId] = session;
    activeRunIdRef.current = session.runId;
    setActiveTurnId(session.turnId);
    syncIdentityInputs(nextIdentity);

    setThread((current) => {
      const currentIdentityInState = getThreadIdentityFromThread(current);
      const nextTurn: PlaygroundRuntimeTurn = {
        kind: 'runtime',
        turnId: session.turnId,
        session: { ...session },
      };
      const nextTurns = [
        ...(isSameThreadIdentity(currentIdentityInState, nextIdentity) ? current.turns : []),
      ];
      const existingIndex = nextTurns.findIndex((turn) => (
        turn.turnId === session.turnId
        || (turn.kind === 'runtime' && turn.session.runId === session.runId)
      ));
      if (existingIndex >= 0) {
        nextTurns[existingIndex] = nextTurn;
      } else {
        nextTurns.push(nextTurn);
      }
      return {
        sessionId: nextIdentity.sessionId,
        conversationId: nextIdentity.conversationId,
        turns: nextTurns,
      };
    });
  }, [stopStreaming, syncIdentityInputs]);

  const refreshExecutorStatus = useCallback(async () => {
    setExecutorLoading(true);
    try {
      const payload: ExecExecutorStatusResponse = await api.getExecExecutorStatus();
      const readyRows = payload.rows.filter((item) => item.dispatch_ready);
      const degradedRows = payload.rows.filter((item) => item.dispatch_degraded);
      setExecutorSummary({
        total: payload.total,
        ready: payload.ready,
        degraded: degradedRows.length,
        generatedAt: payload.generated_at,
        topReadyProfiles: readyRows.slice(0, 3).map((item) => item.executor_profile),
        topDegradedProfiles: degradedRows.slice(0, 3).map((item) => item.executor_profile),
        rows: payload.rows,
      });
    } catch (error: unknown) {
      setPageError((current) => current || getErrorMessage(error, '加载 executor 状态失败'));
    } finally {
      setExecutorLoading(false);
    }
  }, []);

  const streamRun = useCallback(async (
    runId: string,
    options?: { stopOnApproval?: boolean },
  ) => {
    const activeSession = sessionsRef.current[runId];
    if (!activeSession) {
      return;
    }

    stopStreaming();
    const controller = new AbortController();
    const streamToken = createNextRuntimeStreamToken(streamTokenRef.current);
    streamTokenRef.current = streamToken;
    controllerRef.current = controller;
    activeRunIdRef.current = runId;
    activeSession.state = agentRunReducer(activeSession.state, {
      type: 'set_streaming',
      payload: { streaming: true },
    });
    upsertRuntimeSession({ ...activeSession });

    try {
      await api.streamAIRun(runId, {
        afterSeq: activeSession.state.lastSeq,
        signal: controller.signal,
        deadlineMs: resolveRuntimeClientDeadlineMs(180000),
        onEvent: ({ data }) => {
          if (!shouldHandleRuntimeStreamMutation({
            streamToken,
            currentToken: streamTokenRef.current,
            streamRunId: runId,
            activeRunId: activeRunIdRef.current,
          })) {
            return;
          }
          const envelope = normalizeAgentRunEventEnvelope(data);
          const session = sessionsRef.current[runId];
          if (!envelope || !session) {
            return;
          }

          session.state = agentRunReducer(session.state, {
            type: 'append_event',
            payload: { event: envelope as AgentRunEventEnvelope },
          });
          upsertRuntimeSession({ ...session });
          setActiveTurnId(session.turnId);

          const eventType = String(envelope.event_type || '').trim().toLowerCase();
          if (eventType === 'approval_required') {
            const latestApproval = buildLatestPendingApproval(session);
            if (latestApproval) {
              setApprovalDialog(latestApproval);
            }
          }

          const shouldStopOnApproval = options?.stopOnApproval !== false;
          if (eventType === 'run_status_changed') {
            const payload = asObject(envelope.payload);
            const status = String(payload.status || '').trim().toLowerCase();
            if (shouldStopOnRunStatusChanged(status, shouldStopOnApproval)) {
              controller.abort();
            }
            return;
          }
          if (
            eventType === 'run_finished'
            || eventType === 'run_failed'
            || eventType === 'run_cancelled'
            || eventType === 'action_waiting_user_input'
            || (shouldStopOnApproval && eventType === 'approval_required')
          ) {
            controller.abort();
          }
        },
      });
    } catch (error: unknown) {
      const aborted = controller.signal.aborted || String(asObject(error).name || '') === 'AbortError';
      if (!aborted) {
        if (!shouldHandleRuntimeStreamMutation({
          streamToken,
          currentToken: streamTokenRef.current,
          streamRunId: runId,
          activeRunId: activeRunIdRef.current,
        })) {
          return;
        }
        const session = sessionsRef.current[runId];
        if (session) {
          session.state = agentRunReducer(session.state, {
            type: 'set_stream_error',
            payload: { error: getErrorMessage(error, 'runtime 流式订阅失败') },
          });
          upsertRuntimeSession({ ...session });
        }
      }
    } finally {
      const shouldFinalizeCurrentStream = shouldHandleRuntimeStreamMutation({
        streamToken,
        currentToken: streamTokenRef.current,
        streamRunId: runId,
        activeRunId: activeRunIdRef.current,
      });
      if (shouldFinalizeCurrentStream) {
        const session = sessionsRef.current[runId];
        if (session) {
          const hasPendingApprovals = session.state.entities.approvalOrder.some((approvalId) => (
            session?.state.entities.approvalsById[approvalId]?.status === 'pending'
          ));
          const normalizedStatus = String(session.state.runMeta?.status || '').trim().toLowerCase();
          const waitingUserInput = normalizedStatus === 'waiting_user_input';
          if (!isTerminalAgentRunStatus(session.state.runMeta?.status) && !hasPendingApprovals && !waitingUserInput) {
            try {
              const reconciled = await reconcileAIRunState(runId, session.state, {
                stopWhenWaitingApproval: true,
              });
              session.state = reconciled.state;
            } catch (_error) {
              // Ignore reconcile failure and preserve the last streamed state.
            }
          }
          session.state = agentRunReducer(session.state, {
            type: 'set_streaming',
            payload: { streaming: false },
          });
          upsertRuntimeSession({ ...session });
        }
        if (controllerRef.current === controller) {
          controllerRef.current = null;
        }
        if (activeRunIdRef.current === runId) {
          activeRunIdRef.current = null;
        }
      }
    }
  }, [stopStreaming, upsertRuntimeSession]);

  const hydrateRun = useCallback(async (runId: string): Promise<PlaygroundSession> => {
    const normalizedRunId = String(runId || '').trim();
    if (!normalizedRunId) {
      throw new Error('run id 不能为空');
    }

    const snapshotResponse = await api.getAIRun(normalizedRunId);
    let session = buildSessionFromSnapshot(snapshotResponse.run);
    const eventsResponse = await api.getAIRunEvents(normalizedRunId, { afterSeq: 0, limit: 5000 });
    if (eventsResponse.events.length > 0) {
      session = {
        ...session,
        state: agentRunReducer(session.state, {
          type: 'hydrate_events',
          payload: { events: eventsResponse.events },
        }),
      };
    }
    upsertRuntimeSession(session);
    return session;
  }, [upsertRuntimeSession]);

  const hydrateThreadHistory = useCallback(async (
    targetSessionId: string,
    preferredConversationId?: string,
  ) => {
    const normalizedSessionId = String(targetSessionId || '').trim();
    if (!normalizedSessionId) {
      return;
    }
    const detail = await api.getAIHistoryDetail(normalizedSessionId);
    const detailContext = asObject(detail?.context);
    const detailConversationId = String(detailContext.conversation_id || '').trim();
    const nextIdentity = normalizeThreadIdentity({
      sessionId: normalizedSessionId,
      conversationId: preferredConversationId || detailConversationId,
    });
    const historyTurns = buildHistoryTurnSnapshots({
      messages: Array.isArray(detail?.messages) ? detail.messages : [],
      preferredConversationId,
      detailConversationId,
    }).map(toPlaygroundHistoryTurn);

    syncIdentityInputs(nextIdentity);
    setThread((current) => {
      const runtimeTurns = filterRuntimeTurnsByThread(
        current.turns.filter((turn): turn is PlaygroundRuntimeTurn => turn.kind === 'runtime')
          .map((turn) => ({
            ...turn,
            sessionId: turn.session.sessionId,
            conversationId: turn.session.conversationId,
          })),
        nextIdentity,
      ).map(({ sessionId: _sessionId, conversationId: _conversationId, ...turn }) => turn);
      return {
        sessionId: nextIdentity.sessionId,
        conversationId: nextIdentity.conversationId,
        turns: mergeHistoryTurnsWithRuntimeTurns(historyTurns, runtimeTurns),
      };
    });
  }, [syncIdentityInputs]);

  const handleStartRun = useCallback(async () => {
    const trimmedQuestion = question.trim();
    const trimmedServiceName = serviceName.trim();
    const trimmedTraceId = traceId.trim();
    const trimmedSessionId = sessionId.trim();
    const trimmedConversationId = conversationId.trim();
    if (!trimmedQuestion) {
      setPageError('请输入要测试的问题');
      return;
    }

    setSubmitting(true);
    setPageError(null);
    setApprovalDialog(null);
    setManualActionDialog(null);
    try {
      const clientDeadlineMs = resolveRuntimeClientDeadlineMs(180000);
      const analysisContext = buildRuntimeAnalysisContext({
        analysisType,
        traceId: trimmedTraceId,
        serviceName: trimmedServiceName,
        baseContext: {
          agent_mode: 'followup_analysis_runtime',
          runtime_mode: 'followup_analysis',
          runtime_profile: 'ai_runtime_lab',
        },
      });
      const created = await api.createAIRun({
        session_id: trimmedSessionId || undefined,
        question: trimmedQuestion,
        analysis_context: analysisContext,
        runtime_options: {
          mode: 'followup_analysis',
          runtime_profile: 'ai_runtime_lab',
          use_llm: useLLM,
          show_thought: showThought,
          auto_exec_readonly: autoExecReadonly,
          reset: false,
          conversation_id: trimmedConversationId || undefined,
          history: [],
        },
        client_deadline_ms: clientDeadlineMs,
      });

      const session = buildSessionFromSnapshot(created.run, trimmedQuestion);
      const nextIdentity = getThreadIdentityFromSession(session);
      const currentIdentity = getThreadIdentityFromThread(threadRef.current);
      if (shouldHydrateHistoryForThreadSwitch({
        currentIdentity,
        nextIdentity,
        currentTurnCount: threadRef.current.turns.length,
        nextSessionId: created.run.session_id,
      })) {
        await hydrateThreadHistory(created.run.session_id, session.conversationId);
      }
      upsertRuntimeSession(session);
      await streamRun(session.runId, { stopOnApproval: true });
    } catch (error: unknown) {
      setPageError(getErrorMessage(error, '创建 runtime run 失败'));
    } finally {
      setSubmitting(false);
    }
  }, [
    analysisType,
    autoExecReadonly,
    conversationId,
    question,
    serviceName,
    sessionId,
    showThought,
    streamRun,
    traceId,
    hydrateThreadHistory,
    upsertRuntimeSession,
    useLLM,
  ]);

  const handleLoadExistingRun = useCallback(async () => {
    const normalizedRunId = existingRunId.trim();
    if (!normalizedRunId) {
      setPageError('请输入已有 run id');
      return;
    }

    setSubmitting(true);
    setPageError(null);
    setApprovalDialog(null);
    setManualActionDialog(null);
    try {
      const session = await hydrateRun(normalizedRunId);
      if (session.sessionId) {
        await hydrateThreadHistory(session.sessionId, session.conversationId);
      }
      if (!isTerminalAgentRunStatus(session.state.runMeta?.status)) {
        await streamRun(normalizedRunId, { stopOnApproval: true });
      }
    } catch (error: unknown) {
      setPageError(getErrorMessage(error, '加载 run 失败'));
    } finally {
      setSubmitting(false);
    }
  }, [existingRunId, hydrateRun, hydrateThreadHistory, streamRun]);

  const handleRefreshCurrentRun = useCallback(async () => {
    const activeRuntimeTurn = activeTurnId
      ? thread.turns.find((turn): turn is PlaygroundRuntimeTurn => (
          turn.kind === 'runtime' && turn.turnId === activeTurnId
        ))
      : null;
    const activeRunId = activeRunIdRef.current || activeRuntimeTurn?.session.runId || null;
    if (!activeRunId) {
      return;
    }
    setPageError(null);
    try {
      const session = await hydrateRun(activeRunId);
      if (session.sessionId) {
        await hydrateThreadHistory(session.sessionId, session.conversationId);
      }
      if (!isTerminalAgentRunStatus(session.state.runMeta?.status)) {
        await streamRun(activeRunId, { stopOnApproval: true });
      }
    } catch (error: unknown) {
      setPageError(getErrorMessage(error, '刷新运行失败'));
    }
  }, [activeTurnId, hydrateRun, hydrateThreadHistory, streamRun, thread.turns]);

  const handleOpenApprovalDialog = useCallback((approval: RuntimeApprovalEntry) => {
    dismissedApprovalKeysRef.current.delete(buildApprovalDismissKey(approval));
    setManualActionDialog(null);
    setApprovalDialog(approval);
  }, []);

  const handleOpenManualActionDialog = useCallback((action: RuntimeManualActionEntry) => {
    if (!isExecutableManualAction(action)) {
      setPageError('当前动作还缺少一个关键信息，已转入继续确认流程。请在对话区直接说明排查范围或目标后继续。');
      return;
    }
    dismissedManualActionKeysRef.current.delete(buildManualActionDismissKey(action));
    setApprovalDialog(null);
    setManualActionDialog(action);
  }, []);

  const handleCloseApprovalDialog = useCallback(() => {
    if (approvalSubmitting) {
      return;
    }
    if (approvalDialog) {
      dismissedApprovalKeysRef.current.add(buildApprovalDismissKey(approvalDialog));
    }
    setApprovalDialog(null);
  }, [approvalDialog, approvalSubmitting]);

  const handleCloseManualActionDialog = useCallback(() => {
    if (approvalSubmitting) {
      return;
    }
    if (manualActionDialog) {
      dismissedManualActionKeysRef.current.add(buildManualActionDismissKey(manualActionDialog));
    }
    setManualActionDialog(null);
  }, [approvalSubmitting, manualActionDialog]);

  const executeApprovalDecision = useCallback(async (decision: 'approved' | 'rejected') => {
    if (!approvalDialog) {
      return;
    }

    const isApproval = decision === 'approved';
    setApprovalSubmitting(true);
    setPageError(null);
    try {
      const response = await api.approveAIRun(approvalDialog.runtimeRunId, {
        approval_id: approvalDialog.runtimeApprovalId,
        decision,
        confirmed: isApproval && Boolean(approvalDialog.requiresConfirmation || approvalDialog.requiresElevation),
        elevated: isApproval && Boolean(approvalDialog.requiresElevation),
      });
      const session = sessionsRef.current[approvalDialog.runtimeRunId];
      if (session) {
        session.state = agentRunReducer(session.state, {
          type: 'hydrate_snapshot',
          payload: { run: response.run },
        });
        upsertRuntimeSession({ ...session });
      }
      setApprovalDialog(null);
      if (isApproval) {
        const commandStatus = String(asObject(response.command).status || '').trim().toLowerCase();
        await streamRun(approvalDialog.runtimeRunId, { stopOnApproval: commandStatus !== 'running' });
      } else {
        const refreshedSession = await hydrateRun(approvalDialog.runtimeRunId);
        if (refreshedSession.sessionId) {
          await hydrateThreadHistory(refreshedSession.sessionId, refreshedSession.conversationId);
        }
      }
    } catch (error: unknown) {
      const errorMessage = getErrorMessage(error, isApproval ? '审批执行失败' : '拒绝命令失败');
      let latestPendingApproval: RuntimeApprovalEntry | null = null;
      try {
        const refreshedSession = await hydrateRun(approvalDialog.runtimeRunId);
        latestPendingApproval = buildLatestPendingApproval(refreshedSession);
      } catch (_refreshError) {
        // Ignore refresh errors and preserve original approval failure message.
      }
      if (
        isApproval
        && errorMessage.includes('approval_id does not match pending approval')
        && latestPendingApproval
      ) {
        if (
          latestPendingApproval.runtimeApprovalId
          && latestPendingApproval.runtimeApprovalId !== approvalDialog.runtimeApprovalId
        ) {
          try {
            const retryResponse = await api.approveAIRun(approvalDialog.runtimeRunId, {
              approval_id: latestPendingApproval.runtimeApprovalId,
              decision: 'approved',
              confirmed: Boolean(
                latestPendingApproval.requiresConfirmation
                || latestPendingApproval.requiresElevation,
              ),
              elevated: Boolean(latestPendingApproval.requiresElevation),
            });
            const session = sessionsRef.current[approvalDialog.runtimeRunId];
            if (session) {
              session.state = agentRunReducer(session.state, {
                type: 'hydrate_snapshot',
                payload: { run: retryResponse.run },
              });
              upsertRuntimeSession({ ...session });
            }
            setApprovalDialog(null);
            const commandStatus = String(asObject(retryResponse.command).status || '').trim().toLowerCase();
            await streamRun(approvalDialog.runtimeRunId, { stopOnApproval: commandStatus !== 'running' });
            return;
          } catch (retryError: unknown) {
            setApprovalDialog(latestPendingApproval);
            setPageError(getErrorMessage(retryError, '审批单已刷新，请重新确认执行'));
            return;
          }
        }
        if (latestPendingApproval) {
          setApprovalDialog(latestPendingApproval);
          setPageError('审批单已更新，请重新确认后执行。');
          return;
        }
      }
      if (!latestPendingApproval) {
        setApprovalDialog(null);
        setPageError(`${errorMessage}；审批状态已变化，已同步关闭审批窗口。`);
        return;
      }
      if (latestPendingApproval.runtimeApprovalId !== approvalDialog.runtimeApprovalId) {
        setApprovalDialog(latestPendingApproval);
        setPageError('审批单已更新，请重新确认后执行。');
        return;
      }
      setPageError(errorMessage);
    } finally {
      setApprovalSubmitting(false);
    }
  }, [approvalDialog, hydrateRun, hydrateThreadHistory, streamRun, upsertRuntimeSession]);

  const executeManualAction = useCallback(async () => {
    if (!manualActionDialog) {
      return;
    }
    if (!isExecutableManualAction(manualActionDialog)) {
      setManualActionDialog(null);
      setPageError('当前动作还缺少一个关键信息，无法直接执行。请先确认排查范围或目标。');
      return;
    }

    setApprovalSubmitting(true);
    setPageError(null);
    try {
      const stepId = String(manualActionDialog.actionId || '').trim() || 'step-1';
      const purpose = String(
        manualActionDialog.purpose
        || manualActionDialog.title
        || manualActionDialog.command,
      ).trim();
      const baseCommandSpec = (
        asOptionalObject(manualActionDialog.commandSpec)
        || buildRuntimeCommandSpec({
          command: manualActionDialog.command,
          purpose,
          title: manualActionDialog.title,
          stepId,
        })
      );
      const buildRequestPayload = (params?: {
        command?: string;
        commandSpec?: UnknownObject;
      }) => ({
        action_id: manualActionDialog.actionId,
        step_id: stepId,
        command: String(params?.command || manualActionDialog.command).trim() || manualActionDialog.command,
        command_spec: params?.commandSpec || baseCommandSpec,
        purpose,
        title: manualActionDialog.title,
        confirmed: true,
        elevated: Boolean(manualActionDialog.requiresElevation),
        client_deadline_ms: resolveRuntimeClientDeadlineMs(180000),
      });

      let commandResult = await api.executeAIRunCommand(
        manualActionDialog.runtimeRunId,
        buildRequestPayload(),
      );
      let status = String(commandResult.status || '').trim().toLowerCase();
      if (status === 'blocked' || status === 'waiting_user_input') {
        const recoveryPayload = asObject(asObject(commandResult.error).recovery || commandResult.recovery);
        const recovery = parseCommandSpecRecovery(recoveryPayload);
        if (recovery.suggestedCommandSpec) {
          commandResult = await api.executeAIRunCommand(
            manualActionDialog.runtimeRunId,
            buildRequestPayload({
              command: recovery.suggestedCommand || manualActionDialog.command,
              commandSpec: recovery.suggestedCommandSpec,
            }),
          );
          status = String(commandResult.status || '').trim().toLowerCase();
        } else {
          throw new Error(buildRecoveryMessage({
            fallback: '结构化命令预检失败，请先修正 command_spec 后重试',
            responsePayload: asObject(commandResult),
          }));
        }
      }

      const runPayload = asObject(commandResult.run);
      if (runPayload && typeof runPayload === 'object' && String(runPayload.run_id || '').trim()) {
        const session = sessionsRef.current[manualActionDialog.runtimeRunId];
        if (session) {
          session.state = agentRunReducer(session.state, {
            type: 'hydrate_snapshot',
            payload: { run: runPayload as unknown as AgentRunSnapshot },
          });
          upsertRuntimeSession({ ...session });
        }
      }
      if (status === 'elevation_required' || status === 'confirmation_required') {
        const approvalPayload = asObject(commandResult.approval);
        const runSummaryPayload = asObject((runPayload as UnknownObject).summary_json);
        const pendingApprovalPayload = asObject(runSummaryPayload.pending_approval);
        const mergedApprovalPayload: UnknownObject = {
          ...pendingApprovalPayload,
          ...approvalPayload,
        };
        const approvalId = String(
          mergedApprovalPayload.approval_id
          || mergedApprovalPayload.confirmation_ticket
          || '',
        ).trim();
        if (approvalId) {
          setApprovalDialog({
            id: approvalId,
            runtimeRunId: manualActionDialog.runtimeRunId,
            runtimeApprovalId: approvalId,
            title: String(
              mergedApprovalPayload.title
              || manualActionDialog.title
              || manualActionDialog.command,
            ).trim() || manualActionDialog.title,
            command: String(mergedApprovalPayload.command || manualActionDialog.command).trim(),
            purpose: manualActionDialog.purpose,
            message: String(
              mergedApprovalPayload.reason
              || mergedApprovalPayload.message
              || '',
            ).trim() || undefined,
            status: 'pending',
            commandType: String(mergedApprovalPayload.command_type || manualActionDialog.commandType || '').trim() || undefined,
            riskLevel: String(mergedApprovalPayload.risk_level || manualActionDialog.riskLevel || '').trim() || undefined,
            commandFamily: String(mergedApprovalPayload.command_family || '').trim() || undefined,
            approvalPolicy: String(mergedApprovalPayload.approval_policy || '').trim() || undefined,
            executorType: String(mergedApprovalPayload.executor_type || '').trim() || undefined,
            executorProfile: String(mergedApprovalPayload.executor_profile || '').trim() || undefined,
            targetKind: String(mergedApprovalPayload.target_kind || '').trim() || undefined,
            targetIdentity: String(mergedApprovalPayload.target_identity || '').trim() || undefined,
            effectiveExecutorType: String(mergedApprovalPayload.effective_executor_type || '').trim() || undefined,
            effectiveExecutorProfile: String(mergedApprovalPayload.effective_executor_profile || '').trim() || undefined,
            dispatchBackend: String(mergedApprovalPayload.dispatch_backend || '').trim() || undefined,
            dispatchMode: String(mergedApprovalPayload.dispatch_mode || '').trim() || undefined,
            dispatchReason: String(mergedApprovalPayload.dispatch_reason || '').trim() || undefined,
            requiresConfirmation: Boolean(mergedApprovalPayload.requires_confirmation),
            requiresElevation: Boolean(mergedApprovalPayload.requires_elevation),
            messageId: undefined,
            actionId: manualActionDialog.actionId,
            confirmationTicket: String(mergedApprovalPayload.confirmation_ticket || approvalId),
            updatedAt: new Date().toISOString(),
          });
        } else {
          const refreshedSession = await hydrateRun(manualActionDialog.runtimeRunId);
          if (refreshedSession.sessionId) {
            await hydrateThreadHistory(refreshedSession.sessionId, refreshedSession.conversationId);
          }
          const pendingApproval = buildLatestPendingApproval(refreshedSession);
          if (pendingApproval) {
            setApprovalDialog(pendingApproval);
          } else if (!isTerminalAgentRunStatus(refreshedSession.state.runMeta?.status)) {
            await streamRun(manualActionDialog.runtimeRunId, { stopOnApproval: true });
          }
        }
        setManualActionDialog(null);
        return;
      }
      if (status === 'permission_required' || status === 'failed' || status === 'blocked' || status === 'waiting_user_input') {
        const failureMessage = buildRecoveryMessage({
          fallback: '人工确认执行失败',
          responsePayload: asObject(commandResult),
        });
        throw new Error(failureMessage || '人工确认执行失败');
      }
      setManualActionDialog(null);
      const refreshedSession = await hydrateRun(manualActionDialog.runtimeRunId);
      if (refreshedSession.sessionId) {
        await hydrateThreadHistory(refreshedSession.sessionId, refreshedSession.conversationId);
      }
      if (!isTerminalAgentRunStatus(refreshedSession.state.runMeta?.status)) {
        await streamRun(manualActionDialog.runtimeRunId, { stopOnApproval: true });
      }
    } catch (error: unknown) {
      setPageError(getErrorMessage(error, '人工确认执行失败'));
    } finally {
      setApprovalSubmitting(false);
    }
  }, [manualActionDialog, hydrateRun, hydrateThreadHistory, streamRun, upsertRuntimeSession]);

  const handleCancelRun = useCallback(async (runId: string) => {
    setPageError(null);
    try {
      if (activeRunIdRef.current === runId) {
        stopStreaming();
      }
      const response = await api.cancelAIRun(runId, { reason: 'cancelled from ai runtime lab' });
      const session = sessionsRef.current[runId];
      if (session) {
        session.state = agentRunReducer(session.state, {
          type: 'hydrate_snapshot',
          payload: { run: response.run },
        });
        upsertRuntimeSession({ ...session });
      }
    } catch (error: unknown) {
      setPageError(getErrorMessage(error, '取消运行失败'));
    }
  }, [stopStreaming, upsertRuntimeSession]);

  const handleInterruptRun = useCallback(async (runId: string, reason = 'user_interrupt_esc') => {
    setPageError(null);
    try {
      if (activeRunIdRef.current === runId) {
        stopStreaming();
      }
      const response = await api.interruptAIRun(runId, { reason });
      const session = sessionsRef.current[runId];
      if (session) {
        session.state = agentRunReducer(session.state, {
          type: 'hydrate_snapshot',
          payload: { run: response.run },
        });
        upsertRuntimeSession({ ...session });
      }
    } catch (error: unknown) {
      setPageError(getErrorMessage(error, '中断运行失败'));
    }
  }, [stopStreaming, upsertRuntimeSession]);

  useEffect(() => () => {
    stopStreaming({ clearStreamingState: false });
  }, [stopStreaming]);

  useEffect(() => {
    void refreshExecutorStatus();
  }, [refreshExecutorStatus]);

  const activeRuntimeSession = useMemo(() => {
    if (activeTurnId) {
      const matchedTurn = thread.turns.find((turn): turn is PlaygroundRuntimeTurn => (
        turn.kind === 'runtime' && turn.turnId === activeTurnId
      ));
      if (matchedTurn) {
        return matchedTurn.session;
      }
    }
    const runtimeTurns = thread.turns.filter((turn): turn is PlaygroundRuntimeTurn => turn.kind === 'runtime');
    return runtimeTurns.length > 0 ? runtimeTurns[runtimeTurns.length - 1].session : null;
  }, [activeTurnId, thread.turns]);

  const pendingUserInputRequest = useMemo(
    () => (activeRuntimeSession ? buildPendingUserInputRequest(activeRuntimeSession) : null),
    [activeRuntimeSession],
  );

  const handleSubmitInlineUserInput = useCallback(async (params: {
    runId: string;
    text: string;
    source?: string;
  }) => {
    const runId = String(params.runId || '').trim();
    const text = String(params.text || '').trim();
    if (!runId || !text) {
      setPageError('请先输入一句话说明，然后继续运行。');
      return;
    }
    setPendingInputSubmitting(true);
    setPageError(null);
    try {
      const response = await api.continueAIRunWithInput(runId, {
        text,
        source: params.source || 'user',
      });
      setBlockedInputRetryPayload(null);
      const session = sessionsRef.current[runId];
      if (session) {
        session.state = agentRunReducer(session.state, {
          type: 'hydrate_snapshot',
          payload: { run: response.run },
        });
        upsertRuntimeSession({ ...session });
      }
      if (!isTerminalAgentRunStatus(String(response.run.status || ''))) {
        await streamRun(runId, { stopOnApproval: true });
      }
    } catch (error: unknown) {
      const errorCode = getErrorCode(error);
      if (errorCode === 'context_hydration_timeout' || errorCode === 'context_hydration_failed') {
        setBlockedInputRetryPayload({
          runId,
          text,
          source: params.source || 'user',
        });
      } else {
        setBlockedInputRetryPayload(null);
      }
      setPageError(getErrorMessage(error, '提交补充输入失败'));
    } finally {
      setPendingInputSubmitting(false);
    }
  }, [streamRun, upsertRuntimeSession]);

  const handleUseTemplateAsInput = useCallback((params: {
    command: string;
  }) => {
    const suggestedCommand = String(params.command || '').trim();
    if (!suggestedCommand) {
      return;
    }
    const current = String(question || '').trim();
    if (current && current !== suggestedCommand) {
      const shouldReplace = window.confirm('输入框已有内容，是否替换为建议命令？');
      if (!shouldReplace) {
        return;
      }
    }
    setQuestion(suggestedCommand);
    setPageError(null);
  }, [question]);

  const handleRetryBlockedInput = useCallback(async () => {
    if (!blockedInputRetryPayload || pendingInputSubmitting) {
      return;
    }
    await handleSubmitInlineUserInput(blockedInputRetryPayload);
  }, [blockedInputRetryPayload, handleSubmitInlineUserInput, pendingInputSubmitting]);

  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') {
        return;
      }
      if (!activeRuntimeSession) {
        return;
      }
      const status = String(activeRuntimeSession.state.runMeta?.status || '').trim().toLowerCase();
      if (!status || isTerminalAgentRunStatus(status)) {
        return;
      }
      if (interruptingRef.current) {
        return;
      }
      event.preventDefault();
      interruptingRef.current = true;
      void handleInterruptRun(activeRuntimeSession.runId, 'user_interrupt_esc').finally(() => {
        interruptingRef.current = false;
      });
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [activeRuntimeSession, handleInterruptRun]);

  const latestPendingApproval = useMemo(() => (
    activeRuntimeSession ? buildLatestPendingApproval(activeRuntimeSession) : null
  ), [activeRuntimeSession]);

  const runtimeProjectionByTurnId = useMemo(() => {
    let planningAlreadyShown = false;
    return thread.turns.reduce<Record<string, RuntimeTranscriptMessage>>((result, turn) => {
      if (turn.kind !== 'runtime') {
        return result;
      }
      const transcript = buildRuntimeEventProjection({
        runId: turn.session.runId,
        title: turn.session.title,
        state: turn.session.state,
        suppressBoilerplatePlanning: planningAlreadyShown,
      });
      if (!planningAlreadyShown) {
        planningAlreadyShown = transcript.blocks.some((block) => (
          block.type === 'thinking' && (block.phase === 'planning' || block.phase === 'plan')
        ));
      }
      result[turn.turnId] = transcript;
      return result;
    }, {});
  }, [thread.turns]);

  const transcriptMessagesByTurnId = useMemo(() => (
    Object.entries(runtimeProjectionByTurnId).reduce<Record<string, RuntimeTranscriptMessage>>((result, [turnId, message]) => {
      result[turnId] = message;
      return result;
    }, {})
  ), [runtimeProjectionByTurnId]);

  const latestPendingManualAction = useMemo(() => (
    buildLatestPendingManualAction(thread, runtimeProjectionByTurnId)
  ), [thread, runtimeProjectionByTurnId]);

  const analysisModeResolution = useMemo(() => resolveRuntimeAnalysisMode({
    analysisType,
    traceId,
  }), [analysisType, traceId]);
  const downgradeNotice = analysisModeResolution.downgraded
    ? buildRuntimeDowngradeNotice(analysisModeResolution.reason)
    : null;
  const readonlyPolicyNotice = autoExecReadonly
    ? null
    : '当前运行仅生成只读排查命令，不会自动执行；如需自动补证据，请开启“自动执行只读命令”。';

  useEffect(() => {
    if (!approvalDialog) {
      return;
    }
    if (hasPendingApproval(thread, approvalDialog)) {
      return;
    }
    setApprovalDialog(null);
  }, [approvalDialog, thread]);

  useEffect(() => {
    if (!manualActionDialog) {
      return;
    }
    if (hasPendingManualAction(thread, manualActionDialog, runtimeProjectionByTurnId)) {
      return;
    }
    setManualActionDialog(null);
  }, [manualActionDialog, runtimeProjectionByTurnId, thread]);

  useEffect(() => {
    if (approvalSubmitting) {
      return;
    }
    if (latestPendingApproval) {
      if (dismissedApprovalKeysRef.current.has(buildApprovalDismissKey(latestPendingApproval))) {
        return;
      }
      if (manualActionDialog) {
        setManualActionDialog(null);
      }
      setApprovalDialog((current) => (
        current?.runtimeApprovalId === latestPendingApproval.runtimeApprovalId ? current : latestPendingApproval
      ));
      return;
    }
    if (approvalDialog) {
      return;
    }
  }, [
    approvalDialog,
    approvalSubmitting,
    latestPendingApproval,
    manualActionDialog,
  ]);

  useEffect(() => {
    bottomAnchorRef.current?.scrollIntoView({
      behavior: activeRuntimeSession?.state.streaming ? 'auto' : 'smooth',
      block: 'end',
    });
  }, [activeRuntimeSession?.state.lastSeq, activeRuntimeSession?.state.streaming, thread.turns.length]);

  useEffect(() => {
    const node = composerRef.current;
    if (!node) {
      return undefined;
    }

    const updateOffset = () => {
      setComposerOffset(node.getBoundingClientRect().height + 24);
    };

    updateOffset();
    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', updateOffset);
      return () => {
        window.removeEventListener('resize', updateOffset);
      };
    }

    const observer = new ResizeObserver(() => {
      updateOffset();
    });
    observer.observe(node);
    return () => {
      observer.disconnect();
    };
  }, []);

  const debugContent = useMemo(() => {
    if (!activeRuntimeSession) {
      return null;
    }

    const events = activeRuntimeSession.state.entities.events;
    const latestMetadata = activeRuntimeSession.state.runMeta
      ? {
          run_id: activeRuntimeSession.state.runMeta.runId,
          session_id: activeRuntimeSession.state.runMeta.sessionId,
          conversation_id: activeRuntimeSession.conversationId,
          status: activeRuntimeSession.state.runMeta.status,
          current_phase: activeRuntimeSession.state.runMeta.currentPhase,
          iteration: activeRuntimeSession.state.runMeta.iteration,
          event_count: events.length,
          last_seq: activeRuntimeSession.state.lastSeq,
          updated_at: activeRuntimeSession.state.runMeta.updatedAt,
        }
      : {};

    return (
      <div className="space-y-4">
        <div>
          <div className="text-xs font-medium uppercase tracking-wide text-slate-500">Run Snapshot</div>
          <pre className="mt-2 overflow-auto rounded-xl bg-slate-950 p-3 text-[12px] leading-5 text-slate-100">
            {JSON.stringify(latestMetadata, null, 2)}
          </pre>
        </div>
        <div>
          <div className="text-xs font-medium uppercase tracking-wide text-slate-500">Recent Events</div>
          <div className="mt-2 space-y-2">
            {events.length === 0 ? (
              <div className="rounded-xl border border-dashed border-slate-200 bg-white p-3 text-sm text-slate-500">
                当前还没有收到事件。
              </div>
            ) : (
              [...events].slice(-12).reverse().map((event) => (
                <details
                  key={`${event.run_id}-${event.seq}`}
                  className="rounded-xl border border-slate-200 bg-white p-3"
                >
                  <summary className="cursor-pointer list-none">
                    <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
                      <span className="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5">
                        seq {event.seq}
                      </span>
                      <span className="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5">
                        {event.event_type}
                      </span>
                      {event.created_at && <span>{formatTime(event.created_at)}</span>}
                    </div>
                  </summary>
                  <pre className="mt-3 overflow-auto rounded-xl bg-slate-950 p-3 text-[12px] leading-5 text-slate-100">
                    {JSON.stringify(event.payload, null, 2)}
                  </pre>
                </details>
              ))
            )}
          </div>
        </div>
      </div>
    );
  }, [activeRuntimeSession]);

  const runtimeMeta = activeRuntimeSession?.state.runMeta || null;
  const pendingApprovals = activeRuntimeSession
    && String(activeRuntimeSession.state.runMeta?.status || '').trim().toLowerCase() === 'waiting_approval'
    ? activeRuntimeSession.state.entities.approvalOrder.filter((approvalId) => (
        activeRuntimeSession.state.entities.approvalsById[approvalId]?.status === 'pending'
      )).length
    : 0;

  const handleQuestionKeyDown = useCallback((event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
      event.preventDefault();
      if (!submitting && question.trim()) {
        void handleStartRun();
      }
    }
  }, [handleStartRun, question, submitting]);

  return (
    <div className="min-h-full bg-slate-100">
      <div className="mx-auto max-w-6xl px-4 py-5 sm:px-6 lg:px-8">
        <div className="rounded-[28px] border border-slate-200 bg-white shadow-sm">
          <div className="border-b border-slate-200 bg-[radial-gradient(circle_at_top_left,_rgba(8,145,178,0.16),_transparent_34%),linear-gradient(135deg,#f8fafc_0%,#eef2ff_48%,#ecfeff_100%)] px-6 py-6">
            <div className="max-w-3xl">
              <div className="inline-flex items-center gap-2 rounded-full border border-slate-200 bg-white/80 px-3 py-1 text-xs uppercase tracking-[0.22em] text-slate-600">
                <BrainCircuit className="h-3.5 w-3.5" />
                AI Runtime Lab
              </div>
              <h1 className="mt-4 text-2xl font-semibold tracking-tight text-slate-900">
                聊天式运行态验证页
              </h1>
              <p className="mt-2 text-sm leading-6 text-slate-600">
                主会话区只保留正常对话内容，执行细节统一收进可折叠详情。审批和人工确认会直接打断并弹窗，不再让你自己去翻卡片。
              </p>
            </div>
          </div>

          <div className="space-y-5 px-4 py-5 sm:px-6">
            <section className="rounded-3xl border border-slate-200 bg-slate-50 p-4">
              <div className="flex flex-col gap-3 lg:flex-row lg:items-center">
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-semibold text-slate-900">运行上下文</div>
                  <div className="mt-2 flex flex-wrap gap-2 text-xs text-slate-600">
                    <span className="rounded-full border border-slate-200 bg-white px-2.5 py-1">
                      analysis {analysisType}
                    </span>
                    {serviceName.trim() && (
                      <span className="rounded-full border border-slate-200 bg-white px-2.5 py-1">
                        service {serviceName.trim()}
                      </span>
                    )}
                    {runtimeMeta?.status && (
                      <span className={`rounded-full border px-2.5 py-1 ${renderToneClassName(runtimeMeta.status)}`}>
                        {runtimeMeta.status}
                      </span>
                    )}
                    {pendingApprovals > 0 && (
                      <span className="rounded-full border border-amber-200 bg-amber-50 px-2.5 py-1 text-amber-800">
                        待审批 {pendingApprovals}
                      </span>
                    )}
                    {pendingUserInputRequest && (
                      <span className="rounded-full border border-amber-200 bg-amber-50 px-2.5 py-1 text-amber-800">
                        待确认关键信息
                      </span>
                    )}
                    {runtimeMeta?.sessionId && (
                      <span className="rounded-full border border-slate-200 bg-white px-2.5 py-1">
                        session {runtimeMeta.sessionId}
                      </span>
                    )}
                    {runtimeMeta?.currentPhase && (
                      <span className="rounded-full border border-slate-200 bg-white px-2.5 py-1">
                        phase {runtimeMeta.currentPhase}
                      </span>
                    )}
                    {executorSummary && (
                      <span className="rounded-full border border-slate-200 bg-white px-2.5 py-1">
                        executor {executorSummary.ready}/{executorSummary.total} ready
                      </span>
                    )}
                    {executorSummary && executorSummary.degraded > 0 && (
                      <span className="rounded-full border border-amber-200 bg-amber-50 px-2.5 py-1 text-amber-800">
                        fallback {executorSummary.degraded}
                      </span>
                    )}
                  </div>
                  {executorSummary && (
                    <div className="mt-3 space-y-2 text-xs text-slate-500">
                      <div>
                        已接真实模板:
                        {' '}
                        {executorSummary.topReadyProfiles.length > 0
                          ? executorSummary.topReadyProfiles.join(' / ')
                          : '暂无'}
                      </div>
                      <div>
                        仍在回退:
                        {' '}
                        {executorSummary.topDegradedProfiles.length > 0
                          ? executorSummary.topDegradedProfiles.join(' / ')
                          : '无'}
                      </div>
                      {executorSummary.generatedAt && (
                        <div>executor snapshot {formatTime(executorSummary.generatedAt)}</div>
                      )}
                    </div>
                  )}
                  {executorSummary && (
                    <details className="mt-3 rounded-2xl border border-slate-200 bg-white px-4 py-3">
                      <summary className="cursor-pointer list-none text-sm font-medium text-slate-700">
                        查看执行器 readiness 明细
                      </summary>
                      <div className="mt-4 space-y-4">
                        <div>
                          <div className="text-xs font-medium uppercase tracking-wide text-slate-500">Ready Profiles</div>
                          <div className="mt-2 space-y-2">
                            {executorSummary.rows.filter((item) => item.dispatch_ready).length > 0 ? (
                              executorSummary.rows
                                .filter((item) => item.dispatch_ready)
                                .map((item) => (
                                  <div
                                    key={`ready-${item.executor_profile}`}
                                    className="rounded-2xl border border-emerald-200 bg-emerald-50 px-3 py-3 text-xs text-slate-700"
                                  >
                                    <div className="flex flex-wrap gap-2">
                                      <span className="rounded-full border border-emerald-200 bg-white px-2 py-0.5 text-emerald-700">
                                        {item.executor_profile}
                                      </span>
                                      {item.rollout_stage && (
                                        <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5">
                                          {item.rollout_stage}
                                        </span>
                                      )}
                                      <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5">
                                        {item.dispatch_backend}
                                      </span>
                                      <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5">
                                        {item.target_identity}
                                      </span>
                                    </div>
                                    {item.summary && (
                                      <div className="mt-2 text-slate-700">{item.summary}</div>
                                    )}
                                    <div className="mt-2 text-slate-600">{item.dispatch_reason || '已接入真实模板'}</div>
                                    {item.dispatch_template_env && (
                                      <div className="mt-1 text-slate-500">env: {item.dispatch_template_env}</div>
                                    )}
                                    {item.example_template && (
                                      <pre className="mt-2 overflow-auto rounded-xl border border-emerald-100 bg-white p-2 text-[11px] leading-5 text-slate-600">
                                        {item.example_template}
                                      </pre>
                                    )}
                                    <pre className="mt-2 overflow-auto rounded-xl border border-slate-200 bg-slate-950 p-2 text-[11px] leading-5 text-slate-100">
                                      {buildExecutorValuesSnippet(item)}
                                    </pre>
                                  </div>
                                ))
                            ) : (
                              <div className="rounded-2xl border border-dashed border-slate-200 bg-slate-50 px-3 py-3 text-xs text-slate-500">
                                当前还没有已接真实模板的 executor profile。
                              </div>
                            )}
                          </div>
                        </div>
                        <div>
                          <div className="text-xs font-medium uppercase tracking-wide text-slate-500">Fallback Profiles</div>
                          <div className="mt-2 space-y-2">
                            {executorSummary.rows.filter((item) => item.dispatch_degraded).length > 0 ? (
                              executorSummary.rows
                                .filter((item) => item.dispatch_degraded)
                                .map((item) => (
                                  <div
                                    key={`degraded-${item.executor_profile}`}
                                    className="rounded-2xl border border-amber-200 bg-amber-50 px-3 py-3 text-xs text-slate-700"
                                  >
                                    <div className="flex flex-wrap gap-2">
                                      <span className="rounded-full border border-amber-200 bg-white px-2 py-0.5 text-amber-800">
                                        {item.executor_profile}
                                      </span>
                                      {item.rollout_stage && (
                                        <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5">
                                          {item.rollout_stage}
                                        </span>
                                      )}
                                      <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5">
                                        {item.effective_executor_profile}
                                      </span>
                                      <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5">
                                        {item.target_identity}
                                      </span>
                                    </div>
                                    {item.summary && (
                                      <div className="mt-2 text-slate-700">{item.summary}</div>
                                    )}
                                    <div className="mt-2 text-slate-600">{item.dispatch_reason || '仍在本地回退'}</div>
                                    {item.candidate_template_envs.length > 0 && (
                                      <div className="mt-1 text-slate-500">
                                        candidate env: {item.candidate_template_envs.join(' / ')}
                                      </div>
                                    )}
                                    {item.example_template && (
                                      <pre className="mt-2 overflow-auto rounded-xl border border-amber-100 bg-white p-2 text-[11px] leading-5 text-slate-600">
                                        {item.example_template}
                                      </pre>
                                    )}
                                    <pre className="mt-2 overflow-auto rounded-xl border border-slate-200 bg-slate-950 p-2 text-[11px] leading-5 text-slate-100">
                                      {buildExecutorValuesSnippet(item)}
                                    </pre>
                                  </div>
                                ))
                            ) : (
                              <div className="rounded-2xl border border-dashed border-slate-200 bg-slate-50 px-3 py-3 text-xs text-slate-500">
                                当前没有 degraded executor profile。
                              </div>
                            )}
                          </div>
                        </div>
                      </div>
                    </details>
                  )}
                </div>

                <div className="flex flex-wrap gap-2">
                  <button
                    type="button"
                    onClick={() => void refreshExecutorStatus()}
                    disabled={executorLoading}
                    className="inline-flex items-center gap-2 rounded-2xl border border-slate-200 bg-white px-4 py-2 text-sm font-medium text-slate-700 transition hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    {executorLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
                    刷新执行器
                  </button>
                  <button
                    type="button"
                    onClick={() => void handleRefreshCurrentRun()}
                    disabled={!activeRuntimeSession || submitting}
                    className="inline-flex items-center gap-2 rounded-2xl border border-slate-200 bg-white px-4 py-2 text-sm font-medium text-slate-700 transition hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    <RefreshCw className="h-4 w-4" />
                    刷新当前 run
                  </button>
                </div>
              </div>

              <div className="mt-4 flex flex-col gap-3 rounded-2xl border border-slate-200 bg-white p-4 lg:flex-row lg:items-center">
                <div className="min-w-0 flex-1">
                  <div className="mb-1.5 text-xs font-medium text-slate-600">加载已有 Run</div>
                  <input
                    value={existingRunId}
                    onChange={(event) => setExistingRunId(event.target.value)}
                    className="w-full rounded-2xl border border-slate-200 bg-slate-50 px-4 py-2.5 text-sm text-slate-900 outline-none transition focus:border-cyan-400 focus:ring-2 focus:ring-cyan-100"
                    placeholder="例如 run-a6399146b653"
                  />
                </div>
                <button
                  type="button"
                  onClick={() => void handleLoadExistingRun()}
                  disabled={submitting}
                  className="inline-flex items-center justify-center gap-2 rounded-2xl border border-slate-200 bg-slate-900 px-4 py-2.5 text-sm font-medium text-white transition hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {submitting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}
                  加载 run
                </button>
              </div>
            </section>

            {pageError && (
              <div className="rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
                <div className="flex items-start gap-2">
                  <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                  <div className="flex min-w-0 flex-1 items-center justify-between gap-3">
                    <span>{pageError}</span>
                    {blockedInputRetryPayload && (
                      <button
                        type="button"
                        onClick={() => {
                          void handleRetryBlockedInput();
                        }}
                        disabled={pendingInputSubmitting}
                        className="inline-flex shrink-0 items-center justify-center rounded-lg border border-rose-300 bg-white px-3 py-1 text-xs font-medium text-rose-700 transition hover:bg-rose-100 disabled:cursor-not-allowed disabled:opacity-60"
                      >
                        {pendingInputSubmitting ? '重试中...' : '重试'}
                      </button>
                    )}
                  </div>
                </div>
              </div>
            )}

            <section className="space-y-4" style={{ paddingBottom: `${composerOffset}px` }}>
              {thread.turns.length > 0 ? (
                <>
                  {thread.turns.map((turn) => {
                    if (turn.kind === 'history') {
                      return (
                        <div key={turn.turnId} className="space-y-4">
                          {turn.question && (
                            <div className="overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-sm">
                              <div className="flex items-start gap-3 px-5 py-5">
                                <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-cyan-600 text-white">
                                  <BrainCircuit className="h-5 w-5" />
                                </div>
                                <div className="min-w-0">
                                  <div className="text-sm font-semibold text-slate-900">你</div>
                                  <div className="mt-2 whitespace-pre-wrap text-sm leading-7 text-slate-700">
                                    {turn.question}
                                  </div>
                                </div>
                              </div>
                            </div>
                          )}
                          <div className="overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-sm">
                            <div className="flex items-start gap-3 px-5 py-5">
                              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-slate-900 text-white">
                                <BrainCircuit className="h-5 w-5" />
                              </div>
                              <div className="min-w-0">
                                <div className="text-sm font-semibold text-slate-900">AI</div>
                                <div className="mt-2 whitespace-pre-wrap text-sm leading-7 text-slate-700">
                                  {turn.answer}
                                </div>
                                {turn.timestamp && (
                                  <div className="mt-3 text-xs text-slate-500">
                                    {formatTime(turn.timestamp)}
                                  </div>
                                )}
                              </div>
                            </div>
                          </div>
                        </div>
                      );
                    }

                    const transcriptMessage = transcriptMessagesByTurnId[turn.turnId];
                    return (
                      <div key={turn.turnId} className="space-y-4">
                        <div
                          className={`overflow-hidden rounded-3xl border bg-white shadow-sm ${
                            turn.turnId === activeTurnId ? 'border-cyan-300' : 'border-slate-200'
                          }`}
                        >
                          <button
                            type="button"
                            onClick={() => setActiveTurnId(turn.turnId)}
                            className="flex w-full items-start gap-3 px-5 py-5 text-left"
                          >
                            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-cyan-600 text-white">
                              <BrainCircuit className="h-5 w-5" />
                            </div>
                            <div className="min-w-0">
                              <div className="text-sm font-semibold text-slate-900">你</div>
                              <div className="mt-2 whitespace-pre-wrap text-sm leading-7 text-slate-700">
                                {turn.session.question || question}
                              </div>
                            </div>
                          </button>
                        </div>

                        {transcriptMessage && (
                          <RuntimeConversationCard
                            message={transcriptMessage}
                            disabled={submitting || approvalSubmitting || pendingInputSubmitting}
                            submittingUserInput={pendingInputSubmitting}
                            onApprove={(approval) => {
                              setActiveTurnId(turn.turnId);
                              handleOpenApprovalDialog(approval);
                            }}
                            onExecuteManualAction={(action) => {
                              setActiveTurnId(turn.turnId);
                              handleOpenManualActionDialog(action);
                            }}
                            onSubmitUserInput={(params) => {
                              setActiveTurnId(turn.turnId);
                              return handleSubmitInlineUserInput({
                                runId: params.runId,
                                text: params.text,
                                source: params.source,
                              });
                            }}
                            onUseTemplateAsInput={(params) => {
                              setActiveTurnId(turn.turnId);
                              handleUseTemplateAsInput({
                                command: params.command,
                              });
                            }}
                            onCancelRun={(runId) => void handleCancelRun(runId)}
                            debugContent={turn.turnId === activeTurnId ? debugContent : null}
                          />
                        )}
                      </div>
                    );
                  })}
                </>
              ) : (
                <div className="rounded-3xl border border-dashed border-slate-300 bg-white px-6 py-10 text-center text-sm text-slate-500">
                  还没有开始任务。直接在底部输入问题发起任务，或者先加载已有 run。
                </div>
              )}
              <div ref={bottomAnchorRef} />
            </section>
          </div>
        </div>
      </div>

      <div className="sticky bottom-0 z-20 border-t border-slate-200 bg-white/95 backdrop-blur">
        <div
          ref={composerRef}
          className="mx-auto max-w-6xl px-4 pt-4 sm:px-6 lg:px-8"
          style={{ paddingBottom: 'calc(1rem + env(safe-area-inset-bottom, 0px))' }}
        >
          <div className="rounded-[28px] border border-slate-200 bg-white shadow-[0_-16px_48px_rgba(15,23,42,0.06)]">
            <div className="border-b border-slate-100 px-5 py-4">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <div className="text-sm font-semibold text-slate-900">继续输入任务</div>
                  <div className="mt-1 text-xs text-slate-500">
                    `Ctrl/Cmd + Enter` 立即发送。默认按聊天流展示，执行细节和调试信息都放在详情里。
                  </div>
                </div>
                {activeRuntimeSession?.runId && (
                  <div className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-xs text-slate-500">
                    当前 run: {activeRuntimeSession.runId}
                  </div>
                )}
              </div>
            </div>

            <div className="space-y-4 px-5 py-4">
              {latestPendingApproval && (
                <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
                  <div className="min-w-0">
                    <div className="font-medium">有待处理审批</div>
                    <div className="mt-1 truncate text-xs text-amber-800">
                      {latestPendingApproval.title || latestPendingApproval.command}
                    </div>
                  </div>
                  <button
                    type="button"
                    onClick={() => setApprovalDialog(latestPendingApproval)}
                    className="rounded-xl border border-amber-300 bg-white px-3 py-1.5 text-xs font-medium text-amber-900 hover:bg-amber-100"
                  >
                    打开审批
                  </button>
                </div>
              )}

              {!latestPendingApproval && latestPendingManualAction && isExecutableManualAction(latestPendingManualAction) && (
                <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
                  <div className="min-w-0">
                    <div className="font-medium">有待人工确认动作</div>
                    <div className="mt-1 truncate text-xs text-amber-800">
                      {latestPendingManualAction.title}
                    </div>
                  </div>
                  <button
                    type="button"
                    onClick={() => handleOpenManualActionDialog(latestPendingManualAction)}
                    className="rounded-xl border border-amber-300 bg-white px-3 py-1.5 text-xs font-medium text-amber-900 hover:bg-amber-100"
                  >
                    打开确认
                  </button>
                </div>
              )}

              <textarea
                value={question}
                onChange={(event) => setQuestion(event.target.value)}
                onKeyDown={handleQuestionKeyDown}
                rows={4}
                className="min-h-[112px] w-full resize-y rounded-3xl border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-slate-900 shadow-sm outline-none transition focus:border-cyan-400 focus:ring-2 focus:ring-cyan-100"
                placeholder="输入要让 AI 持续完成的任务，例如分析故障、继续执行排查命令、等待审批后继续恢复..."
              />

              <div className="flex flex-wrap items-center justify-between gap-3">
                <details className="group min-w-[280px] flex-1 rounded-2xl border border-slate-200 bg-slate-50 px-4 py-3">
                  <summary className="flex cursor-pointer list-none items-center gap-2 text-sm font-medium text-slate-700">
                    <Settings2 className="h-4 w-4 text-slate-500" />
                    高级参数
                  </summary>
                  <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                    <label className="block">
                      <div className="mb-1.5 text-xs font-medium text-slate-600">服务名</div>
                      <input
                        value={serviceName}
                        onChange={(event) => setServiceName(event.target.value)}
                        className="w-full rounded-2xl border border-slate-200 bg-white px-4 py-2.5 text-sm text-slate-900 shadow-sm outline-none transition focus:border-cyan-400 focus:ring-2 focus:ring-cyan-100"
                        placeholder="checkout-service"
                      />
                    </label>
                    <label className="block">
                      <div className="mb-1.5 text-xs font-medium text-slate-600">分析类型</div>
                      <select
                        value={analysisType}
                        onChange={(event) => setAnalysisType(event.target.value as 'log' | 'trace')}
                        className="w-full rounded-2xl border border-slate-200 bg-white px-4 py-2.5 text-sm text-slate-900 shadow-sm outline-none transition focus:border-cyan-400 focus:ring-2 focus:ring-cyan-100"
                      >
                        <option value="log">log</option>
                        <option value="trace">trace</option>
                      </select>
                    </label>
                    <label className="block">
                      <div className="mb-1.5 text-xs font-medium text-slate-600">Trace ID</div>
                      <input
                        value={traceId}
                        onChange={(event) => setTraceId(event.target.value)}
                        className="w-full rounded-2xl border border-slate-200 bg-white px-4 py-2.5 text-sm text-slate-900 shadow-sm outline-none transition focus:border-cyan-400 focus:ring-2 focus:ring-cyan-100"
                        placeholder="可选"
                      />
                    </label>
                    <label className="block">
                      <div className="mb-1.5 text-xs font-medium text-slate-600">Session ID</div>
                      <input
                        value={sessionId}
                        onChange={(event) => setSessionId(event.target.value)}
                        className="w-full rounded-2xl border border-slate-200 bg-white px-4 py-2.5 text-sm text-slate-900 shadow-sm outline-none transition focus:border-cyan-400 focus:ring-2 focus:ring-cyan-100"
                        placeholder="复用已有分析会话"
                      />
                    </label>
                    <label className="block md:col-span-2 xl:col-span-2">
                      <div className="mb-1.5 text-xs font-medium text-slate-600">Conversation ID</div>
                      <input
                        value={conversationId}
                        onChange={(event) => setConversationId(event.target.value)}
                        className="w-full rounded-2xl border border-slate-200 bg-white px-4 py-2.5 text-sm text-slate-900 shadow-sm outline-none transition focus:border-cyan-400 focus:ring-2 focus:ring-cyan-100"
                        placeholder="可选，用于连续对话恢复"
                      />
                    </label>
                  </div>
                  <div className="mt-3 rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 text-xs text-amber-900">
                    运行入口会复用主分析页的上下文语义：`log` 模式可直接基于当前问题和已有上下文启动；
                    `trace` 模式缺少 Trace ID 时会自动降级为 `log`，避免因为单个主键缺失而直接中断。
                  </div>
                  {downgradeNotice && (
                    <div className="mt-3 rounded-2xl border border-cyan-200 bg-cyan-50 px-4 py-3 text-xs text-cyan-900">
                      {downgradeNotice}
                    </div>
                  )}
                  {readonlyPolicyNotice && (
                    <div className="mt-3 rounded-2xl border border-slate-200 bg-slate-50 px-4 py-3 text-xs text-slate-700">
                      {readonlyPolicyNotice}
                    </div>
                  )}
                  <div className="mt-4 grid gap-2 rounded-2xl border border-slate-200 bg-white p-4 text-sm text-slate-700 md:grid-cols-3">
                    <label className="flex items-center gap-2">
                      <input
                        type="checkbox"
                        checked={useLLM}
                        onChange={(event) => setUseLLM(event.target.checked)}
                        className="h-4 w-4 rounded border-slate-300 text-cyan-600 focus:ring-cyan-500"
                      />
                      <span>启用 LLM runtime</span>
                    </label>
                    <label className="flex items-center gap-2">
                      <input
                        type="checkbox"
                        checked={showThought}
                        onChange={(event) => setShowThought(event.target.checked)}
                        className="h-4 w-4 rounded border-slate-300 text-cyan-600 focus:ring-cyan-500"
                      />
                      <span>输出思考步骤</span>
                    </label>
                    <label className="flex items-center gap-2">
                      <input
                        type="checkbox"
                        checked={autoExecReadonly}
                        onChange={(event) => setAutoExecReadonly(event.target.checked)}
                        className="h-4 w-4 rounded border-slate-300 text-cyan-600 focus:ring-cyan-500"
                      />
                      <span>自动执行只读命令</span>
                    </label>
                  </div>
                </details>

                <button
                  type="button"
                  onClick={() => void handleStartRun()}
                  disabled={submitting || approvalSubmitting}
                  className="inline-flex h-12 items-center gap-2 rounded-2xl bg-cyan-600 px-5 text-sm font-medium text-white shadow-sm transition hover:bg-cyan-700 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {(submitting || approvalSubmitting) ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
                  开始任务
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>

      {approvalDialog && (
        <div className="fixed inset-0 z-50 flex items-end justify-center p-4 sm:items-center">
          <button
            type="button"
            aria-label="关闭审批弹窗"
            onClick={handleCloseApprovalDialog}
            className="absolute inset-0 bg-black/40"
          />
          <div className="relative z-10 flex max-h-[calc(100vh-2rem)] w-full max-w-2xl flex-col overflow-hidden rounded-3xl border border-amber-200 bg-white shadow-xl">
            <div className="flex items-start justify-between gap-3 border-b border-amber-100 px-5 py-4">
              <div>
                <div className="text-xs text-amber-700">人工审批执行</div>
                <h3 className="text-sm font-semibold text-slate-900">
                  {approvalDialog.title || '确认命令执行'}
                </h3>
              </div>
              <button
                type="button"
                onClick={handleCloseApprovalDialog}
                disabled={approvalSubmitting}
                className="inline-flex h-8 w-8 items-center justify-center rounded-xl text-slate-500 transition hover:bg-slate-100 disabled:opacity-50"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="space-y-3 overflow-y-auto px-5 py-4 text-sm">
              <div className="flex flex-wrap gap-2 text-[11px]">
                {approvalDialog.commandType && (
                  <span className="inline-flex items-center rounded-full border border-slate-200 bg-slate-100 px-2 py-0.5 text-slate-700">
                    类型: {approvalDialog.commandType}
                  </span>
                )}
                {approvalDialog.riskLevel && (
                  <span className={`inline-flex items-center rounded-full border px-2 py-0.5 ${
                    String(approvalDialog.riskLevel).toLowerCase() === 'high'
                      ? 'border-rose-200 bg-rose-50 text-rose-700'
                      : 'border-emerald-200 bg-emerald-50 text-emerald-700'
                  }`}>
                    风险: {approvalDialog.riskLevel}
                  </span>
                )}
                {approvalDialog.executorProfile && (
                  <span className="inline-flex items-center rounded-full border border-slate-200 bg-slate-100 px-2 py-0.5 text-slate-700">
                    执行器: {approvalDialog.executorProfile}
                  </span>
                )}
                {approvalDialog.effectiveExecutorProfile && approvalDialog.effectiveExecutorProfile !== approvalDialog.executorProfile && (
                  <span className="inline-flex items-center rounded-full border border-slate-200 bg-slate-100 px-2 py-0.5 text-slate-700">
                    实际: {approvalDialog.effectiveExecutorProfile}
                  </span>
                )}
                {approvalDialog.dispatchBackend && (
                  <span className="inline-flex items-center rounded-full border border-slate-200 bg-slate-100 px-2 py-0.5 text-slate-700">
                    后端: {approvalDialog.dispatchBackend}
                  </span>
                )}
                {approvalDialog.targetIdentity && (
                  <span className="inline-flex items-center rounded-full border border-slate-200 bg-slate-100 px-2 py-0.5 text-slate-700">
                    目标: {approvalDialog.targetIdentity}
                  </span>
                )}
                {approvalDialog.requiresElevation && (
                  <span className="inline-flex items-center rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-amber-800">
                    需要提权
                  </span>
                )}
                {approvalDialog.requiresConfirmation && !approvalDialog.requiresElevation && (
                  <span className="inline-flex items-center rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-amber-800">
                    需要确认
                  </span>
                )}
              </div>
              <div className="rounded-2xl border border-slate-200 bg-slate-50 p-3 text-[12px] text-slate-800 break-all">
                <code>{approvalDialog.command}</code>
              </div>
              {approvalDialog.message && (
                <div className="rounded-2xl border border-amber-200 bg-amber-50 p-3 text-[12px] text-amber-800 whitespace-pre-wrap">
                  {approvalDialog.message}
                </div>
              )}
              {approvalDialog.dispatchReason && (
                <div className="rounded-2xl border border-slate-200 bg-slate-50 p-3 text-[12px] text-slate-600 whitespace-pre-wrap">
                  {approvalDialog.dispatchReason}
                </div>
              )}
              <div className="text-[11px] text-slate-500">
                审批动作会先做策略预检；若命令仍被策略拒绝，会把结果继续写回当前对话流。
              </div>
            </div>
            <div className="flex justify-end gap-2 border-t border-amber-100 px-5 py-4">
              <button
                type="button"
                onClick={() => {
                  void executeApprovalDecision('rejected');
                }}
                disabled={approvalSubmitting}
                className="rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm text-slate-700 transition hover:bg-slate-50 disabled:opacity-50"
              >
                拒绝
              </button>
              <button
                type="button"
                onClick={() => {
                  void executeApprovalDecision('approved');
                }}
                disabled={approvalSubmitting}
                className="inline-flex items-center gap-2 rounded-xl bg-amber-600 px-3 py-2 text-sm text-white transition hover:bg-amber-700 disabled:opacity-50"
              >
                {approvalSubmitting ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
                审批并执行
              </button>
            </div>
          </div>
        </div>
      )}

      {manualActionDialog && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/45 px-4">
          <div className="w-full max-w-2xl rounded-3xl border border-slate-200 bg-white shadow-2xl">
            <div className="border-b border-slate-200 px-6 py-5">
              <div className="flex items-start justify-between gap-4">
                <div>
                  <div className="text-lg font-semibold text-slate-900">
                    人工确认关键信息后执行
                  </div>
                  <div className="mt-1 text-sm text-slate-500">
                    这类动作未进入系统审批流，需要你先确认当前排查范围或目标，再决定是否执行。
                  </div>
                </div>
                <button
                  type="button"
                  onClick={handleCloseManualActionDialog}
                  disabled={approvalSubmitting}
                  className="rounded-xl border border-slate-200 p-2 text-slate-500 transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
            </div>

            <div className="space-y-4 px-6 py-5">
              <div className="flex flex-wrap gap-2 text-xs text-slate-600">
                {manualActionDialog.commandType && (
                  <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1">
                    类型: {manualActionDialog.commandType}
                  </span>
                )}
                {manualActionDialog.riskLevel && (
                  <span className={`rounded-full border px-2.5 py-1 ${
                    String(manualActionDialog.riskLevel).toLowerCase() === 'high'
                      ? 'border-rose-200 bg-rose-50 text-rose-700'
                      : 'border-slate-200 bg-slate-50 text-slate-600'
                  }`}>
                    风险: {manualActionDialog.riskLevel}
                  </span>
                )}
                {manualActionDialog.requiresElevation && (
                  <span className="rounded-full border border-amber-200 bg-amber-50 px-2.5 py-1 text-amber-800">
                    可能需要提权
                  </span>
                )}
              </div>

              {manualActionDialog.command.trim() ? (
                <pre className="max-h-[280px] overflow-auto whitespace-pre-wrap rounded-2xl bg-slate-950 p-4 text-[13px] leading-6 text-emerald-100">
                  <code>{manualActionDialog.command}</code>
                </pre>
              ) : (
                <div className="rounded-2xl border border-dashed border-amber-300 bg-amber-50 p-4 text-sm leading-6 text-amber-900">
                  当前动作未附带可执行命令。可以先确认当前步骤是否合理，再让 AI 继续补足执行动作。
                </div>
              )}
              {manualActionDialog.message && (
                <div className="whitespace-pre-wrap rounded-2xl border border-amber-200 bg-amber-50 p-4 text-sm leading-6 text-amber-900">
                  {manualActionDialog.message}
                </div>
              )}
            </div>

            <div className="flex flex-wrap items-center justify-end gap-3 border-t border-slate-200 px-6 py-5">
              <button
                type="button"
                onClick={handleCloseManualActionDialog}
                disabled={approvalSubmitting}
                className="rounded-xl border border-slate-200 px-4 py-2 text-sm font-medium text-slate-700 transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-60"
              >
                关闭
              </button>
              {manualActionDialog.command.trim() && (
                <button
                  type="button"
                  onClick={() => {
                    void executeManualAction();
                  }}
                  disabled={approvalSubmitting}
                  className="inline-flex items-center gap-2 rounded-xl bg-amber-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-amber-700 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {approvalSubmitting ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
                  确认并执行
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default AIRuntimePlayground;
