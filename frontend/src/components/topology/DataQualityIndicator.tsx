import React, { useState, useEffect } from 'react';
import { AlertTriangle, ChevronDown, ChevronUp, X } from 'lucide-react';
import type { DataQuality } from '../../utils/api';

interface Props {
  dataQuality: DataQuality;
}

const DISMISS_KEY = 'topology:quality-dismissed';
const DISMISS_TTL_MS = 24 * 60 * 60 * 1000;

function isDismissed(): boolean {
  try {
    const raw = localStorage.getItem(DISMISS_KEY);
    if (!raw) return false;
    const ts = Number(raw);
    return Number.isFinite(ts) && Date.now() - ts < DISMISS_TTL_MS;
  } catch {
    return false;
  }
}

function setDismissed(): void {
  try {
    localStorage.setItem(DISMISS_KEY, String(Date.now()));
  } catch {
    // ignore
  }
}

const STATUS_LABELS: Record<string, { label: string; color: string }> = {
  traces: { label: 'Traces', color: 'bg-amber-500' },
  logs: { label: 'Logs', color: 'bg-emerald-500' },
  metrics: { label: 'Metrics', color: 'bg-emerald-500' },
};

const DataQualityIndicator: React.FC<Props> = ({ dataQuality }) => {
  const [collapsed, setCollapsed] = useState(false);
  const [hidden, setHidden] = useState(isDismissed());

  useEffect(() => {
    setHidden(isDismissed());
  }, [dataQuality]);

  if (hidden) return null;

  const statuses: Array<{ key: string; label: string; available: boolean; color: string }> = [];
  for (const [key, info] of Object.entries(STATUS_LABELS)) {
    const available =
      key === 'traces'
        ? dataQuality.traces_available
        : key === 'logs'
          ? dataQuality.logs_available
          : dataQuality.metrics_available;
    statuses.push({
      key,
      label: info.label,
      available,
      color: available ? 'bg-emerald-500' : 'bg-amber-500',
    });
  }

  const missingFields: string[] = [];
  const ds = dataQuality.dimension_status;
  if (ds.latency === 'missing') missingFields.push('P99/P95');
  if (ds.error_rate_edge === 'missing') missingFields.push('边错误率');
  if (ds.quality_score === 'logs_only') missingFields.push('质量分(降级)');

  return (
    <div className="mb-2 rounded border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-100">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <AlertTriangle className="h-4 w-4 text-amber-400" />
          <span className="font-medium">数据完整性</span>
          {statuses.map((s) => (
            <span key={s.key} className="flex items-center gap-1">
              <span className={`inline-block h-2 w-2 rounded-full ${s.color}`} />
              {s.label}: {s.available ? '正常' : '缺失'}
            </span>
          ))}
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={() => setCollapsed((c) => !c)}
            className="rounded p-0.5 hover:bg-amber-500/20"
            title={collapsed ? '展开详情' : '收起'}
          >
            {collapsed ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronUp className="h-3.5 w-3.5" />}
          </button>
          <button
            onClick={() => { setHidden(true); setDismissed(); }}
            className="rounded p-0.5 hover:bg-amber-500/20"
            title="关闭（24 小时内不再显示）"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>
      {!collapsed && missingFields.length > 0 && (
        <div className="mt-1.5 text-amber-300/80">
          部分指标已降级 — 当前无 Trace 数据，
          {missingFields.join('、')}等指标不可用，
          已切换为基于日志的替代评分。
        </div>
      )}
    </div>
  );
};

export default DataQualityIndicator;
