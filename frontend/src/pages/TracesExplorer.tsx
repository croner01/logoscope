/**
 * 追踪浏览器页面
 * 参考 Datadog 设计风格
 */
import React, { useState, useMemo, useEffect } from 'react';
import { useLocation } from 'react-router-dom';
import { useTraces, useTraceStats, useTraceSpans } from '../hooks/useApi';
import { useNavigation } from '../hooks/useNavigation';
import LoadingState from '../components/common/LoadingState';
import ErrorState from '../components/common/ErrorState';
import EmptyState from '../components/common/EmptyState';
import { formatTimeCST, formatTimeUTC, formatDuration } from '../utils/formatters';
import { Search, RefreshCw, X, Clock, AlertCircle, FileText, Network, BrainCircuit, Activity } from 'lucide-react';
import TraceTimeline from '../components/traces/TraceTimeline';

interface TraceItem {
  trace_id: string;
  service_name: string;
  operation_name: string;
  start_time: string;
  duration_ms: number;
  status_code?: string;
  status?: string;
}

interface SpanItem {
  trace_id: string;
  span_id: string;
  parent_span_id: string;
  service_name: string;
  operation_name: string;
  start_time: string;
  duration_ms: number;
  status: string;
  tags: Record<string, unknown>;
}

const resolveSpanDurationMs = (span: SpanItem | null | undefined): number => {
  const direct = Number(span?.duration_ms);
  if (Number.isFinite(direct) && direct > 0) {
    return direct;
  }
  const tags = span?.tags || {};
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

const DEFAULT_TRACE_TIME_WINDOW = '24 HOUR';

const TRACE_TIME_WINDOW_OPTIONS = [
  '15 MINUTE',
  '30 MINUTE',
  '1 HOUR',
  '6 HOUR',
  '24 HOUR',
  '7 DAY',
];

const toIsoTimeOrUndefined = (value: string): string | undefined => {
  const text = String(value || '').trim();
  if (!text) {
    return undefined;
  }
  const dt = new Date(text);
  if (Number.isNaN(dt.getTime())) {
    return undefined;
  }
  return dt.toISOString();
};

const TracesExplorer: React.FC = () => {
  const location = useLocation();
  const navigation = useNavigation();
  const [searchQuery, setSearchQuery] = useState('');
  const [selectedTrace, setSelectedTrace] = useState<TraceItem | null>(null);
  const [selectedSpan, setSelectedSpan] = useState<SpanItem | null>(null);
  const [statusFilter, setStatusFilter] = useState<string>('all');
  const [pageSize, setPageSize] = useState(200);
  const [page, setPage] = useState(1);
  const [timeWindow, setTimeWindow] = useState(DEFAULT_TRACE_TIME_WINDOW);
  const [startTimeInput, setStartTimeInput] = useState('');
  const [endTimeInput, setEndTimeInput] = useState('');
  const queryParams = useMemo(() => new URLSearchParams(location.search), [location.search]);
  const serviceFilter = queryParams.get('service') || undefined;
  const traceIdFilter = queryParams.get('trace_id') || undefined;
  const tracesQuery = useMemo(() => {
    const startTime = toIsoTimeOrUndefined(startTimeInput);
    const endTime = toIsoTimeOrUndefined(endTimeInput);
    const query: Record<string, unknown> = {
      limit: pageSize,
      offset: Math.max((page - 1) * pageSize, 0),
      service_name: serviceFilter,
      trace_id: traceIdFilter,
      start_time: startTime,
      end_time: endTime,
    };
    if (!startTime && !endTime) {
      query.time_window = timeWindow;
    }
    return query;
  }, [endTimeInput, page, pageSize, serviceFilter, startTimeInput, timeWindow, traceIdFilter]);
  const statsQuery = useMemo(() => {
    const startTime = toIsoTimeOrUndefined(startTimeInput);
    const endTime = toIsoTimeOrUndefined(endTimeInput);
    const query: Record<string, unknown> = {
      start_time: startTime,
      end_time: endTime,
    };
    if (!startTime && !endTime) {
      query.time_window = timeWindow;
    }
    return query;
  }, [endTimeInput, startTimeInput, timeWindow]);

  const { data, loading, error, refetch } = useTraces(tracesQuery);
  const { data: statsData } = useTraceStats(statsQuery);
  const { data: spansData, loading: spansLoading } = useTraceSpans(selectedTrace?.trace_id || null);

  useEffect(() => {
    setPage(1);
  }, [serviceFilter, traceIdFilter, pageSize, timeWindow, startTimeInput, endTimeInput]);

  useEffect(() => {
    if (traceIdFilter) {
      setSearchQuery(traceIdFilter);
    } else if (serviceFilter) {
      setSearchQuery(serviceFilter);
    }
  }, [serviceFilter, traceIdFilter]);

  useEffect(() => {
    if (!traceIdFilter || !data?.traces?.length) {
      return;
    }
    const matchedTrace = data.traces.find((trace) => trace.trace_id === traceIdFilter);
    if (matchedTrace) {
      setSelectedTrace(matchedTrace);
    }
  }, [data?.traces, traceIdFilter]);

  useEffect(() => {
    if (!selectedTrace?.trace_id) {
      return;
    }
    const existsInCurrentPage = Boolean(data?.traces?.some((trace) => trace.trace_id === selectedTrace.trace_id));
    if (!existsInCurrentPage) {
      setSelectedTrace(null);
      setSelectedSpan(null);
    }
  }, [data?.traces, selectedTrace?.trace_id]);

  // 过滤追踪
  const filteredTraces = useMemo(() => {
    if (!data?.traces) return [];

    let traces = data.traces;

    if (searchQuery) {
      const query = searchQuery.toLowerCase();
      traces = traces.filter(trace =>
        trace.trace_id.toLowerCase().includes(query) ||
        trace.service_name.toLowerCase().includes(query) ||
        trace.operation_name.toLowerCase().includes(query)
      );
    }

    if (statusFilter !== 'all') {
      traces = traces.filter(trace => {
        if (statusFilter === 'error') {
          return trace.status_code === 'STATUS_CODE_ERROR';
        }
        return trace.status_code !== 'STATUS_CODE_ERROR';
      });
    }

    return traces;
  }, [data?.traces, searchQuery, statusFilter]);

  const totalTraces = Number(data?.total || 0);
  const currentOffset = Number(data?.offset || Math.max((page - 1) * pageSize, 0));
  const pageCount = Math.max(1, Math.ceil(totalTraces / Math.max(pageSize, 1)));
  const pageStart = totalTraces > 0 ? currentOffset + 1 : 0;
  const pageEnd = totalTraces > 0 ? Math.min(currentOffset + Number(data?.count || 0), totalTraces) : 0;
  const hasPrevPage = page > 1;
  const hasNextPage = Boolean(data?.has_more || currentOffset + Number(data?.count || 0) < totalTraces);

  if (loading) return <LoadingState message="加载追踪数据..." />;
  if (error) return <ErrorState message={error.message} onRetry={refetch} />;

  return (
    <div className="flex flex-col h-full">
      {/* 页面标题 */}
      <div className="page-header mb-4">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-xl flex items-center justify-center" style={{ background: 'rgba(139,92,246,0.1)', color: '#7c3aed' }}>
            <Activity size={18} />
          </div>
          <div>
            <h1 className="page-title">追踪浏览器</h1>
            <p className="text-xs mt-0.5" style={{ color: 'var(--app-text-subtle)' }}>分析和调试分布式追踪</p>
          </div>
        </div>
        <button onClick={refetch} className="btn btn-secondary">
          <RefreshCw size={13} />
          刷新
        </button>
      </div>

      {/* 统计卡片 */}
      {statsData && (
        <div className="grid grid-cols-4 gap-3 mb-4">
          <div className="kpi-card tone-blue">
            <div className="kpi-label">总追踪数</div>
            <div className="kpi-value">{totalTraces.toLocaleString()}</div>
          </div>
          <div className="kpi-card tone-teal">
            <div className="kpi-label">平均延迟</div>
            <div className="kpi-value">
              {statsData.avg_duration || statsData.avg_latency
                ? formatDuration(Number(statsData.avg_duration ?? statsData.avg_latency ?? 0))
                : '-'}
            </div>
          </div>
          <div className="kpi-card tone-purple">
            <div className="kpi-label">P99 延迟</div>
            <div className="kpi-value">
              {statsData.p99_duration || statsData.p99_latency
                ? formatDuration(Number(statsData.p99_duration ?? statsData.p99_latency ?? 0))
                : '-'}
            </div>
          </div>
          <div className="kpi-card tone-red">
            <div className="kpi-label">错误率</div>
            <div className="kpi-value">
              {statsData.error_rate !== undefined && statsData.error_rate !== null
                ? `${(Number(statsData.error_rate) * 100).toFixed(2)}%`
                : '0%'}
            </div>
          </div>
        </div>
      )}

      {/* 工具栏 */}
      <div className="card p-3 mb-4">
        <div className="grid grid-cols-1 gap-2 lg:grid-cols-8 items-center">
          <div className="lg:col-span-2 relative">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5" style={{ color: 'var(--app-text-subtle)' }} />
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="搜索 Trace ID、服务名、操作名…"
              className="input input-sm pl-8"
            />
          </div>
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="input input-sm"
          >
            <option value="all">全部状态</option>
            <option value="success">成功</option>
            <option value="error">错误</option>
          </select>
          <select
            value={timeWindow}
            onChange={(e) => setTimeWindow(e.target.value)}
            className="input input-sm"
            disabled={Boolean(startTimeInput || endTimeInput)}
            title={startTimeInput || endTimeInput ? '已启用绝对时间范围，时间窗口暂不生效' : '时间窗口'}
          >
            {TRACE_TIME_WINDOW_OPTIONS.map((option) => (
              <option key={option} value={option}>{option}</option>
            ))}
          </select>
          <input
            type="datetime-local"
            value={startTimeInput}
            onChange={(e) => setStartTimeInput(e.target.value)}
            className="input input-sm"
            title="开始时间（可选）"
          />
          <input
            type="datetime-local"
            value={endTimeInput}
            onChange={(e) => setEndTimeInput(e.target.value)}
            className="input input-sm"
            title="结束时间（可选）"
          />
          <select
            value={pageSize}
            onChange={(e) => setPageSize(Number(e.target.value))}
            className="input input-sm"
          >
            <option value={100}>100 条</option>
            <option value={200}>200 条</option>
            <option value={500}>500 条</option>
            <option value={1000}>1000 条</option>
          </select>
          <button
            onClick={() => { setStartTimeInput(''); setEndTimeInput(''); }}
            className="btn btn-ghost text-xs"
            title="清空时间范围并回退到时间窗口"
          >
            清空时间
          </button>
          <input
            type="number"
            min={1}
            max={Math.max(pageCount, 1)}
            value={page}
            onChange={(e) => {
              const next = Number(e.target.value) || 1;
              setPage(Math.max(1, Math.min(next, Math.max(pageCount, 1))));
            }}
            className="input input-sm"
            title="页码"
          />
          <div className="lg:col-span-full text-xs" style={{ color: 'var(--app-text-subtle)' }}>
            当前范围: {startTimeInput || endTimeInput ? '绝对时间筛选' : `时间窗口 ${timeWindow}`}，返回 {data?.count || 0} 条，累计 {totalTraces} 条
          </div>
        </div>
      </div>

      {/* 主内容区 */}
      <div className="flex-1 flex gap-4 overflow-hidden">
        {/* 追踪列表 */}
        <div className={`card overflow-hidden transition-all ${selectedTrace ? 'flex-1' : 'w-full'}`}>
          <div className="overflow-auto h-full">
            {filteredTraces.length > 0 ? (
              <table className="data-table">
                <thead>
                  <tr>
                    <th>时间</th>
                    <th>Trace ID</th>
                    <th>服务</th>
                    <th>操作</th>
                    <th>延迟</th>
                    <th>状态</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredTraces.map((trace) => {
                    const isError = trace.status_code === 'STATUS_CODE_ERROR';
                    const isSelected = selectedTrace?.trace_id === trace.trace_id;
                    return (
                      <tr
                        key={trace.trace_id}
                        onClick={() => setSelectedTrace(trace)}
                        className="cursor-pointer"
                        style={isSelected ? { background: 'var(--brand-primary-soft)' } : {}}
                      >
                        <td className="whitespace-nowrap text-xs" style={{ color: 'var(--app-text-subtle)' }} title={`UTC: ${formatTimeUTC(trace.start_time)}`}>
                          {formatTimeCST(trace.start_time)}
                        </td>
                        <td className="font-mono text-xs" style={{ color: 'var(--app-text-muted)' }}>
                          {trace.trace_id.substring(0, 16)}…
                        </td>
                        <td className="text-xs font-semibold" style={{ color: 'var(--brand-primary)' }}>
                          {trace.service_name}
                        </td>
                        <td className="text-xs max-w-xs truncate" style={{ color: 'var(--app-text)' }}>
                          {trace.operation_name}
                        </td>
                        <td className="whitespace-nowrap text-xs">
                          <span
                            className="font-semibold"
                            style={{
                              color: trace.duration_ms > 1000
                                ? 'var(--color-error-dark)'
                                : trace.duration_ms > 500
                                ? 'var(--color-warning-dark)'
                                : 'var(--color-success-dark)',
                            }}
                          >
                            {formatDuration(trace.duration_ms)}
                          </span>
                        </td>
                        <td>
                          {isError ? (
                            <span className="inline-flex items-center gap-1 text-xs" style={{ color: 'var(--color-error-dark)' }}>
                              <AlertCircle size={11} />
                              错误
                            </span>
                          ) : (
                            <span className="inline-flex items-center gap-1 text-xs" style={{ color: 'var(--color-success-dark)' }}>
                              <Clock size={11} />
                              正常
                            </span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            ) : (
              <EmptyState title="没有找到匹配的追踪" description="尝试调整搜索条件" />
            )}
          </div>
        </div>

        {/* 追踪详情面板 */}
        {selectedTrace && (
          <div className="w-80 card overflow-hidden flex flex-col" style={{ minWidth: '20rem' }}>
            <div className="card-header">
              <div className="card-title">追踪详情</div>
              <button
                onClick={() => { setSelectedTrace(null); setSelectedSpan(null); }}
                className="btn btn-ghost btn-icon"
              >
                <X size={14} />
              </button>
            </div>
            <div className="card-body overflow-auto flex-1">
              <div className="space-y-3">
                {[
                  { label: 'Trace ID', value: <span className="font-mono break-all text-xs">{selectedTrace.trace_id}</span> },
                  { label: '开始时间', value: <span title={`UTC: ${formatTimeUTC(selectedTrace.start_time)}`}>{formatTimeCST(selectedTrace.start_time)}</span> },
                  { label: '服务', value: <span className="font-semibold" style={{ color: 'var(--brand-primary)' }}>{selectedTrace.service_name}</span> },
                  { label: '操作', value: selectedTrace.operation_name },
                  { label: '持续时间', value: <span className="font-semibold">{formatDuration(selectedTrace.duration_ms)}</span> },
                ].map(({ label, value }) => (
                  <div key={label}>
                    <div className="section-label mb-0.5">{label}</div>
                    <div className="text-sm" style={{ color: 'var(--app-text)' }}>{value}</div>
                  </div>
                ))}

                {/* 状态 */}
                <div>
                  <div className="section-label mb-1">状态</div>
                  {(selectedTrace.status_code === 'STATUS_CODE_ERROR' || (!selectedTrace.status_code && selectedTrace.status === 'error')) ? (
                    <span className="badge badge-critical">错误</span>
                  ) : (selectedTrace.status_code === 'STATUS_CODE_OK' || (!selectedTrace.status_code && selectedTrace.status === 'ok')) ? (
                    <span className="badge badge-low">成功</span>
                  ) : (
                    <span className="badge badge-neutral">{selectedTrace.status_code || selectedTrace.status || '未知'}</span>
                  )}
                </div>

                {/* 快速操作 */}
                <div className="pt-2" style={{ borderTop: '1px solid var(--app-border)' }}>
                  <div className="section-label mb-2">快速操作</div>
                  <div className="space-y-1.5">
                    <button
                      onClick={() => navigation.goToLogs({ serviceName: selectedTrace.service_name, traceId: selectedTrace.trace_id })}
                      className="w-full flex items-center gap-2 px-3 py-2 rounded-lg text-xs font-medium transition-colors"
                      style={{ background: 'var(--brand-primary-soft)', color: 'var(--brand-primary)', border: '1px solid rgba(59,130,246,0.2)' }}
                      onMouseEnter={e => (e.currentTarget.style.opacity = '0.8')}
                      onMouseLeave={e => (e.currentTarget.style.opacity = '1')}
                    >
                      <FileText size={13} />
                      查看关联日志
                    </button>
                    <button
                      onClick={() => navigation.goToTopology({ serviceName: selectedTrace.service_name })}
                      className="w-full flex items-center gap-2 px-3 py-2 rounded-lg text-xs font-medium transition-colors"
                      style={{ background: 'var(--color-success-soft)', color: 'var(--color-success-dark)', border: '1px solid rgba(16,185,129,0.2)' }}
                      onMouseEnter={e => (e.currentTarget.style.opacity = '0.8')}
                      onMouseLeave={e => (e.currentTarget.style.opacity = '1')}
                    >
                      <Network size={13} />
                      查看服务拓扑
                    </button>
                    <button
                      onClick={() => navigation.goToAIAnalysis({ traceId: selectedTrace.trace_id, serviceName: selectedTrace.service_name })}
                      className="w-full flex items-center gap-2 px-3 py-2 rounded-lg text-xs font-medium transition-colors"
                      style={{ background: 'rgba(139,92,246,0.08)', color: '#7c3aed', border: '1px solid rgba(139,92,246,0.15)' }}
                      onMouseEnter={e => (e.currentTarget.style.opacity = '0.8')}
                      onMouseLeave={e => (e.currentTarget.style.opacity = '1')}
                    >
                      <BrainCircuit size={13} />
                      AI 诊断链路
                    </button>
                  </div>
                </div>

                {/* Span 时间线 */}
                <div className="pt-2" style={{ borderTop: '1px solid var(--app-border)' }}>
                  <div className="flex items-center justify-between mb-2">
                    <div className="section-label">Span 时间线</div>
                    {spansData && spansData.length > 0 && (
                      <span className="text-xs" style={{ color: 'var(--app-text-subtle)' }}>{spansData.length} 个 spans</span>
                    )}
                  </div>
                  {spansLoading ? (
                    <div className="flex items-center justify-center h-40">
                      <div className="animate-spin rounded-full h-7 w-7 border-2" style={{ borderColor: 'var(--brand-primary)', borderTopColor: 'transparent' }} />
                    </div>
                  ) : spansData && spansData.length > 0 ? (
                    <TraceTimeline
                      spans={spansData}
                      selectedSpanId={selectedSpan?.span_id || null}
                      onSpanClick={(span) => setSelectedSpan(span)}
                    />
                  ) : (
                    <div className="rounded-xl p-6 flex items-center justify-center" style={{ background: 'var(--app-surface-muted)', minHeight: '6rem' }}>
                      <p className="text-xs" style={{ color: 'var(--app-text-subtle)' }}>该追踪暂无 Span 数据</p>
                    </div>
                  )}
                </div>

                {/* Span 详情面板 */}
                {selectedSpan && (
                  <div className="pt-2" style={{ borderTop: '1px solid var(--app-border)' }}>
                    <div className="flex items-center justify-between mb-2">
                      <div className="section-label">Span 详情</div>
                      <button onClick={() => setSelectedSpan(null)} className="btn btn-ghost btn-icon">
                        <X size={12} />
                      </button>
                    </div>
                    <div className="space-y-2">
                      <div className="grid grid-cols-2 gap-2">
                        {[
                          { label: 'Service', value: selectedSpan.service_name },
                          { label: 'Operation', value: selectedSpan.operation_name },
                          { label: 'Duration', value: <span className="font-mono">{formatDuration(resolveSpanDurationMs(selectedSpan))}</span> },
                          { label: 'Start Time', value: <span title={`UTC: ${formatTimeUTC(selectedSpan.start_time)}`}>{formatTimeCST(selectedSpan.start_time)}</span> },
                        ].map(({ label, value }) => (
                          <div key={label} className="p-2 rounded-lg" style={{ background: 'var(--app-surface-muted)', border: '1px solid var(--app-border)' }}>
                            <div className="section-label mb-0.5">{label}</div>
                            <div className="text-xs" style={{ color: 'var(--app-text)' }}>{value}</div>
                          </div>
                        ))}
                      </div>
                      {selectedSpan.status && (
                        <div className="p-2 rounded-lg" style={{ background: 'var(--app-surface-muted)', border: '1px solid var(--app-border)' }}>
                          <div className="section-label mb-0.5">Status</div>
                          <div className="text-xs">
                            {selectedSpan.status === 'STATUS_CODE_ERROR' ? (
                              <span style={{ color: 'var(--color-error-dark)' }}>错误</span>
                            ) : selectedSpan.status === 'STATUS_CODE_OK' ? (
                              <span style={{ color: 'var(--color-success-dark)' }}>成功</span>
                            ) : (
                              <span style={{ color: 'var(--app-text-muted)' }}>{selectedSpan.status}</span>
                            )}
                          </div>
                        </div>
                      )}
                      {selectedSpan.tags && Object.keys(selectedSpan.tags).length > 0 && (
                        <div className="p-2 rounded-lg" style={{ border: '1px solid var(--app-border)' }}>
                          <div className="section-label mb-1.5">Tags</div>
                          <div className="flex flex-wrap gap-1">
                            {Object.entries(selectedSpan.tags).map(([key, value]) => (
                              <span key={key} className="inline-flex items-center px-1.5 py-0.5 rounded text-xs font-mono" style={{ background: 'var(--brand-primary-soft)', color: 'var(--brand-primary)', border: '1px solid rgba(59,130,246,0.15)' }}>
                                {key}:<span className="ml-0.5 opacity-80">{String(value)}</span>
                              </span>
                            ))}
                          </div>
                        </div>
                      )}
                    </div>
                  </div>
                )}
              </div>
            </div>
          </div>
        )}
      </div>

      {/* 分页 */}
      <div className="mt-3 flex items-center justify-between text-xs" style={{ color: 'var(--app-text-subtle)' }}>
        <div>
          显示 {filteredTraces.length} 条追踪（第 {pageStart}–{pageEnd} 条，共 {totalTraces} 条）
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setPage((prev) => Math.max(prev - 1, 1))}
            disabled={!hasPrevPage}
            className="btn btn-ghost text-xs disabled:opacity-40"
          >
            上一页
          </button>
          <span>第 {page} / {pageCount} 页</span>
          <button
            onClick={() => { if (!hasNextPage) return; setPage((prev) => prev + 1); }}
            disabled={!hasNextPage}
            className="btn btn-ghost text-xs disabled:opacity-40"
          >
            下一页
          </button>
        </div>
      </div>
    </div>
  );
};

export default TracesExplorer;
