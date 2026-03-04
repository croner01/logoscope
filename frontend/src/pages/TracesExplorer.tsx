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
import { Search, RefreshCw, X, Clock, AlertCircle, FileText, Network, BrainCircuit } from 'lucide-react';
import TraceTimeline from '../components/traces/TraceTimeline';

const resolveSpanDurationMs = (span: any): number => {
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

const TracesExplorer: React.FC = () => {
  const location = useLocation();
  const navigation = useNavigation();
  const [searchQuery, setSearchQuery] = useState('');
  const [selectedTrace, setSelectedTrace] = useState<any>(null);
  const [selectedSpan, setSelectedSpan] = useState<any>(null);
  const [statusFilter, setStatusFilter] = useState<string>('all');
  const [limit, setLimit] = useState(100);
  const queryParams = useMemo(() => new URLSearchParams(location.search), [location.search]);
  const serviceFilter = queryParams.get('service') || undefined;
  const traceIdFilter = queryParams.get('trace_id') || undefined;

  const { data, loading, error, refetch } = useTraces({
    limit,
    service_name: serviceFilter,
    trace_id: traceIdFilter,
  });
  const { data: statsData } = useTraceStats();
  const { data: spansData, loading: spansLoading } = useTraceSpans(selectedTrace?.trace_id || null);

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

  // 过滤追踪
  const filteredTraces = useMemo(() => {
    if (!data?.traces) return [];

    let traces = data.traces;

    // 搜索过滤
    if (searchQuery) {
      const query = searchQuery.toLowerCase();
      traces = traces.filter(trace =>
        trace.trace_id.toLowerCase().includes(query) ||
        trace.service_name.toLowerCase().includes(query) ||
        trace.operation_name.toLowerCase().includes(query)
      );
    }

    // 状态过滤
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

  if (loading) return <LoadingState message="加载追踪数据..." />;
  if (error) return <ErrorState message={error.message} onRetry={refetch} />;

  return (
    <div className="flex flex-col h-full">
      {/* 页面标题 */}
      <div className="flex items-center justify-between mb-4">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">追踪浏览器</h1>
          <p className="text-gray-500 mt-1">分析和调试分布式追踪</p>
        </div>
        <button
          onClick={refetch}
          className="flex items-center px-3 py-2 text-gray-600 hover:bg-gray-100 rounded-lg transition-colors"
        >
          <RefreshCw className="w-4 h-4 mr-2" />
          刷新
        </button>
      </div>

      {/* 统计卡片 */}
      {statsData && (
        <div className="grid grid-cols-4 gap-4 mb-4">
          <div className="bg-white rounded-lg shadow-md p-4">
            <div className="text-sm text-gray-500">总追踪数</div>
            <div className="text-2xl font-bold text-gray-900">{data?.total || 0}</div>
          </div>
          <div className="bg-white rounded-lg shadow-md p-4">
            <div className="text-sm text-gray-500">平均延迟</div>
            <div className="text-2xl font-bold text-gray-900">
              {statsData.avg_duration || statsData.avg_latency ? formatDuration(statsData.avg_duration || statsData.avg_latency) : '-'}
            </div>
          </div>
          <div className="bg-white rounded-lg shadow-md p-4">
            <div className="text-sm text-gray-500">P99 延迟</div>
            <div className="text-2xl font-bold text-gray-900">
              {statsData.p99_duration || statsData.p99_latency ? formatDuration(statsData.p99_duration || statsData.p99_latency) : '-'}
            </div>
          </div>
          <div className="bg-white rounded-lg shadow-md p-4">
            <div className="text-sm text-gray-500">错误率</div>
            <div className="text-2xl font-bold text-red-600">
              {statsData.error_rate !== undefined && statsData.error_rate !== null
                ? `${(statsData.error_rate * 100).toFixed(2)}%`
                : '0%'}
            </div>
          </div>
        </div>
      )}

      {/* 搜索栏 */}
      <div className="bg-white rounded-lg shadow-md p-4 mb-4">
        <div className="flex items-center space-x-4">
          <div className="flex-1 relative">
            <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-5 h-5 text-gray-400" />
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="搜索 Trace ID、服务名、操作名..."
              className="w-full pl-10 pr-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="border border-gray-300 rounded-lg px-3 py-2"
          >
            <option value="all">全部状态</option>
            <option value="success">成功</option>
            <option value="error">错误</option>
          </select>
          <select
            value={limit}
            onChange={(e) => setLimit(Number(e.target.value))}
            className="border border-gray-300 rounded-lg px-3 py-2"
          >
            <option value={50}>50 条</option>
            <option value={100}>100 条</option>
            <option value={200}>200 条</option>
          </select>
        </div>
      </div>

      {/* 主内容区 */}
      <div className="flex-1 flex gap-4 overflow-hidden">
        {/* 追踪列表 */}
        <div className={`bg-white rounded-lg shadow-md overflow-hidden ${selectedTrace ? 'w-2/3' : 'w-full'}`}>
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
                  {filteredTraces.map((trace) => (
                    <tr
                      key={trace.trace_id}
                      onClick={() => setSelectedTrace(trace)}
                      className={`cursor-pointer ${
                        selectedTrace?.trace_id === trace.trace_id ? 'bg-blue-50' : ''
                      }`}
                    >
                      <td className="whitespace-nowrap text-xs text-gray-500" title={`UTC: ${formatTimeUTC(trace.start_time)}`}>
                        {formatTimeCST(trace.start_time)}
                      </td>
                      <td className="font-mono text-xs">
                        {trace.trace_id.substring(0, 16)}...
                      </td>
                      <td className="text-xs font-medium text-blue-600">
                        {trace.service_name}
                      </td>
                      <td className="text-xs text-gray-700 max-w-xs truncate">
                        {trace.operation_name}
                      </td>
                      <td className="whitespace-nowrap text-xs">
                        <span
                          className={`font-medium ${
                            trace.duration_ms > 1000
                              ? 'text-red-600'
                              : trace.duration_ms > 500
                              ? 'text-yellow-600'
                              : 'text-green-600'
                          }`}
                        >
                          {formatDuration(trace.duration_ms)}
                        </span>
                      </td>
                      <td>
                        {trace.status_code === 'STATUS_CODE_ERROR' ? (
                          <span className="inline-flex items-center text-xs text-red-600">
                            <AlertCircle className="w-3 h-3 mr-1" />
                            错误
                          </span>
                        ) : (
                          <span className="inline-flex items-center text-xs text-green-600">
                            <Clock className="w-3 h-3 mr-1" />
                            正常
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <EmptyState title="没有找到匹配的追踪" description="尝试调整搜索条件" />
            )}
          </div>
        </div>

        {/* 追踪详情面板 */}
        {selectedTrace && (
          <div className="w-1/3 bg-white rounded-lg shadow-md overflow-hidden">
            <div className="flex items-center justify-between p-4 border-b border-gray-200">
              <h3 className="font-semibold text-gray-900">追踪详情</h3>
              <button
                onClick={() => setSelectedTrace(null)}
                className="text-gray-400 hover:text-gray-600"
              >
                <X className="w-5 h-5" />
              </button>
            </div>
            <div className="p-4 overflow-auto h-full">
              <div className="space-y-4">
                <div>
                  <label className="text-xs text-gray-500">Trace ID</label>
                  <p className="text-sm font-mono break-all">{selectedTrace.trace_id}</p>
                </div>
                <div>
                  <label className="text-xs text-gray-500">开始时间</label>
                  <p className="text-sm" title={`UTC: ${formatTimeUTC(selectedTrace.start_time)}`}>{formatTimeCST(selectedTrace.start_time)}</p>
                </div>
                <div>
                  <label className="text-xs text-gray-500">服务</label>
                  <p className="text-sm font-medium text-blue-600">{selectedTrace.service_name}</p>
                </div>
                <div>
                  <label className="text-xs text-gray-500">操作</label>
                  <p className="text-sm">{selectedTrace.operation_name}</p>
                </div>
                <div>
                  <label className="text-xs text-gray-500">持续时间</label>
                  <p className="text-sm font-medium">
                    {formatDuration(selectedTrace.duration_ms)}
                  </p>
                </div>
                <div>
                  <label className="text-xs text-gray-500">状态</label>
                  <p>
                    {(selectedTrace.status_code === 'STATUS_CODE_ERROR' || (!selectedTrace.status_code && selectedTrace.status === 'error')) ? (
                      <span className="px-2 py-1 text-xs bg-red-100 text-red-700 rounded">
                        错误
                      </span>
                    ) : (selectedTrace.status_code === 'STATUS_CODE_OK' || (!selectedTrace.status_code && selectedTrace.status === 'ok')) ? (
                      <span className="px-2 py-1 text-xs bg-green-100 text-green-700 rounded">
                        成功
                      </span>
                    ) : (
                      <span className="px-2 py-1 text-xs bg-gray-100 text-gray-700 rounded">
                        {selectedTrace.status_code || selectedTrace.status || '未知'}
                      </span>
                    )}
                  </p>
                </div>

                <div className="pt-3 border-t border-gray-200">
                  <label className="text-xs text-gray-500 block mb-2">快速操作</label>
                  <div className="grid grid-cols-1 gap-2">
                    <button
                      onClick={() => navigation.goToLogs({
                        serviceName: selectedTrace.service_name,
                        traceId: selectedTrace.trace_id,
                      })}
                      className="flex items-center justify-center gap-2 px-3 py-2 text-sm bg-blue-50 hover:bg-blue-100 text-blue-700 rounded-lg transition-colors"
                    >
                      <FileText className="w-4 h-4" />
                      查看关联日志
                    </button>
                    <button
                      onClick={() => navigation.goToTopology({ serviceName: selectedTrace.service_name })}
                      className="flex items-center justify-center gap-2 px-3 py-2 text-sm bg-green-50 hover:bg-green-100 text-green-700 rounded-lg transition-colors"
                    >
                      <Network className="w-4 h-4" />
                      查看服务拓扑
                    </button>
                    <button
                      onClick={() => navigation.goToAIAnalysis({
                        traceId: selectedTrace.trace_id,
                        serviceName: selectedTrace.service_name,
                      })}
                      className="flex items-center justify-center gap-2 px-3 py-2 text-sm bg-purple-50 hover:bg-purple-100 text-purple-700 rounded-lg transition-colors"
                    >
                      <BrainCircuit className="w-4 h-4" />
                      AI 诊断链路
                    </button>
                  </div>
                </div>

                {/* Span 时间线 */}
                <div className="mt-4">
                  <div className="flex items-center justify-between mb-2">
                    <label className="text-xs text-gray-500">Span 时间线</label>
                    {spansData && spansData.length > 0 && (
                      <span className="text-xs text-gray-400">{spansData.length} 个 spans</span>
                    )}
                  </div>
                  {spansLoading ? (
                    <div className="flex items-center justify-center h-48">
                      <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-gray-900 border-t-transparent"></div>
                    </div>
                  ) : spansData && spansData.length > 0 ? (
                    <TraceTimeline
                      spans={spansData}
                      selectedSpanId={selectedSpan?.span_id || null}
                      onSpanClick={(span) => setSelectedSpan(span)}
                    />
                  ) : (
                    <div className="bg-gray-50 rounded-lg p-8 h-48 flex items-center justify-center">
                      <p className="text-sm text-gray-400">该追踪暂无 Span 数据</p>
                    </div>
                  )}
                </div>

                {/* Span 详情面板 */}
                {selectedSpan && (
                  <div className="mt-4 border-t border-gray-200 pt-4">
                    <div className="flex items-center justify-between mb-3">
                      <h4 className="text-sm font-semibold text-gray-900">Span 详情</h4>
                      <button
                        onClick={() => setSelectedSpan(null)}
                        className="text-gray-400 hover:text-gray-600"
                      >
                        <X className="w-4 h-4" />
                      </button>
                    </div>
                    <div className="space-y-2">
                      <div className="grid grid-cols-2 gap-2">
                        <div className="bg-gray-50 rounded p-2">
                          <div className="text-xs text-gray-500">Service</div>
                          <div className="text-sm font-medium">{selectedSpan.service_name}</div>
                        </div>
                        <div className="bg-gray-50 rounded p-2">
                          <div className="text-xs text-gray-500">Operation</div>
                          <div className="text-sm">{selectedSpan.operation_name}</div>
                        </div>
                      </div>
                      <div className="grid grid-cols-2 gap-2">
                        <div className="bg-gray-50 rounded p-2">
                          <div className="text-xs text-gray-500">Duration</div>
                          <div className="text-sm font-mono">{formatDuration(resolveSpanDurationMs(selectedSpan))}</div>
                        </div>
                        <div className="bg-gray-50 rounded p-2">
                          <div className="text-xs text-gray-500">Start Time</div>
                          <div className="text-sm" title={`UTC: ${formatTimeUTC(selectedSpan.start_time)}`}>{formatTimeCST(selectedSpan.start_time)}</div>
                        </div>
                      </div>
                      {selectedSpan.status && (
                        <div className="bg-gray-50 rounded p-2">
                          <div className="text-xs text-gray-500">Status</div>
                          <div className="text-sm">
                            {selectedSpan.status === 'STATUS_CODE_ERROR' ? (
                              <span className="text-red-600">错误</span>
                            ) : selectedSpan.status === 'STATUS_CODE_OK' ? (
                              <span className="text-green-600">成功</span>
                            ) : (
                              <span className="text-gray-600">{selectedSpan.status}</span>
                            )}
                          </div>
                        </div>
                      )}
                      {selectedSpan.tags && Object.keys(selectedSpan.tags).length > 0 && (
                        <div className="bg-white rounded border border-gray-200 p-2">
                          <div className="text-xs text-gray-500 mb-1">Tags</div>
                          <div className="flex flex-wrap gap-1">
                            {Object.entries(selectedSpan.tags).map(([key, value]) => (
                              <span key={key} className="inline-flex items-center px-2 py-1 bg-blue-50 text-blue-700 rounded text-xs">
                                <span className="font-mono">{key}</span>
                                <span className="mx-1">:</span>
                                <span>{String(value)}</span>
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

      {/* 统计信息 */}
      <div className="mt-4 text-sm text-gray-500">
        显示 {filteredTraces.length} 条追踪（共 {data?.total || 0} 条）
      </div>
    </div>
  );
};

export default TracesExplorer;
