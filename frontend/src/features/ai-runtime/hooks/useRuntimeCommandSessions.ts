import { useCallback, useEffect, useMemo, useRef, type MutableRefObject } from 'react';

import type { AgentRunState } from '../../../utils/aiAgentRuntimeReducer';
import {
  buildRuntimePanelRuns,
  collectRuntimeRunIdsFromMessages,
  isTerminalAgentRunStatus,
  type RuntimeMessageLike,
  type RuntimeSessionLike,
  type RuntimeThoughtLike,
} from '../utils/runtimeView';
import type { RuntimePanelRunView } from '../types/view';

interface UseRuntimeCommandSessionsParams<Session extends RuntimeSessionLike> {
  followUpMessages: RuntimeMessageLike[];
  sessionsRef: MutableRefObject<Record<string, Session>>;
  controllersRef: MutableRefObject<Record<string, AbortController>>;
  ensureSession: (params: { runId: string }) => Promise<Session>;
  streamSession: (runId: string, options?: { stopOnApproval?: boolean }) => Promise<void>;
  syncSessionMessage: (runId: string) => void;
  isUnavailableError: (error: unknown) => boolean;
  buildThoughtTimeline: (runtimeState: AgentRunState) => RuntimeThoughtLike[];
  formatTimestamp: (value?: string) => string;
}

interface UseRuntimeCommandSessionsResult {
  runtimePanelRuns: RuntimePanelRunView[];
  resetRuntimeSessions: () => void;
}

export const useRuntimeCommandSessions = <Session extends RuntimeSessionLike>({
  followUpMessages,
  sessionsRef,
  controllersRef,
  ensureSession,
  streamSession,
  syncSessionMessage,
  isUnavailableError,
  buildThoughtTimeline,
  formatTimestamp,
}: UseRuntimeCommandSessionsParams<Session>): UseRuntimeCommandSessionsResult => {
  const hydratedRuntimeRunIdsRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    const runtimeRunIds = collectRuntimeRunIdsFromMessages(followUpMessages);
    if (runtimeRunIds.length === 0) {
      return undefined;
    }

    let cancelled = false;
    runtimeRunIds.forEach((runId) => {
      const normalizedRunId = String(runId || '').trim();
      if (!normalizedRunId || hydratedRuntimeRunIdsRef.current.has(normalizedRunId)) {
        return;
      }
      hydratedRuntimeRunIdsRef.current.add(normalizedRunId);
      void (async () => {
        try {
          const session = await ensureSession({ runId: normalizedRunId });
          if (cancelled) {
            return;
          }
          if (
            !isTerminalAgentRunStatus(session.state.runMeta?.status)
            && !controllersRef.current[normalizedRunId]
          ) {
            void streamSession(normalizedRunId, { stopOnApproval: false });
          } else {
            syncSessionMessage(normalizedRunId);
          }
        } catch (error: unknown) {
          if (!cancelled && !isUnavailableError(error)) {
            console.warn(`Failed to hydrate runtime run ${normalizedRunId}:`, error);
          }
        }
      })();
    });

    return () => {
      cancelled = true;
    };
  }, [
    controllersRef,
    ensureSession,
    followUpMessages,
    isUnavailableError,
    streamSession,
    syncSessionMessage,
  ]);

  const runtimePanelRuns = useMemo(() => buildRuntimePanelRuns({
    sessions: sessionsRef.current,
    buildThoughtTimeline,
    formatTimestamp,
  }), [
    sessionsRef,
    buildThoughtTimeline,
    formatTimestamp,
  ]);

  const resetRuntimeSessions = useCallback(() => {
    Object.values(controllersRef.current).forEach((controller) => {
      try {
        controller.abort();
      } catch (_error) {
        // ignore abort cleanup failure
      }
    });
    controllersRef.current = {};
    sessionsRef.current = {};
    hydratedRuntimeRunIdsRef.current.clear();
  }, [controllersRef, sessionsRef]);

  return {
    runtimePanelRuns,
    resetRuntimeSessions,
  };
};

export default useRuntimeCommandSessions;
