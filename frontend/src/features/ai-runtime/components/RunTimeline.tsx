import React from 'react';

import type { RuntimeTimelineEntry } from '../types/view';

interface RunTimelineProps {
  timeline: RuntimeTimelineEntry[];
}

const getTimelineStatusClassName = (status: string): string => {
  const normalized = String(status || '').trim().toLowerCase();
  if (normalized === 'success' || normalized === 'completed') {
    return 'border-emerald-200 bg-emerald-50 text-emerald-700';
  }
  if (normalized === 'error' || normalized === 'failed') {
    return 'border-rose-200 bg-rose-50 text-rose-700';
  }
  if (normalized === 'warning' || normalized === 'pending') {
    return 'border-amber-200 bg-amber-50 text-amber-800';
  }
  return 'border-slate-200 bg-slate-100 text-slate-700';
};

const RunTimeline: React.FC<RunTimelineProps> = ({ timeline }) => {
  if (!timeline.length) {
    return (
      <div className="rounded border border-dashed border-slate-200 bg-slate-50 p-2 text-[11px] text-slate-500">
        当前还没有可展示的运行时间线。
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {timeline.slice(-6).reverse().map((item) => (
        <div key={item.id} className="rounded border border-slate-200 bg-white p-2">
          <div className="flex flex-wrap items-center gap-2 text-[10px] text-slate-500">
            <span className={`inline-flex items-center rounded-full border px-1.5 py-0.5 ${getTimelineStatusClassName(item.status)}`}>
              {item.phase}
            </span>
            {typeof item.iteration === 'number' && item.iteration > 0 && <span>iter {item.iteration}</span>}
            {item.timestamp && <span>{item.timestamp}</span>}
          </div>
          <div className="mt-1 text-[11px] font-medium text-slate-800 whitespace-pre-wrap">{item.title}</div>
          {item.detail && (
            <div className="mt-1 text-[11px] text-slate-600 whitespace-pre-wrap">{item.detail}</div>
          )}
        </div>
      ))}
    </div>
  );
};

export default RunTimeline;
