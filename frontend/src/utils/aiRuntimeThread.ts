import type { AgentRunSnapshot } from './aiAgentRuntime';

export interface ThreadIdentity {
  sessionId: string;
  conversationId: string;
}

export interface AIHistoryMessageLike {
  message_id?: string;
  role: 'user' | 'assistant' | string;
  content: string;
  timestamp?: string;
}

export interface AIHistoryTurnSnapshot {
  turnId: string;
  question: string;
  answer: string;
  userMessageId?: string;
  assistantMessageId?: string;
  timestamp?: string;
}

export interface RuntimeTurnLike {
  turnId: string;
  sessionId?: string;
  conversationId?: string;
}

type UnknownObject = Record<string, unknown>;

const asObject = (value: unknown): UnknownObject => (
  value && typeof value === 'object' ? value as UnknownObject : {}
);

const asText = (value: unknown): string => (
  typeof value === 'string' ? value : value === null || value === undefined ? '' : String(value)
);

export const normalizeThreadIdentity = (params?: {
  sessionId?: string;
  conversationId?: string;
}): ThreadIdentity => ({
  sessionId: asText(params?.sessionId).trim(),
  conversationId: asText(params?.conversationId).trim(),
});

export const isSameThreadIdentity = (left: ThreadIdentity, right: ThreadIdentity): boolean => (
  left.sessionId === right.sessionId && left.conversationId === right.conversationId
);

export const getConversationIdFromRun = (run: AgentRunSnapshot): string => {
  const directConversationId = asText(run.conversation_id).trim();
  if (directConversationId) {
    return directConversationId;
  }
  const summaryJson = asObject(run.summary_json);
  const runtimeOptions = asObject(summaryJson.runtime_options);
  return asText(runtimeOptions.conversation_id).trim();
};

export const buildHistoryTurns = (params: {
  messages: AIHistoryMessageLike[];
  preferredConversationId?: string;
  detailConversationId?: string;
}): AIHistoryTurnSnapshot[] => {
  const preferredConversationId = asText(params.preferredConversationId).trim();
  const detailConversationId = asText(params.detailConversationId).trim();
  if (preferredConversationId && detailConversationId && preferredConversationId !== detailConversationId) {
    return [];
  }

  const turns: AIHistoryTurnSnapshot[] = [];
  let pendingUser: { message_id?: string; content: string; timestamp?: string } | null = null;

  params.messages.forEach((message, index) => {
    if (!message || typeof message !== 'object') {
      return;
    }
    const role = asText(message.role).trim().toLowerCase();
    const content = asText(message.content).trim();
    if (!content) {
      return;
    }
    if (role === 'user') {
      pendingUser = {
        message_id: asText(message.message_id).trim() || undefined,
        content,
        timestamp: asText(message.timestamp).trim() || undefined,
      };
      return;
    }
    if (role !== 'assistant') {
      return;
    }
    const assistantMessageId = asText(message.message_id).trim() || undefined;
    const turnId = assistantMessageId || pendingUser?.message_id || `history-turn-${index}`;
    turns.push({
      turnId,
      question: pendingUser?.content || '',
      answer: content,
      userMessageId: pendingUser?.message_id,
      assistantMessageId,
      timestamp: asText(message.timestamp || pendingUser?.timestamp).trim() || undefined,
    });
    pendingUser = null;
  });

  return turns;
};

export const filterRuntimeTurnsByThread = <TRuntimeTurn extends RuntimeTurnLike>(
  runtimeTurns: TRuntimeTurn[],
  identity: ThreadIdentity,
): TRuntimeTurn[] => (
  runtimeTurns.filter((turn) => (
    isSameThreadIdentity(
      normalizeThreadIdentity({
        sessionId: turn.sessionId,
        conversationId: turn.conversationId,
      }),
      identity,
    )
  ))
);

export const mergeHistoryTurnsWithRuntimeTurns = <
  THistoryTurn extends { turnId: string },
  TRuntimeTurn extends { turnId: string },
>(
  historyTurns: THistoryTurn[],
  runtimeTurns: TRuntimeTurn[],
): Array<THistoryTurn | TRuntimeTurn> => {
  const runtimeTurnIds = new Set(runtimeTurns.map((turn) => turn.turnId));
  const nextHistoryTurns = historyTurns.filter((turn) => !runtimeTurnIds.has(turn.turnId));
  return [...nextHistoryTurns, ...runtimeTurns];
};

export const shouldHydrateHistoryForThreadSwitch = (params: {
  currentIdentity: ThreadIdentity;
  nextIdentity: ThreadIdentity;
  currentTurnCount: number;
  nextSessionId?: string;
}): boolean => {
  const nextSessionId = asText(params.nextSessionId).trim();
  if (!nextSessionId) {
    return false;
  }
  if (params.currentTurnCount <= 0) {
    return true;
  }
  return !isSameThreadIdentity(params.currentIdentity, params.nextIdentity);
};
