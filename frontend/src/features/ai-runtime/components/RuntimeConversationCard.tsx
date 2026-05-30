import React, { useMemo, useState } from 'react';
import {
  AlertTriangle,
  Bot,
  Clock3,
  Loader2,
  Terminal,
  Zap,
} from 'lucide-react';

import type {
  RuntimeApprovalEntry,
  RuntimeManualActionEntry,
  RuntimeTranscriptAnswerBlock,
  RuntimeTranscriptApprovalBlock,
  RuntimeTranscriptBlock,
  RuntimeTranscriptCommandBlock,
  RuntimeTranscriptManualActionBlock,
  RuntimeTranscriptMessage,
  RuntimeTranscriptSkillBlock,
  RuntimeTranscriptSkillMatchedBlock,
  RuntimeTranscriptStatusBlock,
  RuntimeTranscriptTemplateHintBlock,
  RuntimeTranscriptThinkingBlock,
  RuntimeTranscriptUserInputBlock,
} from '../types/view';
import { buildRuntimePresentation } from '../utils/runtimePresentation';
import SkillBadge from './SkillBadge';

interface RuntimeConversationCardProps {
  message: RuntimeTranscriptMessage;
  disabled?: boolean;
  submittingUserInput?: boolean;
  onApprove?: (approval: RuntimeApprovalEntry) => void;
  onExecuteManualAction?: (action: RuntimeManualActionEntry) => void;
  onSubmitUserInput?: (params: {
    runId: string;
    actionId?: string;
    text: string;
    source?: string;
  }) => Promise<void> | void;
  onUseTemplateAsInput?: (params: {
    runId: string;
    actionId?: string;
    command: string;
    commandSpec?: Record<string, unknown>;
    source: 'recovery' | 'replan_template';
    reason?: string;
    fixHint?: string;
  }) => void;
  onCancelRun?: (runId: string) => void;
  detailContent?: React.ReactNode;
  debugContent?: React.ReactNode;
}

const formatRuntimeTime = (value?: string): string => {
  const normalized = String(value || '').trim();
  if (!normalized) {
    return '';
  }
  const parsed = new Date(normalized);
  if (Number.isNaN(parsed.getTime())) {
    return normalized;
  }
  return parsed.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
};

const getStatusClassName = (status: string): string => {
  const normalized = String(status || '').trim().toLowerCase();
  if (normalized === 'completed' || normalized === 'approved') {
    return 'border-emerald-200 bg-emerald-50 text-emerald-700';
  }
  if (
    normalized === 'pending'
    || normalized === 'running'
    || normalized === 'waiting_approval'
    || normalized === 'waiting_user_input'
  ) {
    return 'border-amber-200 bg-amber-50 text-amber-800';
  }
  if (normalized === 'blocked' || normalized === 'failed' || normalized === 'cancelled' || normalized === 'rejected') {
    return 'border-rose-200 bg-rose-50 text-rose-700';
  }
  return 'border-sky-200 bg-sky-50 text-sky-700';
};

const formatStatusLabel = (status: string): string => {
  const normalized = String(status || '').trim().toLowerCase();
  if (normalized === 'waiting_approval') {
    return '等待审批';
  }
  if (normalized === 'waiting_user_input') {
    return '待确认信息';
  }
  if (normalized === 'pending') {
    return '待确认';
  }
  if (normalized === 'blocked') {
    return '已阻断';
  }
  if (normalized === 'completed' || normalized === 'approved') {
    return '已完成';
  }
  if (normalized === 'failed') {
    return '执行失败';
  }
  if (normalized === 'cancelled') {
    return '已取消';
  }
  if (normalized === 'rejected') {
    return '已拒绝';
  }
  if (normalized === 'running') {
    return '执行中';
  }
  return normalized || '运行中';
};

const isTerminalStatus = (status: string): boolean => {
  const normalized = String(status || '').trim().toLowerCase();
  return normalized === 'blocked' || normalized === 'completed' || normalized === 'failed' || normalized === 'cancelled';
};

const renderMetadataBadges = (params: {
  commandType?: string;
  riskLevel?: string;
  executorProfile?: string;
  effectiveExecutorProfile?: string;
  dispatchBackend?: string;
  targetIdentity?: string;
  requiresElevation?: boolean;
  requiresConfirmation?: boolean;
}) => {
  const items: Array<{ key: string; label: string; tone?: 'amber' | 'rose' | 'slate' }> = [];
  if (params.commandType) {
    items.push({ key: 'commandType', label: `类型 ${params.commandType}` });
  }
  if (params.riskLevel) {
    items.push({
      key: 'riskLevel',
      label: `风险 ${params.riskLevel}`,
      tone: String(params.riskLevel).toLowerCase() === 'high' ? 'rose' : 'slate',
    });
  }
  if (params.executorProfile) {
    items.push({ key: 'executorProfile', label: params.executorProfile });
  }
  if (params.effectiveExecutorProfile && params.effectiveExecutorProfile !== params.executorProfile) {
    items.push({ key: 'effectiveExecutorProfile', label: `实际 ${params.effectiveExecutorProfile}` });
  }
  if (params.dispatchBackend) {
    items.push({ key: 'dispatchBackend', label: `后端 ${params.dispatchBackend}` });
  }
  if (params.targetIdentity) {
    items.push({ key: 'targetIdentity', label: params.targetIdentity });
  }
  if (params.requiresElevation) {
    items.push({ key: 'requiresElevation', label: '需要提权', tone: 'amber' });
  } else if (params.requiresConfirmation) {
    items.push({ key: 'requiresConfirmation', label: '需要确认', tone: 'amber' });
  }
  if (!items.length) {
    return null;
  }
  return (
    <div className="mt-2 flex flex-wrap gap-2 text-[11px]">
      {items.map((item) => {
        const toneClassName = item.tone === 'rose'
          ? 'border-rose-200 bg-rose-50 text-rose-700'
          : item.tone === 'amber'
            ? 'border-amber-200 bg-amber-50 text-amber-800'
            : 'border-slate-200 bg-slate-50 text-slate-600';
        return (
          <span key={item.key} className={`rounded-full border px-2 py-0.5 ${toneClassName}`}>
            {item.label}
          </span>
        );
      })}
    </div>
  );
};

const renderResolvedTargetContext = (params: {
  targetClusterId?: string;
  targetNamespace?: string;
  targetNodeName?: string;
  targetKind?: string;
  targetIdentity?: string;
  resolvedTargetContext?: Record<string, unknown>;
}) => {
  const rawScope = (
    params.resolvedTargetContext
      && typeof params.resolvedTargetContext.execution_scope === 'object'
      ? params.resolvedTargetContext.execution_scope as Record<string, unknown>
      : {}
  );
  const clusterId = String(
    params.targetClusterId
    || rawScope.cluster_id
    || '',
  ).trim();
  const namespace = String(
    params.targetNamespace
    || rawScope.namespace
    || '',
  ).trim();
  const nodeName = String(
    params.targetNodeName
    || rawScope.node_name
    || '',
  ).trim();
  const targetKind = String(params.targetKind || '').trim();
  const targetIdentity = String(params.targetIdentity || '').trim();
  if (!clusterId && !namespace && !nodeName && !targetKind && !targetIdentity) {
    return null;
  }
  return (
    <div className="mt-3 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2">
      <div className="text-[11px] font-medium uppercase tracking-wide text-slate-500">目标上下文</div>
      <div className="mt-2 grid gap-1 text-xs text-slate-700 sm:grid-cols-2">
        {targetKind && <div>类型：{targetKind}</div>}
        {targetIdentity && <div>标识：{targetIdentity}</div>}
        {clusterId && <div>集群：{clusterId}</div>}
        {namespace && <div>命名空间：{namespace}</div>}
        {nodeName && <div>节点：{nodeName}</div>}
      </div>
    </div>
  );
};

const renderStatusBlock = (block: RuntimeTranscriptStatusBlock) => (
  <div key={block.id} className="rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3">
    <div className="flex items-start gap-2 text-sm text-rose-700">
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
      <div className="min-w-0">
        <div className="font-medium">{block.summary || '运行流异常'}</div>
        {block.streamError && (
          <div className="mt-1 whitespace-pre-wrap text-xs leading-6 text-rose-800">
            {block.streamError}
          </div>
        )}
      </div>
    </div>
  </div>
);

const renderThinkingBlock = (block: RuntimeTranscriptThinkingBlock) => {
  const isPreCommandPlan = String(block.title || '').trim() === '执行前计划';
  const shellClassName = isPreCommandPlan
    ? 'rounded-2xl border border-sky-200 bg-sky-50 px-4 py-3'
    : 'rounded-2xl border border-slate-200 bg-slate-50 px-4 py-3';
  const detailClassName = isPreCommandPlan
    ? 'mt-1 whitespace-pre-wrap text-sm leading-6 text-sky-900'
    : 'mt-1 whitespace-pre-wrap text-sm leading-6 text-slate-700';
  const titleClassName = isPreCommandPlan
    ? 'mt-2 text-sm font-semibold text-sky-900'
    : 'mt-2 text-sm font-medium text-slate-900';
  return (
    <div key={block.id} className={shellClassName}>
      <div className={`flex flex-wrap items-center gap-2 text-xs ${isPreCommandPlan ? 'text-sky-700' : 'text-slate-500'}`}>
        <span className={`rounded-full border px-2 py-0.5 ${getStatusClassName(block.status)}`}>
          {block.phase || 'thinking'}
        </span>
        {typeof block.iteration === 'number' && block.iteration > 0 && <span>iter {block.iteration}</span>}
        {block.timestamp && <span>{formatRuntimeTime(block.timestamp)}</span>}
      </div>
      <div className={titleClassName}>{block.title}</div>
      {block.detail && (
        <div className={detailClassName}>{block.detail}</div>
      )}
    </div>
  );
};

const renderCommandBlock = (block: RuntimeTranscriptCommandBlock) => {
  const stdout = String(block.stdout || '').trim();
  const stderr = String(block.stderr || '').trim();
  return (
    <div key={block.id} className="rounded-2xl border border-slate-200 bg-white px-4 py-3">
      <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
        <span className={`rounded-full border px-2 py-0.5 ${getStatusClassName(block.status)}`}>
          {formatStatusLabel(block.status)}
        </span>
        {typeof block.exitCode === 'number' && <span>exit {block.exitCode}</span>}
        {block.timestamp && <span>{formatRuntimeTime(block.timestamp)}</span>}
      </div>
      {block.purpose && (
        <div className="mt-2 text-xs text-slate-600">目的：{block.purpose}</div>
      )}
      <div className="mt-2 flex items-start gap-2">
        <Terminal className="mt-0.5 h-4 w-4 shrink-0 text-slate-400" />
        <code className="break-all text-sm text-slate-900">{block.command}</code>
      </div>
      {renderMetadataBadges({
        commandType: block.commandType,
        riskLevel: block.riskLevel,
        executorProfile: block.executorProfile,
        effectiveExecutorProfile: block.effectiveExecutorProfile,
        dispatchBackend: block.dispatchBackend,
        targetIdentity: block.targetIdentity,
      })}
      {renderResolvedTargetContext({
        targetClusterId: block.targetClusterId,
        targetNamespace: block.targetNamespace,
        targetNodeName: block.targetNodeName,
        targetKind: block.targetKind,
        targetIdentity: block.targetIdentity,
        resolvedTargetContext: block.resolvedTargetContext,
      })}
      {(stdout || stderr) && (
        <details className="mt-3 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2">
          <summary className="cursor-pointer list-none text-xs font-medium text-slate-600">
            展开原始命令输出
          </summary>
          <div className="mt-3 space-y-3">
            {stdout && (
              <div>
                <div className="text-[11px] font-medium uppercase tracking-wide text-slate-500">stdout</div>
                <pre className="mt-1 max-h-52 overflow-auto whitespace-pre-wrap rounded-xl bg-slate-950 p-3 text-[12px] leading-5 text-emerald-200">
                  {stdout}
                </pre>
              </div>
            )}
            {stderr && (
              <div>
                <div className="text-[11px] font-medium uppercase tracking-wide text-slate-500">stderr</div>
                <pre className="mt-1 max-h-52 overflow-auto whitespace-pre-wrap rounded-xl bg-slate-950 p-3 text-[12px] leading-5 text-rose-200">
                  {stderr}
                </pre>
              </div>
            )}
          </div>
        </details>
      )}
      {block.message && (
        <div className="mt-3 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-xs leading-5 text-slate-600">
          {block.message}
        </div>
      )}
      {block.dispatchReason && (
        <div className="mt-2 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-xs leading-5 text-slate-600">
          {block.dispatchReason}
        </div>
      )}
    </div>
  );
};

const renderApprovalBlock = (
  block: RuntimeTranscriptApprovalBlock,
  disabled?: boolean,
  onApprove?: (approval: RuntimeApprovalEntry) => void,
) => {
  const approval = block.approval;
  const pending = String(approval.status || '').trim().toLowerCase() === 'pending';
  return (
    <div key={block.id} className="rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className={`rounded-full border px-2 py-0.5 text-xs ${getStatusClassName(approval.status)}`}>
              {formatStatusLabel(approval.status)}
            </span>
            {approval.updatedAt && (
              <span className="text-xs text-amber-800">{formatRuntimeTime(approval.updatedAt)}</span>
            )}
          </div>
          <div className="mt-2 text-sm font-medium text-amber-950">{approval.title || '命令审批'}</div>
          {approval.purpose && (
            <div className="mt-1 text-xs text-amber-900">目的：{approval.purpose}</div>
          )}
        </div>
        {pending && (
          <button
            type="button"
            onClick={() => onApprove?.(approval)}
            disabled={disabled || typeof onApprove !== 'function'}
            className="inline-flex items-center gap-2 rounded-xl bg-amber-600 px-3 py-2 text-sm font-medium text-white transition hover:bg-amber-700 disabled:cursor-not-allowed disabled:opacity-60"
          >
            审批并执行
          </button>
        )}
      </div>
      <pre className="mt-3 overflow-auto whitespace-pre-wrap rounded-xl border border-amber-200 bg-slate-950 p-3 text-[12px] leading-5 text-amber-100">
        {approval.command}
      </pre>
      {approval.message && (
        <div className="mt-2 whitespace-pre-wrap rounded-xl border border-amber-200 bg-white p-3 text-sm text-amber-900">
          {approval.message}
        </div>
      )}
      {renderMetadataBadges({
        commandType: approval.commandType,
        riskLevel: approval.riskLevel,
        executorProfile: approval.executorProfile,
        effectiveExecutorProfile: approval.effectiveExecutorProfile,
        dispatchBackend: approval.dispatchBackend,
        targetIdentity: approval.targetIdentity,
        requiresElevation: approval.requiresElevation,
        requiresConfirmation: approval.requiresConfirmation,
      })}
    </div>
  );
};

const renderManualBlock = (
  block: RuntimeTranscriptManualActionBlock,
  disabled?: boolean,
  onExecuteManualAction?: (action: RuntimeManualActionEntry) => void,
) => (
  <div key={block.id} className="rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0">
        <div className="text-sm font-medium text-amber-950">{block.action.title || '人工确认动作'}</div>
        {block.action.purpose && (
          <div className="mt-1 text-xs text-amber-900">目的：{block.action.purpose}</div>
        )}
      </div>
      {block.action.command.trim()
        && String(block.action.commandType || '').trim().toLowerCase() !== 'unknown'
        && typeof onExecuteManualAction === 'function' && (
        <button
          type="button"
          onClick={() => onExecuteManualAction?.(block.action)}
          disabled={disabled}
          className="inline-flex items-center gap-2 rounded-xl bg-amber-600 px-3 py-2 text-sm font-medium text-white transition hover:bg-amber-700 disabled:cursor-not-allowed disabled:opacity-60"
        >
          人工确认
        </button>
      )}
    </div>
    {block.action.command.trim() ? (
      <pre className="mt-3 overflow-auto whitespace-pre-wrap rounded-xl border border-amber-200 bg-slate-950 p-3 text-[12px] leading-5 text-amber-100">
        {block.action.command}
      </pre>
    ) : (
      <div className="mt-3 rounded-xl border border-dashed border-amber-300 bg-white/80 p-3 text-sm text-amber-900">
        当前动作未附带可执行命令，需要先确认当前步骤是否合理，再决定后续执行方式。
      </div>
    )}
    {block.action.message && (
      <div className="mt-2 whitespace-pre-wrap rounded-xl border border-amber-200 bg-white p-3 text-sm text-amber-900">
        {block.action.message}
      </div>
    )}
  </div>
);

const renderUserInputBlock = (
  block: RuntimeTranscriptUserInputBlock,
  params: {
    disabled?: boolean;
    submitting?: boolean;
    inputValue: string;
    onInputChange: (nextValue: string) => void;
    onSubmit?: (params: {
      runId: string;
      actionId?: string;
      text: string;
      source?: string;
    }) => Promise<void> | void;
    onUseTemplateAsInput?: (params: {
      runId: string;
      actionId?: string;
      command: string;
      commandSpec?: Record<string, unknown>;
      source: 'recovery' | 'replan_template';
      reason?: string;
      fixHint?: string;
    }) => void;
  },
) => {
  const value = String(params.inputValue || '').trim();
  const normalizedStatus = String(block.status || '').trim().toLowerCase();
  const isBusinessQuestion = String(block.kind || '').trim().toLowerCase() === 'business_question'
    || Boolean(String(block.questionKind || '').trim());
  const recovery = block.recovery;
  const recoveryCode = String(recovery?.fixCode || '').trim();
  const recoveryHint = String(recovery?.fixHint || '').trim();
  const recoveryDetail = String(recovery?.fixDetail || '').trim();
  const suggestedCommand = String(recovery?.suggestedCommand || '').trim();
  const hasSuggestedSpec = Boolean(
    recovery?.suggestedCommandSpec
    && Object.keys(recovery.suggestedCommandSpec).length > 0,
  );
  const submitLabel = normalizedStatus === 'blocked'
    ? '重试'
    : (isBusinessQuestion ? '提交并继续' : '继续运行');
  const placeholder = (() => {
    const questionKind = String(block.questionKind || '').trim().toLowerCase();
    if (questionKind === 'write_safety_context') {
      return '例如：当前现象是错误率升高，已确认哪些证据，还缺什么，以及为什么现在必须执行这一步';
    }
    if (questionKind === 'diagnosis_goal') {
      return '例如：先定位根因，不要先做修复动作';
    }
    return '例如：先看最近 15 分钟';
  })();
  return (
    <div key={block.id} className="rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3">
      <div className="flex flex-wrap items-center gap-2 text-xs text-amber-800">
        <span className={`rounded-full border px-2 py-0.5 ${getStatusClassName(block.status)}`}>
          {formatStatusLabel(block.status)}
        </span>
        {block.timestamp && <span>{formatRuntimeTime(block.timestamp)}</span>}
      </div>
      <div className="mt-2 text-sm font-medium text-amber-950">{block.title}</div>
      {block.prompt && (
        <div className="mt-1 whitespace-pre-wrap text-xs leading-6 text-amber-900">{block.prompt}</div>
      )}
      {block.reason && (
        <div className="mt-2 rounded-xl border border-amber-200 bg-white px-3 py-2 text-xs text-amber-900">
          说明：{block.reason}
        </div>
      )}
      {(recoveryCode || recoveryHint || recoveryDetail || suggestedCommand) && (
        <div className="mt-2 rounded-xl border border-amber-200 bg-white px-3 py-2 text-xs text-amber-900">
          {recoveryCode && (
            <div className="font-medium">修复码：{recoveryCode}</div>
          )}
          {(recoveryHint || recoveryDetail) && (
            <div className="mt-1 whitespace-pre-wrap">
              {[recoveryHint, recoveryDetail].filter(Boolean).join(' ')}
            </div>
          )}
          {hasSuggestedSpec && (
            <div className="mt-1 text-amber-800">
              已生成结构化命令草稿，可直接使用建议命令继续执行。
            </div>
          )}
        </div>
      )}
      {block.purpose && (
        <div className="mt-2 text-xs text-amber-900">目的：{block.purpose}</div>
      )}
      {block.command && !isBusinessQuestion && (
        <pre className="mt-3 overflow-auto whitespace-pre-wrap rounded-xl border border-amber-200 bg-slate-950 p-3 text-[12px] leading-5 text-amber-100">
          {block.command}
        </pre>
      )}
      {suggestedCommand && (
        <div className="mt-3 rounded-xl border border-amber-200 bg-white p-3">
          <div className="text-[11px] font-medium uppercase tracking-wide text-amber-800">建议命令</div>
          <pre className="mt-2 overflow-auto whitespace-pre-wrap rounded-xl bg-slate-950 p-3 text-[12px] leading-5 text-amber-100">
            {suggestedCommand}
          </pre>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => {
                if (!suggestedCommand) {
                  return;
                }
                if (value && value !== suggestedCommand) {
                  const shouldReplace = window.confirm('输入框已有内容，是否替换为建议命令？');
                  if (!shouldReplace) {
                    return;
                  }
                }
                params.onInputChange(suggestedCommand);
              }}
              disabled={params.disabled || params.submitting}
              className="inline-flex items-center justify-center rounded-lg border border-amber-300 bg-amber-50 px-3 py-1.5 text-xs font-medium text-amber-800 transition hover:bg-amber-100 disabled:cursor-not-allowed disabled:opacity-60"
            >
              使用建议命令
            </button>
            {typeof params.onUseTemplateAsInput === 'function' && (
              <button
                type="button"
                onClick={() => {
                  if (!suggestedCommand) {
                    return;
                  }
                  params.onUseTemplateAsInput?.({
                    runId: block.runId,
                    actionId: block.actionId,
                    command: suggestedCommand,
                    commandSpec: hasSuggestedSpec ? recovery?.suggestedCommandSpec : undefined,
                    source: 'recovery',
                    reason: recoveryCode || block.reason,
                    fixHint: recoveryHint || recoveryDetail,
                  });
                }}
                disabled={params.disabled || params.submitting}
                className="inline-flex items-center justify-center rounded-lg border border-cyan-300 bg-cyan-50 px-3 py-1.5 text-xs font-medium text-cyan-800 transition hover:bg-cyan-100 disabled:cursor-not-allowed disabled:opacity-60"
              >
                作为下一轮输入
              </button>
            )}
            <button
              type="button"
              onClick={() => {
                const writer = globalThis.navigator?.clipboard?.writeText;
                if (typeof writer === 'function') {
                  void writer.call(globalThis.navigator.clipboard, suggestedCommand);
                }
              }}
              disabled={params.disabled || params.submitting}
              className="inline-flex items-center justify-center rounded-lg border border-slate-300 bg-slate-50 px-3 py-1.5 text-xs font-medium text-slate-700 transition hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-60"
            >
              复制建议命令
            </button>
          </div>
        </div>
      )}
      <div className="mt-3 flex flex-col gap-2 sm:flex-row">
        <input
          value={params.inputValue}
          onChange={(event) => params.onInputChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault();
              if (!params.submitting && value && typeof params.onSubmit === 'function') {
                void params.onSubmit({
                  runId: block.runId,
                  actionId: block.actionId,
                  text: params.inputValue,
                  source: 'user',
                });
              }
            }
          }}
          placeholder={placeholder}
          className="w-full rounded-xl border border-amber-200 bg-white px-3 py-2 text-sm text-slate-900 outline-none transition focus:border-amber-400 focus:ring-2 focus:ring-amber-200"
          disabled={params.disabled || params.submitting}
        />
        <button
          type="button"
          onClick={() => {
            if (typeof params.onSubmit !== 'function' || !value) {
              return;
            }
            void params.onSubmit({
              runId: block.runId,
              actionId: block.actionId,
              text: params.inputValue,
              source: 'user',
            });
          }}
          disabled={params.disabled || params.submitting || !value || typeof params.onSubmit !== 'function'}
          className="inline-flex items-center justify-center gap-2 rounded-xl bg-amber-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-amber-700 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {params.submitting && <Loader2 className="h-4 w-4 animate-spin" />}
          {submitLabel}
        </button>
      </div>
    </div>
  );
};

const renderTemplateHintBlock = (
  block: RuntimeTranscriptTemplateHintBlock,
  params: {
    disabled?: boolean;
    onUseTemplateAsInput?: (params: {
      runId: string;
      actionId?: string;
      command: string;
      commandSpec?: Record<string, unknown>;
      source: 'recovery' | 'replan_template';
      reason?: string;
      fixHint?: string;
    }) => void;
  },
) => {
  const suggestedCommand = String(block.suggestedCommand || '').trim();
  const hasSuggestedSpec = Boolean(
    block.suggestedCommandSpec
    && Object.keys(block.suggestedCommandSpec).length > 0,
  );
  const reason = String(block.reason || '').trim();
  return (
    <div key={block.id} className="rounded-2xl border border-cyan-200 bg-cyan-50 px-4 py-3">
      <div className="flex flex-wrap items-center gap-2 text-xs text-cyan-800">
        <span className="rounded-full border border-cyan-200 bg-white/90 px-2 py-0.5 text-cyan-700">
          模板建议
        </span>
        {reason && (
          <span className="rounded-full border border-cyan-200 bg-white/90 px-2 py-0.5 text-cyan-700">
            {reason}
          </span>
        )}
        {block.timestamp && <span>{formatRuntimeTime(block.timestamp)}</span>}
      </div>
      <div className="mt-2 text-sm font-medium text-cyan-950">{block.title}</div>
      {block.summary && (
        <div className="mt-1 whitespace-pre-wrap text-xs leading-6 text-cyan-900">{block.summary}</div>
      )}
      {block.fixHint && (
        <div className="mt-2 rounded-xl border border-cyan-200 bg-white px-3 py-2 text-xs text-cyan-900">
          修复建议：{block.fixHint}
        </div>
      )}
      {hasSuggestedSpec && (
        <div className="mt-2 text-xs text-cyan-800">
          已生成结构化命令草稿，可直接继续下一轮输入。
        </div>
      )}
      {suggestedCommand && (
        <div className="mt-3 rounded-xl border border-cyan-200 bg-white p-3">
          <div className="text-[11px] font-medium uppercase tracking-wide text-cyan-800">建议命令</div>
          <pre className="mt-2 overflow-auto whitespace-pre-wrap rounded-xl bg-slate-950 p-3 text-[12px] leading-5 text-cyan-100">
            {suggestedCommand}
          </pre>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            {typeof params.onUseTemplateAsInput === 'function' && (
              <button
                type="button"
                onClick={() => {
                  params.onUseTemplateAsInput?.({
                    runId: block.runId,
                    actionId: block.actionId,
                    command: suggestedCommand,
                    commandSpec: hasSuggestedSpec ? block.suggestedCommandSpec : undefined,
                    source: 'replan_template',
                    reason: block.reason,
                    fixHint: block.fixHint,
                  });
                }}
                disabled={params.disabled}
                className="inline-flex items-center justify-center rounded-lg border border-cyan-300 bg-cyan-50 px-3 py-1.5 text-xs font-medium text-cyan-800 transition hover:bg-cyan-100 disabled:cursor-not-allowed disabled:opacity-60"
              >
                Use Template as Input
              </button>
            )}
            <button
              type="button"
              onClick={() => {
                const writer = globalThis.navigator?.clipboard?.writeText;
                if (typeof writer === 'function') {
                  void writer.call(globalThis.navigator.clipboard, suggestedCommand);
                }
              }}
              disabled={params.disabled}
              className="inline-flex items-center justify-center rounded-lg border border-slate-300 bg-slate-50 px-3 py-1.5 text-xs font-medium text-slate-700 transition hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-60"
            >
              复制建议命令
            </button>
          </div>
        </div>
      )}
    </div>
  );
};

const renderSkillMatchedBlock = (block: RuntimeTranscriptSkillMatchedBlock) => (
  <div key={block.id} className="rounded-2xl border border-indigo-200 bg-indigo-50 px-4 py-3">
    <div className="flex flex-wrap items-center gap-2 text-xs text-indigo-700">
      <span className="inline-flex items-center gap-1 rounded-full border border-indigo-200 bg-white/90 px-2 py-0.5">
        <Zap className="h-3 w-3" />
        技能选择
      </span>
      {block.timestamp && <span>{formatRuntimeTime(block.timestamp)}</span>}
    </div>
    <div className="mt-2 text-sm font-medium text-indigo-950">{block.summary}</div>
    <div className="mt-2 flex flex-wrap gap-2">
      {block.selectedSkills.map((skill) => (
        <SkillBadge
          key={skill.name}
          skillName={skill.name}
          displayName={skill.displayName}
          riskLevel={skill.riskLevel}
        />
      ))}
    </div>
  </div>
);

const renderSkillStepBlock = (block: RuntimeTranscriptSkillBlock) => {
  const stdout = String(block.stdout || '').trim();
  const evidence = block.evidence ?? [];
  const statusNormalized = String(block.status || '').trim().toLowerCase();
  const isRunning = statusNormalized === 'running' || statusNormalized === 'executing';
  const isCompleted = statusNormalized === 'completed';
  return (
    <div key={block.id} className="rounded-2xl border border-violet-200 bg-violet-50 px-4 py-3">
      <div className="flex flex-wrap items-center gap-2 text-xs text-violet-700">
        <SkillBadge skillName={block.skillName} displayName={block.skillDisplayName} compact />
        <span className={`rounded-full border px-2 py-0.5 ${getStatusClassName(block.status)}`}>
          {isRunning ? '执行中' : isCompleted ? '已完成' : '已计划'}
        </span>
        {typeof block.seq === 'number' && <span>step {block.seq}</span>}
        {block.timestamp && <span>{formatRuntimeTime(block.timestamp)}</span>}
      </div>
      <div className="mt-2 text-sm font-medium text-violet-950">{block.stepTitle}</div>
      {block.stepPurpose && (
        <div className="mt-1 text-xs text-violet-800">{block.stepPurpose}</div>
      )}
      {block.command && (
        <div className="mt-2 flex items-start gap-2">
          <Terminal className="mt-0.5 h-4 w-4 shrink-0 text-violet-400" />
          <code className="break-all text-sm text-violet-900">{block.command}</code>
        </div>
      )}
      {evidence.length > 0 && (
        <div className="mt-2 rounded-xl border border-violet-200 bg-white/80 px-3 py-2">
          <div className="text-[11px] font-medium uppercase tracking-wide text-violet-600">提取证据</div>
          <ul className="mt-1 space-y-0.5">
            {evidence.map((line, i) => (
              <li key={`evidence-${i}-${line.slice(0, 16)}`} className="text-xs text-violet-800">{line}</li>
            ))}
          </ul>
        </div>
      )}
      {stdout && (
        <details className="mt-3 rounded-xl border border-violet-200 bg-white/80 px-3 py-2">
          <summary className="cursor-pointer list-none text-xs font-medium text-violet-700">
            展开技能输出
          </summary>
          <pre className="mt-2 max-h-52 overflow-auto whitespace-pre-wrap rounded-xl bg-slate-950 p-3 text-[12px] leading-5 text-emerald-200">
            {stdout}
          </pre>
        </details>
      )}
    </div>
  );
};

const renderAnswerBlock = (block: RuntimeTranscriptAnswerBlock) => (
  <div key={block.id} className="rounded-2xl border border-slate-200 bg-white px-4 py-3">
    <div className="whitespace-pre-wrap text-sm leading-7 text-slate-800">
      {block.content}
    </div>
    {block.streaming && (
      <div className="mt-2 inline-flex items-center gap-1 rounded-full border border-sky-200 bg-sky-50 px-2 py-0.5 text-xs text-sky-700">
        <Loader2 className="h-3 w-3 animate-spin" />
        持续处理中
      </div>
    )}
  </div>
);

const renderBlock = (
  block: RuntimeTranscriptBlock,
  params: {
    disabled?: boolean;
    submittingUserInput?: boolean;
    inputValueByBlockId: Record<string, string>;
    onInputChange: (blockId: string, nextValue: string) => void;
    onApprove?: (approval: RuntimeApprovalEntry) => void;
    onExecuteManualAction?: (action: RuntimeManualActionEntry) => void;
    onSubmitUserInput?: (params: {
      runId: string;
      actionId?: string;
      text: string;
      source?: string;
    }) => Promise<void> | void;
    onUseTemplateAsInput?: (params: {
      runId: string;
      actionId?: string;
      command: string;
      commandSpec?: Record<string, unknown>;
      source: 'recovery' | 'replan_template';
      reason?: string;
      fixHint?: string;
    }) => void;
  },
): React.ReactNode => {
  if (block.type === 'status') {
    return renderStatusBlock(block);
  }
  if (block.type === 'thinking') {
    return renderThinkingBlock(block);
  }
  if (block.type === 'command') {
    return renderCommandBlock(block);
  }
  if (block.type === 'approval') {
    return renderApprovalBlock(block, params.disabled, params.onApprove);
  }
  if (block.type === 'manual_action') {
    return renderManualBlock(block, params.disabled, params.onExecuteManualAction);
  }
  if (block.type === 'user_input') {
    return renderUserInputBlock(block, {
      disabled: params.disabled,
      submitting: params.submittingUserInput,
      inputValue: params.inputValueByBlockId[block.id] ?? block.command ?? '',
      onInputChange: (nextValue) => params.onInputChange(block.id, nextValue),
      onSubmit: params.onSubmitUserInput,
      onUseTemplateAsInput: params.onUseTemplateAsInput,
    });
  }
  if (block.type === 'template_hint') {
    return renderTemplateHintBlock(block, {
      disabled: params.disabled,
      onUseTemplateAsInput: params.onUseTemplateAsInput,
    });
  }
  if (block.type === 'skill_matched') {
    return renderSkillMatchedBlock(block);
  }
  if (block.type === 'skill_step') {
    return renderSkillStepBlock(block);
  }
  return renderAnswerBlock(block);
};

const RuntimeConversationCard: React.FC<RuntimeConversationCardProps> = ({
  message,
  disabled,
  submittingUserInput,
  onApprove,
  onExecuteManualAction,
  onSubmitUserInput,
  onUseTemplateAsInput,
  onCancelRun,
  detailContent,
  debugContent,
}) => {
  const [inputValueByBlockId, setInputValueByBlockId] = useState<Record<string, string>>({});
  const presentation = useMemo(() => buildRuntimePresentation(message), [message]);
  const conversationMessage = presentation.conversation;
  const detailBlocks = presentation.detailBlocks;
  const hasDetailContent = detailBlocks.length > 0 || Boolean(detailContent) || Boolean(debugContent);

  const normalizedInputs = useMemo(() => {
    const nextValues: Record<string, string> = { ...inputValueByBlockId };
    conversationMessage.blocks.forEach((block) => {
      if (block.type !== 'user_input') {
        return;
      }
      if (typeof nextValues[block.id] !== 'string') {
        nextValues[block.id] = block.command || '';
      }
    });
    return nextValues;
  }, [conversationMessage.blocks, inputValueByBlockId]);

  const handleChangeInput = (blockId: string, nextValue: string) => {
    setInputValueByBlockId((current) => ({
      ...current,
      [blockId]: nextValue,
    }));
  };

  return (
    <div className="overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-sm">
    <div className="flex items-start gap-3 px-5 py-5">
      <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl bg-slate-900 text-white">
        <Bot className="h-5 w-5" />
      </div>

      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-semibold text-slate-900">AI</span>
            <span className={`rounded-full border px-2 py-0.5 text-xs ${getStatusClassName(conversationMessage.status)}`}>
              {formatStatusLabel(conversationMessage.status)}
            </span>
            {conversationMessage.currentPhase && (
              <span className="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-xs text-slate-500">
                {conversationMessage.currentPhase}
              </span>
            )}
          </div>

          <div className="flex flex-wrap items-center gap-3">
            {conversationMessage.updatedAt && (
              <span className="inline-flex items-center gap-1 text-xs text-slate-400">
                <Clock3 className="h-3 w-3" />
                {formatRuntimeTime(conversationMessage.updatedAt)}
              </span>
            )}
            {!isTerminalStatus(conversationMessage.status) && typeof onCancelRun === 'function' && (
              <button
                type="button"
                onClick={() => onCancelRun(conversationMessage.runId)}
                disabled={disabled}
                className="rounded-xl border border-rose-200 bg-rose-50 px-3 py-1.5 text-xs font-medium text-rose-700 transition hover:bg-rose-100 disabled:cursor-not-allowed disabled:opacity-60"
              >
                取消
              </button>
            )}
          </div>
        </div>

        <div className="mt-4 space-y-3">
          {conversationMessage.blocks.map((block) => renderBlock(block, {
            disabled,
            submittingUserInput,
            inputValueByBlockId: normalizedInputs,
            onInputChange: handleChangeInput,
            onApprove,
            onExecuteManualAction,
            onSubmitUserInput,
            onUseTemplateAsInput,
          }))}
        </div>

        {hasDetailContent && (
          <details className="mt-4 rounded-2xl border border-slate-200 bg-slate-50 px-4 py-3">
            <summary className="cursor-pointer list-none text-sm font-medium text-slate-700">
              查看详情
            </summary>
            <div className="mt-3 space-y-3">
              {detailBlocks.map((block) => (
                renderBlock(block, {
                  disabled,
                  submittingUserInput,
                  inputValueByBlockId: normalizedInputs,
                  onInputChange: handleChangeInput,
                  onApprove,
                  onExecuteManualAction,
                  onSubmitUserInput,
                  onUseTemplateAsInput,
                })
              ))}
              {detailContent}
              {debugContent && (
                <div className="rounded-2xl border border-slate-200 bg-white px-4 py-3">
                  <div className="text-xs font-medium text-slate-600">调试信息</div>
                  <div className="mt-2">{debugContent}</div>
                </div>
              )}
            </div>
          </details>
        )}
      </div>
    </div>
    </div>
  );
};

export default RuntimeConversationCard;
