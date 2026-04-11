/**
 * 日志浏览器页面 - 侧边栏上下文版本
 * 
 * 优化点：
 * 1. 修复Pod标签显示问题
 * 2. 采用侧边框形式展示上下文信息
 * 3. 侧边框显示全面的日志内容
 * 4. 上下文日志条数可选择
 * 5. Trace类型日志单行完整显示
 * 6. 健康检查日志过滤优化
 * 7. 实时日志流 (WebSocket)
 * 8. 统一跳转功能
 * 9. 数据导出功能（按当前筛选）
 */
import React, { useState, useMemo, useEffect, useRef, useCallback, useDeferredValue } from 'react';
import { useLocation } from 'react-router-dom';
import { useEvents, useLogFacets, useLogContext, useAnalyzeLog, useRealtimeLogs, useAggregatedLogs } from '../hooks/useApi';
import { useNavigation } from '../hooks/useNavigation';
import LoadingState from '../components/common/LoadingState';
import ErrorState from '../components/common/ErrorState';
import EmptyState from '../components/common/EmptyState';
import { AISuggestionCard } from '../components/common/AISuggestionCard';
import Tooltip from '../components/common/Tooltip';
import VirtualLogList from '../components/logs/VirtualLogList';
import AggregatedLogRow from '../components/logs/AggregatedLogRow';
import { api } from '../utils/api';
import type { AggregatedLogsParams, Event, LogsFacetQueryParams, LogsQueryParams } from '../utils/api';
import { extractEventRequestIds, extractEventTraceIds } from '../utils/logCorrelation';
import { copyTextToClipboard } from '../utils/clipboard';
import { formatTime } from '../utils/formatters';
import { exportLogsToCSV, exportToJSON, generateExportFilename } from '../utils/export';
import { resolveCanonicalServiceName } from '../utils/serviceName';
import {
  Search,
  RefreshCw,
  Download,
  X,
  Copy,
  ChevronLeft,
  ChevronDown,
  ChevronRight,
  Tag,
  LayoutGrid,
  PanelLeft,
  Server,
  Check,
  FilterX,
  MapPin,
  Clock,
  Sparkles,
  Activity,
  Radio,
  Pause,
  FileJson,
  FileSpreadsheet,
  GripVertical,
} from 'lucide-react';

type LogLevel = Event['level'];
const LOG_LEVELS: readonly LogLevel[] = ['TRACE', 'DEBUG', 'INFO', 'WARN', 'ERROR', 'FATAL'];
const isLogLevel = (value: string): value is LogLevel =>
  (LOG_LEVELS as readonly string[]).includes(value);
const PAGE_SIZE = 200;
const DEFAULT_LOGS_TIME_WINDOW = '1 HOUR';
const FALLBACK_LOGS_TIME_WINDOW = '6 HOUR';
type ResizableColumn = 'time' | 'service' | 'pod' | 'level' | 'action';
type ColumnWidths = Record<ResizableColumn, number>;
const DEFAULT_COLUMN_WIDTHS: ColumnWidths = {
  time: 210,
  service: 130,
  pod: 180,
  level: 80,
  action: 110,
};
const COLUMN_MIN_WIDTH: ColumnWidths = {
  time: 170,
  service: 100,
  pod: 140,
  level: 64,
  action: 90,
};
const COLUMN_MAX_WIDTH: ColumnWidths = {
  time: 360,
  service: 260,
  pod: 320,
  level: 140,
  action: 200,
};

interface LogEvent extends Event {
  host?: string;
  host_ip?: string;
  container?: string;
  context?: Record<string, unknown>;
  attributes: Event['attributes'] & {
    k8s?: Record<string, unknown>;
    labels?: Record<string, string>;
    trace_id?: string;
    traceId?: string;
    log_meta?: Event['log_meta'] | Record<string, unknown>;
  };
}

const asRecord = (value: unknown): Record<string, unknown> =>
  value && typeof value === 'object' ? (value as Record<string, unknown>) : {};

const asStringRecord = (value: unknown): Record<string, string> => {
  const record = asRecord(value);
  const result: Record<string, string> = {};
  Object.entries(record).forEach(([key, itemValue]) => {
    if (typeof itemValue === 'string') {
      result[key] = itemValue;
    }
  });
  return result;
};

const getErrorMessage = (error: unknown, fallback: string): string => {
  const record = asRecord(error);
  const message = record.message;
  if (typeof message === 'string' && message.trim()) {
    return message.trim();
  }
  return fallback;
};

const pickText = (...values: unknown[]): string => {
  for (const item of values) {
    if (!item) {
      continue;
    }
    const text = String(item).trim();
    if (text) {
      return text;
    }
  }
  return '-';
};

// 日志级别颜色配置
const LEVEL_COLORS: Record<string, { bg: string; text: string; border: string; dot: string; solid: string }> = {
  TRACE: { bg: 'bg-gray-100', text: 'text-gray-600', border: 'border-gray-300', dot: 'bg-gray-400', solid: '#9ca3af' },
  DEBUG: { bg: 'bg-indigo-100', text: 'text-indigo-700', border: 'border-indigo-300', dot: 'bg-indigo-500', solid: '#6366f1' },
  INFO: { bg: 'bg-blue-100', text: 'text-blue-700', border: 'border-blue-300', dot: 'bg-blue-500', solid: '#3b82f6' },
  WARN: { bg: 'bg-amber-100', text: 'text-amber-700', border: 'border-amber-300', dot: 'bg-amber-500', solid: '#f59e0b' },
  ERROR: { bg: 'bg-red-100', text: 'text-red-700', border: 'border-red-300', dot: 'bg-red-500', solid: '#ef4444' },
  FATAL: { bg: 'bg-red-200', text: 'text-red-800', border: 'border-red-400', dot: 'bg-red-600', solid: '#dc2626' },
};

function normalizeDisplayLevel(value: unknown): LogLevel {
  const raw = String(value || '').trim().toUpperCase();
  if (!raw) {
    return 'INFO';
  }
  const normalized = raw === 'WARNING' ? 'WARN' : raw;
  return isLogLevel(normalized) ? normalized : 'INFO';
}

// 标签颜色配置
const TAG_COLORS = [
  { bg: 'bg-blue-50', border: 'border-blue-200', text: 'text-blue-700', keyColor: 'text-blue-500' },
  { bg: 'bg-green-50', border: 'border-green-200', text: 'text-green-700', keyColor: 'text-green-500' },
  { bg: 'bg-purple-50', border: 'border-purple-200', text: 'text-purple-700', keyColor: 'text-purple-500' },
  { bg: 'bg-amber-50', border: 'border-amber-200', text: 'text-amber-700', keyColor: 'text-amber-500' },
  { bg: 'bg-pink-50', border: 'border-pink-200', text: 'text-pink-700', keyColor: 'text-pink-500' },
  { bg: 'bg-cyan-50', border: 'border-cyan-200', text: 'text-cyan-700', keyColor: 'text-cyan-500' },
  { bg: 'bg-indigo-50', border: 'border-indigo-200', text: 'text-indigo-700', keyColor: 'text-indigo-500' },
  { bg: 'bg-rose-50', border: 'border-rose-200', text: 'text-rose-700', keyColor: 'text-rose-500' },
];

// 获取标签颜色
function getTagColor(key: string) {
  let hash = 0;
  for (let i = 0; i < key.length; i++) {
    hash = ((hash << 5) - hash) + key.charCodeAt(i);
    hash = hash & hash;
  }
  return TAG_COLORS[Math.abs(hash) % TAG_COLORS.length];
}

// 提取 Pod 标签 - 修复版
function extractPodLabels(event: LogEvent): Record<string, string> {
  try {
    // 直接从 labels 字段获取（后端已解析）
    if (event.labels && typeof event.labels === 'object' && !Array.isArray(event.labels)) {
      return asStringRecord(event.labels);
    }

    const attributes = asRecord(event.attributes);
    const context = asRecord(event.context);
    const attributesK8s = asRecord(attributes.k8s);
    const contextK8s = asRecord(context.k8s);
    // 尝试多种可能的路径获取标签
    const labels = attributesK8s.labels ||
                   attributes.labels ||
                   contextK8s.labels ||
                   context.labels ||
                   {};

    return asStringRecord(labels);
  } catch (e) {
    console.error('Error extracting labels:', e);
    return {};
  }
}

// 提取主机信息
function extractHost(event: LogEvent): string {
  const attributes = asRecord(event.attributes);
  const context = asRecord(event.context);
  const attributesK8s = asRecord(attributes.k8s);
  const contextK8s = asRecord(context.k8s);
  return pickText(
    event.node_name,
    attributesK8s.node,
    attributes.host,
    contextK8s.node,
    context.host,
    event.host,
    event.host_ip,
  );
}

// 提取容器信息
function extractContainer(event: LogEvent): string {
  const attributes = asRecord(event.attributes);
  const context = asRecord(event.context);
  const attributesK8s = asRecord(attributes.k8s);
  const contextK8s = asRecord(context.k8s);
  return pickText(
    event.container_name,
    attributesK8s.container_name,
    attributes.container,
    contextK8s.container_name,
    context.container,
    event.container,
  );
}

// 提取命名空间
function extractNamespace(event: LogEvent): string {
  const attributes = asRecord(event.attributes);
  const context = asRecord(event.context);
  const attributesK8s = asRecord(attributes.k8s);
  const contextK8s = asRecord(context.k8s);
  return pickText(
    event.namespace,
    attributesK8s.namespace,
    attributes.namespace,
    contextK8s.namespace,
    context.namespace,
  );
}

function normalizeNamespaceValue(event: LogEvent): string {
  const raw = String(extractNamespace(event) || '').trim();
  if (!raw || raw === '-') {
    return 'unknown';
  }
  return raw;
}

function normalizeK8sFilterValue(value: string): string {
  const normalized = String(value || '').trim();
  if (!normalized) {
    return '';
  }
  const lowered = normalized.toLowerCase();
  if (normalized === '-' || lowered === 'unknown') {
    return '';
  }
  return normalized;
}

function extractLogMeta(event: LogEvent): { stream?: string; collector_time?: string; line_count?: number } {
  const fromAttributes = asRecord(asRecord(event.attributes).log_meta);
  const fromEvent = asRecord(event.log_meta);

  return {
    ...fromAttributes,
    ...fromEvent,
  };
}

function buildFallbackFacetCounts(events: LogEvent[]): { services: Record<string, number>; levels: Record<string, number>; namespaces: Record<string, number> } {
  const serviceCounts: Record<string, number> = {};
  const levelCounts: Record<string, number> = {};
  const namespaceCounts: Record<string, number> = {};

  events.forEach((event) => {
    const serviceName = resolveCanonicalServiceName(event?.service_name, event?.pod_name);
    if (serviceName) {
      serviceCounts[serviceName] = (serviceCounts[serviceName] || 0) + 1;
    }

    const rawLevel = String(event?.level || '').trim().toUpperCase();
    const levelName = rawLevel === 'WARNING' ? 'WARN' : rawLevel;
    if (levelName) {
      levelCounts[levelName] = (levelCounts[levelName] || 0) + 1;
    }

    const namespace = normalizeNamespaceValue(event);
    namespaceCounts[namespace] = (namespaceCounts[namespace] || 0) + 1;
  });

  return {
    services: serviceCounts,
    levels: levelCounts,
    namespaces: namespaceCounts,
  };
}

function formatCollectorTime(value?: string): string {
  if (!value) {
    return '-';
  }
  const ts = Date.parse(value);
  if (Number.isNaN(ts)) {
    return value;
  }
  return formatTime(value);
}

function resolveEdgeSideMeta(side?: Event['edge_side']): { label: string; className: string; description: string } | null {
  switch (side) {
    case 'source':
      return {
        label: '源端日志',
        className: 'border-cyan-200 bg-cyan-50 text-cyan-700',
        description: '当前日志来自链路源服务。',
      };
    case 'target':
      return {
        label: '目标端日志',
        className: 'border-amber-200 bg-amber-50 text-amber-700',
        description: '当前日志来自链路目标服务。',
      };
    case 'correlated':
      return {
        label: '关联日志',
        className: 'border-violet-200 bg-violet-50 text-violet-700',
        description: '当前日志通过链路相关候选规则命中。',
      };
    default:
      return null;
  }
}

function resolveEdgePrecisionMeta(log: Event, context?: TopologyJumpContext | null): { label: string; className: string; description: string } | null {
  const contextTraceIds = new Set((context?.traceIds || []).map((value) => String(value || '').trim()).filter(Boolean));
  const contextRequestIds = new Set((context?.requestIds || []).map((value) => String(value || '').trim()).filter(Boolean));
  const matchedTraceIds = extractEventTraceIds(log).filter((value) => contextTraceIds.has(value));
  const matchedRequestIds = extractEventRequestIds(log).filter((value) => contextRequestIds.has(value));

  if (!matchedTraceIds.length && !matchedRequestIds.length) {
    return null;
  }

  const parts: string[] = [];
  if (matchedTraceIds.length) {
    parts.push(`trace_id=${matchedTraceIds[0]}`);
  }
  if (matchedRequestIds.length) {
    parts.push(`request_id=${matchedRequestIds[0]}`);
  }

  return {
    label: '精确关联',
    className: 'border-emerald-200 bg-emerald-50 text-emerald-700',
    description: `当前日志通过 ${parts.join(' / ')} 精确落入拓扑关联结果。`,
  };
}

function resolveEdgeMatchMeta(kind?: Event['edge_match_kind']): { label: string; className: string; description: string } | null {
  switch (kind) {
    case 'source_mentions_target':
      return {
        label: '源端命中',
        className: 'border-cyan-200 bg-cyan-50 text-cyan-700',
        description: '源服务日志正文或属性中提到了目标服务。',
      };
    case 'target_mentions_source':
      return {
        label: '目标命中',
        className: 'border-amber-200 bg-amber-50 text-amber-700',
        description: '目标服务日志正文或属性中提到了源服务。',
      };
    case 'dual_text':
      return {
        label: '双边文本',
        className: 'border-violet-200 bg-violet-50 text-violet-700',
        description: '日志正文或属性中同时命中了源服务和目标服务。',
      };
    case 'source_service':
      return {
        label: '源端候选',
        className: 'border-sky-200 bg-sky-50 text-sky-700',
        description: '当前日志来自源服务，作为链路候选被纳入结果。',
      };
    case 'target_service':
      return {
        label: '目标候选',
        className: 'border-orange-200 bg-orange-50 text-orange-700',
        description: '当前日志来自目标服务，作为链路候选被纳入结果。',
      };
    case 'correlated_text':
      return {
        label: '关联候选',
        className: 'border-slate-200 bg-slate-50 text-slate-700',
        description: '当前日志通过源/目标文本相关性被纳入候选结果。',
      };
    default:
      return null;
  }
}

const SQL_KEYWORDS = new Set([
  'SELECT', 'FROM', 'WHERE', 'JOIN', 'INNER', 'LEFT', 'RIGHT', 'FULL', 'OUTER', 'ON',
  'GROUP', 'ORDER', 'BY', 'LIMIT', 'HAVING', 'DISTINCT', 'INSERT', 'INTO', 'VALUES',
  'UPDATE', 'SET', 'DELETE', 'UNION', 'ALL', 'AS',
]);

type HighlightMode = 'normal' | 'enhanced';
type HighlightTokenType = 'timestamp' | 'level' | 'sql' | 'class';

interface HighlightRenderOptions {
  mode: HighlightMode;
  onTokenClick?: (token: string, type: HighlightTokenType) => void;
}

interface TopologyJumpContext {
  sourceService?: string;
  targetService?: string;
  sourceNamespace?: string;
  targetNamespace?: string;
  timeWindow?: string;
  anchorTime?: string;
  traceIds?: string[];
  requestIds?: string[];
  correlationMode?: 'and' | 'or';
}

const TIMESTAMP_TOKEN_REGEX = /\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z)?\b/;
const LEVEL_TOKEN_REGEX = /\b(?:TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL)\b/i;
const CLASS_TOKEN_REGEX = /\b(?:[A-Za-z_][\w$]*\.){1,}[A-Za-z_][\w$]*(?:\([^)]+\))?\b/;
const TOKEN_SPLIT_REGEX = /(\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z)?\b|\b(?:TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL)\b|\b(?:SELECT|FROM|WHERE|JOIN|INNER|LEFT|RIGHT|FULL|OUTER|ON|GROUP|ORDER|BY|LIMIT|HAVING|DISTINCT|INSERT|INTO|VALUES|UPDATE|SET|DELETE|UNION|ALL|AS)\b|\b(?:[A-Za-z_][\w$]*\.){1,}[A-Za-z_][\w$]*(?:\([^)]+\))?\b)/gi;

function normalizeUrlValueList(...rawValues: Array<string | null | undefined>): string[] {
  const normalized: string[] = [];
  const seen = new Set<string>();
  for (const rawValue of rawValues) {
    const text = String(rawValue || '').trim();
    if (!text) {
      continue;
    }
    for (const part of text.split(',')) {
      const value = part.trim();
      if (!value || seen.has(value)) {
        continue;
      }
      seen.add(value);
      normalized.push(value);
    }
  }
  return normalized;
}

function normalizeTopologyJumpValue(rawValue: string | null | undefined): string | undefined {
  const value = String(rawValue || '').trim();
  if (!value) {
    return undefined;
  }
  if (['-', 'unknown', 'none', 'null', 'n/a'].includes(value.toLowerCase())) {
    return undefined;
  }
  return value;
}

function normalizeCorrelationMode(rawValue: string | null | undefined): 'and' | 'or' | undefined {
  const value = String(rawValue || '').trim().toLowerCase();
  if (value === 'or') {
    return 'or';
  }
  if (value === 'and') {
    return 'and';
  }
  return undefined;
}

function resolveTimeWindowRange(timeWindow: string, anchorTime?: string): { start: string; end: string } | null {
  const normalized = (timeWindow || '').trim().toUpperCase();
  const matched = normalized.match(/^(\d+)\s*(MINUTE|MINUTES|HOUR|HOURS|DAY|DAYS)$/);
  if (!matched) {
    return null;
  }

  const amount = Number(matched[1]);
  const unit = matched[2];
  if (!Number.isFinite(amount) || amount <= 0) {
    return null;
  }

  let ms = 0;
  if (unit.startsWith('MINUTE')) {
    ms = amount * 60 * 1000;
  } else if (unit.startsWith('HOUR')) {
    ms = amount * 60 * 60 * 1000;
  } else if (unit.startsWith('DAY')) {
    ms = amount * 24 * 60 * 60 * 1000;
  }

  if (ms <= 0) {
    return null;
  }

  const parsedAnchorTime = String(anchorTime || '').trim();
  const anchorDate = parsedAnchorTime ? new Date(parsedAnchorTime) : new Date();
  const end = Number.isNaN(anchorDate.getTime()) ? new Date() : anchorDate;
  const start = new Date(end.getTime() - ms);
  return {
    start: start.toISOString(),
    end: end.toISOString(),
  };
}

function toLocalDatetimeInputValue(isoValue: string): string {
  const raw = String(isoValue || '').trim();
  if (!raw) {
    return '';
  }

  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) {
    return '';
  }

  const pad = (value: number) => String(value).padStart(2, '0');
  const year = date.getFullYear();
  const month = pad(date.getMonth() + 1);
  const day = pad(date.getDate());
  const hour = pad(date.getHours());
  const minute = pad(date.getMinutes());
  return `${year}-${month}-${day}T${hour}:${minute}`;
}

function fromLocalDatetimeInputValue(localValue: string): string {
  const raw = String(localValue || '').trim();
  if (!raw) {
    return '';
  }

  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) {
    return '';
  }
  return date.toISOString();
}

function normalizeTimeRange(startIso: string, endIso: string): { start: string; end: string } {
  if (!startIso || !endIso) {
    return { start: startIso, end: endIso };
  }

  const startTs = Date.parse(startIso);
  const endTs = Date.parse(endIso);
  if (!Number.isFinite(startTs) || !Number.isFinite(endTs) || startTs <= endTs) {
    return { start: startIso, end: endIso };
  }

  return { start: endIso, end: startIso };
}

function extractTraceIdFromLog(event: LogEvent | null | undefined): string {
  return extractEventTraceIds(event as Event | null | undefined)[0] || '';
}

function compareLogEventsDesc(a: LogEvent, b: LogEvent): number {
  const aTimestamp = String(a?.timestamp || '');
  const bTimestamp = String(b?.timestamp || '');
  const aTs = Date.parse(aTimestamp);
  const bTs = Date.parse(bTimestamp);
  const aValid = Number.isFinite(aTs);
  const bValid = Number.isFinite(bTs);

  if (aValid && bValid && aTs !== bTs) {
    return bTs - aTs;
  }
  if (aValid !== bValid) {
    return aValid ? -1 : 1;
  }

  if (!aValid && !bValid && aTimestamp !== bTimestamp) {
    return bTimestamp.localeCompare(aTimestamp);
  }

  const aId = String(a?.id || '');
  const bId = String(b?.id || '');
  if (aId !== bId) {
    return bId.localeCompare(aId);
  }

  return 0;
}

function hashEventText(input: string): string {
  let hash = 2166136261;
  for (let i = 0; i < input.length; i += 1) {
    hash ^= input.charCodeAt(i);
    hash += (hash << 1) + (hash << 4) + (hash << 7) + (hash << 8) + (hash << 24);
  }
  return (hash >>> 0).toString(16).padStart(8, '0');
}

function buildLogEventIdentity(event: LogEvent): string {
  const stableId = String(event?.id || '').trim();
  const timestamp = String(event?.timestamp || '');
  const serviceName = resolveCanonicalServiceName(event?.service_name, event?.pod_name);
  const podName = String(event?.pod_name || '');
  const namespace = String(event?.namespace || '');
  const level = String(event?.level || '');
  const traceId = extractTraceIdFromLog(event);
  const message = String(event?.message || '');

  return `${stableId}|${timestamp}|${serviceName}|${podName}|${namespace}|${level}|${traceId}|${hashEventText(message)}`;
}

function getLevelTokenClass(levelToken: string): string {
  const normalized = levelToken.toUpperCase();
  if (normalized === 'ERROR' || normalized === 'FATAL') {
    return 'text-red-700 font-semibold';
  }
  if (normalized === 'WARN' || normalized === 'WARNING') {
    return 'text-amber-700 font-semibold';
  }
  if (normalized === 'DEBUG') {
    return 'text-indigo-700 font-semibold';
  }
  if (normalized === 'INFO') {
    return 'text-blue-700 font-semibold';
  }
  return 'text-slate-700 font-semibold';
}

function renderHighlightedToken(
  token: string,
  tokenType: HighlightTokenType,
  className: string,
  key: string,
  options: HighlightRenderOptions
): React.ReactNode {
  if (options.mode === 'enhanced' && options.onTokenClick) {
    return (
      <button
        key={key}
        type="button"
        onClick={() => options.onTokenClick?.(token, tokenType)}
        className={`${className} rounded px-0.5 -mx-0.5 hover:bg-slate-200/70 underline decoration-dotted underline-offset-2`}
        title={`点击快速过滤: ${token}`}
      >
        {token}
      </button>
    );
  }

  return (
    <span key={key} className={className}>
      {token}
    </span>
  );
}

function renderHighlightedLine(line: string, lineIndex: number, options: HighlightRenderOptions): React.ReactNode {
  const parts = line.split(TOKEN_SPLIT_REGEX);

  return parts.map((part, tokenIndex) => {
    if (!part) {
      return null;
    }

    const key = `l${lineIndex}-t${tokenIndex}`;
    if (TIMESTAMP_TOKEN_REGEX.test(part)) {
      return renderHighlightedToken(
        part,
        'timestamp',
        options.mode === 'enhanced' ? 'text-cyan-900 font-bold bg-cyan-100/80' : 'text-cyan-700 font-semibold',
        key,
        options
      );
    }

    if (LEVEL_TOKEN_REGEX.test(part)) {
      const levelClass = options.mode === 'enhanced'
        ? `${getLevelTokenClass(part)} bg-slate-200/70`
        : getLevelTokenClass(part);
      return renderHighlightedToken(part, 'level', levelClass, key, options);
    }

    if (CLASS_TOKEN_REGEX.test(part)) {
      return renderHighlightedToken(
        part,
        'class',
        options.mode === 'enhanced' ? 'text-emerald-800 font-semibold bg-emerald-100/70' : 'text-emerald-700',
        key,
        options
      );
    }

    if (SQL_KEYWORDS.has(part.toUpperCase())) {
      return renderHighlightedToken(
        part,
        'sql',
        options.mode === 'enhanced' ? 'text-violet-900 font-bold bg-violet-100/80' : 'text-violet-700 font-semibold',
        key,
        options
      );
    }

    return <React.Fragment key={key}>{part}</React.Fragment>;
  });
}

function renderHighlightedLogMessage(message: string, options: HighlightRenderOptions): React.ReactNode {
  const lines = String(message || '').split('\n');

  return lines.map((line, lineIndex) => (
    <React.Fragment key={`line-${lineIndex}`}>
      {renderHighlightedLine(line, lineIndex, options)}
      {lineIndex < lines.length - 1 ? '\n' : ''}
    </React.Fragment>
  ));
}

// 检查日志是否匹配选中的标签
function matchesSelectedLabels(event: LogEvent, selectedLabels: Record<string, string[]>): boolean {
  if (Object.keys(selectedLabels).length === 0) return true;
  
  const labels = extractPodLabels(event);
  return Object.entries(selectedLabels).every(([key, values]) => {
    if (values.length === 0) return true;
    return values.includes(labels[key]);
  });
}

const LogsExplorer: React.FC = () => {
  const tableRef = useRef<HTMLDivElement>(null);
  const location = useLocation();
  const navigation = useNavigation();

  // ========== 状态管理 ==========
  
  // 实时模式状态
  const [realtimeMode, setRealtimeMode] = useState(false);
  const [logsViewMode, setLogsViewMode] = useState<'stream' | 'pattern'>('stream');
  
  // 搜索和筛选状态
  const [searchQuery, setSearchQuery] = useState('');
  const deferredSearchQuery = useDeferredValue(searchQuery.trim().toLowerCase());
  const [debouncedSearchQuery, setDebouncedSearchQuery] = useState('');
  const [selectedLevels, setSelectedLevels] = useState<string[]>([]);
  const [selectedServices, setSelectedServices] = useState<string[]>([]);
  const [selectedNamespaces, setSelectedNamespaces] = useState<string[]>([]);
  const [selectedContainers, setSelectedContainers] = useState<string[]>([]);
  const [traceIdFilter, setTraceIdFilter] = useState('');
  const [requestIdFilter, setRequestIdFilter] = useState('');
  const [podNameFilter, setPodNameFilter] = useState('');
  const [selectedHosts, setSelectedHosts] = useState<string[]>([]);
  const [selectedLabels, setSelectedLabels] = useState<Record<string, string[]>>({});
  const [serviceSearchQuery, setServiceSearchQuery] = useState('');
  const [namespaceSearchQuery, setNamespaceSearchQuery] = useState('');
  
  // UI 状态
  const [expandedLogId, setExpandedLogId] = useState<string | null>(null);
  const [selectedLogOverride, setSelectedLogOverride] = useState<LogEvent | null>(null);
  const [showFilterPanel, setShowFilterPanel] = useState(true);
  const [filterPanelCollapsed, setFilterPanelCollapsed] = useState(false);
  const [filterPanelWidth, setFilterPanelWidth] = useState(260);
  const [columnWidths, setColumnWidths] = useState<ColumnWidths>(DEFAULT_COLUMN_WIDTHS);
  const [isResizing, setIsResizing] = useState(false);
  const [copyNotice, setCopyNotice] = useState<string | null>(null);
  
  // 侧边栏状态
  const [showSidebar, setShowSidebar] = useState(false);
  const [sidebarWidth, setSidebarWidth] = useState(450);
  const [sidebarTab, setSidebarTab] = useState<'context' | 'detail' | 'json' | 'ai'>('context');
  const [contextBeforeCount, setContextBeforeCount] = useState(5);
  const [contextAfterCount, setContextAfterCount] = useState(5);

  // AI 分析状态
  const aiAnalysis = useAnalyzeLog();
  const [aiMode, setAiMode] = useState<'log' | 'trace'>('log');
  const [aiUseLLM, setAiUseLLM] = useState(true);
  const [savingAiCase, setSavingAiCase] = useState(false);
  const [aiCaseNotice, setAiCaseNotice] = useState<string | null>(null);
  
  // 详情面板状态
  const [wordWrap, setWordWrap] = useState(true);
  const [highlightMode, setHighlightMode] = useState<HighlightMode>('normal');
  const [topologyJumpContext, setTopologyJumpContext] = useState<TopologyJumpContext | null>(null);

  // 时间筛选状态
  const [startTime, setStartTime] = useState<string>('');
  const [endTime, setEndTime] = useState<string>('');
  const [showTimeFilter, setShowTimeFilter] = useState(false);
  
  // 筛选框折叠状态
  const [collapsedFilters, setCollapsedFilters] = useState<Record<string, boolean>>({
    levels: false,
    services: true,
    namespaces: true,
    hosts: true,
    labels: true,
  });

  // 可用选项
  const [availableServices, setAvailableServices] = useState<string[]>([]);
  const [serviceCountMap, setServiceCountMap] = useState<Record<string, number>>({});
  const [availableNamespaces, setAvailableNamespaces] = useState<string[]>([]);
  const [namespaceCountMap, setNamespaceCountMap] = useState<Record<string, number>>({});
  const [levelCountMap, setLevelCountMap] = useState<Record<string, number>>({});
  const [availableHosts, setAvailableHosts] = useState<string[]>([]);
  const [availableLabels, setAvailableLabels] = useState<Record<string, string[]>>({});

  // 健康检查过滤与导出
  const [excludeHealthCheck, setExcludeHealthCheck] = useState(false);
  const [showExportMenu, setShowExportMenu] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [pagedEvents, setPagedEvents] = useState<LogEvent[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [anchorTime, setAnchorTime] = useState<string | null>(null);
  const [correlationTraceIds, setCorrelationTraceIds] = useState<string[]>([]);
  const [correlationRequestIds, setCorrelationRequestIds] = useState<string[]>([]);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadedPageCount, setLoadedPageCount] = useState(0);
  const [autoExpandedWindow, setAutoExpandedWindow] = useState(false);
  const loadingMoreRef = useRef(false);
  const effectiveDefaultTimeWindow = autoExpandedWindow ? FALLBACK_LOGS_TIME_WINDOW : DEFAULT_LOGS_TIME_WINDOW;

  const applyTimeRange = useCallback((nextStart: string, nextEnd: string) => {
    const normalized = normalizeTimeRange(nextStart, nextEnd);
    setStartTime(normalized.start);
    setEndTime(normalized.end);
  }, []);

  // 根据 URL 参数初始化筛选器，支持日志/拓扑/AI 页面直接跳转定位
  useEffect(() => {
    const params = new URLSearchParams(location.search);
    const service = params.get('service');
    const level = params.get('level');
    const search = params.get('search');
    const traceId = params.get('trace_id');
    const traceIds = normalizeUrlValueList(params.get('trace_ids'));
    const requestId = params.get('request_id');
    const requestIds = normalizeUrlValueList(params.get('request_ids'));
    const pod = params.get('pod');
    const namespace = params.get('namespace');
    const logId = params.get('id');
    const sourceService = normalizeTopologyJumpValue(params.get('source_service'));
    const targetService = normalizeTopologyJumpValue(params.get('target_service'));
    const sourceNamespace = normalizeTopologyJumpValue(params.get('source_namespace'));
    const targetNamespace = normalizeTopologyJumpValue(params.get('target_namespace'));
    const timeWindow = normalizeTopologyJumpValue(params.get('time_window'));
    const correlationMode = normalizeCorrelationMode(params.get('correlation_mode'));
    const jumpAnchorTime = params.get('anchor_time') || params.get('ts') || '';
    const excludeHealthCheckParam = (params.get('exclude_health_check') || '').trim().toLowerCase();
    const shouldExcludeHealthCheck = ['1', 'true', 'yes', 'on'].includes(excludeHealthCheckParam);

    const normalizedLevel = (level || '').toUpperCase();

    setSelectedServices(service ? [service] : []);
    setSelectedNamespaces(namespace ? [namespace] : []);
    setSelectedLevels(isLogLevel(normalizedLevel) ? [normalizedLevel] : []);
    setSearchQuery(search || '');
    setTraceIdFilter(traceId || '');
    setCorrelationTraceIds(traceIds);
    setRequestIdFilter(requestId || '');
    setCorrelationRequestIds(requestIds);
    setPodNameFilter(pod || '');
    setExcludeHealthCheck(shouldExcludeHealthCheck);

    if (sourceService || targetService || timeWindow || traceIds.length > 0 || requestIds.length > 0 || jumpAnchorTime) {
      setTopologyJumpContext({
        sourceService,
        targetService,
        sourceNamespace,
        targetNamespace,
        timeWindow,
        anchorTime: jumpAnchorTime || undefined,
        traceIds,
        requestIds,
        correlationMode,
      });
    } else {
      setTopologyJumpContext(null);
    }

    if (timeWindow) {
      const range = resolveTimeWindowRange(timeWindow, jumpAnchorTime || undefined);
      if (range) {
        setStartTime(range.start);
        setEndTime(range.end);
      }
    }
    setAnchorTime(jumpAnchorTime || null);

    if (logId) {
      setExpandedLogId(logId);
      setShowSidebar(true);
      setSidebarTab('detail');
    } else {
      setExpandedLogId(null);
    }
  }, [location.search]);

  useEffect(() => {
    const timer = setTimeout(() => {
      setDebouncedSearchQuery(searchQuery.trim());
    }, 400);
    return () => clearTimeout(timer);
  }, [searchQuery]);

  // ========== 数据获取 ==========
  const hasExplicitServerFilters = Boolean(
    selectedLevels.length > 0
    || selectedServices.length > 0
    || selectedNamespaces.length > 0
    || selectedContainers.length > 0
    || traceIdFilter
    || correlationTraceIds.length > 0
    || requestIdFilter
    || correlationRequestIds.length > 0
    || podNameFilter
    || debouncedSearchQuery
    || startTime
    || endTime
    || topologyJumpContext
  );
  const isPatternMode = logsViewMode === 'pattern';
  const isStreamMode = logsViewMode === 'stream';
  const hasPreciseCorrelationFilters = Boolean(
    traceIdFilter || requestIdFilter || correlationTraceIds.length > 0 || correlationRequestIds.length > 0,
  );
  
  const apiParams = useMemo(() => {
    const params: LogsQueryParams = { limit: PAGE_SIZE };
    if (selectedLevels.length === 1) params.level = selectedLevels[0];
    if (selectedLevels.length > 1) params.levels = selectedLevels.join(',');
    if (selectedServices.length === 1) params.service_name = selectedServices[0];
    if (selectedServices.length > 1) params.service_names = selectedServices.join(',');
    if (selectedNamespaces.length === 1) params.namespace = selectedNamespaces[0];
    if (selectedNamespaces.length > 1) params.namespaces = selectedNamespaces.join(',');
    if (selectedContainers.length === 1) params.container_name = selectedContainers[0];
    if (traceIdFilter) params.trace_id = traceIdFilter;
    if (correlationTraceIds.length > 0) params.trace_ids = correlationTraceIds.join(',');
    if (requestIdFilter) params.request_id = requestIdFilter;
    if (correlationRequestIds.length > 0) params.request_ids = correlationRequestIds.join(',');
    if (podNameFilter) params.pod_name = podNameFilter;
    if (debouncedSearchQuery) params.search = debouncedSearchQuery;
    if (startTime) params.start_time = startTime;
    if (endTime) params.end_time = endTime;
    if (excludeHealthCheck) params.exclude_health_check = true;
    if (anchorTime) params.anchor_time = anchorTime;
    if (topologyJumpContext) {
      if (!hasPreciseCorrelationFilters && topologyJumpContext.sourceService) params.source_service = topologyJumpContext.sourceService;
      if (!hasPreciseCorrelationFilters && topologyJumpContext.targetService) params.target_service = topologyJumpContext.targetService;
      if (!hasPreciseCorrelationFilters && topologyJumpContext.sourceNamespace) params.source_namespace = topologyJumpContext.sourceNamespace;
      if (!hasPreciseCorrelationFilters && topologyJumpContext.targetNamespace) params.target_namespace = topologyJumpContext.targetNamespace;
      if (topologyJumpContext.timeWindow) params.time_window = topologyJumpContext.timeWindow;
      if (topologyJumpContext.correlationMode) params.correlation_mode = topologyJumpContext.correlationMode;
    } else if (!startTime && !endTime) {
      params.time_window = effectiveDefaultTimeWindow;
    }
    return params;
  }, [selectedLevels, selectedServices, selectedNamespaces, selectedContainers, traceIdFilter, correlationTraceIds, requestIdFilter, correlationRequestIds, podNameFilter, debouncedSearchQuery, startTime, endTime, excludeHealthCheck, anchorTime, topologyJumpContext, hasPreciseCorrelationFilters, effectiveDefaultTimeWindow]);

  const { data, loading, error, refetch } = useEvents(apiParams);
  const aggregatedParams = useMemo(() => {
    const streamSafeMinPatternCount = 100;
    const params: AggregatedLogsParams = {
      limit: isPatternMode ? 2000 : 10,
      min_pattern_count: isPatternMode ? 2 : streamSafeMinPatternCount,
      max_patterns: isPatternMode ? 120 : 1,
      max_samples: isPatternMode ? 5 : 1,
    };
    if (selectedLevels.length === 1) params.level = selectedLevels[0];
    if (selectedLevels.length > 1) params.levels = selectedLevels.join(',');
    if (selectedServices.length === 1) params.service_name = selectedServices[0];
    if (selectedServices.length > 1) params.service_names = selectedServices.join(',');
    if (selectedNamespaces.length === 1) params.namespace = selectedNamespaces[0];
    if (selectedNamespaces.length > 1) params.namespaces = selectedNamespaces.join(',');
    if (selectedContainers.length === 1) params.container_name = selectedContainers[0];
    if (traceIdFilter) params.trace_id = traceIdFilter;
    if (correlationTraceIds.length > 0) params.trace_ids = correlationTraceIds.join(',');
    if (requestIdFilter) params.request_id = requestIdFilter;
    if (correlationRequestIds.length > 0) params.request_ids = correlationRequestIds.join(',');
    if (podNameFilter) params.pod_name = podNameFilter;
    if (debouncedSearchQuery) params.search = debouncedSearchQuery;
    if (startTime) params.start_time = startTime;
    if (endTime) params.end_time = endTime;
    if (excludeHealthCheck) params.exclude_health_check = true;
    if (anchorTime) params.anchor_time = anchorTime;
    if (topologyJumpContext) {
      if (!hasPreciseCorrelationFilters && topologyJumpContext.sourceService) params.source_service = topologyJumpContext.sourceService;
      if (!hasPreciseCorrelationFilters && topologyJumpContext.targetService) params.target_service = topologyJumpContext.targetService;
      if (!hasPreciseCorrelationFilters && topologyJumpContext.sourceNamespace) params.source_namespace = topologyJumpContext.sourceNamespace;
      if (!hasPreciseCorrelationFilters && topologyJumpContext.targetNamespace) params.target_namespace = topologyJumpContext.targetNamespace;
      if (topologyJumpContext.timeWindow) params.time_window = topologyJumpContext.timeWindow;
      if (topologyJumpContext.correlationMode) params.correlation_mode = topologyJumpContext.correlationMode;
    } else if (!startTime && !endTime) {
      params.time_window = effectiveDefaultTimeWindow;
    }
    return params;
  }, [
    isPatternMode,
    selectedLevels,
    selectedServices,
    selectedNamespaces,
    selectedContainers,
    traceIdFilter,
    correlationTraceIds,
    requestIdFilter,
    correlationRequestIds,
    podNameFilter,
    debouncedSearchQuery,
    startTime,
    endTime,
    excludeHealthCheck,
    anchorTime,
    topologyJumpContext,
    hasPreciseCorrelationFilters,
    effectiveDefaultTimeWindow,
  ]);
  const {
    data: aggregatedData,
    loading: aggregatedLoading,
    error: aggregatedError,
    refetch: refetchAggregated,
  } = useAggregatedLogs(aggregatedParams);
  const aggregatedPatterns = useMemo(
    () => (Array.isArray(aggregatedData?.patterns) ? aggregatedData.patterns : []),
    [aggregatedData],
  );
  const facetParams = useMemo(() => {
    const params: LogsFacetQueryParams = {};
    if (selectedLevels.length === 1) params.level = selectedLevels[0];
    if (selectedLevels.length > 1) params.levels = selectedLevels.join(',');
    if (selectedServices.length === 1) params.service_name = selectedServices[0];
    if (selectedServices.length > 1) params.service_names = selectedServices.join(',');
    if (selectedNamespaces.length === 1) params.namespace = selectedNamespaces[0];
    if (selectedNamespaces.length > 1) params.namespaces = selectedNamespaces.join(',');
    if (selectedContainers.length === 1) params.container_name = selectedContainers[0];
    if (traceIdFilter) params.trace_id = traceIdFilter;
    if (correlationTraceIds.length > 0) params.trace_ids = correlationTraceIds.join(',');
    if (requestIdFilter) params.request_id = requestIdFilter;
    if (correlationRequestIds.length > 0) params.request_ids = correlationRequestIds.join(',');
    if (podNameFilter) params.pod_name = podNameFilter;
    if (debouncedSearchQuery) params.search = debouncedSearchQuery;
    if (startTime) params.start_time = startTime;
    if (endTime) params.end_time = endTime;
    if (excludeHealthCheck) params.exclude_health_check = true;
    if (anchorTime) params.anchor_time = anchorTime;
    if (topologyJumpContext) {
      if (!hasPreciseCorrelationFilters && topologyJumpContext.sourceService) params.source_service = topologyJumpContext.sourceService;
      if (!hasPreciseCorrelationFilters && topologyJumpContext.targetService) params.target_service = topologyJumpContext.targetService;
      if (!hasPreciseCorrelationFilters && topologyJumpContext.sourceNamespace) params.source_namespace = topologyJumpContext.sourceNamespace;
      if (!hasPreciseCorrelationFilters && topologyJumpContext.targetNamespace) params.target_namespace = topologyJumpContext.targetNamespace;
      if (topologyJumpContext.timeWindow) params.time_window = topologyJumpContext.timeWindow;
      if (topologyJumpContext.correlationMode) params.correlation_mode = topologyJumpContext.correlationMode;
    } else if (!startTime && !endTime) {
      params.time_window = effectiveDefaultTimeWindow;
    }
    params.limit_services = 300;
    params.limit_namespaces = 300;
    params.limit_levels = 20;
    return params;
  }, [selectedLevels, selectedServices, selectedNamespaces, selectedContainers, traceIdFilter, correlationTraceIds, requestIdFilter, correlationRequestIds, podNameFilter, debouncedSearchQuery, startTime, endTime, excludeHealthCheck, anchorTime, topologyJumpContext, hasPreciseCorrelationFilters, effectiveDefaultTimeWindow]);
  const { data: facetsData } = useLogFacets(facetParams);

  useEffect(() => {
    if (!data) {
      return;
    }
    setPagedEvents(data.events || []);
    setNextCursor(data.next_cursor || null);
    setAnchorTime(data.anchor_time || null);
    setLoadedPageCount((data.events || []).length > 0 ? 1 : 0);
  }, [data]);

  useEffect(() => {
    if (hasExplicitServerFilters) {
      if (autoExpandedWindow) {
        setAutoExpandedWindow(false);
      }
      return;
    }

    if (!loading && data && (data.events || []).length === 0 && !autoExpandedWindow) {
      setAutoExpandedWindow(true);
    }
  }, [hasExplicitServerFilters, autoExpandedWindow, loading, data]);

  // 实时日志流
  const realtimeFilters = useMemo(() => ({
    service_name: selectedServices.length === 1 ? selectedServices[0] : undefined,
    namespace: selectedNamespaces.length === 1 ? selectedNamespaces[0] : undefined,
    container_name: selectedContainers.length === 1 ? selectedContainers[0] : undefined,
    level: selectedLevels.length === 1 ? selectedLevels[0] : undefined,
    exclude_health_check: excludeHealthCheck,
  }), [selectedServices, selectedNamespaces, selectedContainers, selectedLevels, excludeHealthCheck]);

  const {
    logs: realtimeLogs,
    isConnected: realtimeConnected,
    clearLogs: clearRealtimeLogs,
  } = useRealtimeLogs({
    enabled: realtimeMode && isStreamMode,
    maxLogs: 500,
    filters: realtimeFilters,
  });

  useEffect(() => {
    if (!isPatternMode || !realtimeMode) {
      return;
    }
    setRealtimeMode(false);
    clearRealtimeLogs();
  }, [isPatternMode, realtimeMode, clearRealtimeLogs]);

  // 合并实时日志和静态日志
  const allEvents = useMemo(() => {
    const staticEvents = pagedEvents;
    if (!realtimeMode || realtimeLogs.length === 0) {
      return staticEvents;
    }

    const merged = new Map<string, LogEvent>();
    [...(realtimeLogs as LogEvent[]), ...staticEvents].forEach((event) => {
      const key = buildLogEventIdentity(event);
      if (!merged.has(key)) {
        merged.set(key, event);
      }
    });

    return Array.from(merged.values()).sort(compareLogEventsDesc);
  }, [realtimeMode, realtimeLogs, pagedEvents]);

  // 获取当前选中日志的上下文
  const currentSelectedLog = useMemo(() => {
    if (!expandedLogId) return null;
    const matched = allEvents.find((e) => e.id === expandedLogId);
    if (matched) {
      return matched;
    }
    if (selectedLogOverride && selectedLogOverride.id === expandedLogId) {
      return selectedLogOverride;
    }
    return null;
  }, [expandedLogId, allEvents, selectedLogOverride]);
  const selectedTraceId = useMemo(() => extractTraceIdFromLog(currentSelectedLog), [currentSelectedLog]);

  useEffect(() => {
    if (aiMode === 'trace' && !selectedTraceId) {
      setAiMode('log');
    }
  }, [aiMode, selectedTraceId]);

  useEffect(() => {
    setAiCaseNotice(null);
  }, [expandedLogId, aiMode, aiAnalysis.data]);

  const logContextParams = useMemo(() => {
    if (!currentSelectedLog) return null;
    const resolvedLogId = String(currentSelectedLog.id || '').trim();
    const canUseExactLogId = Boolean(resolvedLogId) && !resolvedLogId.startsWith('evt-');
    const resolvedPodName = String(currentSelectedLog.pod_name || '').trim();
    const resolvedNamespace = String(currentSelectedLog.namespace || '').trim();
    const resolvedContainerName = String(
      currentSelectedLog.container_name
      || currentSelectedLog.attributes?.k8s?.container_name
      || '',
    ).trim();
    // 优先使用 log_id 精确锚定；pod_name/timestamp 作为兜底模式。
    return {
      log_id: canUseExactLogId ? resolvedLogId : undefined,
      pod_name: resolvedPodName && resolvedPodName.toLowerCase() !== 'unknown' ? resolvedPodName : undefined,
      namespace: resolvedNamespace && resolvedNamespace.toLowerCase() !== 'unknown' ? resolvedNamespace : undefined,
      container_name: resolvedContainerName && resolvedContainerName.toLowerCase() !== 'unknown'
        ? resolvedContainerName
        : undefined,
      timestamp: currentSelectedLog.timestamp,
      before_count: contextBeforeCount,
      after_count: contextAfterCount,
    };
  }, [currentSelectedLog, contextBeforeCount, contextAfterCount]);

  const { data: logContextData, loading: logContextLoading } = useLogContext(logContextParams);
  const contextCurrentMatches = useMemo<LogEvent[]>(() => {
    const matches = Array.isArray(logContextData?.current_matches) ? logContextData.current_matches : [];
    return matches
      .map((item) => ({
        ...item,
        level: normalizeDisplayLevel(item.level),
      }) as LogEvent)
      .filter((item) => Boolean(item?.id));
  }, [logContextData?.current_matches]);

  const contextCurrentLog = useMemo<LogEvent | null>(() => {
    if (!currentSelectedLog) {
      return null;
    }
    const current = logContextData?.current;
    if (!current || typeof current !== 'object') {
      return {
        ...currentSelectedLog,
        level: normalizeDisplayLevel(currentSelectedLog.level),
      } as LogEvent;
    }

    return {
      ...currentSelectedLog,
      ...current,
      id: String(current.id || currentSelectedLog.id || ''),
      timestamp: String(current.timestamp || currentSelectedLog.timestamp || ''),
      level: normalizeDisplayLevel(current.level || currentSelectedLog.level),
      message: String(current.message || currentSelectedLog.message || ''),
      service_name: resolveCanonicalServiceName(
        current.service_name || currentSelectedLog.service_name,
        current.pod_name || currentSelectedLog.pod_name,
      ),
    } as LogEvent;
  }, [currentSelectedLog, logContextData]);

  // 点击外部关闭时间筛选器
  const timeFilterRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (timeFilterRef.current && !timeFilterRef.current.contains(event.target as Node)) {
        setShowTimeFilter(false);
      }
    };

    if (showTimeFilter) {
      document.addEventListener('mousedown', handleClickOutside);
      return () => document.removeEventListener('mousedown', handleClickOutside);
    }
  }, [showTimeFilter]);

  // ========== 数据提取与处理 ==========
  
  useEffect(() => {
    const hasFacetServices = Boolean(facetsData?.services && facetsData.services.length > 0);
    const hasFacetNamespaces = Boolean(facetsData?.namespaces && facetsData.namespaces.length > 0);
    const hasFacetLevels = Boolean(facetsData?.levels && facetsData.levels.length > 0);
    const fallbackEvents = allEvents.length > 0 ? allEvents : pagedEvents;
    const fallbackCounts = buildFallbackFacetCounts(fallbackEvents);
    const fallbackServiceTotal = Object.values(fallbackCounts.services).reduce((sum, count) => sum + Number(count || 0), 0);
    const fallbackNamespaceTotal = Object.values(fallbackCounts.namespaces).reduce((sum, count) => sum + Number(count || 0), 0);
    const fallbackKnownLevelTotal = LOG_LEVELS.reduce(
      (sum, level) => sum + Number(fallbackCounts.levels[level] || 0),
      0,
    );

    if (hasFacetServices) {
      const facetServiceTotal = facetsData!.services.reduce(
        (sum: number, item) => sum + Number(item?.count || 0),
        0,
      );
      const nextServiceCounts: Record<string, number> = {};
      facetsData!.services.forEach((item) => {
        const key = String(item?.value || '').trim();
        if (!key) {
          return;
        }
        nextServiceCounts[key] = Number(item?.count || 0);
      });
      if (facetServiceTotal > 0 || fallbackServiceTotal <= 0) {
        setAvailableServices(facetsData!.services.map((item) => item.value));
        setServiceCountMap(nextServiceCounts);
      } else {
        setAvailableServices(Object.keys(fallbackCounts.services).sort());
        setServiceCountMap(fallbackCounts.services);
      }
    } else {
      setAvailableServices(Object.keys(fallbackCounts.services).sort());
      setServiceCountMap(fallbackCounts.services);
    }

    if (hasFacetNamespaces) {
      const facetNamespaceTotal = facetsData!.namespaces.reduce(
        (sum: number, item) => sum + Number(item?.count || 0),
        0,
      );
      const nextNamespaceCounts: Record<string, number> = {};
      facetsData!.namespaces.forEach((item) => {
        const key = String(item?.value || '').trim();
        if (!key) {
          return;
        }
        nextNamespaceCounts[key] = Number(item?.count || 0);
      });
      if (facetNamespaceTotal > 0 || fallbackNamespaceTotal <= 0) {
        setAvailableNamespaces(facetsData!.namespaces.map((item) => item.value));
        setNamespaceCountMap(nextNamespaceCounts);
      } else {
        setAvailableNamespaces(Object.keys(fallbackCounts.namespaces).sort());
        setNamespaceCountMap(fallbackCounts.namespaces);
      }
    } else {
      setAvailableNamespaces(Object.keys(fallbackCounts.namespaces).sort());
      setNamespaceCountMap(fallbackCounts.namespaces);
    }

    if (hasFacetLevels) {
      const facetKnownLevelTotal = facetsData!.levels.reduce((sum: number, item) => {
        const key = String(item?.value || '').trim().toUpperCase();
        const normalizedKey = key === 'WARNING' ? 'WARN' : key;
        if (!isLogLevel(normalizedKey)) {
          return sum;
        }
        return sum + Number(item?.count || 0);
      }, 0);
      const nextLevelCounts: Record<string, number> = {};
      facetsData!.levels.forEach((item) => {
        const key = String(item?.value || '').trim().toUpperCase();
        if (!key) {
          return;
        }
        const normalizedKey = key === 'WARNING' ? 'WARN' : key;
        nextLevelCounts[normalizedKey] = Number(item?.count || 0);
      });
      if (facetKnownLevelTotal > 0 || fallbackKnownLevelTotal <= 0) {
        setLevelCountMap(nextLevelCounts);
      } else {
        setLevelCountMap(fallbackCounts.levels);
      }
    } else {
      setLevelCountMap(fallbackCounts.levels);
    }
  }, [facetsData, pagedEvents, allEvents]);

  useEffect(() => {
    const sourceEvents = allEvents.length > 0 ? allEvents : pagedEvents;
    if (sourceEvents.length > 0) {
      const services = new Set<string>();
      const namespaces = new Set<string>();
      const hosts = new Set<string>();
      const labelsMap: Record<string, Set<string>> = {};

      sourceEvents.forEach((event) => {
        services.add(resolveCanonicalServiceName(event?.service_name, event?.pod_name));
        namespaces.add(normalizeNamespaceValue(event));
        hosts.add(extractHost(event));
        
        const labels = extractPodLabels(event);
        Object.entries(labels).forEach(([key, value]) => {
          if (!labelsMap[key]) labelsMap[key] = new Set();
          if (typeof value === 'string') {
            labelsMap[key].add(value);
          }
        });
      });
      const fallbackCounts = buildFallbackFacetCounts(sourceEvents);

      if (!facetsData?.services || facetsData.services.length === 0) {
        setAvailableServices(Array.from(services).sort());
        setServiceCountMap(fallbackCounts.services);
      }
      if (!facetsData?.namespaces || facetsData.namespaces.length === 0) {
        setAvailableNamespaces(Array.from(namespaces).sort());
        setNamespaceCountMap(fallbackCounts.namespaces);
      }
      setAvailableHosts(Array.from(hosts).sort());
      
      const labels: Record<string, string[]> = {};
      Object.entries(labelsMap).forEach(([key, set]) => {
        labels[key] = Array.from(set).sort();
      });
      setAvailableLabels(labels);
    } else {
      if (!facetsData?.services || facetsData.services.length === 0) {
        setAvailableServices([]);
        setServiceCountMap({});
      }
      if (!facetsData?.namespaces || facetsData.namespaces.length === 0) {
        setAvailableNamespaces([]);
        setNamespaceCountMap({});
      }
      setAvailableHosts([]);
      setAvailableLabels({});
    }
  }, [pagedEvents, allEvents, facetsData]);

  const applyClientFilters = useCallback((events: LogEvent[]) => {
    let filtered = events;

    if (deferredSearchQuery) {
      filtered = filtered.filter((event) =>
        event.message?.toLowerCase().includes(deferredSearchQuery) ||
        resolveCanonicalServiceName(event?.service_name, event?.pod_name).toLowerCase().includes(deferredSearchQuery) ||
        event.pod_name?.toLowerCase().includes(deferredSearchQuery)
      );
    }

    if (selectedLevels.length > 0) {
      filtered = filtered.filter((event) => selectedLevels.includes(event.level));
    }

    if (selectedServices.length > 0) {
      filtered = filtered.filter((event) => selectedServices.includes(resolveCanonicalServiceName(event?.service_name, event?.pod_name)));
    }

    if (selectedNamespaces.length > 0) {
      filtered = filtered.filter((event) => selectedNamespaces.includes(normalizeNamespaceValue(event)));
    }

    if (selectedContainers.length > 0) {
      filtered = filtered.filter((event) => {
        const containerValue = normalizeK8sFilterValue(extractContainer(event));
        return containerValue ? selectedContainers.includes(containerValue) : false;
      });
    }

    if (selectedHosts.length > 0) {
      filtered = filtered.filter((event) => selectedHosts.includes(extractHost(event)));
    }

    if (Object.keys(selectedLabels).length > 0) {
      filtered = filtered.filter((event) => matchesSelectedLabels(event, selectedLabels));
    }

    return filtered;
  }, [deferredSearchQuery, selectedLevels, selectedServices, selectedNamespaces, selectedContainers, selectedHosts, selectedLabels]);

  // 过滤日志
  const filteredEvents = useMemo(() => {
    if (!allEvents.length) {
      return [];
    }

    return applyClientFilters(allEvents);
  }, [allEvents, applyClientFilters]);

  // ========== 事件处理 ==========
  
  const toggleLevel = (level: string) => {
    setSelectedLevels(prev =>
      prev.includes(level) ? prev.filter(l => l !== level) : [...prev, level]
    );
  };

  const toggleService = (service: string) => {
    setSelectedServices(prev =>
      prev.includes(service) ? prev.filter(s => s !== service) : [...prev, service]
    );
  };

  const toggleNamespace = (namespace: string) => {
    setSelectedNamespaces((prev) =>
      prev.includes(namespace) ? prev.filter((item) => item !== namespace) : [...prev, namespace]
    );
  };

  const toggleContainer = (container: string) => {
    setSelectedContainers((prev) =>
      prev.includes(container) ? prev.filter((item) => item !== container) : [...prev, container]
    );
  };

  const toggleHost = (host: string) => {
    setSelectedHosts(prev =>
      prev.includes(host) ? prev.filter(h => h !== host) : [...prev, host]
    );
  };

  const applyKubernetesQuickFilter = (filterType: 'namespace' | 'host' | 'container', rawValue: string) => {
    const value = normalizeK8sFilterValue(rawValue);
    if (!value) {
      return;
    }

    if (filterType === 'namespace') {
      toggleNamespace(value);
    } else if (filterType === 'host') {
      toggleHost(value);
    } else {
      toggleContainer(value);
    }

    setShowFilterPanel(true);
    setFilterPanelCollapsed(false);
  };

  const toggleLabel = (key: string, value: string) => {
    setSelectedLabels(prev => {
      const current = prev[key] || [];
      const updated = current.includes(value)
        ? current.filter(v => v !== value)
        : [...current, value];
      
      if (updated.length === 0) {
        const rest = { ...prev };
        delete rest[key];
        return rest;
      }
      
      return { ...prev, [key]: updated };
    });
  };

  const isLabelSelected = (key: string, value: string): boolean => {
    return selectedLabels[key]?.includes(value) || false;
  };

  const toggleFilterCollapse = (filterKey: string) => {
    setCollapsedFilters(prev => ({
      ...prev,
      [filterKey]: !prev[filterKey]
    }));
  };

  const selectLog = (logId: string) => {
    setSelectedLogOverride(null);
    setExpandedLogId(logId);
    setShowSidebar(true);
    setSidebarTab('context');
  };

  const selectPatternSampleLog = (log: LogEvent) => {
    setSelectedLogOverride(log);
    setExpandedLogId(String(log?.id || ''));
    setShowSidebar(true);
    setSidebarTab('detail');
  };

  const closeSidebar = () => {
    setShowSidebar(false);
    setExpandedLogId(null);
    setSelectedLogOverride(null);
  };

  const clearAllFilters = () => {
    setSearchQuery('');
    setSelectedLevels([]);
    setSelectedServices([]);
    setSelectedNamespaces([]);
    setSelectedContainers([]);
    setTraceIdFilter('');
    setCorrelationTraceIds([]);
    setRequestIdFilter('');
    setCorrelationRequestIds([]);
    setPodNameFilter('');
    setSelectedHosts([]);
    setSelectedLabels({});
    setServiceSearchQuery('');
    setNamespaceSearchQuery('');
    setTopologyJumpContext(null);
    setExcludeHealthCheck(false);
    setAnchorTime(null);
    setStartTime('');
    setEndTime('');
  };

  const clearLabelFilter = (key: string, value?: string) => {
    if (value) {
      setSelectedLabels(prev => {
        const current = prev[key] || [];
        const updated = current.filter(v => v !== value);
        if (updated.length === 0) {
          const rest = { ...prev };
          delete rest[key];
          return rest;
        }
        return { ...prev, [key]: updated };
      });
    } else {
      setSelectedLabels(prev => {
        const rest = { ...prev };
        delete rest[key];
        return rest;
      });
    }
  };

  const hasActiveFilters = selectedLevels.length > 0 || selectedServices.length > 0 ||
                          selectedNamespaces.length > 0 ||
                          selectedContainers.length > 0 ||
                          selectedHosts.length > 0 || Object.keys(selectedLabels).length > 0 ||
                          searchQuery.length > 0 || traceIdFilter.length > 0 ||
                          correlationTraceIds.length > 0 || requestIdFilter.length > 0 ||
                          correlationRequestIds.length > 0 || podNameFilter.length > 0 ||
                          excludeHealthCheck || startTime || endTime;
  const activeFilterCount = selectedLevels.length +
    selectedServices.length +
    selectedNamespaces.length +
    selectedContainers.length +
    selectedHosts.length +
    Object.values(selectedLabels).reduce((sum, values) => sum + values.length, 0) +
    (searchQuery.length > 0 ? 1 : 0) +
    (traceIdFilter.length > 0 ? 1 : 0) +
    (correlationTraceIds.length > 0 ? 1 : 0) +
    (requestIdFilter.length > 0 ? 1 : 0) +
    (correlationRequestIds.length > 0 ? 1 : 0) +
    (podNameFilter.length > 0 ? 1 : 0) +
    (excludeHealthCheck ? 1 : 0) +
    (startTime || endTime ? 1 : 0);
  const hasMorePages = isStreamMode && !realtimeMode && Boolean(nextCursor);
  const hasClientOnlyFilters = selectedHosts.length > 0 || selectedContainers.length > 1 || Object.keys(selectedLabels).length > 0;
  const selectedSingleLevel = selectedLevels.length === 1 ? selectedLevels[0] : '';
  const selectedSingleLevelServerCount = selectedSingleLevel
    ? Number(levelCountMap[selectedSingleLevel] || 0)
    : 0;
  const selectedSingleLevelLoadedCount = useMemo(() => {
    if (!selectedSingleLevel) {
      return 0;
    }
    return allEvents.reduce((sum, event) => (
      normalizeDisplayLevel(event?.level) === selectedSingleLevel ? sum + 1 : sum
    ), 0);
  }, [allEvents, selectedSingleLevel]);
  const shouldBackfillSelectedLevel = Boolean(
    selectedSingleLevel
    && selectedSingleLevelServerCount > 0
    && selectedSingleLevelLoadedCount < selectedSingleLevelServerCount
    && hasMorePages,
  );
  const filteredAvailableServices = useMemo(() => {
    const keyword = serviceSearchQuery.trim().toLowerCase();
    if (!keyword) {
      return availableServices;
    }
    return availableServices.filter((service) => service.toLowerCase().includes(keyword));
  }, [availableServices, serviceSearchQuery]);
  const filteredAvailableNamespaces = useMemo(() => {
    const keyword = namespaceSearchQuery.trim().toLowerCase();
    if (!keyword) {
      return availableNamespaces;
    }
    return availableNamespaces.filter((namespace) => namespace.toLowerCase().includes(keyword));
  }, [availableNamespaces, namespaceSearchQuery]);
  const columnTemplate = `${columnWidths.time}px ${columnWidths.service}px ${columnWidths.pod}px ${columnWidths.level}px minmax(320px, 1fr) ${columnWidths.action}px`;

  const loadMoreLogs = useCallback(async () => {
    if (loadingMoreRef.current || !nextCursor) {
      return;
    }

    loadingMoreRef.current = true;
    setLoadingMore(true);
    try {
      const result = await api.getEvents({
        ...apiParams,
        cursor: nextCursor,
        anchor_time: anchorTime || undefined,
      });

      setPagedEvents((prev) => {
        const merged = new Map<string, LogEvent>();
        [...prev, ...((result.events || []) as LogEvent[])].forEach((event) => {
          const key = buildLogEventIdentity(event);
          if (!merged.has(key)) {
            merged.set(key, event);
          }
        });
        return Array.from(merged.values()).sort(compareLogEventsDesc);
      });
      setNextCursor(result.next_cursor || null);
      if (result.anchor_time) {
        setAnchorTime(result.anchor_time);
      }
      if ((result.events || []).length > 0) {
        setLoadedPageCount((prev) => prev + 1);
      }
    } catch (err) {
      console.error('Load more logs failed:', err);
      alert('加载更多失败，请稍后重试');
    } finally {
      loadingMoreRef.current = false;
      setLoadingMore(false);
    }
  }, [nextCursor, apiParams, anchorTime]);

  // host/label 为前端过滤条件：当当前页无命中但后续仍有分页时，自动继续拉取直到命中或分页结束。
  useEffect(() => {
    if (realtimeMode) {
      return;
    }
    if (!hasClientOnlyFilters) {
      return;
    }
    if (filteredEvents.length > 0) {
      return;
    }
    if (!hasMorePages) {
      return;
    }
    if (loading || loadingMore || loadingMoreRef.current) {
      return;
    }
    if (allEvents.length === 0) {
      return;
    }

    void loadMoreLogs();
  }, [
    realtimeMode,
    hasClientOnlyFilters,
    filteredEvents.length,
    hasMorePages,
    loading,
    loadingMore,
    allEvents.length,
    loadMoreLogs,
  ]);

  // 单选级别时，若 facet 统计明显高于已加载数量，则自动补页，减少“计数有但列表没显示”的错觉。
  useEffect(() => {
    if (!isStreamMode || realtimeMode) {
      return;
    }
    if (!shouldBackfillSelectedLevel) {
      return;
    }
    if (loading || loadingMore || loadingMoreRef.current) {
      return;
    }
    void loadMoreLogs();
  }, [
    isStreamMode,
    realtimeMode,
    shouldBackfillSelectedLevel,
    loading,
    loadingMore,
    loadMoreLogs,
  ]);

  const exportLogs = useCallback(async (format: 'csv' | 'json' = 'csv') => {
    if (exporting) {
      return;
    }

    setShowExportMenu(false);
    setExporting(true);

    try {
      const exportParams: LogsQueryParams = {
        ...apiParams,
        limit: 10000,
      };
      const serverResult = await api.getEvents(exportParams);
      let exportData = applyClientFilters(serverResult.events || []);

      // 实时模式下优先导出当前视图（含前端接收但未落库的新日志）。
      if (realtimeMode && realtimeLogs.length > 0) {
        exportData = applyClientFilters(allEvents);
      }

      if (exportData.length === 0) {
        alert('没有可导出的日志');
        return;
      }

      const filename = generateExportFilename('logs', format);
      if (format === 'csv') {
        exportLogsToCSV(
          exportData.map((item): Record<string, unknown> => ({ ...item })),
          filename,
        );
      } else {
        exportToJSON(exportData, filename);
      }
    } catch (err) {
      console.error('Export logs failed:', err);
      alert('导出失败，请稍后重试');
    } finally {
      setExporting(false);
    }
  }, [exporting, apiParams, applyClientFilters, realtimeMode, realtimeLogs.length, allEvents]);

  const openFilterPanel = useCallback(() => {
    setShowFilterPanel(true);
    setFilterPanelCollapsed(false);
  }, []);

  const copyToClipboard = useCallback(async (content: string, successText = '已复制到剪贴板') => {
    const copied = await copyTextToClipboard(content);
    if (copied) {
      setCopyNotice(successText);
      window.setTimeout(() => setCopyNotice(null), 1800);
      return;
    }
    setCopyNotice('复制失败，请检查浏览器剪贴板权限');
    window.setTimeout(() => setCopyNotice(null), 2400);
  }, []);

  const saveCurrentAICase = useCallback(async (log: LogEvent) => {
    const suggestion = aiAnalysis.data;
    if (!suggestion?.overview) {
      setAiCaseNotice('请先完成一次 AI 分析，再保存到知识库');
      return;
    }

    setSavingAiCase(true);
    setAiCaseNotice(null);
    try {
      const traceId = extractTraceIdFromLog(log);
      const llmModel = String(suggestion.model || '');
      const llmMethod = String(suggestion.analysis_method || '');
      await api.saveCase({
        problem_type: suggestion.overview.problem || 'unknown',
        severity: suggestion.overview.severity || 'medium',
        summary: suggestion.overview.description || suggestion.overview.problem || 'AI 分析知识条目',
        log_content: String(log?.message || ''),
        service_name: resolveCanonicalServiceName(log?.service_name, log?.pod_name),
        root_causes: (suggestion.rootCauses || []).map((item) => item.title).filter(Boolean),
        solutions: suggestion.solutions || [],
        context: {
          ...(log?.attributes || {}),
          trace_id: traceId || undefined,
          ai_mode: aiMode,
          ai_analysis_method: llmMethod || undefined,
          ai_saved_from: 'logs-explorer',
        },
        llm_provider: llmMethod === 'llm' ? 'runtime' : '',
        llm_model: llmModel,
        llm_metadata: {
          analysis_method: llmMethod || undefined,
          latency_ms: suggestion.latency_ms,
          cached: suggestion.cached,
        },
        source: 'logs-explorer',
        tags: ['logs', aiMode],
      });
      setAiCaseNotice('已保存到知识库');
    } catch (err: unknown) {
      console.error('Save AI case failed:', err);
      setAiCaseNotice(getErrorMessage(err, '保存知识库条目失败'));
    } finally {
      setSavingAiCase(false);
    }
  }, [aiAnalysis.data, aiMode]);

  const applyQuickTokenFilter = useCallback((token: string, tokenType: HighlightTokenType) => {
    const trimmed = token.trim();
    if (!trimmed) {
      return;
    }

    if (tokenType === 'level') {
      const normalized = trimmed.toUpperCase() === 'WARNING' ? 'WARN' : trimmed.toUpperCase();
      if (isLogLevel(normalized)) {
        setSelectedLevels([normalized]);
      }
      openFilterPanel();
      return;
    }

    setSearchQuery(trimmed);
    openFilterPanel();
  }, [openFilterPanel]);

  // 拖拽调整侧栏面板宽度
  const handleResizeStart = useCallback((e: React.MouseEvent, isSidebar: boolean = false) => {
    e.preventDefault();
    setIsResizing(true);
    const startX = e.clientX;
    const startWidth = isSidebar ? sidebarWidth : filterPanelWidth;

    const handleMouseMove = (e: MouseEvent) => {
      const delta = e.clientX - startX;
      if (isSidebar) {
        const newWidth = Math.max(350, Math.min(800, startWidth - delta));
        setSidebarWidth(newWidth);
      } else {
        const newWidth = Math.max(200, Math.min(400, startWidth + delta));
        setFilterPanelWidth(newWidth);
      }
    };

    const handleMouseUp = () => {
      setIsResizing(false);
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleMouseUp);
    };

    document.addEventListener('mousemove', handleMouseMove);
    document.addEventListener('mouseup', handleMouseUp);
  }, [filterPanelWidth, sidebarWidth]);

  // 拖拽调整日志表格列宽（时间/服务/Pod/级别/操作）
  const handleColumnResizeStart = useCallback((e: React.MouseEvent, column: ResizableColumn) => {
    e.preventDefault();
    e.stopPropagation();
    setIsResizing(true);
    const startX = e.clientX;
    const startWidth = columnWidths[column];

    const handleMouseMove = (event: MouseEvent) => {
      const delta = event.clientX - startX;
      const nextWidth = Math.max(
        COLUMN_MIN_WIDTH[column],
        Math.min(COLUMN_MAX_WIDTH[column], startWidth + delta),
      );
      setColumnWidths((prev) => ({
        ...prev,
        [column]: nextWidth,
      }));
    };

    const handleMouseUp = () => {
      setIsResizing(false);
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleMouseUp);
    };

    document.addEventListener('mousemove', handleMouseMove);
    document.addEventListener('mouseup', handleMouseUp);
  }, [columnWidths]);

  // ========== 渲染辅助组件 ==========

  const renderFilterGroup = (
    key: string,
    title: string,
    icon: React.ReactNode,
    selectedCount: number,
    onClear: () => void,
    children: React.ReactNode
  ) => {
    const isCollapsed = collapsedFilters[key];
    
    return (
      <div className="border-b border-gray-100 last:border-0">
        <button
          onClick={() => toggleFilterCollapse(key)}
          className="w-full flex items-center justify-between px-4 py-3 hover:bg-gray-50 transition-colors"
        >
          <div className="flex items-center gap-2">
            <span className="text-gray-400">{icon}</span>
            <span className="text-sm font-semibold text-gray-800">{title}</span>
            {selectedCount > 0 && (
              <span className="bg-blue-100 text-blue-700 text-[10px] px-1.5 py-0.5 rounded-full font-medium">
                {selectedCount}
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            {selectedCount > 0 && (
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  onClear();
                }}
                className="text-xs text-blue-600 hover:text-blue-700 font-medium"
              >
                清除
              </button>
            )}
            {isCollapsed ? (
              <ChevronRight className="w-4 h-4 text-gray-400" />
            ) : (
              <ChevronDown className="w-4 h-4 text-gray-400" />
            )}
          </div>
        </button>
        
        {!isCollapsed && (
          <div className="px-4 pb-4">
            {children}
          </div>
        )}

      </div>
    );
  };

  // 渲染侧边栏内容
  const renderSidebar = () => {
    if (!currentSelectedLog) return null;
    
    const log = currentSelectedLog;
    const contextLog = contextCurrentLog || log;
    const contextLevel = normalizeDisplayLevel(contextLog.level);
    const labels = extractPodLabels(log);
    const host = extractHost(log);
    const container = extractContainer(log);
    const namespace = extractNamespace(log);
    const normalizedHost = normalizeK8sFilterValue(host);
    const normalizedContainer = normalizeK8sFilterValue(container);
    const normalizedNamespace = normalizeK8sFilterValue(namespace);
    const hostSelected = Boolean(normalizedHost) && selectedHosts.includes(normalizedHost);
    const containerSelected = Boolean(normalizedContainer) && selectedContainers.includes(normalizedContainer);
    const namespaceSelected = Boolean(normalizedNamespace) && selectedNamespaces.includes(normalizedNamespace);
    const resolvedServiceName = resolveCanonicalServiceName(log?.service_name, log?.pod_name);
    const levelColors = LEVEL_COLORS[normalizeDisplayLevel(log.level)] || LEVEL_COLORS.INFO;
    const contextLevelColors = LEVEL_COLORS[contextLevel] || LEVEL_COLORS.INFO;
    const logMeta = extractLogMeta(log);
    const contextLogMeta = extractLogMeta(contextLog);
    const edgeSideMeta = resolveEdgeSideMeta(log.edge_side);
    const edgeMatchMeta = resolveEdgeMatchMeta(log.edge_match_kind);
    const edgePrecisionMeta = resolveEdgePrecisionMeta(log, topologyJumpContext);
    const hasEdgeExplanation = Boolean(
      topologyJumpContext?.sourceService
      || topologyJumpContext?.targetService
      || edgeSideMeta
      || edgeMatchMeta
      || edgePrecisionMeta,
    );

    return (
      <div 
        className="bg-white border-l border-gray-200 flex flex-col shrink-0"
        style={{ width: sidebarWidth }}
      >
        {/* 侧边栏头部 */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-gray-200 bg-gray-50">
          <div className="flex items-center gap-3">
            <span
              className="w-2 h-2 rounded-full"
              style={{ backgroundColor: (sidebarTab === 'context' ? contextLevelColors : levelColors).solid }}
            />
            <h3 className="text-sm font-semibold text-gray-900">日志详情</h3>
          </div>
          <div className="flex items-center gap-1">
            <button
              onClick={() => setSidebarTab('context')}
              className={`px-3 py-1.5 text-xs font-medium rounded transition-colors ${
                sidebarTab === 'context' ? 'bg-blue-100 text-blue-700' : 'text-gray-600 hover:bg-gray-100'
              }`}
            >
              上下文
            </button>
            <button
              onClick={() => setSidebarTab('detail')}
              className={`px-3 py-1.5 text-xs font-medium rounded transition-colors ${
                sidebarTab === 'detail' ? 'bg-blue-100 text-blue-700' : 'text-gray-600 hover:bg-gray-100'
              }`}
            >
              详情
            </button>
            <button
              onClick={() => setSidebarTab('json')}
              className={`px-3 py-1.5 text-xs font-medium rounded transition-colors ${
                sidebarTab === 'json' ? 'bg-blue-100 text-blue-700' : 'text-gray-600 hover:bg-gray-100'
              }`}
            >
              JSON
            </button>
            <button
              onClick={() => setSidebarTab('ai')}
              className={`px-3 py-1.5 text-xs font-medium rounded transition-colors ${
                sidebarTab === 'ai' ? 'bg-purple-100 text-purple-700' : 'text-gray-600 hover:bg-gray-100'
              }`}
            >
              <Sparkles className="w-3.5 h-3.5 inline mr-1" />
              AI
            </button>
            <button
              onClick={closeSidebar}
              className="p-1.5 text-gray-400 hover:text-gray-600 hover:bg-gray-200 rounded ml-2"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>

        {/* 侧边栏内容 */}
        <div className="flex-1 overflow-y-auto">
          {sidebarTab === 'context' && (
            <div className="p-4 space-y-4">
              {/* 上下文条数选择 */}
              <div className="flex items-center gap-4 pb-4 border-b border-gray-100">
                <div className="flex items-center gap-2">
                  <span className="text-xs text-gray-500">前文</span>
                  <select
                    value={contextBeforeCount}
                    onChange={(e) => setContextBeforeCount(Number(e.target.value))}
                    className="text-xs border border-gray-300 rounded px-2 py-1"
                  >
                    <option value={3}>3</option>
                    <option value={5}>5</option>
                    <option value={10}>10</option>
                    <option value={20}>20</option>
                    <option value={50}>50</option>
                  </select>
                  <span className="text-xs text-gray-500">条</span>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-xs text-gray-500">后文</span>
                  <select
                    value={contextAfterCount}
                    onChange={(e) => setContextAfterCount(Number(e.target.value))}
                    className="text-xs border border-gray-300 rounded px-2 py-1"
                  >
                    <option value={3}>3</option>
                    <option value={5}>5</option>
                    <option value={10}>10</option>
                    <option value={20}>20</option>
                    <option value={50}>50</option>
                  </select>
                  <span className="text-xs text-gray-500">条</span>
                </div>
                {logContextLoading && (
                  <span className="text-xs text-blue-600">加载中...</span>
                )}
              </div>

              {/* 上下文日志列表 - 优先 log_id 精确锚定，pod_name + timestamp 兜底 */}
              <div className="space-y-1">
                {/* 前文日志 */}
                {logContextData?.before?.map((ctxLog, idx: number) => (
                  <div 
                    key={`before-${idx}`} 
                    className="p-3 rounded-lg bg-gray-50 border border-gray-100 hover:bg-gray-100 transition-colors"
                  >
                    {(() => {
                      const level = normalizeDisplayLevel(ctxLog.level);
                      const colors = LEVEL_COLORS[level] || LEVEL_COLORS.INFO;
                      return (
                        <div className="flex items-center gap-2 mb-1">
                          <span className="text-xs text-gray-400 font-mono">{formatTime(String(ctxLog.timestamp || ''))}</span>
                          <span 
                            className="w-2 h-2 rounded-full" 
                            style={{ backgroundColor: colors.solid }}
                          />
                          <span className="text-xs font-medium text-gray-600">{level}</span>
                        </div>
                      );
                    })()}
                    <div className="text-sm text-gray-700 font-mono whitespace-pre-wrap break-words leading-5">
                      {ctxLog.message}
                    </div>
                  </div>
                ))}
                
                {/* 当前日志 */}
                <div 
                  className="p-3 rounded-lg border-l-4 bg-blue-50/50"
                  style={{ borderLeftColor: contextLevelColors.solid }}
                >
                  <div className="flex items-center gap-2 mb-1">
                    <span className="text-xs font-semibold font-mono" style={{ color: contextLevelColors.solid }}>
                      {formatTime(contextLog.timestamp)}
                    </span>
                    <span 
                      className="w-2 h-2 rounded-full" 
                      style={{ backgroundColor: contextLevelColors.solid }}
                    />
                    <span className="text-xs font-bold" style={{ color: contextLevelColors.solid }}>{contextLevel}</span>
                    <span className="text-xs text-gray-500">当前</span>
                    {contextLogMeta.stream && (
                      <span className="text-[11px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-600 font-mono">
                        {String(contextLogMeta.stream).toUpperCase()}
                      </span>
                    )}
                    {typeof contextLogMeta.line_count === 'number' && contextLogMeta.line_count > 1 && (
                      <span className="text-[11px] px-1.5 py-0.5 rounded bg-blue-100 text-blue-700 font-medium">
                        {contextLogMeta.line_count} lines
                      </span>
                    )}
                    {contextCurrentMatches.length > 1 && (
                      <span className="text-[11px] px-1.5 py-0.5 rounded bg-indigo-100 text-indigo-700 font-medium">
                        同一时间戳 {contextCurrentMatches.length} 条
                      </span>
                    )}
                  </div>
                  <div className="text-sm text-gray-900 font-mono whitespace-pre-wrap break-words leading-5">
                    {contextLog.message}
                  </div>
                </div>

                {contextCurrentMatches.slice(1).map((ctxLog, idx: number) => {
                  const level = normalizeDisplayLevel(ctxLog.level);
                  const colors = LEVEL_COLORS[level] || LEVEL_COLORS.INFO;
                  return (
                    <div
                      key={`current-sibling-${idx}`}
                      className="p-3 rounded-lg border border-indigo-100 bg-indigo-50/40"
                    >
                      <div className="flex items-center gap-2 mb-1">
                        <span className="text-xs text-indigo-700 font-mono">{formatTime(String(ctxLog.timestamp || ''))}</span>
                        <span className="w-2 h-2 rounded-full" style={{ backgroundColor: colors.solid }} />
                        <span className="text-xs font-medium text-indigo-700">{level}</span>
                        <span className="text-xs text-indigo-500">同刻日志</span>
                      </div>
                      <div className="text-sm text-gray-800 font-mono whitespace-pre-wrap break-words leading-5">
                        {ctxLog.message}
                      </div>
                    </div>
                  );
                })}
                
                {/* 后文日志 */}
                {logContextData?.after?.map((ctxLog, idx: number) => (
                  <div 
                    key={`after-${idx}`} 
                    className="p-3 rounded-lg bg-gray-50 border border-gray-100 hover:bg-gray-100 transition-colors"
                  >
                    {(() => {
                      const level = normalizeDisplayLevel(ctxLog.level);
                      const colors = LEVEL_COLORS[level] || LEVEL_COLORS.INFO;
                      return (
                        <div className="flex items-center gap-2 mb-1">
                          <span className="text-xs text-gray-400 font-mono">{formatTime(String(ctxLog.timestamp || ''))}</span>
                          <span 
                            className="w-2 h-2 rounded-full" 
                            style={{ backgroundColor: colors.solid }}
                          />
                          <span className="text-xs font-medium text-gray-600">{level}</span>
                        </div>
                      );
                    })()}
                    <div className="text-sm text-gray-700 font-mono whitespace-pre-wrap break-words leading-5">
                      {ctxLog.message}
                    </div>
                  </div>
                ))}
                
                {/* 空状态显示 */}
                {!logContextLoading && !logContextData?.before?.length && !logContextData?.after?.length && !contextCurrentMatches.slice(1).length && (
                  <div className="text-center text-sm text-gray-400 py-8">
                    暂无上下文日志
                  </div>
                )}
              </div>
            </div>
          )}

          {sidebarTab === 'detail' && (
            <div className="p-4 space-y-4">
              {/* 基本信息 */}
              <div className="grid grid-cols-2 gap-3">
                <div className="bg-gray-50 rounded-lg p-3 border border-gray-100">
                  <div className="text-[11px] text-gray-400 uppercase mb-1">时间戳</div>
                  <div className="text-xs font-mono text-gray-800 break-all">{log.timestamp}</div>
                </div>
                <div className="bg-gray-50 rounded-lg p-3 border border-gray-100">
                  <div className="text-[11px] text-gray-400 uppercase mb-1">级别</div>
                  <span 
                    className="inline-flex items-center gap-1.5 px-2 py-1 text-xs font-semibold rounded text-white"
                    style={{ backgroundColor: levelColors.solid }}
                  >
                    {log.level}
                  </span>
                </div>
                <div className="bg-gray-50 rounded-lg p-3 border border-gray-100">
                  <div className="text-[11px] text-gray-400 uppercase mb-1">服务</div>
                  <div className="text-sm text-blue-600 font-semibold">{resolvedServiceName}</div>
                </div>
                <div className="bg-gray-50 rounded-lg p-3 border border-gray-100">
                  <div className="text-[11px] text-gray-400 uppercase mb-1">Pod</div>
                  <div className="text-xs font-mono text-gray-700">{log.pod_name}</div>
                </div>
              </div>

              {hasEdgeExplanation && (
                <div className="rounded-lg border border-sky-200 bg-sky-50/70 p-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-[11px] font-semibold uppercase tracking-wide text-sky-700">链路命中说明</span>
                    {edgeSideMeta ? (
                      <span className={`rounded border px-2 py-0.5 text-[11px] font-medium ${edgeSideMeta.className}`}>
                        {edgeSideMeta.label}
                      </span>
                    ) : null}
                    {edgeMatchMeta ? (
                      <span className={`rounded border px-2 py-0.5 text-[11px] font-medium ${edgeMatchMeta.className}`}>
                        {edgeMatchMeta.label}
                      </span>
                    ) : null}
                    {edgePrecisionMeta ? (
                      <span className={`rounded border px-2 py-0.5 text-[11px] font-medium ${edgePrecisionMeta.className}`}>
                        {edgePrecisionMeta.label}
                      </span>
                    ) : null}
                  </div>
                  <div className="mt-2 space-y-1 text-xs leading-5 text-slate-700">
                    {edgeSideMeta ? <p>{edgeSideMeta.description}</p> : null}
                    {edgeMatchMeta ? <p>{edgeMatchMeta.description}</p> : null}
                    {edgePrecisionMeta ? <p>{edgePrecisionMeta.description}</p> : null}
                    {(topologyJumpContext?.sourceService || topologyJumpContext?.targetService) ? (
                      <p>
                        当前拓扑上下文: <span className="font-medium text-slate-900">{topologyJumpContext?.sourceService || '未指定源端'}</span> →{' '}
                        <span className="font-medium text-slate-900">{topologyJumpContext?.targetService || '未指定目标端'}</span>
                        {topologyJumpContext?.timeWindow ? ` · 窗口 ${topologyJumpContext.timeWindow}` : ''}
                        {topologyJumpContext?.anchorTime ? ` · 锚点 ${formatCollectorTime(topologyJumpContext.anchorTime)}` : ''}
                      </p>
                    ) : null}
                  </div>
                </div>
              )}

              {/* 日志消息 */}
              <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
                <div className="flex items-center justify-between px-3 py-2 bg-gray-50 border-b border-gray-200">
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-semibold text-gray-600 uppercase">日志消息</span>
                    {logMeta.stream && (
                      <span className="text-[11px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-600 font-mono">
                        {String(logMeta.stream).toUpperCase()}
                      </span>
                    )}
                    {typeof logMeta.line_count === 'number' && logMeta.line_count > 1 && (
                      <span className="text-[11px] px-1.5 py-0.5 rounded bg-blue-100 text-blue-700 font-medium">
                        {logMeta.line_count} lines
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-2">
                    <button
                      onClick={() => setHighlightMode(prev => prev === 'normal' ? 'enhanced' : 'normal')}
                      className={`text-xs px-2 py-1 rounded ${
                        highlightMode === 'enhanced'
                          ? 'bg-violet-100 text-violet-700'
                          : 'text-gray-500 hover:bg-gray-100'
                      }`}
                    >
                      高亮: {highlightMode === 'enhanced' ? '增强' : '普通'}
                    </button>
                    <button
                      onClick={() => setWordWrap(!wordWrap)}
                      className={`text-xs px-2 py-1 rounded ${wordWrap ? 'bg-blue-100 text-blue-600' : 'text-gray-500 hover:bg-gray-100'}`}
                    >
                      换行
                    </button>
                    <button
                      onClick={() => {
                        void copyToClipboard(log.message, '日志内容已复制');
                      }}
                      className="text-xs text-gray-500 hover:text-blue-600"
                    >
                      <Copy className="w-3.5 h-3.5" />
                    </button>
                  </div>
                </div>
                {highlightMode === 'enhanced' && (
                  <div className="px-3 py-1.5 bg-violet-50 border-b border-violet-100 text-[11px] text-violet-700">
                    增强高亮已启用，可点击时间戳 / 级别 / 类名 / SQL 关键字快速过滤
                  </div>
                )}
                {logMeta.collector_time && (
                  <div className="px-3 py-1.5 bg-slate-50 border-b border-slate-100 text-[11px] text-slate-500 font-mono">
                    collector_time: {formatCollectorTime(logMeta.collector_time)}
                  </div>
                )}
                <div className="p-3">
                  <pre 
                    className={`text-sm text-gray-800 font-mono bg-slate-50 p-3 rounded border border-slate-200 ${
                      wordWrap ? 'whitespace-pre-wrap break-all' : 'whitespace-pre overflow-x-auto'
                    }`}
                  >
                    {renderHighlightedLogMessage(log.message, {
                      mode: highlightMode,
                      onTokenClick: highlightMode === 'enhanced' ? applyQuickTokenFilter : undefined,
                    })}
                  </pre>
                </div>
              </div>

              {/* Kubernetes 信息 */}
              <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
                <div className="px-3 py-2 bg-gray-50 border-b border-gray-200 flex items-center justify-between">
                  <span className="text-xs font-semibold text-gray-600 uppercase">Kubernetes 信息</span>
                  <span className="text-[11px] text-gray-400">点击可添加筛选</span>
                </div>
                <div className="p-3 grid grid-cols-2 gap-3">
                  <div>
                    <span className="text-[11px] text-gray-400 uppercase">节点</span>
                    <div className="mt-1 flex items-center gap-2">
                      <div className="text-xs font-mono text-gray-700 break-all">{host}</div>
                      <button
                        type="button"
                        disabled={!normalizedHost}
                        onClick={() => applyKubernetesQuickFilter('host', host)}
                        className={`inline-flex items-center rounded px-2 py-0.5 text-[11px] transition-colors ${
                          hostSelected
                            ? 'bg-blue-100 text-blue-700'
                            : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
                        } disabled:cursor-not-allowed disabled:opacity-40`}
                      >
                        {hostSelected ? '取消筛选' : '添加筛选'}
                      </button>
                    </div>
                  </div>
                  <div>
                    <span className="text-[11px] text-gray-400 uppercase">容器</span>
                    <div className="mt-1 flex items-center gap-2">
                      <div className="text-xs font-mono text-gray-700 break-all">{container}</div>
                      <button
                        type="button"
                        disabled={!normalizedContainer}
                        onClick={() => applyKubernetesQuickFilter('container', container)}
                        className={`inline-flex items-center rounded px-2 py-0.5 text-[11px] transition-colors ${
                          containerSelected
                            ? 'bg-blue-100 text-blue-700'
                            : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
                        } disabled:cursor-not-allowed disabled:opacity-40`}
                      >
                        {containerSelected ? '取消筛选' : '添加筛选'}
                      </button>
                    </div>
                  </div>
                  <div>
                    <span className="text-[11px] text-gray-400 uppercase">命名空间</span>
                    <div className="mt-1 flex items-center gap-2">
                      <div className="text-xs font-mono text-gray-700 break-all">{namespace}</div>
                      <button
                        type="button"
                        disabled={!normalizedNamespace}
                        onClick={() => applyKubernetesQuickFilter('namespace', namespace)}
                        className={`inline-flex items-center rounded px-2 py-0.5 text-[11px] transition-colors ${
                          namespaceSelected
                            ? 'bg-blue-100 text-blue-700'
                            : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
                        } disabled:cursor-not-allowed disabled:opacity-40`}
                      >
                        {namespaceSelected ? '取消筛选' : '添加筛选'}
                      </button>
                    </div>
                  </div>
                </div>
              </div>

              {/* Pod 标签 */}
              {Object.keys(labels).length > 0 && (
                <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
                  <div className="px-3 py-2 bg-gray-50 border-b border-gray-200 flex items-center justify-between">
                    <span className="text-xs font-semibold text-gray-600 uppercase">Pod 标签</span>
                    <span className="text-xs text-gray-400">点击筛选</span>
                  </div>
                  <div className="p-3">
                    <div className="flex flex-wrap gap-2">
                      {Object.entries(labels).map(([key, value]) => {
                        const colors = getTagColor(key);
                        const isSelected = isLabelSelected(key, value as string);
                        
                        return (
                          <button
                            key={key}
                            onClick={() => toggleLabel(key, value as string)}
                            className={`inline-flex items-center px-2 py-1 text-xs rounded-md border transition-all ${
                              isSelected 
                                ? 'bg-blue-100 border-blue-300 text-blue-700 ring-1 ring-blue-300' 
                                : `${colors.bg} ${colors.border} ${colors.text} hover:shadow-sm`
                            }`}
                          >
                            <span className={`${colors.keyColor} font-medium`}>{key}</span>
                            <span className="mx-1 text-gray-300">|</span>
                            <span className="font-semibold">{String(value)}</span>
                            {isSelected && <Check className="w-3 h-3 ml-1" />}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                </div>
              )}
            </div>
          )}

          {sidebarTab === 'json' && (
            <div className="p-4">
              <div className="bg-slate-900 rounded-lg overflow-hidden">
                <div className="flex items-center justify-between px-3 py-2 bg-slate-800 border-b border-slate-700">
                  <span className="text-xs text-slate-400">JSON</span>
                  <button
                    onClick={() => {
                      void copyToClipboard(JSON.stringify(log, null, 2), 'JSON 已复制');
                    }}
                    className="text-xs text-slate-400 hover:text-white"
                  >
                    <Copy className="w-3.5 h-3.5" />
                  </button>
                </div>
                <pre className="text-xs text-slate-100 p-3 overflow-auto max-h-[600px] font-mono">
                  {JSON.stringify(log, null, 2)}
                </pre>
              </div>
            </div>
          )}

          {sidebarTab === 'ai' && (
            <div className="h-full flex flex-col">
              <div className="px-4 py-3 border-b border-gray-200 bg-gray-50 space-y-3">
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => setAiMode('log')}
                    className={`px-3 py-1.5 text-xs font-medium rounded transition-colors ${
                      aiMode === 'log' ? 'bg-purple-100 text-purple-700' : 'bg-white text-gray-600 hover:bg-gray-100'
                    }`}
                  >
                    日志分析
                  </button>
                  <button
                    type="button"
                    onClick={() => setAiMode('trace')}
                    disabled={!selectedTraceId}
                    className={`px-3 py-1.5 text-xs font-medium rounded transition-colors ${
                      aiMode === 'trace'
                        ? 'bg-emerald-100 text-emerald-700'
                        : 'bg-white text-gray-600 hover:bg-gray-100'
                    } disabled:opacity-50 disabled:cursor-not-allowed`}
                    title={selectedTraceId ? '使用 trace_id 执行追踪分析' : '当前日志无 trace_id'}
                  >
                    Trace 分析
                  </button>
                </div>

                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <span className="text-xs text-gray-600">LLM 大模型</span>
                    <button
                      type="button"
                      onClick={() => setAiUseLLM((prev) => !prev)}
                      className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors ${
                        aiUseLLM ? 'bg-purple-600' : 'bg-gray-300'
                      }`}
                    >
                      <span
                        className={`inline-block h-3 w-3 transform rounded-full bg-white transition-transform ${
                          aiUseLLM ? 'translate-x-5' : 'translate-x-1'
                        }`}
                      />
                    </button>
                  </div>
                  <button
                    type="button"
                    onClick={() => {
                      if (aiMode === 'trace' && selectedTraceId) {
                        navigation.goToAIAnalysis({
                          traceId: selectedTraceId,
                          serviceName: resolvedServiceName,
                          autoAnalyze: false,
                        });
                        return;
                      }
                      navigation.goToAIAnalysis({ logData: log, autoAnalyze: false });
                    }}
                    className="text-xs text-blue-600 hover:text-blue-700 font-medium"
                  >
                    打开完整分析页
                  </button>
                </div>

                {selectedTraceId ? (
                  <div className="text-[11px] text-gray-500 font-mono break-all">trace_id: {selectedTraceId}</div>
                ) : (
                  <div className="text-[11px] text-amber-600">当前日志无 trace_id，仅支持日志分析</div>
                )}
              </div>

              <div className="flex-1 overflow-y-auto">
                <AISuggestionCard
                  loading={aiAnalysis.loading}
                  error={aiAnalysis.error?.message}
                  analysisLabel={aiMode === 'trace' ? 'Trace' : '日志'}
                  suggestion={aiAnalysis.data || undefined}
                  onAnalyze={async () => {
                    try {
                      await aiAnalysis.analyze(log, {
                        mode: aiMode,
                        useLLM: aiUseLLM,
                        traceId: selectedTraceId || undefined,
                      });
                    } catch (err) {
                      console.error('AI analysis failed:', err);
                    }
                  }}
                />
                <div className="px-4 pb-4">
                  <button
                    type="button"
                    onClick={() => saveCurrentAICase(log)}
                    disabled={!aiAnalysis.data || savingAiCase}
                    className="w-full px-3 py-2 text-sm rounded-lg bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    {savingAiCase ? '保存中...' : '一键保存当前分析到知识库'}
                  </button>
                  {aiCaseNotice && (
                    <div className="mt-2 text-xs text-gray-600">{aiCaseNotice}</div>
                  )}
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    );
  };

  // ========== 主渲染 ==========
  
  if (isStreamMode && loading && pagedEvents.length === 0) return <LoadingState message="加载日志数据..." />;
  if (isPatternMode && aggregatedLoading && aggregatedPatterns.length === 0) {
    return <LoadingState message="正在聚合日志模式..." />;
  }
  if (isStreamMode && error) return <ErrorState message={error.message} onRetry={refetch} />;
  if (isPatternMode && aggregatedError) {
    return <ErrorState message={aggregatedError.message} onRetry={refetchAggregated} />;
  }

  return (
    <div className="flex flex-col h-full bg-[#f8fafc] overflow-hidden">
      {/* 顶部工具栏 */}
      <div className="bg-white border-b border-gray-200 px-4 py-3 shrink-0">
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-3 shrink-0">
            <h1 className="text-base font-semibold text-gray-900">日志浏览器</h1>
            <span className="text-xs text-gray-500 bg-gray-100 px-2 py-1 rounded-full">
              {isPatternMode ? (
                `Pattern ${aggregatedPatterns.length.toLocaleString()} / 原始日志 ${(aggregatedData?.total_logs || 0).toLocaleString()}`
              ) : realtimeMode ? (
                <>
                  <span className="inline-flex items-center gap-1">
                    <span className="w-1.5 h-1.5 bg-green-500 rounded-full animate-pulse"></span>
                    实时 {filteredEvents.length.toLocaleString()} 条
                  </span>
                </>
              ) : (
                `筛选后 ${filteredEvents.length.toLocaleString()} / 已加载 ${allEvents.length.toLocaleString()}${hasMorePages ? '+' : ''}`
              )}
            </span>
            {isStreamMode && !realtimeMode && selectedSingleLevel && selectedSingleLevelServerCount > 0 && (
              <span className={`text-[11px] px-2 py-1 rounded-full ${
                shouldBackfillSelectedLevel ? 'text-amber-700 bg-amber-50' : 'text-gray-600 bg-gray-100'
              }`}>
                {selectedSingleLevel} 已加载 {selectedSingleLevelLoadedCount.toLocaleString()} / 统计 {selectedSingleLevelServerCount.toLocaleString()}
              </span>
            )}
            {(isStreamMode ? loading : aggregatedLoading) && (
              <span className="text-[11px] text-blue-600">刷新中...</span>
            )}
            {!hasExplicitServerFilters && autoExpandedWindow && (
              <span className="text-[11px] text-amber-700 bg-amber-50 px-2 py-1 rounded-full">
                近 1 小时无数据，已自动扩展到近 6 小时
              </span>
            )}
          </div>

          <div className="flex-1 max-w-xl">
            <div className="relative">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
              <input
                type="text"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="搜索日志内容、服务名、Pod名..."
                className="w-full pl-9 pr-9 py-2 text-sm border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent"
              />
              {searchQuery && (
                <button
                  onClick={() => setSearchQuery('')}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
                >
                  <X className="w-4 h-4" />
                </button>
              )}
            </div>
          </div>

          <div className="flex items-center gap-2 shrink-0">
            <button
              onClick={() => {
                const nextMode = isPatternMode ? 'stream' : 'pattern';
                setLogsViewMode(nextMode);
                if (nextMode === 'pattern') {
                  setRealtimeMode(false);
                  clearRealtimeLogs();
                }
              }}
              className={`flex items-center gap-1.5 px-3 py-2 rounded-lg transition-colors ${
                isPatternMode ? 'bg-indigo-100 text-indigo-700' : 'hover:bg-gray-100 text-gray-600'
              }`}
              title={isPatternMode ? '切换到事件流视图' : '切换到 Pattern 聚合视图'}
            >
              <LayoutGrid className="w-4 h-4" />
              <span className="text-sm">{isPatternMode ? '事件流' : '聚合'}</span>
            </button>

            {/* 实时模式按钮 */}
            <button
              onClick={() => {
                setRealtimeMode(!realtimeMode);
                if (!realtimeMode) {
                  clearRealtimeLogs();
                }
              }}
              disabled={isPatternMode}
              className={`flex items-center gap-1.5 px-3 py-2 rounded-lg transition-colors ${
                realtimeMode 
                  ? 'bg-green-100 text-green-700 ring-2 ring-green-300' 
                  : 'hover:bg-gray-100 text-gray-600'
              } ${isPatternMode ? 'opacity-50 cursor-not-allowed' : ''}`}
              title={realtimeMode ? '暂停实时日志' : '开启实时日志'}
            >
              {realtimeMode ? (
                <>
                  <Pause className="w-4 h-4" />
                  <span className="text-sm">暂停</span>
                </>
              ) : (
                <>
                  <Radio className="w-4 h-4" />
                  <span className="text-sm">实时</span>
                </>
              )}
              {realtimeMode && realtimeConnected && (
                <span className="w-2 h-2 bg-green-500 rounded-full animate-pulse"></span>
              )}
            </button>

            {/* 时间筛选器 */}
            <div className="relative" ref={timeFilterRef}>
              <button
                onClick={() => setShowTimeFilter(!showTimeFilter)}
                className={`flex items-center gap-1.5 px-3 py-2 rounded-lg transition-colors ${
                  showTimeFilter || startTime || endTime ? 'bg-blue-100 text-blue-700' : 'hover:bg-gray-100 text-gray-600'
                }`}
              >
                <Clock className="w-4 h-4" />
                <span className="text-sm">时间</span>
                {(startTime || endTime) && (
                  <span className="w-2 h-2 bg-blue-500 rounded-full"></span>
                )}
              </button>

              {/* 时间筛选下拉面板 */}
              {showTimeFilter && (
                <div className="absolute top-full right-0 mt-2 w-80 bg-white border border-gray-200 rounded-lg shadow-lg z-50 p-4">
                  <div className="space-y-3">
                    <div className="flex items-center justify-between">
                      <h3 className="text-sm font-medium text-gray-900">时间范围</h3>
                      <button
                        onClick={() => {
                          setStartTime('');
                          setEndTime('');
                        }}
                        className="text-xs text-blue-600 hover:text-blue-700"
                      >
                        清除
                      </button>
                    </div>

                    {/* 快捷时间选项 */}
                    <div className="grid grid-cols-4 gap-2">
                      {[
                        { label: '最近5分钟', value: '5m' },
                        { label: '最近15分钟', value: '15m' },
                        { label: '最近30分钟', value: '30m' },
                        { label: '最近1小时', value: '1h' },
                        { label: '最近3小时', value: '3h' },
                        { label: '最近6小时', value: '6h' },
                        { label: '最近12小时', value: '12h' },
                        { label: '最近24小时', value: '24h' },
                      ].map((preset) => (
                        <button
                          key={preset.value}
                          onClick={() => {
                            const end = new Date();
                            const start = new Date();
                            const value = parseInt(preset.value);
                            if (preset.value.includes('h')) {
                              start.setHours(start.getHours() - value);
                            } else {
                              start.setMinutes(start.getMinutes() - value);
                            }
                            applyTimeRange(start.toISOString(), end.toISOString());
                          }}
                          className="px-2 py-1.5 text-xs bg-gray-100 hover:bg-gray-200 text-gray-700 rounded transition-colors"
                        >
                          {preset.label}
                        </button>
                      ))}
                    </div>

                    <div className="border-t border-gray-200 pt-3">
                      <div className="space-y-2">
                        <div>
                          <label className="block text-xs text-gray-500 mb-1">开始时间</label>
                          <input
                            type="datetime-local"
                            value={toLocalDatetimeInputValue(startTime)}
                            onChange={(e) => {
                              const nextStart = fromLocalDatetimeInputValue(e.target.value);
                              applyTimeRange(nextStart, endTime);
                            }}
                            className="w-full text-sm border border-gray-300 rounded px-2 py-1.5 focus:outline-none focus:ring-2 focus:ring-blue-500"
                          />
                        </div>
                        <div>
                          <label className="block text-xs text-gray-500 mb-1">结束时间</label>
                          <input
                            type="datetime-local"
                            value={toLocalDatetimeInputValue(endTime)}
                            onChange={(e) => {
                              const nextEnd = fromLocalDatetimeInputValue(e.target.value);
                              applyTimeRange(startTime, nextEnd);
                            }}
                            className="w-full text-sm border border-gray-300 rounded px-2 py-1.5 focus:outline-none focus:ring-2 focus:ring-blue-500"
                          />
                        </div>
                      </div>
                    </div>

                    <button
                      onClick={() => setShowTimeFilter(false)}
                      className="w-full py-2 bg-blue-600 hover:bg-blue-700 text-white text-sm rounded-lg transition-colors"
                    >
                      应用
                    </button>
                  </div>
                </div>
              )}
            </div>

            <div className="flex items-center gap-1">
              <button
                onClick={() => setExcludeHealthCheck(!excludeHealthCheck)}
                className={`flex items-center gap-1.5 rounded-lg px-3 py-2 transition-colors ${
                  excludeHealthCheck ? 'bg-green-100 text-green-700' : 'text-gray-600 hover:bg-gray-100'
                }`}
              >
                <Activity className="w-4 h-4" />
                <span className="text-sm">健康检查</span>
              </button>
              <Tooltip
                title="健康检查过滤"
                lines={[
                  '开启后会过滤 /health、readiness、liveness 等健康探测日志。',
                  '建议排障时开启，避免心跳日志稀释错误信号。',
                  '若排查探活失败问题，请临时关闭该过滤。',
                ]}
                widthClass="w-[320px]"
              />
            </div>

            <button
              onClick={() => {
                if (!showFilterPanel) {
                  setShowFilterPanel(true);
                  setFilterPanelCollapsed(false);
                } else if (filterPanelCollapsed) {
                  setFilterPanelCollapsed(false);
                } else {
                  setShowFilterPanel(false);
                }
              }}
              className={`flex items-center gap-1.5 px-3 py-2 rounded-lg transition-colors ${
                showFilterPanel ? 'bg-blue-100 text-blue-700' : 'hover:bg-gray-100 text-gray-600'
              }`}
              title={showFilterPanel ? (filterPanelCollapsed ? '展开筛选面板' : '隐藏筛选面板') : '显示筛选面板'}
            >
              <PanelLeft className="w-4 h-4" />
              <span className="text-sm">{showFilterPanel ? (filterPanelCollapsed ? '展开筛选' : '筛选') : '筛选'}</span>
              {activeFilterCount > 0 && (
                <span className="inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-white/80 px-1 text-[10px] font-semibold text-blue-700">
                  {activeFilterCount}
                </span>
              )}
            </button>
            
            <button
              onClick={isPatternMode ? refetchAggregated : refetch}
              className="p-2 hover:bg-gray-100 text-gray-600 rounded-lg transition-colors"
              title="刷新"
            >
              <RefreshCw className="w-4 h-4" />
            </button>

            {/* 导出按钮 */}
            <div className="relative">
              <button
                onClick={() => setShowExportMenu(!showExportMenu)}
                disabled={exporting}
                className="flex items-center gap-1.5 px-3 py-2 rounded-lg hover:bg-gray-100 text-gray-600 transition-colors disabled:cursor-not-allowed disabled:opacity-60"
                title="按当前筛选导出"
              >
                <Download className="w-4 h-4" />
                <span className="text-sm">{exporting ? '导出中...' : '导出'}</span>
                <ChevronDown className="w-3.5 h-3.5" />
              </button>
              
              {showExportMenu && (
                <div className="absolute right-0 top-full mt-1 bg-white border border-gray-200 rounded-lg shadow-lg py-1 z-50 min-w-[140px]">
                  <button
                    onClick={() => {
                      void exportLogs('csv');
                    }}
                    className="w-full flex items-center gap-2 px-3 py-2 text-sm text-gray-700 hover:bg-gray-100"
                  >
                    <FileSpreadsheet className="w-4 h-4" />
                    导出 CSV（筛选）
                  </button>
                  <button
                    onClick={() => {
                      void exportLogs('json');
                    }}
                    className="w-full flex items-center gap-2 px-3 py-2 text-sm text-gray-700 hover:bg-gray-100"
                  >
                    <FileJson className="w-4 h-4" />
                    导出 JSON（筛选）
                  </button>
                </div>
              )}
            </div>
          </div>
        </div>

        {/* 活跃筛选标签 */}
        {hasActiveFilters && (
          <div className="flex items-center gap-2 mt-3 flex-wrap">
            {selectedLevels.map(level => (
              <span key={level} className="inline-flex items-center gap-1.5 px-2.5 py-1 text-xs bg-blue-100 text-blue-700 rounded-md font-medium">
                {level}
                <button onClick={() => toggleLevel(level)} className="hover:text-blue-900">
                  <X className="w-3 h-3" />
                </button>
              </span>
            ))}
            {selectedServices.map(service => (
              <span key={service} className="inline-flex items-center gap-1.5 px-2.5 py-1 text-xs bg-green-100 text-green-700 rounded-md font-medium">
                {service}
                <button onClick={() => toggleService(service)} className="hover:text-green-900">
                  <X className="w-3 h-3" />
                </button>
              </span>
            ))}
            {selectedNamespaces.map((namespace) => (
              <span key={namespace} className="inline-flex items-center gap-1.5 px-2.5 py-1 text-xs bg-cyan-100 text-cyan-700 rounded-md font-medium">
                ns: {namespace}
                <button onClick={() => toggleNamespace(namespace)} className="hover:text-cyan-900">
                  <X className="w-3 h-3" />
                </button>
              </span>
            ))}
            {selectedContainers.map((container) => (
              <span key={container} className="inline-flex items-center gap-1.5 px-2.5 py-1 text-xs bg-amber-100 text-amber-700 rounded-md font-medium">
                container: {container}
                <button onClick={() => toggleContainer(container)} className="hover:text-amber-900">
                  <X className="w-3 h-3" />
                </button>
              </span>
            ))}
            {selectedHosts.map((host) => (
              <span key={host} className="inline-flex items-center gap-1.5 px-2.5 py-1 text-xs bg-sky-100 text-sky-700 rounded-md font-medium">
                node: {host}
                <button onClick={() => toggleHost(host)} className="hover:text-sky-900">
                  <X className="w-3 h-3" />
                </button>
              </span>
            ))}
            {Object.entries(selectedLabels).map(([key, values]) =>
              values.map(value => {
                const colors = getTagColor(key);
                return (
                  <span
                    key={`${key}:${value}`}
                    className={`inline-flex items-center gap-1.5 px-2.5 py-1 text-xs rounded-md font-medium border ${colors.bg} ${colors.border} ${colors.text}`}
                  >
                    <Tag className="w-3 h-3" />
                    <span className={colors.keyColor}>{key}</span>
                    <span className="text-gray-400">:</span>
                    {value}
                    <button
                      onClick={() => clearLabelFilter(key, value)}
                      className="hover:text-gray-900"
                    >
                      <X className="w-3 h-3" />
                    </button>
                  </span>
                );
              })
            )}
            {(startTime || endTime) && (
              <span className="inline-flex items-center gap-1.5 px-2.5 py-1 text-xs bg-purple-100 text-purple-700 rounded-md font-medium">
                <Clock className="w-3 h-3" />
                {startTime && endTime
                  ? `${formatTime(startTime)} - ${formatTime(endTime)}`
                  : startTime
                  ? `从 ${formatTime(startTime)}`
                  : `到 ${formatTime(endTime!)}`}
                <button
                  onClick={() => {
                    setStartTime('');
                    setEndTime('');
                  }}
                  className="hover:text-purple-900"
                >
                  <X className="w-3 h-3" />
                </button>
              </span>
            )}
            <button
              onClick={clearAllFilters}
              className="text-xs text-red-600 hover:text-red-700 flex items-center gap-1 font-medium"
            >
              <FilterX className="w-3 h-3" />
              清除全部
            </button>
          </div>
        )}
        {topologyJumpContext && (
          <div className="mt-3 rounded-lg border border-cyan-200 bg-cyan-50 px-3 py-2">
            <div className="flex items-center justify-between gap-2">
              <div className="text-xs text-cyan-800">
                来自拓扑跳转：
                {topologyJumpContext.sourceService || topologyJumpContext.targetService ? (
                  <>
                    <span className="font-semibold">{topologyJumpContext.sourceService || '未指定源端'}</span> →{' '}
                    <span className="font-semibold">{topologyJumpContext.targetService || '未指定目标端'}</span>
                  </>
                ) : (
                  <span className="font-semibold">当前筛选承接拓扑上下文</span>
                )}
                {topologyJumpContext.timeWindow ? (
                  <>
                    {' '}，窗口 <span className="font-semibold">{topologyJumpContext.timeWindow}</span>
                  </>
                ) : null}
                {(correlationTraceIds.length > 0 || correlationRequestIds.length > 0) && (
                  <span>
                    {' '}· 精确关联 trace_id {correlationTraceIds.length} / request_id {correlationRequestIds.length}
                  </span>
                )}
              </div>
              <button
                onClick={() => {
                  setTopologyJumpContext(null);
                  setTraceIdFilter('');
                  setRequestIdFilter('');
                  setCorrelationTraceIds([]);
                  setCorrelationRequestIds([]);
                  setAnchorTime(null);
                }}
                className="text-cyan-700 hover:text-cyan-900"
                title="隐藏拓扑上下文"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
          </div>
        )}
        {copyNotice && (
          <div className="mt-3 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-700">
            {copyNotice}
          </div>
        )}
      </div>

      {/* 主内容区 */}
      <div className="flex-1 flex overflow-hidden">
        {/* 左侧筛选面板 */}
        {showFilterPanel && (
          <>
            {filterPanelCollapsed ? (
              <div className="w-12 bg-white border-r border-gray-200 shrink-0 flex flex-col py-2">
                <div className="flex flex-col items-center gap-3">
                  <button
                    type="button"
                    onClick={() => {
                      setFilterPanelCollapsed(false);
                      setShowTimeFilter(true);
                    }}
                    className="relative p-1.5 rounded-md text-gray-500 hover:bg-gray-100 hover:text-gray-700"
                    title="快速打开时间筛选"
                  >
                    <Clock className="w-4 h-4" />
                    {(startTime || endTime) && <span className="absolute -right-0.5 -top-0.5 h-2 w-2 rounded-full bg-blue-500" />}
                  </button>
                  <div className="text-[10px] text-gray-500 [writing-mode:vertical-rl] tracking-wide">
                    筛选 {activeFilterCount > 0 ? `(${activeFilterCount})` : ''}
                  </div>
                </div>
                <div className="mt-auto border-t border-gray-200 pt-2 px-1">
                  <button
                    type="button"
                    onClick={() => setFilterPanelCollapsed(false)}
                    className="w-full flex items-center justify-center py-1.5 rounded-md text-gray-500 hover:bg-gray-100 hover:text-gray-700"
                    title="展开筛选面板"
                  >
                    <ChevronRight className="w-4 h-4" />
                  </button>
                </div>
              </div>
            ) : (
              <>
                <div
                  className="bg-white border-r border-gray-200 overflow-y-auto shrink-0 flex flex-col"
                  style={{ width: filterPanelWidth }}
                >
                  <div className="flex items-center border-b border-gray-100 px-3 py-2">
                    <div className="flex items-center gap-2 text-xs font-semibold text-gray-700">
                      <PanelLeft className="w-3.5 h-3.5" />
                      筛选条件
                    </div>
                  </div>

                  <div className="flex-1 py-2">
                    {/* 日志级别筛选 */}
                    {renderFilterGroup(
                      'levels',
                      '日志级别',
                      <LayoutGrid className="w-4 h-4" />,
                      selectedLevels.length,
                      () => setSelectedLevels([]),
                      <div className="space-y-1">
                        {LOG_LEVELS.map((level) => {
                          const colors = LEVEL_COLORS[level];
                          const isSelected = selectedLevels.includes(level);
                          const levelCount = levelCountMap[level] || 0;
                          return (
                            <button
                              key={level}
                              onClick={() => toggleLevel(level)}
                              className={`w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm transition-all ${
                                isSelected
                                  ? `${colors.bg} ${colors.text} font-medium`
                                  : 'hover:bg-gray-100 text-gray-600'
                              }`}
                            >
                              <span
                                className="w-2 h-2 rounded-full"
                                style={{ backgroundColor: colors.solid }}
                              />
                              <span>{level}</span>
                              <span className="ml-auto inline-flex items-center gap-1.5">
                                <span className="text-[10px] text-gray-500 font-mono">{levelCount.toLocaleString()}</span>
                                {isSelected && <Check className="w-4 h-4" />}
                              </span>
                            </button>
                          );
                        })}
                      </div>
                    )}

                    {/* 服务筛选 */}
                    {availableServices.length > 0 && renderFilterGroup(
                      'services',
                      '服务',
                      <Server className="w-4 h-4" />,
                      selectedServices.length,
                      () => setSelectedServices([]),
                      <div className="space-y-2">
                        <div className="relative">
                          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-400" />
                          <input
                            type="text"
                            value={serviceSearchQuery}
                            onChange={(e) => setServiceSearchQuery(e.target.value)}
                            placeholder={`筛选服务（共 ${availableServices.length} 项）`}
                            className="w-full rounded-md border border-gray-200 pl-8 pr-7 py-1.5 text-xs focus:outline-none focus:ring-2 focus:ring-blue-500"
                          />
                          {serviceSearchQuery && (
                            <button
                              type="button"
                              onClick={() => setServiceSearchQuery('')}
                              className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
                            >
                              <X className="w-3.5 h-3.5" />
                            </button>
                          )}
                        </div>
                        <div className="space-y-1 max-h-56 overflow-y-auto">
                          {filteredAvailableServices.length === 0 && (
                            <div className="px-2 py-2 text-xs text-gray-400 text-center">无匹配服务</div>
                          )}
                          {filteredAvailableServices.map((service) => {
                            const isSelected = selectedServices.includes(service);
                            const serviceCount = serviceCountMap[service] || 0;
                            return (
                              <button
                                key={service}
                                onClick={() => toggleService(service)}
                                className={`w-full flex items-center justify-between px-3 py-2 rounded-lg text-sm transition-all ${
                                  isSelected
                                    ? 'bg-blue-50 text-blue-700 font-medium'
                                    : 'hover:bg-gray-100 text-gray-600'
                                }`}
                              >
                                <span className="truncate">{service}</span>
                                <span className="ml-2 inline-flex items-center gap-1.5 shrink-0">
                                  <span className="text-[10px] text-gray-500 font-mono">{serviceCount.toLocaleString()}</span>
                                  {isSelected && <Check className="w-4 h-4" />}
                                </span>
                              </button>
                            );
                          })}
                        </div>
                      </div>
                    )}

                    {/* 命名空间筛选 */}
                    {availableNamespaces.length > 0 && renderFilterGroup(
                      'namespaces',
                      'Namespace',
                      <Tag className="w-4 h-4" />,
                      selectedNamespaces.length,
                      () => setSelectedNamespaces([]),
                      <div className="space-y-2">
                        <div className="relative">
                          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-400" />
                          <input
                            type="text"
                            value={namespaceSearchQuery}
                            onChange={(e) => setNamespaceSearchQuery(e.target.value)}
                            placeholder={`筛选 namespace（共 ${availableNamespaces.length} 项）`}
                            className="w-full rounded-md border border-gray-200 pl-8 pr-7 py-1.5 text-xs focus:outline-none focus:ring-2 focus:ring-blue-500"
                          />
                          {namespaceSearchQuery && (
                            <button
                              type="button"
                              onClick={() => setNamespaceSearchQuery('')}
                              className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
                            >
                              <X className="w-3.5 h-3.5" />
                            </button>
                          )}
                        </div>
                        <div className="space-y-1 max-h-40 overflow-y-auto">
                          {filteredAvailableNamespaces.length === 0 && (
                            <div className="px-2 py-2 text-xs text-gray-400 text-center">无匹配 namespace</div>
                          )}
                          {filteredAvailableNamespaces.map((namespace) => {
                            const isSelected = selectedNamespaces.includes(namespace);
                            const namespaceCount = namespaceCountMap[namespace] || 0;
                            return (
                              <button
                                key={namespace}
                                onClick={() => toggleNamespace(namespace)}
                                className={`w-full flex items-center justify-between px-3 py-2 rounded-lg text-sm transition-all ${
                                  isSelected
                                    ? 'bg-cyan-50 text-cyan-700 font-medium'
                                    : 'hover:bg-gray-100 text-gray-600'
                                }`}
                              >
                                <span className="truncate font-mono text-xs">{namespace}</span>
                                <span className="ml-2 inline-flex items-center gap-1.5 shrink-0">
                                  <span className="text-[10px] text-gray-500 font-mono">{namespaceCount.toLocaleString()}</span>
                                  {isSelected && <Check className="w-4 h-4" />}
                                </span>
                              </button>
                            );
                          })}
                        </div>
                      </div>
                    )}

                    {/* 主机筛选 */}
                    {availableHosts.length > 0 && renderFilterGroup(
                      'hosts',
                      '主机',
                      <MapPin className="w-4 h-4" />,
                      selectedHosts.length,
                      () => setSelectedHosts([]),
                      <div className="space-y-1 max-h-48 overflow-y-auto">
                        {availableHosts.map((host) => {
                          const isSelected = selectedHosts.includes(host);
                          return (
                            <button
                              key={host}
                              onClick={() => toggleHost(host)}
                              className={`w-full flex items-center justify-between px-3 py-2 rounded-lg text-sm transition-all ${
                                isSelected
                                  ? 'bg-blue-50 text-blue-700 font-medium'
                                  : 'hover:bg-gray-100 text-gray-600'
                              }`}
                            >
                              <span className="truncate font-mono text-xs">{host}</span>
                              {isSelected && <Check className="w-4 h-4 shrink-0" />}
                            </button>
                          );
                        })}
                      </div>
                    )}

                    {/* Label 筛选 */}
                    {Object.entries(availableLabels).length > 0 && renderFilterGroup(
                      'labels',
                      '标签',
                      <Tag className="w-4 h-4" />,
                      Object.values(selectedLabels).flat().length,
                      () => setSelectedLabels({}),
                      <div className="space-y-3">
                        {Object.entries(availableLabels).slice(0, 5).map(([key, values]) => (
                          <div key={key}>
                            <div className="text-xs text-gray-500 mb-1.5 font-medium">{key}</div>
                            <div className="flex flex-wrap gap-1.5">
                              {values.slice(0, 8).map((value) => {
                                const isSelected = isLabelSelected(key, value);
                                return (
                                  <button
                                    key={value}
                                    onClick={() => toggleLabel(key, value)}
                                    className={`px-2 py-1 text-xs rounded-md transition-all ${
                                      isSelected
                                        ? 'bg-blue-100 text-blue-700 font-medium ring-1 ring-blue-300'
                                        : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
                                    }`}
                                  >
                                    {value.length > 15 ? `${value.substring(0, 15)}...` : value}
                                    {isSelected && <Check className="w-3 h-3 inline ml-1" />}
                                  </button>
                                );
                              })}
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>

                  <div className="border-t border-gray-200 p-2">
                    <button
                      type="button"
                      onClick={() => setFilterPanelCollapsed(true)}
                      className="w-full flex items-center rounded-lg px-2 py-1.5 text-xs text-gray-600 hover:bg-gray-100 hover:text-gray-800 transition-colors"
                      title="收起筛选面板"
                    >
                      <ChevronLeft className="w-4 h-4" />
                      <span className="ml-2">收起筛选面板</span>
                    </button>
                  </div>
                </div>

                <div
                  className="w-1 cursor-col-resize hover:bg-blue-400 transition-colors shrink-0"
                  onMouseDown={(e) => handleResizeStart(e, false)}
                />
              </>
            )}
          </>
        )}

        {/* 中间日志列表 */}
        <div className="flex-1 flex flex-col min-w-0 overflow-hidden">
          {isStreamMode ? (
            <>
              <div className="bg-gray-50 border-b border-gray-200 shrink-0 overflow-x-auto">
                <div className="px-4 py-1.5 text-[11px] text-gray-500 border-b border-gray-200">
                  提示: 拖拽列标题右侧 <GripVertical className="inline h-3 w-3 -mt-0.5" /> 可手动调整列宽
                </div>
                <div className="grid gap-2 px-4 py-2.5 min-w-max" style={{ gridTemplateColumns: columnTemplate }}>
                  <div className="relative pr-2 text-xs font-semibold text-gray-500 uppercase tracking-wider">
                    时间
                    <button
                      type="button"
                      onMouseDown={(e) => handleColumnResizeStart(e, 'time')}
                      className="absolute right-0 top-0 h-full w-2 cursor-col-resize text-transparent hover:text-blue-500"
                      title="拖拽调整时间列宽"
                    >
                      |
                    </button>
                  </div>
                  <div className="relative pr-2 text-xs font-semibold text-gray-500 uppercase tracking-wider">
                    服务
                    <button
                      type="button"
                      onMouseDown={(e) => handleColumnResizeStart(e, 'service')}
                      className="absolute right-0 top-0 h-full w-2 cursor-col-resize text-transparent hover:text-blue-500"
                      title="拖拽调整服务列宽"
                    >
                      |
                    </button>
                  </div>
                  <div className="relative pr-2 text-xs font-semibold text-gray-500 uppercase tracking-wider">
                    Pod
                    <button
                      type="button"
                      onMouseDown={(e) => handleColumnResizeStart(e, 'pod')}
                      className="absolute right-0 top-0 h-full w-2 cursor-col-resize text-transparent hover:text-blue-500"
                      title="拖拽调整 Pod 列宽"
                    >
                      |
                    </button>
                  </div>
                  <div className="relative pr-2 text-xs font-semibold text-gray-500 uppercase tracking-wider">
                    级别
                    <button
                      type="button"
                      onMouseDown={(e) => handleColumnResizeStart(e, 'level')}
                      className="absolute right-0 top-0 h-full w-2 cursor-col-resize text-transparent hover:text-blue-500"
                      title="拖拽调整级别列宽"
                    >
                      |
                    </button>
                  </div>

                  <div className="text-xs font-semibold text-gray-500 uppercase tracking-wider">消息</div>
                  <div className="relative pr-2 text-xs font-semibold text-gray-500 uppercase tracking-wider text-center">
                    操作
                    <button
                      type="button"
                      onMouseDown={(e) => handleColumnResizeStart(e, 'action')}
                      className="absolute right-0 top-0 h-full w-2 cursor-col-resize text-transparent hover:text-blue-500"
                      title="拖拽调整操作列宽"
                    >
                      |
                    </button>
                  </div>
                </div>
              </div>

              {/* 日志列表 */}
              <div
                ref={tableRef}
                className="flex-1 overflow-y-auto overflow-x-auto"
              >
                {filteredEvents.length > 0 ? (
                  <VirtualLogList
                    logs={filteredEvents}
                    height={600}
                    columnTemplate={columnTemplate}
                    selectedLogId={expandedLogId}
                    onSelectLog={selectLog}
                    onGoToTopology={(serviceName, namespace) => navigation.goToTopology({
                      serviceName,
                      namespace: normalizeK8sFilterValue(namespace || '') || undefined,
                    })}
                    onGoToAIAnalysis={(log) => navigation.goToAIAnalysis({ logData: log, autoAnalyze: false })}
                    onGoToTraces={(traceId) => navigation.goToTraces({ traceId })}
                    onNearEnd={() => {
                      if (!realtimeMode && hasMorePages && !loadingMore) {
                        void loadMoreLogs();
                      }
                    }}
                  />
                ) : (
                  <div className="flex items-center justify-center h-full">
                    <EmptyState
                      icon={<Search className="w-12 h-12 text-gray-300" />}
                      title="没有找到匹配的日志"
                      description="尝试调整搜索条件或过滤选项"
                    />
                  </div>
                )}
              </div>

              {!realtimeMode && (
                <div className="shrink-0 border-t border-gray-200 bg-white px-4 py-2.5 flex items-center justify-between">
                  <span className="text-xs text-gray-500">
                    已加载 {allEvents.length.toLocaleString()} 条（{loadedPageCount} 页）
                    {hasMorePages ? '，滚动到底会自动加载' : '，已到当前查询末尾'}
                    {anchorTime ? `，锚点 ${formatTime(anchorTime)}` : ''}
                  </span>
                  <button
                    type="button"
                    onClick={() => void loadMoreLogs()}
                    disabled={!hasMorePages || loadingMore}
                    className="px-3 py-1.5 text-xs rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    {loadingMore ? '加载中...' : hasMorePages ? '加载更多' : '无更多数据'}
                  </button>
                </div>
              )}
            </>
          ) : (
            <>
              <div className="bg-gray-50 border-b border-gray-200 shrink-0 px-4 py-2 text-xs text-gray-600">
                Pattern 聚合视图：用于快速定位重复错误、噪声模式与高频异常。
              </div>
              <div className="flex-1 overflow-y-auto bg-white">
                {aggregatedPatterns.length > 0 ? (
                  <div>
                    {aggregatedPatterns.map((pattern, index: number) => (
                      <AggregatedLogRow
                        key={`${pattern.pattern_hash || pattern.pattern}-${index}`}
                        pattern={pattern}
                        onSelectLog={(event) => selectPatternSampleLog(event as LogEvent)}
                        defaultExpanded={index === 0}
                      />
                    ))}
                  </div>
                ) : (
                  <div className="flex items-center justify-center h-full">
                    <EmptyState
                      icon={<LayoutGrid className="w-12 h-12 text-gray-300" />}
                      title="没有匹配的日志模式"
                      description="尝试缩小时间范围或调整筛选条件"
                    />
                  </div>
                )}
              </div>
              <div className="shrink-0 border-t border-gray-200 bg-white px-4 py-2.5 text-xs text-gray-500">
                统计：pattern {aggregatedPatterns.length.toLocaleString()}，聚合覆盖 {(Number(aggregatedData?.aggregation_ratio || 0) * 100).toFixed(1)}%
              </div>
            </>
          )}
        </div>

        {/* 右侧侧边栏 */}
        {showSidebar && (
          <>
            <div
              className="w-1 cursor-col-resize hover:bg-blue-400 transition-colors shrink-0"
              onMouseDown={(e) => handleResizeStart(e, true)}
            />
            {renderSidebar()}
          </>
        )}
      </div>

      {/* 调整大小时的遮罩 */}
      {isResizing && (
        <div className="fixed inset-0 z-50 cursor-col-resize" />
      )}
    </div>
  );
};

export default LogsExplorer;
