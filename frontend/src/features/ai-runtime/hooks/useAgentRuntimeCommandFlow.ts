import { useCallback, type MutableRefObject } from 'react';

import { api } from '../../../utils/api';
import { normalizeAgentRunEventEnvelope, type AgentRunEventEnvelope } from '../../../utils/aiAgentRuntime';
import {
  buildRuntimeCommandSpec,
  buildRuntimePipelineSteps,
  resolveRuntimeClientDeadlineMs,
} from '../../../utils/commandSpec';
import { buildRuntimeAnalysisContext } from '../../../utils/runtimeAnalysisMode';
import {
  agentRunReducer,
  createInitialAgentRunState,
  selectCommandRuns,
  selectPendingApprovals,
} from '../../../utils/aiAgentRuntimeReducer';
import { reconcileAIRunState } from '../../../utils/aiRuntimeSync';
import type { AgentRuntimeCommandClassification, AgentRuntimeCommandSession } from '../types/command';

type UnknownObject = Record<string, unknown>;

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

const buildStructuredFailureMessage = (result: UnknownObject): string => {
  const errorPayload = asObject(result.error);
  const recoveryPayload = asObject(errorPayload.recovery || result.recovery);
  const recovery = parseCommandSpecRecovery(recoveryPayload);
  const detailParts = [recovery.fixHint, recovery.fixDetail].filter(Boolean);
  if (detailParts.length > 0) {
    return detailParts.join(' ');
  }
  return String(
    errorPayload.message
    || errorPayload.detail
    || result.message
    || '结构化命令校验失败',
  ).trim() || '结构化命令校验失败';
};

const getLocalErrorMessage = (error: unknown, fallback: string): string => {
  const response = asObject(asObject(error).response);
  const responseData = asObject(response.data);
  const responseDetail = responseData.detail;
  if (typeof responseDetail === 'string' && responseDetail.trim()) {
    return responseDetail.trim();
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

interface UseAgentRuntimeCommandFlowParams {
  sessionsRef: MutableRefObject<Record<string, AgentRuntimeCommandSession>>;
  controllersRef: MutableRefObject<Record<string, AbortController>>;
  analysisSessionId: string;
  analysisType: string;
  serviceName?: string;
  traceId?: string;
  runtimeEnabled: boolean;
  classifyCommand: (command: string) => AgentRuntimeCommandClassification;
  syncSessionMessage: (runId: string) => void;
  removeMessageById: (messageId: string) => void;
  isUnavailableError: (error: unknown) => boolean;
}

export const useAgentRuntimeCommandFlow = ({
  sessionsRef,
  controllersRef,
  analysisSessionId,
  analysisType,
  serviceName,
  traceId,
  runtimeEnabled,
  classifyCommand,
  syncSessionMessage,
  removeMessageById,
  isUnavailableError,
}: UseAgentRuntimeCommandFlowParams) => {
  const rebuildSessionState = useCallback(async (params: {
    runId: string;
    command?: string;
    purpose?: string;
    title?: string;
    sourceMessageId?: string;
    actionId?: string;
    preserveExisting?: boolean;
  }): Promise<AgentRuntimeCommandSession> => {
    const runId = String(params.runId || '').trim();
    if (!runId) {
      throw new Error('runtime run id is required');
    }
    const existing = params.preserveExisting ? sessionsRef.current[runId] : undefined;

    const snapshotResponse = await api.getAIRun(runId);
    let nextState = agentRunReducer(createInitialAgentRunState(), {
      type: 'hydrate_snapshot',
      payload: { run: snapshotResponse.run },
    });
    const eventsResponse = await api.getAIRunEvents(runId, { afterSeq: 0, limit: 5000 });
    if (eventsResponse.events.length > 0) {
      nextState = agentRunReducer(nextState, {
        type: 'hydrate_events',
        payload: { events: eventsResponse.events },
      });
    }

    const latestApproval = selectPendingApprovals(nextState).slice(-1)[0] || null;
    const latestCommandRun = selectCommandRuns(nextState).slice(-1)[0] || null;
    const derivedCommandInput = String(
      params.command
      || existing?.command
      || latestApproval?.command
      || latestCommandRun?.command
      || '',
    ).trim();
    const classified = derivedCommandInput
      ? classifyCommand(derivedCommandInput)
      : { command: '', commandType: 'unknown', riskLevel: 'high' };
    const derivedPurpose = String(
      params.purpose
      || existing?.purpose
      || params.title
      || snapshotResponse.run.question
      || derivedCommandInput,
    ).trim();
    const session: AgentRuntimeCommandSession = {
      runId,
      messageId: snapshotResponse.run.assistant_message_id || existing?.messageId || runId,
      state: nextState,
      sourceMessageId: params.sourceMessageId || existing?.sourceMessageId,
      actionId: params.actionId || existing?.actionId || latestApproval?.actionId || latestCommandRun?.actionId,
      command: classified.command || derivedCommandInput,
      commandSpec: existing?.commandSpec,
      clientDeadlineMs: existing?.clientDeadlineMs,
      purpose: derivedPurpose || derivedCommandInput,
      commandType: classified.commandType,
      riskLevel: classified.riskLevel,
      title: String(
        params.title
        || existing?.title
        || latestApproval?.title
        || latestCommandRun?.command
        || derivedCommandInput
        || '执行命令',
      ).trim() || '执行命令',
    };
    sessionsRef.current[runId] = session;
    syncSessionMessage(runId);
    return session;
  }, [classifyCommand, sessionsRef, syncSessionMessage]);

  const discardSession = useCallback((runId: string) => {
    const session = sessionsRef.current[runId];
    const controller = controllersRef.current[runId];
    if (controller) {
      controller.abort();
      delete controllersRef.current[runId];
    }
    delete sessionsRef.current[runId];
    if (session?.messageId) {
      removeMessageById(session.messageId);
    }
  }, [controllersRef, removeMessageById, sessionsRef]);

  const streamSession = useCallback(async (
    runId: string,
    options?: { stopOnApproval?: boolean },
  ) => {
    const session = sessionsRef.current[runId];
    if (!session) {
      return;
    }
    const existingController = controllersRef.current[runId];
    if (existingController) {
      existingController.abort();
      delete controllersRef.current[runId];
    }
    const controller = new AbortController();
    controllersRef.current[runId] = controller;
    session.state = agentRunReducer(session.state, {
      type: 'set_streaming',
      payload: { streaming: true },
    });
    syncSessionMessage(runId);

    try {
      await api.streamAIRun(runId, {
        afterSeq: session.state.lastSeq,
        signal: controller.signal,
        deadlineMs: session.clientDeadlineMs,
        onEvent: ({ data }) => {
          const envelope = normalizeAgentRunEventEnvelope(data);
          if (!envelope) {
            return;
          }
          const activeSession = sessionsRef.current[runId];
          if (!activeSession) {
            return;
          }
          activeSession.state = agentRunReducer(activeSession.state, {
            type: 'append_event',
            payload: { event: envelope as AgentRunEventEnvelope },
          });
          syncSessionMessage(runId);
          const eventType = String(envelope.event_type || '').trim().toLowerCase();
          const shouldStopOnApproval = options?.stopOnApproval !== false;
          if (eventType === 'run_status_changed') {
            const status = String(asObject(envelope.payload).status || '').trim().toLowerCase();
            if (
              status === 'completed'
              || status === 'failed'
              || status === 'cancelled'
              || status === 'blocked'
              || status === 'waiting_user_input'
              || (shouldStopOnApproval && status === 'waiting_approval')
            ) {
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
        session.state = agentRunReducer(session.state, {
          type: 'set_stream_error',
          payload: { error: getLocalErrorMessage(error, '命令流式执行失败') },
        });
      }
    } finally {
      const activeSession = sessionsRef.current[runId];
      if (activeSession) {
        const hasPendingApprovals = activeSession.state.entities.approvalOrder.some((approvalId) => (
          activeSession.state.entities.approvalsById[approvalId]?.status === 'pending'
        ));
        const waitingUserInput = String(activeSession.state.runMeta?.status || '').trim().toLowerCase() === 'waiting_user_input';
        if (
          String(activeSession.state.runMeta?.status || '').trim().toLowerCase() !== 'completed'
          && String(activeSession.state.runMeta?.status || '').trim().toLowerCase() !== 'failed'
          && String(activeSession.state.runMeta?.status || '').trim().toLowerCase() !== 'cancelled'
          && String(activeSession.state.runMeta?.status || '').trim().toLowerCase() !== 'blocked'
          && !hasPendingApprovals
          && !waitingUserInput
        ) {
          try {
            const reconciled = await reconcileAIRunState(runId, activeSession.state, {
              stopWhenWaitingApproval: true,
            });
            activeSession.state = reconciled.state;
          } catch (_error) {
            // Ignore reconcile failure and preserve the last streamed state.
          }
        }
        activeSession.state = agentRunReducer(activeSession.state, {
          type: 'set_streaming',
          payload: { streaming: false },
        });
        syncSessionMessage(runId);
      }
      if (controllersRef.current[runId] === controller) {
        delete controllersRef.current[runId];
      }
    }
  }, [controllersRef, sessionsRef, syncSessionMessage]);

  const createSession = useCallback(async (params: {
    question: string;
    command: string;
    commandSpec?: Record<string, unknown>;
    purpose?: string;
    sourceMessageId?: string;
    actionId?: string;
    title?: string;
  }): Promise<AgentRuntimeCommandSession> => {
    const classified = classifyCommand(params.command);
    const stepId = String(params.actionId || '').trim() || 'step-1';
    const resolvedPurpose = String(
      params.purpose
      || params.title
      || params.question
      || params.command,
    ).trim();
    const clientDeadlineMs = resolveRuntimeClientDeadlineMs(180000);
    const normalizedTraceId = String(traceId || '').trim();
    const normalizedServiceName = String(serviceName || '').trim();
    const normalizedAnalysisType = analysisType === 'trace' ? 'trace' : 'log';
    const commandSpec = (
      asOptionalObject(params.commandSpec)
      || buildRuntimeCommandSpec({
        command: classified.command || params.command,
        purpose: resolvedPurpose || params.command,
        title: params.title,
        stepId,
      })
    );
    const created = await api.createAIRun({
      session_id: analysisSessionId || undefined,
      question: params.question,
      analysis_context: buildRuntimeAnalysisContext({
        analysisType: normalizedAnalysisType,
        traceId: normalizedTraceId,
        serviceName: normalizedServiceName,
        baseContext: {
          source_message_id: params.sourceMessageId || undefined,
          source_command: params.command,
          agent_mode: 'followup_command_runtime',
        },
      }),
      runtime_options: {
        auto_exec_readonly: false,
      },
      client_deadline_ms: clientDeadlineMs,
      pipeline_steps: buildRuntimePipelineSteps({
        command: classified.command || params.command,
        purpose: resolvedPurpose || params.command,
        title: params.title,
        stepId,
      }),
    });
    const initialState = agentRunReducer(createInitialAgentRunState(), {
      type: 'hydrate_snapshot',
      payload: { run: created.run },
    });
    const session: AgentRuntimeCommandSession = {
      runId: created.run.run_id,
      messageId: created.run.assistant_message_id,
      state: agentRunReducer(initialState, {
        type: 'set_streaming',
        payload: { streaming: true },
      }),
      sourceMessageId: params.sourceMessageId,
      actionId: params.actionId,
      command: classified.command,
      commandSpec,
      clientDeadlineMs,
      purpose: resolvedPurpose || params.command,
      commandType: classified.commandType,
      riskLevel: classified.riskLevel,
      title: String(params.title || params.command).trim() || params.command,
    };
    sessionsRef.current[session.runId] = session;
    syncSessionMessage(session.runId);
    return session;
  }, [
    analysisSessionId,
    analysisType,
    classifyCommand,
    sessionsRef,
    serviceName,
    syncSessionMessage,
    traceId,
  ]);

  const ensureSession = useCallback(async (params: {
    runId: string;
    command?: string;
    purpose?: string;
    title?: string;
    sourceMessageId?: string;
    actionId?: string;
  }): Promise<AgentRuntimeCommandSession> => {
    const runId = String(params.runId || '').trim();
    if (!runId) {
      throw new Error('runtime run id is required');
    }
    const existing = sessionsRef.current[runId];
    if (existing) {
      return existing;
    }
    return rebuildSessionState(params);
  }, [rebuildSessionState, sessionsRef]);

  const refreshSession = useCallback(async (params: {
    runId: string;
    command?: string;
    purpose?: string;
    title?: string;
    sourceMessageId?: string;
    actionId?: string;
  }): Promise<AgentRuntimeCommandSession> => rebuildSessionState({
    ...params,
    preserveExisting: true,
  }), [rebuildSessionState]);

  const runCommandFlow = useCallback(async (params: {
    question: string;
    command: string;
    commandSpec?: Record<string, unknown>;
    purpose?: string;
    sourceMessageId?: string;
    actionId?: string;
    title?: string;
    autoApprove?: boolean;
    elevated?: boolean;
  }): Promise<boolean> => {
    if (!runtimeEnabled) {
      return false;
    }

    let session: AgentRuntimeCommandSession | null = null;
    try {
      session = await createSession(params);
      const activeSession = session;
      const stepId = String(params.actionId || '').trim() || 'step-1';
      const buildCommandRequest = (override?: {
        command?: string;
        commandSpec?: UnknownObject;
      }) => ({
        action_id: params.actionId,
        step_id: stepId,
        command: String(override?.command || activeSession.command || '').trim(),
        command_spec: (
          override?.commandSpec
          || (
            activeSession.commandSpec && typeof activeSession.commandSpec === 'object'
              ? activeSession.commandSpec
              : buildRuntimeCommandSpec({
                command: activeSession.command || '',
                purpose: activeSession.purpose || activeSession.title || activeSession.command || '',
                title: activeSession.title,
                stepId,
              })
          )
        ),
        purpose: activeSession.purpose || activeSession.title || activeSession.command,
        title: activeSession.title,
        confirmed: Boolean(params.autoApprove),
        elevated: Boolean(params.elevated),
        client_deadline_ms: activeSession.clientDeadlineMs || resolveRuntimeClientDeadlineMs(180000),
      });

      let commandResult = await api.executeAIRunCommand(activeSession.runId, buildCommandRequest());
      let status = String(commandResult.status || '').trim().toLowerCase();
      if (status === 'blocked' || status === 'waiting_user_input') {
        const recoveryPayload = asObject(asObject(commandResult.error).recovery || commandResult.recovery);
        const recovery = parseCommandSpecRecovery(recoveryPayload);
        if (recovery.suggestedCommandSpec) {
          if (recovery.suggestedCommand) {
            activeSession.command = recovery.suggestedCommand;
          }
          activeSession.commandSpec = recovery.suggestedCommandSpec;
          commandResult = await api.executeAIRunCommand(
            activeSession.runId,
            buildCommandRequest({
              command: recovery.suggestedCommand || activeSession.command,
              commandSpec: recovery.suggestedCommandSpec,
            }),
          );
          status = String(commandResult.status || '').trim().toLowerCase();
        }
      }
      if (status === 'blocked' || status === 'waiting_user_input') {
        throw new Error(buildStructuredFailureMessage(asObject(commandResult)));
      }
      const approvalPayload = asObject(commandResult.approval);
      const approvalId = String(approvalPayload.approval_id || '').trim();

      if ((status === 'elevation_required' || status === 'confirmation_required') && params.autoApprove && approvalId) {
        await api.approveAIRun(activeSession.runId, {
          approval_id: approvalId,
          decision: 'approved',
          confirmed: true,
          elevated: Boolean(params.elevated),
        });
        await streamSession(activeSession.runId, { stopOnApproval: false });
        return true;
      }

      await streamSession(activeSession.runId, {
        stopOnApproval: !(params.autoApprove && approvalId),
      });
      return true;
    } catch (error: unknown) {
      if (session && isUnavailableError(error)) {
        discardSession(session.runId);
        return false;
      }
      throw error;
    }
  }, [
    createSession,
    discardSession,
    isUnavailableError,
    runtimeEnabled,
    streamSession,
  ]);

  const resumeApprovalFlow = useCallback(async (params: {
    runId: string;
    approvalId: string;
    command?: string;
    title?: string;
    sourceMessageId?: string;
    actionId?: string;
    elevated?: boolean;
  }) => {
    const session = await ensureSession({
      runId: params.runId,
      command: params.command,
      purpose: params.title || params.command,
      title: params.title,
      sourceMessageId: params.sourceMessageId,
      actionId: params.actionId,
    });
    const refreshedSession = await refreshSession({
      runId: params.runId,
      command: params.command,
      purpose: params.title || params.command,
      title: params.title,
      sourceMessageId: params.sourceMessageId,
      actionId: params.actionId,
    });
    const latestPending = selectPendingApprovals(refreshedSession.state).slice(-1)[0] || null;
    let effectiveApprovalId = String(params.approvalId || '').trim();
    if (latestPending?.approvalId && latestPending.approvalId !== effectiveApprovalId) {
      effectiveApprovalId = latestPending.approvalId;
    }
    if (!effectiveApprovalId) {
      throw new Error('run is not waiting approval');
    }

    session.state = agentRunReducer(session.state, {
      type: 'set_streaming',
      payload: { streaming: true },
    });
    syncSessionMessage(params.runId);
    const approveRequest = async (approvalId: string) => {
      let response = await api.approveAIRun(params.runId, {
        approval_id: approvalId,
        decision: 'approved',
        confirmed: true,
        elevated: Boolean(params.elevated),
      });
      const commandStatus = String(asObject(response.command).status || '').trim().toLowerCase();
      const requiresRetry = commandStatus === 'confirmation_required' || commandStatus === 'elevation_required';
      const fallbackReason = String(
        asObject(asObject(response.command).approval).reason
        || asObject(asObject(response.command).error).message
        || asObject(asObject(response.command).error).detail
        || '',
      ).trim().toLowerCase();
      if (requiresRetry && fallbackReason.includes('confirmation ticket invalid')) {
        const retryApprovalId = String(
          asObject(asObject(response.command).approval).approval_id
          || asObject(asObject(response.command).approval).confirmation_ticket
          || '',
        ).trim();
        if (retryApprovalId) {
          response = await api.approveAIRun(params.runId, {
            approval_id: retryApprovalId,
            decision: 'approved',
            confirmed: true,
            elevated: Boolean(params.elevated),
          });
        }
      }
      return response;
    };

    try {
      const approveResponse = await approveRequest(effectiveApprovalId);
      session.state = agentRunReducer(session.state, {
        type: 'hydrate_snapshot',
        payload: { run: approveResponse.run },
      });
      syncSessionMessage(params.runId);
      const latestCommandStatus = String(asObject(approveResponse.command).status || '').trim().toLowerCase();
      await streamSession(params.runId, { stopOnApproval: latestCommandStatus !== 'running' });
    } catch (error: unknown) {
      const message = getLocalErrorMessage(error, '审批执行失败');
      if (message.includes('approval_id does not match pending approval')) {
        const latestSession = await refreshSession({
          runId: params.runId,
          command: params.command,
          purpose: params.title || params.command,
          title: params.title,
          sourceMessageId: params.sourceMessageId,
          actionId: params.actionId,
        });
        const latestApproval = selectPendingApprovals(latestSession.state).slice(-1)[0] || null;
        const retryApprovalId = String(latestApproval?.approvalId || '').trim();
        if (retryApprovalId && retryApprovalId !== effectiveApprovalId) {
          const retryResponse = await approveRequest(retryApprovalId);
          session.state = agentRunReducer(session.state, {
            type: 'hydrate_snapshot',
            payload: { run: retryResponse.run },
          });
          syncSessionMessage(params.runId);
          const retryCommandStatus = String(asObject(retryResponse.command).status || '').trim().toLowerCase();
          await streamSession(params.runId, { stopOnApproval: retryCommandStatus !== 'running' });
          return;
        }
      }
      await refreshSession({
        runId: params.runId,
        command: params.command,
        purpose: params.title || params.command,
        title: params.title,
        sourceMessageId: params.sourceMessageId,
        actionId: params.actionId,
      }).catch(() => undefined);
      throw error;
    }
  }, [ensureSession, refreshSession, streamSession, syncSessionMessage]);

  const cancelRun = useCallback(async (runId: string) => {
    const normalizedRunId = String(runId || '').trim();
    if (!normalizedRunId) {
      return;
    }
    const controller = controllersRef.current[normalizedRunId];
    if (controller) {
      controller.abort();
      delete controllersRef.current[normalizedRunId];
    }
    const session = await ensureSession({ runId: normalizedRunId });
    const cancelled = await api.cancelAIRun(normalizedRunId, { reason: 'user_cancelled' });
    session.state = agentRunReducer(session.state, {
      type: 'hydrate_snapshot',
      payload: { run: cancelled.run },
    });
    const eventsPayload = await api.getAIRunEvents(normalizedRunId, {
      afterSeq: session.state.lastSeq,
      limit: 200,
    });
    if (eventsPayload.events.length > 0) {
      session.state = agentRunReducer(session.state, {
        type: 'hydrate_events',
        payload: { events: eventsPayload.events },
      });
    }
    session.state = agentRunReducer(session.state, {
      type: 'set_streaming',
      payload: { streaming: false },
    });
    syncSessionMessage(normalizedRunId);
  }, [controllersRef, ensureSession, syncSessionMessage]);

  return {
    discardSession,
    streamSession,
    createSession,
    ensureSession,
    refreshSession,
    runCommandFlow,
    resumeApprovalFlow,
    cancelRun,
  };
};

export default useAgentRuntimeCommandFlow;
