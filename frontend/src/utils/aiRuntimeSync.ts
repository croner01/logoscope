import { api } from './api';
import {
  agentRunReducer,
  type AgentRunState,
} from './aiAgentRuntimeReducer';
import { isTerminalAgentRunStatus } from '../features/ai-runtime/utils/runtimeView';

const sleep = async (ms: number): Promise<void> => new Promise((resolve) => {
  window.setTimeout(resolve, ms);
});

interface ReconcileOptions {
  pollAttempts?: number;
  pollIntervalMs?: number;
  stopWhenWaitingApproval?: boolean;
}

interface ReconcileResult {
  state: AgentRunState;
  terminal: boolean;
  waitingApproval: boolean;
}

const isWaitingApprovalState = (state: AgentRunState): boolean => (
  String(state.runMeta?.status || '').trim().toLowerCase() === 'waiting_approval'
);

export const reconcileAIRunState = async (
  runId: string,
  state: AgentRunState,
  options?: ReconcileOptions,
): Promise<ReconcileResult> => {
  const normalizedRunId = String(runId || '').trim();
  if (!normalizedRunId) {
    return {
      state,
      terminal: false,
      waitingApproval: false,
    };
  }

  const pollAttempts = Number.isFinite(Number(options?.pollAttempts))
    ? Math.max(1, Math.floor(Number(options?.pollAttempts)))
    : 6;
  const pollIntervalMs = Number.isFinite(Number(options?.pollIntervalMs))
    ? Math.max(250, Math.floor(Number(options?.pollIntervalMs)))
    : 3000;
  const stopWhenWaitingApproval = options?.stopWhenWaitingApproval !== false;

  let nextState = state;

  for (let attempt = 0; attempt < pollAttempts; attempt += 1) {
    const snapshotResponse = await api.getAIRun(normalizedRunId);
    nextState = agentRunReducer(nextState, {
      type: 'hydrate_snapshot',
      payload: { run: snapshotResponse.run },
    });

    const eventsResponse = await api.getAIRunEvents(normalizedRunId, {
      afterSeq: nextState.lastSeq,
      limit: 5000,
    });
    if (eventsResponse.events.length > 0) {
      nextState = agentRunReducer(nextState, {
        type: 'hydrate_events',
        payload: { events: eventsResponse.events },
      });
    }

    const terminal = isTerminalAgentRunStatus(nextState.runMeta?.status);
    const waitingApproval = isWaitingApprovalState(nextState);
    if (terminal || (stopWhenWaitingApproval && waitingApproval)) {
      return {
        state: nextState,
        terminal,
        waitingApproval,
      };
    }

    if (attempt < pollAttempts - 1) {
      await sleep(pollIntervalMs);
    }
  }

  return {
    state: nextState,
    terminal: isTerminalAgentRunStatus(nextState.runMeta?.status),
    waitingApproval: isWaitingApprovalState(nextState),
  };
};
