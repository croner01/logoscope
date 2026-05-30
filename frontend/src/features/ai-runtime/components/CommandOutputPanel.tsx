import React from 'react';

import type { RuntimeCommandEntry } from '../types/view';

interface CommandOutputPanelProps {
  commandRuns: RuntimeCommandEntry[];
}

const getCommandStatusClassName = (status: string): string => {
  const normalized = String(status || '').trim().toLowerCase();
  if (normalized === 'completed') {
    return 'border-emerald-200 bg-emerald-50 text-emerald-700';
  }
  if (normalized === 'failed' || normalized === 'cancelled') {
    return 'border-rose-200 bg-rose-50 text-rose-700';
  }
  return 'border-blue-200 bg-blue-50 text-blue-700';
};

const CommandOutputPanel: React.FC<CommandOutputPanelProps> = ({ commandRuns }) => {
  if (!commandRuns.length) {
    return (
      <div className="rounded border border-dashed border-slate-200 bg-slate-50 p-2 text-[11px] text-slate-500">
        运行已创建，尚未出现命令执行输出。
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {commandRuns.slice(-3).reverse().map((item) => {
        const stdout = String(item.stdout || '').trim();
        const stderr = String(item.stderr || '').trim();
        return (
          <details
            key={item.id}
            className="rounded border border-slate-200 bg-white p-2"
            open={String(item.status || '').trim().toLowerCase() === 'running'}
          >
            <summary className="cursor-pointer list-none">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="text-[11px] font-medium text-slate-800 break-all">{item.command}</div>
                  <div className="mt-1 flex flex-wrap items-center gap-2 text-[10px] text-slate-500">
                    <span className={`inline-flex items-center rounded-full border px-1.5 py-0.5 ${getCommandStatusClassName(item.status)}`}>
                      {item.status}
                    </span>
                    {item.commandType && <span>type: {item.commandType}</span>}
                    {item.riskLevel && <span>risk: {item.riskLevel}</span>}
                    {typeof item.exitCode === 'number' && <span>exit: {item.exitCode}</span>}
                    {item.timedOut && <span>timeout</span>}
                  </div>
                </div>
              </div>
            </summary>
            {(stdout || stderr) && (
              <div className="mt-2 space-y-2">
                {stdout && (
                  <div>
                    <div className="text-[10px] font-medium uppercase tracking-wide text-slate-500">stdout</div>
                    <pre className="mt-1 max-h-40 overflow-auto rounded border border-slate-200 bg-slate-950 p-2 text-[11px] text-emerald-200 whitespace-pre-wrap">
                      {stdout}
                    </pre>
                  </div>
                )}
                {stderr && (
                  <div>
                    <div className="text-[10px] font-medium uppercase tracking-wide text-slate-500">stderr</div>
                    <pre className="mt-1 max-h-32 overflow-auto rounded border border-slate-200 bg-slate-950 p-2 text-[11px] text-rose-200 whitespace-pre-wrap">
                      {stderr}
                    </pre>
                  </div>
                )}
              </div>
            )}
          </details>
        );
      })}
    </div>
  );
};

export default CommandOutputPanel;
