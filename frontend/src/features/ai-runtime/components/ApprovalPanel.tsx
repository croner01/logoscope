import React from 'react';

import type { RuntimeApprovalEntry } from '../types/view';

interface ApprovalPanelProps {
  approvals: RuntimeApprovalEntry[];
  disabled?: boolean;
  onApprove?: (approval: RuntimeApprovalEntry) => void;
}

const ApprovalPanel: React.FC<ApprovalPanelProps> = ({ approvals, disabled, onApprove }) => {
  if (!approvals.length) {
    return (
      <div className="rounded border border-dashed border-slate-200 bg-slate-50 p-2 text-[11px] text-slate-500">
        当前没有待审批动作。
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {approvals.map((item) => (
        <div key={item.id} className="rounded border border-amber-200 bg-amber-50 p-2">
          <div className="text-[11px] font-medium text-amber-900">{item.title}</div>
          <div className="mt-1 break-all rounded border border-amber-100 bg-white px-2 py-1 text-[11px] text-amber-900">
            <code>{item.command}</code>
          </div>
          <div className="mt-1 flex flex-wrap gap-2 text-[10px] text-amber-700">
            {item.commandType && <span>type: {item.commandType}</span>}
            {item.riskLevel && <span>risk: {item.riskLevel}</span>}
            {item.requiresElevation && <span>need elevation</span>}
            {item.requiresConfirmation && !item.requiresElevation && <span>need confirm</span>}
          </div>
          {item.message && (
            <div className="mt-1 text-[10px] text-amber-800 whitespace-pre-wrap">{item.message}</div>
          )}
          <div className="mt-2">
            <button
              type="button"
              onClick={() => onApprove?.(item)}
              disabled={disabled || typeof onApprove !== 'function'}
              className="rounded bg-amber-600 px-2 py-1 text-[11px] text-white hover:bg-amber-700 disabled:opacity-50"
            >
              审批并执行
            </button>
          </div>
        </div>
      ))}
    </div>
  );
};

export default ApprovalPanel;
