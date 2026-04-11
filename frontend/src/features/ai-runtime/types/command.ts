import type { AgentRunState } from '../../../utils/aiAgentRuntimeReducer';

export interface AgentRuntimeCommandClassification {
  command: string;
  commandType: 'query' | 'repair' | 'unknown' | string;
  riskLevel: 'low' | 'high' | string;
}

export interface AgentRuntimeCommandSession {
  runId: string;
  messageId: string;
  state: AgentRunState;
  sourceMessageId?: string;
  actionId?: string;
  command: string;
  commandSpec?: Record<string, unknown>;
  clientDeadlineMs?: number;
  purpose: string;
  commandType: AgentRuntimeCommandClassification['commandType'];
  riskLevel: AgentRuntimeCommandClassification['riskLevel'];
  title: string;
}
