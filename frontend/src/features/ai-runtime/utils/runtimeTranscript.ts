import {
  selectAssistantMessage,
  selectPendingApprovals,
  type AgentRunState,
} from '../../../utils/aiAgentRuntimeReducer.js';
import type {
  RuntimeApprovalEntry,
  RuntimeCommandSpecRecovery,
  RuntimeManualActionEntry,
  RuntimeTranscriptBlock,
  RuntimeTranscriptCommandBlock,
  RuntimeTranscriptMessage,
  RuntimeTranscriptSkillBlock,
  RuntimeTranscriptTemplateHintBlock,
  RuntimeTranscriptUserInputBlock,
} from '../types/view.js';

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
  const normalized = asText(value).trim();
  return normalized || undefined;
};

const asOptionalNumber = (value: unknown): number | undefined => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
};

const asOptionalObject = (value: unknown): UnknownObject | undefined => (
  value && typeof value === 'object' ? value as UnknownObject : undefined
);

const normalizeCommandSpecRecovery = (value: unknown): RuntimeCommandSpecRecovery | undefined => {
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

const normalizeTargetContext = (payload: UnknownObject): UnknownObject | undefined => {
  const explicit = asOptionalObject(payload.resolved_target_context);
  if (explicit) {
    return explicit;
  }
  const targetClusterId = asOptionalText(payload.target_cluster_id);
  const targetNamespace = asOptionalText(payload.target_namespace);
  const targetNodeName = asOptionalText(payload.target_node_name);
  if (!targetClusterId && !targetNamespace && !targetNodeName) {
    return undefined;
  }
  return {
    execution_scope: {
      cluster_id: targetClusterId,
      namespace: targetNamespace,
      node_name: targetNodeName,
    },
  };
};

const normalizeQuestionKey = (payload: UnknownObject): string => {
  const questionKind = asText(payload.question_kind).trim().toLowerCase();
  const title = asText(payload.title).trim().toLowerCase();
  const prompt = asText(payload.prompt).trim().toLowerCase();
  const reason = asText(payload.reason || payload.message).trim().toLowerCase();
  return [questionKind, title, prompt, reason].filter(Boolean).join('|');
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

const buildWaitingUserInputFallback = (payload: UnknownObject): {
  title: string;
  prompt: string;
  reason?: string;
} => {
  const prompt = asText(payload.prompt).trim();
  const title = asText(payload.title).trim();
  const reason = asOptionalText(payload.reason || payload.message);
  if (title || prompt) {
    return {
      title: title || '还需要你确认一个关键信息',
      prompt: prompt || '我还缺少一个关键信息后继续排查。',
      reason,
    };
  }
  if (isBusinessQuestionPayload(payload)) {
    return {
      title: '还需要你确认一个关键信息',
      prompt: '我还需要一个关键信息后继续排查，请直接说明你希望我先确认什么。',
      reason: reason || '当前动作还缺少稳定的排查范围或目标。',
    };
  }
  return {
    title: '还需要你确认一个关键信息',
    prompt: '我还需要一个关键信息后继续排查，请直接说明你希望我先确认什么。',
    reason,
  };
};

const normalizePhase = (phase: unknown): string => {
  const normalized = asText(phase).trim().toLowerCase();
  if (!normalized) {
    return 'system';
  }
  if (normalized.includes('thought') || normalized.includes('reason')) {
    return 'reasoning';
  }
  if (normalized.includes('tool') || normalized.includes('command')) {
    return 'action';
  }
  if (normalized.includes('observe')) {
    return 'observation';
  }
  return normalized;
};

const buildDefaultAnswer = (params: {
  status: string;
  phaseText: string;
  hasPendingApprovals: boolean;
}): string => {
  if (params.hasPendingApprovals) {
    return '等待审批后继续执行。';
  }

  const normalizedStatus = asText(params.status).trim().toLowerCase();
  if (normalizedStatus === 'waiting_user_input') {
    return '我还需要一个关键信息后继续排查。';
  }
  if (normalizedStatus === 'blocked') {
    return '命令审批被拒绝，当前运行已阻塞。';
  }
  if (normalizedStatus === 'failed') {
    return '执行失败，可展开执行详情查看命令和错误输出。';
  }
  if (normalizedStatus === 'cancelled') {
    return '运行已取消。';
  }
  if (normalizedStatus === 'completed') {
    return '运行已完成。';
  }
  if (params.phaseText) {
    return `正在继续处理。\n当前阶段: ${params.phaseText}`;
  }
  return '正在继续处理。';
};

const buildActionPurposeLookup = (assistantMetadata: UnknownObject): {
  byActionId: Map<string, string>;
  byCommand: Map<string, string>;
} => {
  const byActionId = new Map<string, string>();
  const byCommand = new Map<string, string>();
  const actions = Array.isArray(assistantMetadata.actions) ? assistantMetadata.actions : [];
  actions.forEach((item) => {
    const entry = asObject(item);
    const actionId = asText(entry.id).trim();
    const command = asText(entry.command).trim();
    const purpose = asText(entry.purpose || entry.expected_outcome).trim();
    if (purpose) {
      if (actionId) {
        byActionId.set(actionId, purpose);
      }
      if (command) {
        byCommand.set(command, purpose);
      }
    }
  });
  return { byActionId, byCommand };
};

const resolveActionPurpose = (
  lookup: {
    byActionId: Map<string, string>;
    byCommand: Map<string, string>;
  },
  actionId: string,
  command: string,
): string | undefined => (
  lookup.byActionId.get(actionId)
  || lookup.byCommand.get(command)
  || undefined
);

const buildStatusSummary = (status: string): string => {
  const normalized = asText(status).trim().toLowerCase();
  if (normalized === 'waiting_approval') {
    return '等待人工审批后继续执行。';
  }
  if (normalized === 'waiting_user_input') {
    return '等待用户补充关键信息后继续排查。';
  }
  if (normalized === 'blocked') {
    return '运行被阻断，已停止继续执行。';
  }
  if (normalized === 'failed') {
    return '运行失败。';
  }
  if (normalized === 'cancelled') {
    return '运行已取消。';
  }
  if (normalized === 'completed') {
    return '运行已完成。';
  }
  return '运行状态更新。';
};

const PLANNING_NOISE_TITLES = new Set([
  '初始化运行上下文',
  '准备分析会话',
  '加载上下文历史',
  '检索长期记忆',
  '加载执行记忆',
]);

const normalizePlanningPhaseForDedupe = (phase: string): string => (
  phase === 'plan' ? 'planning' : phase
);

const buildCommandSummary = (params: {
  status: string;
  exitCode?: number;
  timedOut: boolean;
  message?: string;
  nextSuggestion?: string;
}): string | undefined => {
  const explicitMessage = asOptionalText(params.message);
  const suggestion = asOptionalText(params.nextSuggestion);
  if (params.timedOut) {
    const timeoutSuggestion = suggestion || '建议先缩小查询范围（时间窗口/limit），再提高 timeout 重试。';
    return `命令超时终止（timed_out/exit -9）。${timeoutSuggestion}`;
  }
  if (typeof params.exitCode === 'number' && params.exitCode !== 0) {
    return explicitMessage || `命令执行失败（exit ${params.exitCode}）。`;
  }
  if (asText(params.status).trim().toLowerCase() === 'completed') {
    return explicitMessage || '命令执行完成。';
  }
  return explicitMessage || suggestion;
};

const isPendingManualStatus = (status: unknown): boolean => {
  const normalized = asText(status).trim().toLowerCase();
  if (!normalized) {
    return true;
  }
  return ![
    'completed',
    'executed',
    'succeeded',
    'success',
    'failed',
    'cancelled',
    'rejected',
    'resolved',
    'skipped',
  ].includes(normalized);
};

const buildManualActionEntry = (params: {
  action: UnknownObject;
  fallbackId: string;
  runId: string;
  timestamp?: string;
}): RuntimeManualActionEntry => {
  const action = params.action;
  const actionId = asText(action.id).trim();
  const command = asText(action.command).trim();
  const title = asText(action.title).trim()
    || command
    || '人工确认动作';
  const reason = asOptionalText(action.reason || action.message);
  const purpose = asOptionalText(action.purpose || action.expected_outcome);
  const commandType = asOptionalText(action.command_type);
  const riskLevel = asOptionalText(action.risk_level);
  const commandSpec = asOptionalObject(action.command_spec) || asOptionalObject(action.commandSpec);
  const requiresElevation = Boolean(action.requires_elevation);
  const requiresConfirmation = Boolean(action.requires_confirmation) || requiresElevation;
  const message = reason || (!command ? '当前动作还缺少一个关键信息，请先确认排查范围或目标后继续执行。' : undefined);

  return {
    id: actionId || params.fallbackId,
    runtimeRunId: params.runId,
    title,
    command,
    commandSpec,
    purpose,
    message,
    status: 'pending',
    commandType,
    riskLevel,
    actionId: actionId || undefined,
    requiresConfirmation,
    requiresElevation,
    updatedAt: params.timestamp,
  };
};

const buildTemplateHintBlock = (params: {
  item: UnknownObject;
  fallbackId: string;
  runId: string;
  timestamp?: string;
}): RuntimeTranscriptTemplateHintBlock | null => {
  const item = params.item;
  const reason = asOptionalText(item.reason);
  const suggestedCommand = asOptionalText(item.suggested_command || item.suggestedCommand);
  const suggestedCommandSpec = (
    asOptionalObject(item.suggested_command_spec)
    || asOptionalObject(item.suggestedCommandSpec)
  );
  if (!suggestedCommand && !suggestedCommandSpec) {
    return null;
  }
  const summary = asOptionalText(item.summary || item.message || item.detail);
  const fixHint = asOptionalText(item.fix_hint || item.fixHint);
  const title = asOptionalText(item.title)
    || (reason === 'command_template_suggested' ? '建议补全命令模板' : '建议补全后继续执行');
  const actionId = asOptionalText(item.action_id || item.actionId);
  return {
    id: asOptionalText(item.id) || params.fallbackId,
    type: 'template_hint',
    runId: params.runId,
    actionId,
    title,
    reason,
    summary,
    fixHint,
    suggestedCommand,
    suggestedCommandSpec: suggestedCommandSpec || undefined,
    timestamp: params.timestamp,
  };
};

export const buildRuntimeTranscriptMessage = (params: {
  runId: string;
  title: string;
  state: AgentRunState;
  suppressBoilerplatePlanning?: boolean;
}): RuntimeTranscriptMessage => {
  const assistantMessage = selectAssistantMessage(params.state);
  const assistantMetadata = asObject(assistantMessage?.metadata);
  const pendingApprovals = selectPendingApprovals(params.state);
  const purposeLookup = buildActionPurposeLookup(assistantMetadata);
  const runStatus = asText(params.state.runMeta?.status).trim().toLowerCase();
  const runSummary = asObject(params.state.runMeta?.summaryJson);
  const reactLoopFromSummary = asObject(runSummary.react_loop);
  const observeFromSummary = asObject(reactLoopFromSummary.observe);
  const allowManualActionBlocks = runStatus === 'waiting_user_input' || runStatus === 'blocked';
  const allowUserInputBlocks = runStatus === 'waiting_user_input' || runStatus === 'blocked';
  const allowTemplateHintBlocks = runStatus !== 'waiting_user_input';

  const stepTitleById = new Map<string, string>();
  const commandRunIdByToolCallId = new Map<string, string>();
  const emittedManualActionIds = new Set<string>();
  const emittedTemplateHintKeys = new Set<string>();
  const planningBlockIndexByKey = new Map<string, number>();
  const commandBlockIndexByKey = new Map<string, number>();
  const skillBlockIndexByStepId = new Map<string, number>();
  const emittedUserInputQuestionKeys = new Set<string>();
  const latestPendingUserInputRef: { current: RuntimeTranscriptUserInputBlock | null } = { current: null };
  let manualActionAppended = false;
  const blocks: RuntimeTranscriptBlock[] = [];

  const appendThinkingBlock = (payload: UnknownObject, options: {
    eventId: string;
    timestamp?: string;
    title: string;
    detail?: string;
    status: string;
    iteration?: number;
  }) => {
    const phase = normalizePhase(payload.phase);
    const isPlanningPhase = phase === 'plan' || phase === 'planning';
    const block: RuntimeTranscriptBlock = {
      id: `thinking-${options.eventId}`,
      type: 'thinking',
      title: options.title,
      phase,
      status: options.status,
      summary: options.detail ? options.detail.slice(0, 120) : undefined,
      detail: options.detail,
      timestamp: options.timestamp,
      iteration: options.iteration,
      collapsed: false,
    };
    if (!isPlanningPhase) {
      blocks.push(block);
      return;
    }
    if (params.suppressBoilerplatePlanning) {
      return;
    }
    const normalizedTitle = options.title.trim();
    const titleKey = PLANNING_NOISE_TITLES.has(normalizedTitle)
      ? normalizedTitle
      : normalizedTitle.toLowerCase();
    const phaseKey = normalizePlanningPhaseForDedupe(phase);
    const iterationKey = PLANNING_NOISE_TITLES.has(normalizedTitle)
      ? 'bootstrap'
      : String(Math.max(0, Number(options.iteration ?? 0)));
    const key = [phaseKey, iterationKey, titleKey].join('|');
    const existingIndex = planningBlockIndexByKey.get(key);
    if (typeof existingIndex === 'number' && existingIndex >= 0 && existingIndex < blocks.length) {
      blocks[existingIndex] = {
        ...blocks[existingIndex],
        ...(block as RuntimeTranscriptBlock),
      };
      return;
    }
    planningBlockIndexByKey.set(key, blocks.length);
    blocks.push(block);
  };

  const appendManualActionsFromMetadata = (
    metadataValue: unknown,
    timestamp?: string,
    sourcePrefix = 'manual',
  ) => {
    if (!allowManualActionBlocks) {
      return;
    }
    const metadata = asObject(metadataValue);
    const actions = Array.isArray(metadata.actions) ? metadata.actions : [];
    actions.forEach((item, index) => {
      if (manualActionAppended) {
        return;
      }
      const action = asObject(item);
      const actionType = asText(action.action_type).trim().toLowerCase();
      const executable = action.executable;
      const command = asText(action.command).trim();
      const commandType = asText(action.command_type).trim().toLowerCase();
      const shouldRenderManual = (
        actionType === 'manual'
        || (typeof executable === 'boolean' && !executable)
      );
      if (
        !shouldRenderManual
        || !command
        || commandType === 'unknown'
        || !isPendingManualStatus(action.status)
      ) {
        return;
      }
      const fallbackId = `${sourcePrefix}-${index + 1}`;
      const actionId = asText(action.id).trim();
      const dedupeKey = actionId || [
        asText(action.title).trim(),
        command,
        asText(action.purpose || action.expected_outcome).trim(),
        asText(action.reason || action.message).trim(),
      ].join('|');
      if (emittedManualActionIds.has(dedupeKey)) {
        return;
      }
      emittedManualActionIds.add(dedupeKey);
      blocks.push({
        id: `manual-${actionId || fallbackId}`,
        type: 'manual_action',
        action: buildManualActionEntry({
          action,
          fallbackId,
          runId: params.runId,
          timestamp,
        }),
      });
      manualActionAppended = true;
    });
  };

  const appendTemplateHintsFromMetadata = (
    metadataValue: unknown,
    timestamp?: string,
    sourcePrefix = 'template',
  ) => {
    if (!allowTemplateHintBlocks) {
      return;
    }
    const metadata = asObject(metadataValue);
    const reactLoop = asObject(metadata.react_loop);
    const replan = asObject(reactLoop.replan);
    const items = Array.isArray(replan.items) ? replan.items : [];
    items.forEach((rawItem, index) => {
      const item = asObject(rawItem);
      const candidate = buildTemplateHintBlock({
        item,
        fallbackId: `${sourcePrefix}-${index + 1}`,
        runId: params.runId,
        timestamp,
      });
      if (!candidate) {
        return;
      }
      const dedupeKey = [
        candidate.reason || '',
        candidate.suggestedCommand || '',
        JSON.stringify(candidate.suggestedCommandSpec || {}),
      ].join('|');
      if (emittedTemplateHintKeys.has(dedupeKey)) {
        return;
      }
      emittedTemplateHintKeys.add(dedupeKey);
      blocks.push(candidate);
    });
  };

  params.state.entities.events.forEach((event) => {
    const payload = asObject(event.payload);
    const eventType = asText(event.event_type).trim().toLowerCase();
    const eventId = asText(event.event_id) || `seq-${event.seq}`;
    const timestamp = asOptionalText(event.created_at);

    if (eventType === 'reasoning_step') {
      const stepId = asText(payload.step_id).trim();
      const title = asText(payload.title).trim() || asText(payload.phase).trim() || '思考步骤';
      if (stepId) {
        stepTitleById.set(stepId, title);
      }
      const status = asText(payload.status).trim().toLowerCase() || 'info';
      const detail = asOptionalText(payload.detail || payload.message || payload.summary);
      appendThinkingBlock(payload, {
        eventId,
        title,
        status,
        detail,
        timestamp,
        iteration: asOptionalNumber(payload.iteration),
      });
      return;
    }

    if (eventType === 'reasoning_summary_delta') {
      const stepId = asText(payload.step_id).trim();
      const detail = asOptionalText(payload.text);
      if (!detail) {
        return;
      }
      const title = stepTitleById.get(stepId) || asText(payload.title).trim() || '思考摘要';
      appendThinkingBlock(payload, {
        eventId,
        title,
        status: 'info',
        detail,
        timestamp,
        iteration: asOptionalNumber(payload.iteration),
      });
      return;
    }

    if (eventType === 'action_spec_validated') {
      const detail = asOptionalText(payload.message) || '结构化命令已通过校验。';
      appendThinkingBlock(payload, {
        eventId,
        title: asText(payload.title).trim() || '结构化命令校验通过',
        status: 'success',
        detail,
        timestamp,
        iteration: asOptionalNumber(payload.iteration),
      });
      return;
    }

    if (eventType === 'action_preflight_failed') {
      const detail = asOptionalText(payload.message) || '结构化命令预检失败。';
      appendThinkingBlock(payload, {
        eventId,
        title: asText(payload.title).trim() || '结构化命令预检失败',
        status: 'warning',
        detail,
        timestamp,
        iteration: asOptionalNumber(payload.iteration),
      });
      return;
    }

    if (eventType === 'action_execution_retrying') {
      const command = asText(payload.command).trim();
      const attempt = asOptionalNumber(payload.attempt);
      const maxAttempts = asOptionalNumber(payload.max_attempts);
      const retrySummary = [
        asOptionalText(payload.message),
        typeof attempt === 'number' && typeof maxAttempts === 'number'
          ? `第 ${attempt} / ${maxAttempts} 次执行超时，准备重试。`
          : undefined,
      ].filter(Boolean).join(' ');
      blocks.push({
        id: `command-retrying-${eventId}`,
        type: 'command',
        title: asText(payload.title).trim() || command || '命令重试中',
        command: command || asText(payload.title).trim() || '命令重试中',
        status: 'running',
        message: retrySummary || '命令执行重试中。',
        timestamp,
        collapsed: true,
      });
      return;
    }

    if (
      eventType === 'tool_call_started'
      || eventType === 'tool_call_progress'
      || eventType === 'tool_call_output_delta'
      || eventType === 'tool_call_finished'
      || eventType === 'tool_call_skipped_duplicate'
    ) {
      const toolCallId = asText(payload.tool_call_id).trim();
      const explicitCommandRunId = asText(payload.command_run_id).trim();
      const existingCommandRunId = toolCallId ? asText(commandRunIdByToolCallId.get(toolCallId) || '') : '';
      const commandRunId = explicitCommandRunId || existingCommandRunId || (toolCallId ? `tool-${toolCallId}` : '');
      if (toolCallId && commandRunId) {
        commandRunIdByToolCallId.set(toolCallId, commandRunId);
      }
      const commandKey = commandRunId || (toolCallId ? `tool-${toolCallId}` : `event-${event.seq}`);
      const command = asText(payload.command).trim();
      const title = asText(payload.title).trim() || command || '命令执行';
      const actionId = asText(payload.action_id).trim();
      const purpose = asOptionalText(payload.purpose) || resolveActionPurpose(purposeLookup, actionId, command);
      const baseStatus = asText(payload.status).trim().toLowerCase();
      const status = (
        eventType === 'tool_call_output_delta' || eventType === 'tool_call_progress'
          ? 'running'
          : (baseStatus || 'running')
      );
      const stream = asText(payload.stream || 'stdout').trim().toLowerCase();
      const deltaText = asText(payload.text);
      const stdout = eventType === 'tool_call_output_delta'
        ? (stream === 'stdout' ? deltaText : '')
        : asText(payload.stdout);
      const stderr = eventType === 'tool_call_output_delta'
        ? (stream === 'stderr' ? deltaText : '')
        : asText(payload.stderr);
      const exitCode = asOptionalNumber(payload.exit_code);
      const timedOut = Boolean(payload.timed_out) || exitCode === -9;
      const outputTruncated = Boolean(payload.output_truncated);
      const nextSuggestion = asOptionalText(payload.next_suggestion);
      let message = buildCommandSummary({
        status,
        exitCode,
        timedOut,
        message: asOptionalText(payload.message),
        nextSuggestion,
      });
      if (eventType === 'tool_call_output_delta' && !stdout && !stderr && !message) {
        return;
      }
      const existingIndex = commandBlockIndexByKey.get(commandKey);
      const existingBlock = (
        typeof existingIndex === 'number'
        && existingIndex >= 0
        && existingIndex < blocks.length
        && blocks[existingIndex].type === 'command'
      )
        ? blocks[existingIndex] as RuntimeTranscriptCommandBlock
        : null;
      let mergedStdout = stdout || undefined;
      let mergedStderr = stderr || undefined;
      if (existingBlock) {
        if (eventType === 'tool_call_output_delta') {
          if (stream === 'stdout' && deltaText) {
            mergedStdout = `${asText(existingBlock.stdout)}${deltaText}` || undefined;
          } else {
            mergedStdout = existingBlock.stdout;
          }
          if (stream === 'stderr' && deltaText) {
            mergedStderr = `${asText(existingBlock.stderr)}${deltaText}` || undefined;
          } else {
            mergedStderr = existingBlock.stderr;
          }
        } else if (eventType === 'tool_call_finished') {
          const incomingStdout = asText(payload.stdout);
          const incomingStderr = asText(payload.stderr);
          const existingStdout = asText(existingBlock.stdout);
          const existingStderr = asText(existingBlock.stderr);
          mergedStdout = incomingStdout
            ? (
              outputTruncated && existingStdout.length >= incomingStdout.length
                ? (existingBlock.stdout || undefined)
                : incomingStdout
            )
            : (existingBlock.stdout || undefined);
          mergedStderr = incomingStderr
            ? (
              outputTruncated && existingStderr.length >= incomingStderr.length
                ? (existingBlock.stderr || undefined)
                : incomingStderr
            )
            : (existingBlock.stderr || undefined);
        } else {
          mergedStdout = existingBlock.stdout;
          mergedStderr = existingBlock.stderr;
        }
        if (!message) {
          message = existingBlock.message;
        }
      }
      const nextBlock: RuntimeTranscriptCommandBlock = {
        id: existingBlock?.id || `command-${commandKey}`,
        type: 'command',
        title: title || existingBlock?.title || '命令执行',
        command: command || existingBlock?.command || title,
        purpose: purpose || existingBlock?.purpose,
        status,
        commandType: asOptionalText(payload.command_type),
        riskLevel: asOptionalText(payload.risk_level),
        commandFamily: asOptionalText(payload.command_family),
        approvalPolicy: asOptionalText(payload.approval_policy),
        executorType: asOptionalText(payload.executor_type),
        executorProfile: asOptionalText(payload.executor_profile),
        targetKind: asOptionalText(payload.target_kind),
        targetIdentity: asOptionalText(payload.target_identity),
        effectiveExecutorType: asOptionalText(payload.effective_executor_type),
        effectiveExecutorProfile: asOptionalText(payload.effective_executor_profile),
        dispatchBackend: asOptionalText(payload.dispatch_backend),
        dispatchMode: asOptionalText(payload.dispatch_mode),
        dispatchReason: asOptionalText(payload.dispatch_reason),
        targetClusterId: asOptionalText(payload.target_cluster_id),
        targetNamespace: asOptionalText(payload.target_namespace),
        targetNodeName: asOptionalText(payload.target_node_name),
        resolvedTargetContext: normalizeTargetContext(payload),
        message,
        stdout: mergedStdout,
        stderr: mergedStderr,
        exitCode,
        timedOut,
        timestamp,
        collapsed: true,
      };
      if (typeof existingIndex === 'number' && existingBlock) {
        blocks[existingIndex] = nextBlock;
      } else {
        commandBlockIndexByKey.set(commandKey, blocks.length);
        blocks.push(nextBlock);
      }
      return;
    }

    if (eventType === 'skill_matched') {
      const rawSkills = Array.isArray(payload.selected_skills) ? payload.selected_skills : [];
      const selectedSkills = rawSkills.map((s) => {
        const skill = asObject(s);
        return {
          name: asText(skill.name).trim(),
          displayName: asText(skill.display_name || skill.displayName).trim(),
          description: asText(skill.description).trim(),
          riskLevel: asText(skill.risk_level || skill.riskLevel).trim() || 'low',
        };
      }).filter((s) => Boolean(s.name));
      if (selectedSkills.length > 0) {
        blocks.push({
          id: `skill-matched-${eventId}`,
          type: 'skill_matched',
          selectedSkills,
          summary: asText(payload.summary).trim() || `已选择 ${selectedSkills.length} 个诊断技能`,
          timestamp,
        });
      }
      return;
    }

    if (
      eventType === 'skill_step_planned'
      || eventType === 'skill_step_executing'
      || eventType === 'skill_step_completed'
    ) {
      const skillName = asText(payload.skill_name || payload.skillName).trim();
      const stepId = asText(payload.step_id || payload.stepId).trim();
      const blockKey = stepId || `${skillName}-${eventId}`;
      const status = (
        eventType === 'skill_step_planned' ? 'planned'
          : eventType === 'skill_step_executing' ? 'running'
            : 'completed'
      );
      const existingIndex = skillBlockIndexByStepId.get(blockKey);
      const existingBlock = (
        typeof existingIndex === 'number'
        && existingIndex >= 0
        && existingIndex < blocks.length
        && blocks[existingIndex].type === 'skill_step'
      )
        ? blocks[existingIndex] as RuntimeTranscriptSkillBlock
        : null;

      const nextBlock: RuntimeTranscriptSkillBlock = {
        id: existingBlock?.id || `skill-step-${blockKey}`,
        type: 'skill_step',
        skillName,
        skillDisplayName: asText(payload.skill_display_name || payload.skillDisplayName).trim() || skillName,
        stepId: stepId || blockKey,
        stepTitle: asText(payload.title || payload.step_title || payload.stepTitle).trim() || skillName,
        stepPurpose: asOptionalText(payload.purpose || payload.step_purpose),
        status: existingBlock ? status : status,
        iteration: asOptionalNumber(payload.iteration),
        seq: asOptionalNumber(payload.seq),
        command: asOptionalText(payload.command) || existingBlock?.command,
        commandSpec: asOptionalObject(payload.command_spec || payload.commandSpec) || existingBlock?.commandSpec,
        stdout: asOptionalText(payload.stdout) || existingBlock?.stdout,
        evidence: Array.isArray(payload.evidence)
          ? payload.evidence.map((e: unknown) => asText(e).trim()).filter(Boolean)
          : existingBlock?.evidence,
        timestamp,
        collapsed: existingBlock?.collapsed ?? (status !== 'completed'),
      };

      if (typeof existingIndex === 'number' && existingBlock) {
        blocks[existingIndex] = nextBlock;
      } else {
        skillBlockIndexByStepId.set(blockKey, blocks.length);
        blocks.push(nextBlock);
      }
      return;
    }

    if (eventType === 'approval_required' || eventType === 'approval_resolved') {
      const approvalId = asText(payload.approval_id).trim();
      if (!approvalId) {
        return;
      }
      const actionId = asText(payload.action_id).trim();
      const command = asText(payload.command).trim();
      const entry: RuntimeApprovalEntry = {
        id: approvalId,
        runtimeRunId: params.runId,
        runtimeApprovalId: approvalId,
        title: asText(payload.title || payload.message || command).trim() || command,
        command,
        purpose: asOptionalText(payload.purpose) || resolveActionPurpose(purposeLookup, actionId, command),
        message: asOptionalText(payload.reason || payload.message || payload.comment),
        status: eventType === 'approval_required'
          ? 'pending'
          : (asText(payload.decision).trim().toLowerCase() || 'resolved'),
        commandType: asOptionalText(payload.command_type),
        riskLevel: asOptionalText(payload.risk_level),
        commandFamily: asOptionalText(payload.command_family),
        approvalPolicy: asOptionalText(payload.approval_policy),
        executorType: asOptionalText(payload.executor_type),
        executorProfile: asOptionalText(payload.executor_profile),
        targetKind: asOptionalText(payload.target_kind),
        targetIdentity: asOptionalText(payload.target_identity),
        effectiveExecutorType: asOptionalText(payload.effective_executor_type),
        effectiveExecutorProfile: asOptionalText(payload.effective_executor_profile),
        dispatchBackend: asOptionalText(payload.dispatch_backend),
        dispatchMode: asOptionalText(payload.dispatch_mode),
        dispatchReason: asOptionalText(payload.dispatch_reason),
        requiresConfirmation: Boolean(payload.requires_confirmation),
        requiresElevation: Boolean(payload.requires_elevation),
        messageId: params.state.runMeta?.assistantMessageId,
        actionId: actionId || undefined,
        confirmationTicket: asOptionalText(payload.confirmation_ticket) || approvalId,
        updatedAt: timestamp,
      };
      blocks.push({
        id: `approval-${eventId}`,
        type: 'approval',
        approval: entry,
      });
      return;
    }

    if (eventType === 'action_waiting_user_input') {
      if (!allowUserInputBlocks) {
        return;
      }
      const actionId = asText(payload.action_id).trim();
      const command = asText(payload.command).trim();
      const pendingInput = buildWaitingUserInputFallback(payload);
      const sourceContext = asObject(payload.source_context);
      const recovery = normalizeCommandSpecRecovery(sourceContext.recovery);
      const questionKey = normalizeQuestionKey(payload);
      if (questionKey && emittedUserInputQuestionKeys.has(questionKey)) {
        return;
      }
      if (questionKey) {
        emittedUserInputQuestionKeys.add(questionKey);
      }
      latestPendingUserInputRef.current = {
        id: `user-input-${eventId}`,
        type: 'user_input',
        runId: params.runId,
        actionId: actionId || undefined,
        kind: asOptionalText(payload.kind),
        questionKind: asOptionalText(payload.question_kind),
        title: pendingInput.title,
        prompt: pendingInput.prompt,
        reason: pendingInput.reason || recovery?.fixHint || recovery?.fixDetail,
        command: isBusinessQuestionPayload(payload) ? undefined : (command || undefined),
        purpose: asOptionalText(payload.purpose) || resolveActionPurpose(purposeLookup, actionId, command),
        status: runStatus === 'blocked' ? 'blocked' : 'pending',
        timestamp,
        recoveryAttempts: asOptionalNumber(payload.recovery_attempts),
        recovery,
      };
      return;
    }

    if (eventType === 'assistant_message_finalized') {
      appendManualActionsFromMetadata(payload.metadata, timestamp, eventId || `seq-${event.seq}`);
      return;
    }

    if (eventType === 'run_status_changed') {
      const status = asText(payload.status).trim().toLowerCase();
      if (!status || status === 'running') {
        return;
      }
      blocks.push({
        id: `status-${eventId}`,
        type: 'status',
        status,
        phase: asOptionalText(payload.current_phase),
        summary: buildStatusSummary(status),
        timestamp,
      });
      return;
    }

    if (eventType === 'run_failed' || eventType === 'run_cancelled') {
      const status = eventType === 'run_failed' ? 'failed' : 'cancelled';
      blocks.push({
        id: `status-${eventId}`,
        type: 'status',
        status,
        phase: asOptionalText(payload.current_phase),
        summary: buildStatusSummary(status),
        streamError: asOptionalText(payload.error || payload.detail),
        timestamp,
      });
    }
  });

  if (latestPendingUserInputRef.current) {
    blocks.push(latestPendingUserInputRef.current);
  }

  appendManualActionsFromMetadata(
    assistantMetadata,
    assistantMessage?.updatedAt || assistantMessage?.createdAt,
    'assistant',
  );
  appendTemplateHintsFromMetadata(
    assistantMetadata,
    assistantMessage?.updatedAt || assistantMessage?.createdAt,
    'assistant-template',
  );

  const diagnosisStatus = asOptionalText(runSummary.diagnosis_status);
  const faultSummary = asOptionalText(runSummary.fault_summary);
  const planCoverage = asOptionalNumber(runSummary.plan_coverage ?? observeFromSummary.plan_coverage ?? observeFromSummary.coverage);
  const execCoverage = asOptionalNumber(runSummary.exec_coverage ?? observeFromSummary.exec_coverage);
  const evidenceCoverage = asOptionalNumber(runSummary.evidence_coverage ?? observeFromSummary.evidence_coverage);
  const finalConfidence = asOptionalNumber(runSummary.final_confidence ?? observeFromSummary.final_confidence ?? observeFromSummary.confidence);
  const missingEvidenceSlots = Array.isArray(runSummary.missing_evidence_slots)
    ? runSummary.missing_evidence_slots
      .map((item) => asText(item).trim())
      .filter(Boolean)
    : [];
  const diagnosisSummaryLines: string[] = [];
  if (faultSummary) {
    diagnosisSummaryLines.push(`故障总结：${faultSummary}`);
  }
  if (diagnosisStatus) {
    diagnosisSummaryLines.push(`诊断状态：${diagnosisStatus}`);
  }
  const metricChunks: string[] = [];
  if (typeof planCoverage === 'number') {
    metricChunks.push(`plan=${planCoverage}`);
  }
  if (typeof execCoverage === 'number') {
    metricChunks.push(`exec=${execCoverage}`);
  }
  if (typeof evidenceCoverage === 'number') {
    metricChunks.push(`evidence=${evidenceCoverage}`);
  }
  if (metricChunks.length > 0) {
    diagnosisSummaryLines.push(`覆盖率：${metricChunks.join(', ')}`);
  }
  if (typeof finalConfidence === 'number') {
    diagnosisSummaryLines.push(`最终置信度：${finalConfidence}`);
  }
  if (missingEvidenceSlots.length > 0) {
    diagnosisSummaryLines.push(`待补证据槽位：${missingEvidenceSlots.slice(0, 4).join(', ')}`);
  }
  if (diagnosisSummaryLines.length > 0) {
    blocks.push({
      id: 'diagnosis-status',
      type: 'status',
      status: diagnosisStatus || runStatus || 'running',
      phase: asOptionalText(runSummary.current_phase) || params.state.runMeta?.currentPhase,
      summary: diagnosisSummaryLines.join('；'),
      timestamp: params.state.runMeta?.updatedAt || assistantMessage?.updatedAt || assistantMessage?.createdAt,
    });
  }

  const nextBestCommands = Array.isArray(runSummary.next_best_commands)
    ? runSummary.next_best_commands
      .map((item) => asObject(item))
      .filter((item) => asText(item.command).trim().length > 0)
    : [];
  nextBestCommands.slice(0, 2).forEach((entry, index) => {
    const command = asText(entry.command).trim();
    const why = asOptionalText(entry.why);
    const expectedSignal = asOptionalText(entry.expected_signal);
    const composedMessage = [why, expectedSignal ? `期望信号：${expectedSignal}` : undefined]
      .filter(Boolean)
      .join('\n');
    blocks.push({
      id: `recommended-command-${index + 1}`,
      type: 'command',
      title: asOptionalText(entry.title) || `建议补证据命令 #${index + 1}`,
      command,
      status: 'pending',
      message: composedMessage || '用于补齐关键证据槽位的建议命令。',
      purpose: asOptionalText(entry.slot_id) || asOptionalText(entry.reason),
      timestamp: params.state.runMeta?.updatedAt || assistantMessage?.updatedAt || assistantMessage?.createdAt,
      collapsed: true,
    });
  });

  const answerContent = asText(assistantMessage?.content).trim();
  const normalizedStatus = asText(params.state.runMeta?.status || 'running') || 'running';
  const phaseText = asText(params.state.runMeta?.currentPhase).trim();
  const latestPendingUserInputBlock = latestPendingUserInputRef.current;
  const latestPendingUserInput = allowUserInputBlocks && latestPendingUserInputBlock
    ? {
      prompt: latestPendingUserInputBlock.prompt,
      reason: latestPendingUserInputBlock.reason,
    }
    : null;
  const defaultAnswer = buildDefaultAnswer({
    status: normalizedStatus,
    phaseText,
    hasPendingApprovals: pendingApprovals.length > 0,
  });

  if (params.state.streamError) {
    blocks.push({
      id: 'status-stream-error',
      type: 'status',
      status: normalizedStatus,
      phase: phaseText || undefined,
      summary: '运行流已中断，可刷新后继续订阅。',
      streamError: params.state.streamError,
      timestamp: params.state.runMeta?.updatedAt || params.state.runMeta?.createdAt,
    });
  }

  blocks.push({
    id: 'answer',
    type: 'answer',
    content: answerContent
      || asText(assistantMetadata.answer).trim()
      || latestPendingUserInput?.prompt
      || defaultAnswer,
    finalized: Boolean(assistantMessage?.finalized),
    streaming: params.state.streaming,
    timestamp: assistantMessage?.updatedAt || assistantMessage?.createdAt || params.state.runMeta?.updatedAt,
  });

  return {
    runId: params.runId,
    title: params.title,
    status: normalizedStatus,
    currentPhase: phaseText || undefined,
    updatedAt: params.state.runMeta?.updatedAt || assistantMessage?.updatedAt || assistantMessage?.createdAt,
    blocks,
  };
};

export default buildRuntimeTranscriptMessage;
