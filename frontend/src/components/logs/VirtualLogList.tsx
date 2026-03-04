/**
 * 虚拟滚动日志列表组件
 * 
 * 使用 react-window 实现大数据量日志的高性能渲染
 * 支持 10000+ 条日志流畅滚动
 */
import React, { memo, useCallback, useMemo } from 'react';
import { FixedSizeList as List, ListChildComponentProps, ListOnItemsRenderedProps } from 'react-window';
import { ChevronRight, Network, BrainCircuit, Zap } from 'lucide-react';
import { formatTime } from '../../utils/formatters';

const LEVEL_COLORS: Record<string, { bg: string; text: string; border: string; dot: string; solid: string }> = {
  TRACE: { bg: 'bg-gray-100', text: 'text-gray-600', border: 'border-gray-300', dot: 'bg-gray-400', solid: '#9ca3af' },
  DEBUG: { bg: 'bg-indigo-100', text: 'text-indigo-700', border: 'border-indigo-300', dot: 'bg-indigo-500', solid: '#6366f1' },
  INFO: { bg: 'bg-blue-100', text: 'text-blue-700', border: 'border-blue-300', dot: 'bg-blue-500', solid: '#3b82f6' },
  WARN: { bg: 'bg-amber-100', text: 'text-amber-700', border: 'border-amber-300', dot: 'bg-amber-500', solid: '#f59e0b' },
  ERROR: { bg: 'bg-red-100', text: 'text-red-700', border: 'border-red-300', dot: 'bg-red-500', solid: '#ef4444' },
  FATAL: { bg: 'bg-red-200', text: 'text-red-800', border: 'border-red-400', dot: 'bg-red-600', solid: '#dc2626' },
};

export interface LogItem {
  id: string;
  timestamp: string;
  service_name: string;
  level: string;
  message: string;
  pod_name?: string;
  namespace?: string;
  node_name?: string;
  container_name?: string;
  container_id?: string;
  container_image?: string;
  pod_id?: string;
  trace_id?: string;
  span_id?: string;
  labels?: Record<string, string>;
  attributes?: Record<string, any>;
  log_meta?: {
    wrapped: boolean;
    stream?: string;
    collector_time?: string;
    line_count: number;
  };
  host_ip?: string;
}

export interface VirtualLogListProps {
  logs: LogItem[];
  height: number;
  columnTemplate: string;
  selectedLogId: string | null;
  onSelectLog: (logId: string) => void;
  onGoToTopology: (serviceName: string) => void;
  onGoToAIAnalysis: (log: LogItem) => void;
  onGoToTraces?: (traceId: string) => void;
  onNearEnd?: () => void;
  nearEndThreshold?: number;
}

interface RowData {
  logs: LogItem[];
  columnTemplate: string;
  selectedLogId: string | null;
  onSelectLog: (logId: string) => void;
  onGoToTopology: (serviceName: string) => void;
  onGoToAIAnalysis: (log: LogItem) => void;
  onGoToTraces?: (traceId: string) => void;
}

const ROW_HEIGHT = 44;
const TIME_CELL_CACHE_LIMIT = 5000;
const timeCellCache = new Map<string, string>();

function formatTimeCell(timestamp: string): string {
  const cached = timeCellCache.get(timestamp);
  if (cached) {
    return cached;
  }

  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) {
    return timestamp;
  }
  const datePart = date.toLocaleDateString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  });
  const timePart = date.toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
  const millis = String(date.getMilliseconds()).padStart(3, '0');
  const formatted = `${datePart} ${timePart}.${millis}`;

  if (timeCellCache.size >= TIME_CELL_CACHE_LIMIT) {
    const firstKey = timeCellCache.keys().next().value;
    if (firstKey) {
      timeCellCache.delete(firstKey);
    }
  }
  timeCellCache.set(timestamp, formatted);

  return formatted;
}

const LogRow = memo(({ index, style, data }: ListChildComponentProps<RowData>) => {
  const {
    logs,
    columnTemplate,
    selectedLogId,
    onSelectLog,
    onGoToTopology,
    onGoToAIAnalysis,
    onGoToTraces,
  } = data;
  
  const log = logs[index];
  const isSelected = selectedLogId === log.id;
  const levelColors = LEVEL_COLORS[log.level] || LEVEL_COLORS.INFO;
  const isTrace = log.trace_id || log.message?.includes('trace_id');
  const messageLines = String(log.message || '').split('\n');
  const messagePreview = messageLines[0] || '';
  const lineCount = log.log_meta?.line_count || log.attributes?.log_meta?.line_count || messageLines.length;
  const hasMultiline = lineCount > 1;

  return (
    <div
      style={style}
      onClick={() => onSelectLog(log.id)}
      className={`cursor-pointer transition-colors hover:bg-gray-50 border-b border-gray-100 ${
        isSelected ? 'bg-blue-50/60' : 'bg-white'
      }`}
    >
      <div
        className="grid gap-2 px-4 py-2.5 items-center h-full"
        style={{ gridTemplateColumns: columnTemplate }}
      >
        {/* 时间列 */}
        <div className="flex items-center gap-1">
          {isSelected ? (
            <ChevronRight className="w-3.5 h-3.5 text-blue-500 shrink-0" />
          ) : (
            <div className="w-3.5 shrink-0" />
          )}
          <span className="text-xs text-gray-600 font-mono whitespace-nowrap" title={formatTime(log.timestamp)}>
            {formatTimeCell(log.timestamp)}
          </span>
        </div>
        
        {/* 服务列 */}
        <button
          onClick={(e) => {
            e.stopPropagation();
            onGoToTopology(log.service_name);
          }}
          className="text-sm text-blue-600 font-medium truncate hover:underline text-left"
          title={log.service_name}
        >
          {log.service_name}
        </button>
        
        {/* Pod列 */}
        <div className="text-xs text-gray-600 font-mono truncate" title={log.pod_name}>
          {log.pod_name || '-'}
        </div>
        
        {/* 级别列 */}
        <div>
          <span
            className="inline-flex items-center gap-1 px-1.5 py-0.5 text-[10px] font-semibold rounded text-white"
            style={{ backgroundColor: levelColors.solid }}
          >
            <span className="w-1 h-1 rounded-full bg-white/70" />
            {log.level}
          </span>
        </div>
        
        {/* 消息列 */}
        <div
          className={`text-sm text-gray-700 break-words leading-relaxed truncate ${
            isTrace ? 'font-mono text-xs' : ''
          }`}
          title={log.message}
        >
          {isTrace ? (
            <span className="inline-flex items-center gap-2">
              <span className="px-1.5 py-0.5 bg-purple-100 text-purple-700 text-[10px] rounded font-medium">TRACE</span>
              <span className="truncate">
                {messagePreview}
                {hasMultiline ? `  (+${lineCount - 1} 行)` : ''}
              </span>
            </span>
          ) : (
            <span>
              {messagePreview}
              {hasMultiline ? `  (+${lineCount - 1} 行)` : ''}
            </span>
          )}
        </div>
        
        {/* 操作按钮列 */}
        <div className="flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
          <button
            onClick={(e) => {
              e.stopPropagation();
              onGoToTopology(log.service_name);
            }}
            className="p-1 text-gray-400 hover:text-blue-600 hover:bg-blue-50 rounded transition-colors"
            title="查看服务拓扑"
          >
            <Network className="w-3.5 h-3.5" />
          </button>
          <button
            onClick={(e) => {
              e.stopPropagation();
              onGoToAIAnalysis(log);
            }}
            className="p-1 text-gray-400 hover:text-purple-600 hover:bg-purple-50 rounded transition-colors"
            title="AI 智能分析"
          >
            <BrainCircuit className="w-3.5 h-3.5" />
          </button>
          {log.trace_id && onGoToTraces && (
            <button
              onClick={(e) => {
                e.stopPropagation();
                onGoToTraces(log.trace_id!);
              }}
              className="p-1 text-gray-400 hover:text-green-600 hover:bg-green-50 rounded transition-colors"
              title="查看追踪链路"
            >
              <Zap className="w-3.5 h-3.5" />
            </button>
          )}
        </div>
      </div>
    </div>
  );
});

LogRow.displayName = 'LogRow';

const VirtualLogList: React.FC<VirtualLogListProps> = ({
  logs,
  height,
  columnTemplate,
  selectedLogId,
  onSelectLog,
  onGoToTopology,
  onGoToAIAnalysis,
  onGoToTraces,
  onNearEnd,
  nearEndThreshold = 20,
}) => {
  const itemData = useMemo<RowData>(() => ({
    logs,
    columnTemplate,
    selectedLogId,
    onSelectLog,
    onGoToTopology,
    onGoToAIAnalysis,
    onGoToTraces,
  }), [
    logs,
    columnTemplate,
    selectedLogId,
    onSelectLog,
    onGoToTopology,
    onGoToAIAnalysis,
    onGoToTraces,
  ]);

  const handleItemsRendered = useCallback(({ visibleStopIndex }: ListOnItemsRenderedProps) => {
    if (!onNearEnd || logs.length === 0) {
      return;
    }
    if (visibleStopIndex >= Math.max(0, logs.length - nearEndThreshold)) {
      onNearEnd();
    }
  }, [logs.length, nearEndThreshold, onNearEnd]);

  return (
    <List
      height={height}
      itemCount={logs.length}
      itemSize={ROW_HEIGHT}
      width="100%"
      itemData={itemData}
      overscanCount={10}
      onItemsRendered={handleItemsRendered}
    >
      {LogRow}
    </List>
  );
};

export default VirtualLogList;
