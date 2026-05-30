/**
 * Span 时间线组件 - 简化版
 * 显示 Trace 下所有 Spans 的瀑布图
 */
import React from 'react';
import { formatDuration, formatTimeCST, toEpochMs } from '../../utils/formatters';

export interface Span {
  trace_id: string;
  span_id: string;
  parent_span_id: string;
  service_name: string;
  operation_name: string;
  start_time: string;
  duration_ms: number;
  status: string;
  tags: Record<string, unknown>;
  durationMs?: unknown;
  duration?: unknown;
  latency_ms?: unknown;
  elapsed_ms?: unknown;
  duration_us?: unknown;
  latency_us?: unknown;
  elapsed_us?: unknown;
  duration_ns?: unknown;
  latency_ns?: unknown;
  elapsed_ns?: unknown;
  start_time_unix_nano?: unknown;
  start_unix_nano?: unknown;
  start_ns?: unknown;
  end_time_unix_nano?: unknown;
  end_unix_nano?: unknown;
  end_ns?: unknown;
}

interface TraceTimelineProps {
  spans: Span[];
  selectedSpanId: string | null;
  onSpanClick: (span: Span) => void;
}

type SpanNode = Span & {
  children: SpanNode[];
  level: number;
  _start_ms?: number;
  _duration_ms?: number;
};

const resolveSpanDurationMs = (span: Span): number => {
  const tryPositive = (value: unknown, scale = 1): number => {
    const parsed = Number(value);
    if (Number.isFinite(parsed) && parsed > 0) {
      return parsed * scale;
    }
    return 0;
  };

  const directMs = [
    span.duration_ms,
    span.durationMs,
    span.duration,
    span.latency_ms,
    span.elapsed_ms,
  ];
  for (const candidate of directMs) {
    const parsed = tryPositive(candidate);
    if (parsed > 0) {
      return parsed;
    }
  }

  const directUs = [
    span.duration_us,
    span.latency_us,
    span.elapsed_us,
  ];
  for (const candidate of directUs) {
    const parsed = tryPositive(candidate, 1 / 1000);
    if (parsed > 0) {
      return parsed;
    }
  }

  const directNs = [
    span.duration_ns,
    span.latency_ns,
    span.elapsed_ns,
  ];
  for (const candidate of directNs) {
    const parsed = tryPositive(candidate, 1 / 1_000_000);
    if (parsed > 0) {
      return parsed;
    }
  }

  const tags = span.tags || {};
  const msCandidates = [
    tags.duration_ms,
    tags['span.duration_ms'],
    tags.latency_ms,
    tags.duration,
    tags.elapsed_ms,
  ];
  for (const candidate of msCandidates) {
    const parsed = tryPositive(candidate);
    if (parsed > 0) {
      return parsed;
    }
  }

  const usCandidates = [tags.duration_us, tags['span.duration_us'], tags.latency_us, tags.elapsed_us];
  for (const candidate of usCandidates) {
    const parsed = tryPositive(candidate, 1 / 1000);
    if (parsed > 0) {
      return parsed;
    }
  }

  const nsCandidates = [tags.duration_ns, tags['span.duration_ns'], tags.latency_ns, tags.elapsed_ns];
  for (const candidate of nsCandidates) {
    const parsed = tryPositive(candidate, 1 / 1_000_000);
    if (parsed > 0) {
      return parsed;
    }
  }

  const startNsCandidates = [
    span.start_time_unix_nano,
    span.start_unix_nano,
    span.start_ns,
    tags.start_time_unix_nano,
    tags.start_unix_nano,
    tags.start_ns,
  ];
  const endNsCandidates = [
    span.end_time_unix_nano,
    span.end_unix_nano,
    span.end_ns,
    tags.end_time_unix_nano,
    tags.end_unix_nano,
    tags.end_ns,
  ];
  for (const startRaw of startNsCandidates) {
    for (const endRaw of endNsCandidates) {
      const startNs = Number(startRaw);
      const endNs = Number(endRaw);
      if (Number.isFinite(startNs) && Number.isFinite(endNs) && endNs > startNs) {
        return (endNs - startNs) / 1_000_000;
      }
    }
  }

  return 0;
};

const TraceTimeline: React.FC<TraceTimelineProps> = ({ spans, selectedSpanId, onSpanClick }) => {
  const normalizedSpans = React.useMemo<Array<Span & { _start_ms: number; _duration_ms: number }>>(() => {
    return spans.map((span) => ({
      ...span,
      _start_ms: toEpochMs(span.start_time),
      _duration_ms: resolveSpanDurationMs(span),
    }));
  }, [spans]);

  // 计算时间范围
  const minTime = React.useMemo(() => {
    if (normalizedSpans.length === 0) return 0;
    return Math.min(...normalizedSpans.map((s) => s._start_ms));
  }, [normalizedSpans]);

  const maxTime = React.useMemo(() => {
    if (normalizedSpans.length === 0) return 0;
    return Math.max(...normalizedSpans.map((s) => s._start_ms + s._duration_ms));
  }, [normalizedSpans]);

  const totalDuration = React.useMemo(() => {
    return maxTime - minTime;
  }, [minTime, maxTime]);

  // 状态颜色
  const getStatusColor = React.useCallback((status: string) => {
    const baseColor = 'border-blue-300';
    if (status === 'STATUS_CODE_ERROR') return `${baseColor} bg-red-50 text-red-700`;
    if (status === 'STATUS_CODE_OK') return `${baseColor} bg-green-50 text-green-700`;
    return `${baseColor} bg-gray-50 text-gray-700`;
  }, []);

  // 构建层级树并渲染
  const renderSpanTree = React.useCallback(() => {
    const spanMap = new Map<string, SpanNode>();
    normalizedSpans.forEach((span) => {
      spanMap.set(span.span_id, { ...span, children: [], level: 0 });
    });

    // 建立父子关系
    normalizedSpans.forEach((span) => {
      const current = spanMap.get(span.span_id)!;
      if (span.parent_span_id && spanMap.has(span.parent_span_id)) {
        const parent = spanMap.get(span.parent_span_id)!;
        parent.children.push(current);
        current.level = parent.level + 1;
      }
    });

    // 获取根 spans
    const rootSpans = Array.from(spanMap.values()).filter(s => !s.parent_span_id);

    // 递归渲染函数
    const renderSpan = (span: SpanNode) => {
      const start = Number(span._start_ms ?? toEpochMs(span.start_time));
      const durationMs = Number(span._duration_ms ?? resolveSpanDurationMs(span));
      const offset = start - minTime;
      const timelineDuration = totalDuration > 0 ? totalDuration : 1;
      const offsetPercent = (offset / timelineDuration) * 100;
      const isSelected = selectedSpanId === span.span_id;
      const statusColor = getStatusColor(span.status);

      return (
        <div key={span.span_id} className="mb-2">
          {/* Span 条 */}
          <div
            onClick={() => onSpanClick(span)}
            className={`
              relative cursor-pointer rounded-lg border-2 p-2 transition-all hover:shadow-md
              ${statusColor} ${isSelected ? 'ring-2 ring-blue-500 z-10' : ''}
            `}
            style={{ marginLeft: `${span.level! * 20}px` }}
          >
            <div className="flex items-center justify-between">
              <div className="flex-1 min-w-0">
                <div className="text-xs font-semibold text-gray-900 truncate">
                  {span.operation_name}
                </div>
                <div className="text-xs text-gray-500">
                  {span.service_name}
                </div>
              </div>
              <div className="text-xs font-mono font-semibold">
                {formatDuration(durationMs)}
              </div>
            </div>

            {/* 时间轴指示器 */}
            <div
              className="absolute top-0 bottom-0 w-0.5 bg-gray-200"
              style={{ left: `${offsetPercent}%` }}
            />
          </div>

          {/* 子 Spans */}
          {span.children.map((child) => renderSpan(child))}
        </div>
      );
    };

    return (
      <div>
        {rootSpans.map((span) => renderSpan(span))}
      </div>
    );
  }, [normalizedSpans, selectedSpanId, minTime, totalDuration, getStatusColor, onSpanClick]);

  if (spans.length === 0) {
    return (
      <div className="bg-gray-50 rounded-lg p-8 text-center">
        <p className="text-sm text-gray-500">暂无 Span 数据</p>
      </div>
    );
  }

  return (
    <div className="bg-white rounded-lg shadow-md p-4">
      <h3 className="text-sm font-semibold text-gray-900 mb-4">Span 时间线</h3>

      {/* 时间轴刻度 */}
      <div className="relative mb-6">
        <div className="flex justify-between text-xs text-gray-400 mb-2">
          <span>{formatTimeCST(new Date(minTime).toISOString())}</span>
          <span className="font-medium">总时长: {formatDuration(totalDuration)}</span>
          <span>{formatTimeCST(new Date(maxTime).toISOString())}</span>
        </div>
        <div className="h-1 bg-gray-200 rounded" />
      </div>

      {/* Spans 渲染 */}
      <div className="max-h-[500px] overflow-y-auto">
        {renderSpanTree()}
      </div>

      {/* 图例 */}
      <div className="flex items-center gap-4 mt-4 pt-4 border-t border-gray-200">
        <div className="flex items-center gap-2">
          <div className="w-3 h-3 bg-green-50 border border-green-300 rounded" />
          <span className="text-xs text-gray-600">成功</span>
        </div>
        <div className="flex items-center gap-2">
          <div className="w-3 h-3 bg-red-50 border border-red-300 rounded" />
          <span className="text-xs text-gray-600">错误</span>
        </div>
        <div className="flex items-center gap-2">
          <div className="w-3 h-3 bg-gray-50 border border-gray-300 rounded" />
          <span className="text-xs text-gray-600">未设置</span>
        </div>
      </div>
    </div>
  );
};

export default TraceTimeline;
