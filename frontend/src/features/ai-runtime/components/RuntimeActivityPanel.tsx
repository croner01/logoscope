import React, { useEffect, useState } from 'react';

import type { RuntimeApprovalEntry, RuntimePanelRunView } from '../types/view';
import ApprovalPanel from './ApprovalPanel';
import CommandOutputPanel from './CommandOutputPanel';
import RunHeader from './RunHeader';
import RunTimeline from './RunTimeline';

interface RuntimeActivityPanelProps {
  runs: RuntimePanelRunView[];
  disabled?: boolean;
  onApprove?: (approval: RuntimeApprovalEntry) => void;
  onSubmitUserInput?: (params: {
    runId: string;
    actionId?: string;
    text: string;
    source?: string;
  }) => Promise<void> | void;
  onCancelRun?: (runId: string) => void;
}

const isTerminalStatus = (status: string): boolean => {
  const normalized = String(status || '').trim().toLowerCase();
  return normalized === 'blocked' || normalized === 'completed' || normalized === 'failed' || normalized === 'cancelled';
};

const RuntimeUserInputPanel: React.FC<{
  run: RuntimePanelRunView;
  disabled?: boolean;
  onSubmitUserInput?: (params: {
    runId: string;
    actionId?: string;
    text: string;
    source?: string;
  }) => Promise<void> | void;
}> = ({ run, disabled, onSubmitUserInput }) => {
  const [inputText, setInputText] = useState('');
  const [submitting, setSubmitting] = useState(false);
  useEffect(() => {
    setInputText('');
  }, [run.userInput?.id]);
  if (!run.userInput) {
    return (
      <div className="rounded border border-dashed border-slate-200 bg-slate-50 p-2 text-[11px] text-slate-500">
        当前不需要补充关键信息。
      </div>
    );
  }
  const text = String(inputText || '').trim();
  const handleSubmit = async () => {
    if (!text || typeof onSubmitUserInput !== 'function') {
      return;
    }
    setSubmitting(true);
    try {
      await onSubmitUserInput({
        runId: run.runId,
        actionId: run.userInput?.actionId,
        text,
        source: 'user',
      });
      setInputText('');
    } finally {
      setSubmitting(false);
    }
  };
  return (
    <div className="rounded border border-amber-200 bg-amber-50 p-2">
      <div className="text-[11px] font-medium text-amber-900">{run.userInput.title}</div>
      {run.userInput.prompt && (
        <div className="mt-1 text-[10px] leading-5 text-amber-800 whitespace-pre-wrap">{run.userInput.prompt}</div>
      )}
      {run.userInput.reason && (
        <div className="mt-1 text-[10px] text-amber-800">说明: {run.userInput.reason}</div>
      )}
      <div className="mt-2 flex flex-col gap-2">
        <input
          value={inputText}
          onChange={(event) => setInputText(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault();
              void handleSubmit();
            }
          }}
          placeholder="例如：先看最近 15 分钟"
          className="w-full rounded border border-amber-200 bg-white px-2 py-1 text-[11px] text-slate-900 outline-none focus:border-amber-400 focus:ring-1 focus:ring-amber-200"
          disabled={disabled || submitting}
        />
        <button
          type="button"
          onClick={() => {
            void handleSubmit();
          }}
          disabled={disabled || submitting || !text || typeof onSubmitUserInput !== 'function'}
          className="self-start rounded bg-amber-600 px-2 py-1 text-[11px] text-white hover:bg-amber-700 disabled:opacity-50"
        >
          {submitting ? '提交中...' : '继续运行'}
        </button>
      </div>
    </div>
  );
};

const RuntimeActivityPanel: React.FC<RuntimeActivityPanelProps> = ({
  runs,
  disabled,
  onApprove,
  onSubmitUserInput,
  onCancelRun,
}) => (
  <div className="rounded-lg border border-slate-200 bg-white p-3">
    <div className="flex items-center justify-between gap-2">
      <div>
        <h5 className="text-sm font-medium text-slate-900">运行态</h5>
        <div className="text-[11px] text-slate-500">实时查看命令、审批和思考轨迹</div>
      </div>
      <span className="rounded-full border border-slate-200 bg-slate-100 px-2 py-0.5 text-[10px] text-slate-600">
        runs {runs.length}
      </span>
    </div>

    {runs.length === 0 ? (
      <div className="mt-3 rounded border border-dashed border-slate-200 bg-slate-50 p-3 text-[11px] text-slate-500">
        这里会展示 runtime 命令执行、审批恢复和思考时间线。当前还没有 active run。
      </div>
    ) : (
      <div className="mt-3 space-y-3">
        {runs.map((run) => (
          <div key={run.runId} className="rounded border border-slate-200 bg-slate-50 p-3">
            <RunHeader
              runId={run.runId}
              title={run.title}
              status={run.status}
              currentPhase={run.currentPhase}
              updatedAt={run.updatedAt}
              streaming={run.streaming}
              canCancel={!isTerminalStatus(run.status) && typeof onCancelRun === 'function' && !disabled}
              onCancel={onCancelRun}
            />
            {run.streamError && (
              <div className="mt-2 rounded border border-rose-200 bg-rose-50 p-2 text-[11px] text-rose-700">
                {run.streamError}
              </div>
            )}
            {run.assistantMessage && (
              <div className="mt-2 rounded border border-indigo-100 bg-indigo-50 p-2 text-[11px] text-indigo-900 whitespace-pre-wrap">
                {run.assistantMessage}
              </div>
            )}
            <div className="mt-3 space-y-3">
              <div>
                <div className="mb-1 text-[11px] font-medium text-slate-600">待审批动作</div>
                <ApprovalPanel approvals={run.approvals} disabled={disabled} onApprove={onApprove} />
              </div>
              <div>
                <div className="mb-1 text-[11px] font-medium text-slate-600">待确认信息</div>
                <RuntimeUserInputPanel
                  run={run}
                  disabled={disabled}
                  onSubmitUserInput={onSubmitUserInput}
                />
              </div>
              <div>
                <div className="mb-1 text-[11px] font-medium text-slate-600">命令输出</div>
                <CommandOutputPanel commandRuns={run.commandRuns} />
              </div>
              <div>
                <div className="mb-1 text-[11px] font-medium text-slate-600">时间线</div>
                <RunTimeline timeline={run.timeline} />
              </div>
            </div>
          </div>
        ))}
      </div>
    )}
  </div>
);

export default RuntimeActivityPanel;
