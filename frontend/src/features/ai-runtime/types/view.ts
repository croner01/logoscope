export interface RuntimeTimelineEntry {
  id: string;
  phase: string;
  status: string;
  title: string;
  detail?: string;
  timestamp?: string;
  iteration?: number;
}

export interface RuntimeCommandEntry {
  id: string;
  title: string;
  command: string;
  purpose?: string;
  status: string;
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
  stdout?: string;
  stderr?: string;
  exitCode?: number;
  timedOut?: boolean;
  updatedAt?: string;
}

export interface RuntimeApprovalEntry {
  id: string;
  runtimeRunId: string;
  runtimeApprovalId: string;
  title: string;
  command: string;
  purpose?: string;
  message?: string;
  status: string;
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
  requiresConfirmation?: boolean;
  requiresElevation?: boolean;
  messageId?: string;
  actionId?: string;
  confirmationTicket?: string;
  updatedAt?: string;
}

export interface RuntimeManualActionEntry {
  id: string;
  runtimeRunId: string;
  title: string;
  command: string;
  commandSpec?: Record<string, unknown>;
  purpose?: string;
  message?: string;
  status: string;
  commandType?: string;
  riskLevel?: string;
  actionId?: string;
  requiresConfirmation?: boolean;
  requiresElevation?: boolean;
  updatedAt?: string;
}

export interface RuntimeUserInputEntry {
  id: string;
  runtimeRunId: string;
  actionId?: string;
  title: string;
  prompt?: string;
  reason?: string;
  command?: string;
  purpose?: string;
  status: string;
  updatedAt?: string;
  recovery?: RuntimeCommandSpecRecovery;
}

export interface RuntimePanelRunView {
  runId: string;
  title: string;
  status: string;
  currentPhase?: string;
  updatedAt?: string;
  streaming: boolean;
  streamError?: string;
  assistantMessage?: string;
  commandRuns: RuntimeCommandEntry[];
  approvals: RuntimeApprovalEntry[];
  userInput?: RuntimeUserInputEntry;
  timeline: RuntimeTimelineEntry[];
}

export interface RuntimeTranscriptStatusBlock {
  id: string;
  type: 'status';
  status: string;
  phase?: string;
  summary?: string;
  streamError?: string;
  timestamp?: string;
}

export interface RuntimeTranscriptThinkingBlock {
  id: string;
  type: 'thinking';
  title: string;
  phase?: string;
  status: string;
  summary?: string;
  detail?: string;
  timestamp?: string;
  iteration?: number;
  collapsed?: boolean;
}

export interface RuntimeTranscriptCommandBlock {
  id: string;
  type: 'command';
  title: string;
  command: string;
  purpose?: string;
  status: string;
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
  stdout?: string;
  stderr?: string;
  exitCode?: number;
  timedOut?: boolean;
  timestamp?: string;
  collapsed?: boolean;
}

export interface RuntimeTranscriptApprovalBlock {
  id: string;
  type: 'approval';
  approval: RuntimeApprovalEntry;
}

export interface RuntimeTranscriptManualActionBlock {
  id: string;
  type: 'manual_action';
  action: RuntimeManualActionEntry;
}

export interface RuntimeTranscriptUserInputBlock {
  id: string;
  type: 'user_input';
  runId: string;
  actionId?: string;
  kind?: string;
  questionKind?: string;
  title: string;
  prompt?: string;
  reason?: string;
  command?: string;
  purpose?: string;
  status: string;
  timestamp?: string;
  recoveryAttempts?: number;
  recovery?: RuntimeCommandSpecRecovery;
}

export interface RuntimeTranscriptTemplateHintBlock {
  id: string;
  type: 'template_hint';
  runId: string;
  actionId?: string;
  title: string;
  reason?: string;
  summary?: string;
  fixHint?: string;
  suggestedCommand?: string;
  suggestedCommandSpec?: Record<string, unknown>;
  timestamp?: string;
}

export interface RuntimeCommandSpecRecovery {
  fixCode?: string;
  fixHint?: string;
  fixDetail?: string;
  suggestedCommand?: string;
  suggestedCommandSpec?: Record<string, unknown>;
}

export interface RuntimeTranscriptAnswerBlock {
  id: string;
  type: 'answer';
  content: string;
  finalized: boolean;
  streaming: boolean;
  timestamp?: string;
}

export type RuntimeTranscriptBlock =
  | RuntimeTranscriptStatusBlock
  | RuntimeTranscriptThinkingBlock
  | RuntimeTranscriptCommandBlock
  | RuntimeTranscriptApprovalBlock
  | RuntimeTranscriptManualActionBlock
  | RuntimeTranscriptUserInputBlock
  | RuntimeTranscriptTemplateHintBlock
  | RuntimeTranscriptAnswerBlock;

export interface RuntimeTranscriptMessage {
  runId: string;
  title: string;
  status: string;
  currentPhase?: string;
  updatedAt?: string;
  blocks: RuntimeTranscriptBlock[];
}

export interface RuntimeTranscriptPresentation {
  conversation: RuntimeTranscriptMessage;
  detailBlocks: RuntimeTranscriptBlock[];
  hasDetails: boolean;
}
