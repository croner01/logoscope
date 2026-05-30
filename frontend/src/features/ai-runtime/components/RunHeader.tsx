import React from 'react';

interface RunHeaderProps {
  runId: string;
  title: string;
  status: string;
  currentPhase?: string;
  updatedAt?: string;
  streaming?: boolean;
  canCancel?: boolean;
  onCancel?: (runId: string) => void;
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
  return parsed.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
};

const getStatusClassName = (status: string): string => {
  const normalized = String(status || '').trim().toLowerCase();
  if (normalized === 'completed') {
    return 'border-emerald-200 bg-emerald-50 text-emerald-700';
  }
  if (normalized === 'blocked' || normalized === 'failed' || normalized === 'cancelled') {
    return 'border-rose-200 bg-rose-50 text-rose-700';
  }
  if (normalized === 'waiting_approval' || normalized === 'waiting_user_input') {
    return 'border-amber-200 bg-amber-50 text-amber-800';
  }
  return 'border-blue-200 bg-blue-50 text-blue-700';
};

const RunHeader: React.FC<RunHeaderProps> = ({
  runId,
  title,
  status,
  currentPhase,
  updatedAt,
  streaming,
  canCancel,
  onCancel,
}) => (
  <div className="flex items-start justify-between gap-3">
    <div className="min-w-0">
      <div className="text-sm font-medium text-slate-900 truncate">{title}</div>
      <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
        <span className={`inline-flex items-center rounded-full border px-2 py-0.5 ${getStatusClassName(status)}`}>
          {status}
        </span>
        {currentPhase && (
          <span className="inline-flex items-center rounded-full border border-slate-200 bg-slate-100 px-2 py-0.5 text-slate-600">
            phase: {currentPhase}
          </span>
        )}
        {streaming && (
          <span className="inline-flex items-center rounded-full border border-indigo-200 bg-indigo-50 px-2 py-0.5 text-indigo-700">
            streaming
          </span>
        )}
        {updatedAt && <span>{formatRuntimeTime(updatedAt)}</span>}
      </div>
      <div className="mt-1 text-[10px] text-slate-400 break-all">run: {runId}</div>
    </div>
    {canCancel && typeof onCancel === 'function' && (
      <button
        type="button"
        onClick={() => onCancel(runId)}
        className="shrink-0 rounded border border-rose-200 bg-rose-50 px-2 py-1 text-[11px] text-rose-700 hover:bg-rose-100"
      >
        取消运行
      </button>
    )}
  </div>
);

export default RunHeader;
