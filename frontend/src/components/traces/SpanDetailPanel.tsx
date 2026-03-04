/**
 * Span 详情面板组件
 * 显示单个 Span 的详细信息
 */
import React from 'react';
import { formatTimeCST, formatTimeUTC, formatDuration } from '../../utils/formatters';
import { Clock, Activity } from 'lucide-react';

export interface Span {
  trace_id: string;
  span_id: string;
  parent_span_id: string;
  service_name: string;
  operation_name: string;
  start_time: string;
  duration_ms: number;
  status: string;
  tags: Record<string, any>;
}

interface SpanDetailPanelProps {
  span: Span | null;
  onClose: () => void;
}

const resolveDurationMs = (span: Span): number => {
  const direct = Number(span.duration_ms);
  if (Number.isFinite(direct) && direct > 0) {
    return direct;
  }
  const tags = span.tags || {};
  const msCandidates = [tags.duration_ms, tags['span.duration_ms'], tags.latency_ms, tags.duration, tags.elapsed_ms];
  for (const candidate of msCandidates) {
    const parsed = Number(candidate);
    if (Number.isFinite(parsed) && parsed > 0) {
      return parsed;
    }
  }
  const usCandidates = [tags.duration_us, tags['span.duration_us'], tags.latency_us, tags.elapsed_us];
  for (const candidate of usCandidates) {
    const parsed = Number(candidate);
    if (Number.isFinite(parsed) && parsed > 0) {
      return parsed / 1000;
    }
  }
  const nsCandidates = [tags.duration_ns, tags['span.duration_ns'], tags.latency_ns, tags.elapsed_ns];
  for (const candidate of nsCandidates) {
    const parsed = Number(candidate);
    if (Number.isFinite(parsed) && parsed > 0) {
      return parsed / 1_000_000;
    }
  }
  return 0;
};

const SpanDetailPanel: React.FC<SpanDetailPanelProps> = ({ span, onClose }) => {
  if (!span) return null;
  const durationMs = resolveDurationMs(span);

  const statusColor = {
    'STATUS_CODE_ERROR': 'text-red-600 bg-red-50',
    'STATUS_CODE_OK': 'text-green-600 bg-green-50',
    'STATUS_CODE_UNSET': 'text-gray-600 bg-gray-50',
  }[span.status] || 'STATUS_CODE_UNSET';

  const statusText = {
    'STATUS_CODE_ERROR': '错误',
    'STATUS_CODE_OK': '成功',
    'STATUS_CODE_UNSET': '未设置',
  }[span.status] || '未设置';

  return (
    <div className="bg-white rounded-lg shadow-md overflow-hidden flex flex-col h-full">
      {/* 头部 */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-200 bg-gray-50">
        <h3 className="font-semibold text-gray-900">Span 详情</h3>
        <button
          onClick={onClose}
          className="text-gray-400 hover:text-gray-600"
        >
          <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      </div>

      {/* 内容 */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {/* 基本信息 */}
        <div className="grid grid-cols-2 gap-3">
          <div className="bg-gray-50 rounded-lg p-3 border border-gray-100">
            <div className="text-xs text-gray-500 uppercase mb-1">服务</div>
            <div className="text-sm font-semibold text-blue-600">{span.service_name}</div>
          </div>
          <div className="bg-gray-50 rounded-lg p-3 border border-gray-100">
            <div className="text-xs text-gray-500 uppercase mb-1">操作</div>
            <div className="text-sm text-gray-900 font-medium">{span.operation_name}</div>
          </div>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div className="bg-gray-50 rounded-lg p-3 border border-gray-100">
              <div className="flex items-center gap-2 mb-1">
                <Clock className="w-4 h-4 text-gray-400" />
                <div className="text-xs text-gray-500 uppercase">开始时间</div>
              </div>
            <div className="text-sm text-gray-900 font-mono">
              <span title={`UTC: ${formatTimeUTC(span.start_time)}`}>{formatTimeCST(span.start_time)}</span>
            </div>
          </div>
          <div className="bg-gray-50 rounded-lg p-3 border border-gray-100">
            <div className="flex items-center gap-2 mb-1">
              <Activity className="w-4 h-4 text-gray-400" />
              <div className="text-xs text-gray-500 uppercase">持续时间</div>
            </div>
            <div className="text-sm text-gray-900 font-mono font-semibold">
              {formatDuration(durationMs)}
            </div>
          </div>
        </div>

        {/* 状态 */}
        <div>
          <div className="flex items-center gap-2 mb-2">
            <div className="text-xs text-gray-500 uppercase">状态</div>
          </div>
          <div className={`inline-flex items-center px-3 py-1.5 text-sm font-semibold rounded-lg ${statusColor}`}>
            {statusText}
          </div>
        </div>

        {/* IDs */}
        <div className="space-y-2">
          <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
            <div className="px-3 py-2 bg-gray-50 border-b border-gray-200">
              <div className="text-xs text-gray-500">Trace ID</div>
            </div>
            <div className="px-3 py-2">
              <code className="text-xs text-gray-900 break-all">{span.trace_id}</code>
            </div>
          </div>

          <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
            <div className="px-3 py-2 bg-gray-50 border-b border-gray-200">
              <div className="text-xs text-gray-500">Span ID</div>
            </div>
            <div className="px-3 py-2">
              <code className="text-xs text-gray-900 break-all">{span.span_id}</code>
            </div>
          </div>

          {span.parent_span_id && (
            <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
              <div className="px-3 py-2 bg-gray-50 border-b border-gray-200">
                <div className="text-xs text-gray-500">Parent Span ID</div>
              </div>
              <div className="px-3 py-2">
                <code className="text-xs text-gray-900 break-all">{span.parent_span_id}</code>
              </div>
            </div>
          )}
        </div>

        {/* Tags */}
        {Object.keys(span.tags || {}).length > 0 && (
          <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
            <div className="px-3 py-2 bg-gray-50 border-b border-gray-200 flex items-center justify-between">
              <div className="flex items-center gap-2">
                <div className="text-xs text-gray-500">Tags</div>
              </div>
              <div className="text-xs text-gray-400">{Object.keys(span.tags).length} 个</div>
            </div>
            <div className="p-3">
              <div className="space-y-2">
                {Object.entries(span.tags).map(([key, value]) => (
                  <div key={key} className="flex items-center gap-2 text-sm">
                    <span className="px-2 py-1 bg-blue-50 text-blue-700 rounded font-mono text-xs">{key}</span>
                    <span className="text-gray-600">:</span>
                    <span className="text-gray-900 break-all">{String(value)}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

export default SpanDetailPanel;
