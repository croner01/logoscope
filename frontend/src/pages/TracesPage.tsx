/**
 * Trace 详情页面
 * 
 * 展示分布式追踪调用链：
 * - 调用链瀑布图
 * - 服务节点图
 * - 性能分析
 * - 错误定位
 */
import React, { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { api } from '../utils/api';
import { useNavigation } from '../hooks/useNavigation';
import LoadingState from '../components/common/LoadingState';
import ErrorState from '../components/common/ErrorState';
import {
  ArrowLeft, AlertCircle, Zap, GitBranch,
  ChevronDown, ChevronRight, CheckCircle, XCircle,
  Activity, BrainCircuit
} from 'lucide-react';

interface Span {
  span_id: string;
  service: string;
  operation: string;
  offset_ms: number;
  duration_ms: number;
  status: string;
  depth: number;
}

interface TraceAnalysis {
  trace_id: string;
  total_duration_ms: number;
  service_count: number;
  span_count: number;
  root_cause_spans: Array<{
    span_id: string;
    service_name: string;
    operation_name: string;
    duration_ms: number;
    status: string;
    error?: string;
  }>;
  bottleneck_spans: Array<{
    span_id: string;
    service_name: string;
    operation_name: string;
    duration_ms: number;
    status: string;
  }>;
  error_spans: Array<{
    span_id: string;
    service_name: string;
    operation_name: string;
    duration_ms: number;
    status: string;
    error?: string;
  }>;
  recommendations: string[];
  critical_path: string[];
}

interface TraceVisualization {
  trace_id: string;
  nodes: Array<{
    id: string;
    label: string;
    service: string;
    operation: string;
    duration_ms: number;
    status: string;
  }>;
  edges: Array<{
    source: string;
    target: string;
  }>;
  waterfall: Span[];
  analysis: {
    total_duration_ms: number;
    service_count: number;
    span_count: number;
    critical_path: string[];
    error_count: number;
  };
}

const STATUS_COLORS = {
  ok: { bg: 'bg-green-100', text: 'text-green-700', border: 'border-green-300' },
  error: { bg: 'bg-red-100', text: 'text-red-700', border: 'border-red-300' },
  warning: { bg: 'bg-yellow-100', text: 'text-yellow-700', border: 'border-yellow-300' },
};

const SERVICE_COLORS = [
  'bg-blue-500', 'bg-purple-500', 'bg-green-500', 'bg-orange-500',
  'bg-pink-500', 'bg-indigo-500', 'bg-teal-500', 'bg-amber-500',
];

const TracesPage: React.FC = () => {
  const { traceId } = useParams<{ traceId: string }>();
  const navigate = useNavigate();
  const navigation = useNavigation();

  const [visualization, setVisualization] = useState<TraceVisualization | null>(null);
  const [analysis, setAnalysis] = useState<TraceAnalysis | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedSpans, setExpandedSpans] = useState<Set<string>>(new Set());
  const [inputTraceId, setInputTraceId] = useState(traceId || '');

  useEffect(() => {
    if (traceId) {
      loadTraceData(traceId);
    }
  }, [traceId]);

  const loadTraceData = async (id: string) => {
    setLoading(true);
    setError(null);

    try {
      const [vizData, analysisData] = await Promise.all([
        fetch(`/api/v1/ai/trace/${id}/visualization`).then(r => r.json()),
        api.analyzeTrace({ trace_id: id }),
      ]);

      setVisualization(vizData);
      // 将 API 响应转换为 TraceAnalysis 格式
      const traceAnalysis: TraceAnalysis = {
        trace_id: id,
        total_duration_ms: vizData?.analysis?.total_duration_ms || 0,
        service_count: vizData?.analysis?.service_count || 0,
        span_count: vizData?.analysis?.span_count || 0,
        root_cause_spans: [],
        bottleneck_spans: [],
        error_spans: [],
        recommendations: (analysisData?.solutions || []).map((solution) =>
          solution.description ? `${solution.title}：${solution.description}` : solution.title
        ),
        critical_path: vizData?.analysis?.critical_path || [],
      };
      setAnalysis(traceAnalysis);
    } catch (err: any) {
      setError(err.message || '加载 Trace 数据失败');
    } finally {
      setLoading(false);
    }
  };

  const handleSearch = () => {
    if (inputTraceId.trim()) {
      navigate(`/traces/${inputTraceId.trim()}`);
    }
  };

  const toggleSpan = (spanId: string) => {
    setExpandedSpans(prev => {
      const next = new Set(prev);
      if (next.has(spanId)) {
        next.delete(spanId);
      } else {
        next.add(spanId);
      }
      return next;
    });
  };

  const formatDuration = (ms: number): string => {
    if (ms < 1000) return `${ms}ms`;
    return `${(ms / 1000).toFixed(2)}s`;
  };

  const getServiceColor = (service: string): string => {
    const hash = service.split('').reduce((acc, char) => acc + char.charCodeAt(0), 0);
    return SERVICE_COLORS[hash % SERVICE_COLORS.length];
  };

  if (loading) {
    return <LoadingState message="加载 Trace 数据..." />;
  }

  if (error && !visualization) {
    return (
      <div className="p-6">
        <ErrorState message={error} onRetry={() => traceId && loadTraceData(traceId)} />
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full">
      {/* 头部 */}
      <div className="bg-white border-b border-gray-200 px-6 py-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-4">
            <button
              onClick={() => navigate(-1)}
              className="p-2 hover:bg-gray-100 rounded-lg transition-colors"
            >
              <ArrowLeft className="w-5 h-5 text-gray-600" />
            </button>
            <div>
              <h1 className="text-xl font-semibold text-gray-900">Trace 详情</h1>
              <p className="text-sm text-gray-500">Trace ID: {traceId}</p>
            </div>
          </div>

          <div className="flex items-center gap-3">
            <input
              type="text"
              value={inputTraceId}
              onChange={(e) => setInputTraceId(e.target.value)}
              placeholder="输入 Trace ID"
              className="px-3 py-2 border border-gray-300 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 w-64"
              onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
            />
            <button
              onClick={handleSearch}
              className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700 transition-colors"
            >
              查询
            </button>
          </div>
        </div>
      </div>

      {/* 主内容 */}
      <div className="flex-1 overflow-auto p-6">
        {visualization && (
          <div className="grid grid-cols-1 xl:grid-cols-3 gap-6">
            {/* 左侧：瀑布图 */}
            <div className="xl:col-span-2 space-y-6">
              {/* 概览卡片 */}
              <div className="bg-white rounded-lg shadow-md p-4">
                <div className="grid grid-cols-4 gap-4">
                  <div className="text-center">
                    <div className="text-2xl font-bold text-gray-900">
                      {formatDuration(visualization.analysis.total_duration_ms)}
                    </div>
                    <div className="text-sm text-gray-500">总耗时</div>
                  </div>
                  <div className="text-center">
                    <div className="text-2xl font-bold text-gray-900">
                      {visualization.analysis.service_count}
                    </div>
                    <div className="text-sm text-gray-500">服务数</div>
                  </div>
                  <div className="text-center">
                    <div className="text-2xl font-bold text-gray-900">
                      {visualization.analysis.span_count}
                    </div>
                    <div className="text-sm text-gray-500">Span 数</div>
                  </div>
                  <div className="text-center">
                    <div className={`text-2xl font-bold ${
                      visualization.analysis.error_count > 0 ? 'text-red-600' : 'text-green-600'
                    }`}>
                      {visualization.analysis.error_count}
                    </div>
                    <div className="text-sm text-gray-500">错误数</div>
                  </div>
                </div>
              </div>

              {/* 瀑布图 */}
              <div className="bg-white rounded-lg shadow-md overflow-hidden">
                <div className="px-4 py-3 border-b border-gray-200">
                  <h3 className="font-medium text-gray-900">调用链瀑布图</h3>
                </div>
                <div className="p-4">
                  <div className="space-y-1">
                    {visualization.waterfall.map((span) => {
                      const statusColors = STATUS_COLORS[span.status as keyof typeof STATUS_COLORS] || STATUS_COLORS.ok;
                      const serviceColor = getServiceColor(span.service);
                      const widthPercent = Math.max(
                        (span.duration_ms / visualization.analysis.total_duration_ms) * 100,
                        1
                      );
                      const leftPercent = (span.offset_ms / visualization.analysis.total_duration_ms) * 100;

                      return (
                        <div
                          key={span.span_id}
                          className="flex items-center gap-2 py-1 hover:bg-gray-50 rounded cursor-pointer"
                          onClick={() => toggleSpan(span.span_id)}
                        >
                          {/* 深度缩进 */}
                          <div style={{ width: `${span.depth * 20}px` }} className="shrink-0" />

                          {/* 展开/收起图标 */}
                          <div className="w-4 shrink-0">
                            {expandedSpans.has(span.span_id) ? (
                              <ChevronDown className="w-4 h-4 text-gray-400" />
                            ) : (
                              <ChevronRight className="w-4 h-4 text-gray-400" />
                            )}
                          </div>

                          {/* 服务名 */}
                          <div className="w-32 shrink-0 flex items-center gap-2">
                            <div className={`w-2 h-2 rounded-full ${serviceColor}`} />
                            <span className="text-sm font-medium text-gray-800 truncate">
                              {span.service}
                            </span>
                          </div>

                          {/* 操作名 */}
                          <div className="flex-1 text-sm text-gray-600 truncate">
                            {span.operation}
                          </div>

                          {/* 时间条 */}
                          <div className="w-48 shrink-0 relative h-6">
                            <div
                              className={`absolute h-4 top-1 rounded ${statusColors.bg} ${statusColors.border} border`}
                              style={{
                                left: `${leftPercent}%`,
                                width: `${widthPercent}%`,
                              }}
                            />
                          </div>

                          {/* 耗时 */}
                          <div className="w-20 text-right text-sm text-gray-600 shrink-0">
                            {formatDuration(span.duration_ms)}
                          </div>

                          {/* 状态 */}
                          <div className="w-6 shrink-0">
                            {span.status === 'error' ? (
                              <XCircle className="w-4 h-4 text-red-500" />
                            ) : (
                              <CheckCircle className="w-4 h-4 text-green-500" />
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>
              </div>
            </div>

            {/* 右侧：分析结果 */}
            <div className="space-y-6">
              {/* 错误节点 */}
              {analysis?.error_spans && analysis.error_spans.length > 0 && (
                <div className="bg-white rounded-lg shadow-md overflow-hidden">
                  <div className="px-4 py-3 border-b border-gray-200 bg-red-50">
                    <div className="flex items-center gap-2">
                      <AlertCircle className="w-5 h-5 text-red-500" />
                      <h3 className="font-medium text-red-700">错误节点</h3>
                    </div>
                  </div>
                  <div className="p-4 space-y-3">
                    {analysis.error_spans.map((span, index) => (
                      <div key={index} className="p-3 bg-red-50 rounded-lg">
                        <div className="font-medium text-red-800">{span.service_name}</div>
                        <div className="text-sm text-red-600">{span.operation_name}</div>
                        {span.error && (
                          <div className="text-xs text-red-500 mt-1">{span.error}</div>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* 性能瓶颈 */}
              {analysis?.bottleneck_spans && analysis.bottleneck_spans.length > 0 && (
                <div className="bg-white rounded-lg shadow-md overflow-hidden">
                  <div className="px-4 py-3 border-b border-gray-200 bg-amber-50">
                    <div className="flex items-center gap-2">
                      <Zap className="w-5 h-5 text-amber-500" />
                      <h3 className="font-medium text-amber-700">性能瓶颈</h3>
                    </div>
                  </div>
                  <div className="p-4 space-y-3">
                    {analysis.bottleneck_spans.map((span, index) => (
                      <div key={index} className="p-3 bg-amber-50 rounded-lg">
                        <div className="flex justify-between">
                          <span className="font-medium text-amber-800">{span.service_name}</span>
                          <span className="text-amber-600">{formatDuration(span.duration_ms)}</span>
                        </div>
                        <div className="text-sm text-amber-600">{span.operation_name}</div>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* 优化建议 */}
              {analysis?.recommendations && analysis.recommendations.length > 0 && (
                <div className="bg-white rounded-lg shadow-md overflow-hidden">
                  <div className="px-4 py-3 border-b border-gray-200">
                    <div className="flex items-center gap-2">
                      <Activity className="w-5 h-5 text-blue-500" />
                      <h3 className="font-medium text-gray-900">优化建议</h3>
                    </div>
                  </div>
                  <div className="p-4">
                    <ul className="space-y-2">
                      {analysis.recommendations.map((rec, index) => (
                        <li key={index} className="flex items-start gap-2 text-sm">
                          <span className="text-blue-500 mt-0.5">•</span>
                          <span className="text-gray-700">{rec}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                </div>
              )}

              {/* 关键路径 */}
              {analysis?.critical_path && analysis.critical_path.length > 0 && (
                <div className="bg-white rounded-lg shadow-md overflow-hidden">
                  <div className="px-4 py-3 border-b border-gray-200">
                    <div className="flex items-center gap-2">
                      <GitBranch className="w-5 h-5 text-purple-500" />
                      <h3 className="font-medium text-gray-900">关键路径</h3>
                    </div>
                  </div>
                  <div className="p-4">
                    <div className="flex flex-wrap items-center gap-2">
                      {analysis.critical_path.map((service, index) => (
                        <React.Fragment key={index}>
                          <span className="px-2 py-1 bg-purple-100 text-purple-700 rounded text-sm">
                            {service}
                          </span>
                          {index < analysis.critical_path.length - 1 && (
                            <span className="text-gray-400">→</span>
                          )}
                        </React.Fragment>
                      ))}
                    </div>
                  </div>
                </div>
              )}

              {/* 快速操作 */}
              <div className="bg-white rounded-lg shadow-md overflow-hidden">
                <div className="px-4 py-3 border-b border-gray-200">
                  <h3 className="font-medium text-gray-900">快速操作</h3>
                </div>
                <div className="p-4 space-y-2">
                  <button
                    onClick={() => navigation.goToAIAnalysis({
                      traceId: traceId,
                      message: `分析 Trace ${traceId} 的调用链问题`
                    })}
                    className="w-full flex items-center justify-between px-3 py-2 bg-purple-50 hover:bg-purple-100 text-purple-700 rounded-lg transition-colors"
                  >
                    <span className="flex items-center gap-2">
                      <BrainCircuit className="w-4 h-4" />
                      <span className="text-sm font-medium">AI 分析调用链</span>
                    </span>
                  </button>
                  {analysis?.error_spans && analysis.error_spans.length > 0 && (
                    <button
                      onClick={() => navigation.goToLogs({
                        serviceName: analysis.error_spans[0].service_name,
                        search: analysis.error_spans[0].error || 'error'
                      })}
                      className="w-full flex items-center justify-between px-3 py-2 bg-red-50 hover:bg-red-100 text-red-700 rounded-lg transition-colors"
                    >
                      <span className="flex items-center gap-2">
                        <AlertCircle className="w-4 h-4" />
                        <span className="text-sm font-medium">查看错误服务日志</span>
                      </span>
                    </button>
                  )}
                </div>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

export default TracesPage;
