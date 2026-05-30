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
  Activity, BrainCircuit, Search
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

const SERVICE_COLORS = [
  '#3b82f6', '#8b5cf6', '#10b981', '#f97316',
  '#ec4899', '#6366f1', '#14b8a6', '#f59e0b',
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
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '加载 Trace 数据失败');
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
      <div className="page-header mb-4" style={{ borderBottom: '1px solid var(--app-border)', paddingBottom: '1rem' }}>
        <div className="flex items-center gap-3">
          <button
            onClick={() => navigate(-1)}
            className="btn btn-ghost btn-icon"
          >
            <ArrowLeft size={16} />
          </button>
          <div>
            <h1 className="page-title">Trace 详情</h1>
            <p className="text-xs mt-0.5 font-mono" style={{ color: 'var(--app-text-subtle)' }}>
              {traceId}
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2">
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5" style={{ color: 'var(--app-text-subtle)' }} />
            <input
              type="text"
              value={inputTraceId}
              onChange={(e) => setInputTraceId(e.target.value)}
              placeholder="输入 Trace ID"
              className="input input-sm pl-8 w-64"
              onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
            />
          </div>
          <button onClick={handleSearch} className="btn btn-primary">
            查询
          </button>
        </div>
      </div>

      {/* 主内容 */}
      <div className="flex-1 overflow-auto">
        {visualization && (
          <div className="grid grid-cols-1 xl:grid-cols-3 gap-6">
            {/* 左侧：瀑布图 */}
            <div className="xl:col-span-2 space-y-4">
              {/* 概览 KPI */}
              <div className="grid grid-cols-4 gap-3">
                {[
                  { label: '总耗时', value: formatDuration(visualization.analysis.total_duration_ms), tone: 'blue' },
                  { label: '服务数', value: String(visualization.analysis.service_count), tone: 'teal' },
                  { label: 'Span 数', value: String(visualization.analysis.span_count), tone: 'purple' },
                  {
                    label: '错误数',
                    value: String(visualization.analysis.error_count),
                    tone: visualization.analysis.error_count > 0 ? 'red' : 'green',
                  },
                ].map(({ label, value, tone }) => (
                  <div key={label} className={`kpi-card tone-${tone}`}>
                    <div className="kpi-label">{label}</div>
                    <div className="kpi-value text-xl">{value}</div>
                  </div>
                ))}
              </div>

              {/* 瀑布图 */}
              <div className="card overflow-hidden">
                <div className="card-header">
                  <div className="card-title">调用链瀑布图</div>
                </div>
                <div className="card-body">
                  <div className="space-y-0.5">
                    {visualization.waterfall.map((span) => {
                      const isError = span.status === 'error';
                      const serviceColor = getServiceColor(span.service);
                      const widthPercent = Math.max(
                        (span.duration_ms / visualization.analysis.total_duration_ms) * 100,
                        1
                      );
                      const leftPercent = (span.offset_ms / visualization.analysis.total_duration_ms) * 100;

                      return (
                        <div
                          key={span.span_id}
                          className="flex items-center gap-2 py-1.5 px-2 rounded-lg cursor-pointer transition-colors"
                          style={{ '--hover-bg': 'var(--app-surface-muted)' } as React.CSSProperties}
                          onMouseEnter={e => (e.currentTarget.style.background = 'var(--app-surface-muted)')}
                          onMouseLeave={e => (e.currentTarget.style.background = '')}
                          onClick={() => toggleSpan(span.span_id)}
                        >
                          {/* 深度缩进 */}
                          <div style={{ width: `${span.depth * 16}px` }} className="shrink-0" />

                          {/* 展开/收起图标 */}
                          <div className="w-4 shrink-0" style={{ color: 'var(--app-text-subtle)' }}>
                            {expandedSpans.has(span.span_id) ? (
                              <ChevronDown size={14} />
                            ) : (
                              <ChevronRight size={14} />
                            )}
                          </div>

                          {/* 服务名 */}
                          <div className="w-28 shrink-0 flex items-center gap-1.5">
                            <div className="w-2 h-2 rounded-full shrink-0" style={{ background: serviceColor }} />
                            <span className="text-xs font-medium truncate" style={{ color: 'var(--app-text)' }}>
                              {span.service}
                            </span>
                          </div>

                          {/* 操作名 */}
                          <div className="flex-1 text-xs truncate" style={{ color: 'var(--app-text-muted)' }}>
                            {span.operation}
                          </div>

                          {/* 时间条 */}
                          <div className="w-48 shrink-0 relative h-5">
                            <div
                              className="absolute h-3 top-1 rounded"
                              style={{
                                left: `${leftPercent}%`,
                                width: `${widthPercent}%`,
                                background: isError ? 'var(--color-error-soft)' : 'var(--brand-primary-soft)',
                                border: `1px solid ${isError ? 'var(--color-error-border)' : 'var(--brand-primary)'}`,
                                opacity: 0.85,
                              }}
                            />
                          </div>

                          {/* 耗时 */}
                          <div className="w-16 text-right text-xs shrink-0" style={{ color: 'var(--app-text-muted)' }}>
                            {formatDuration(span.duration_ms)}
                          </div>

                          {/* 状态 */}
                          <div className="w-5 shrink-0">
                            {isError ? (
                              <XCircle size={14} style={{ color: 'var(--color-error-dark)' }} />
                            ) : (
                              <CheckCircle size={14} style={{ color: 'var(--color-success-dark)' }} />
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
            <div className="space-y-4">
              {/* 错误节点 */}
              {analysis?.error_spans && analysis.error_spans.length > 0 && (
                <div className="card overflow-hidden">
                  <div className="card-header" style={{ background: 'var(--color-error-soft)' }}>
                    <div className="card-title" style={{ color: 'var(--color-error-dark)' }}>
                      <AlertCircle size={14} />
                      错误节点
                    </div>
                  </div>
                  <div className="card-body space-y-2">
                    {analysis.error_spans.map((span, index) => (
                      <div key={index} className="p-3 rounded-xl" style={{ background: 'var(--color-error-soft)', border: '1px solid var(--color-error-border)' }}>
                        <div className="text-sm font-semibold" style={{ color: 'var(--color-error-dark)' }}>{span.service_name}</div>
                        <div className="text-xs mt-0.5" style={{ color: 'var(--color-error-dark)', opacity: 0.8 }}>{span.operation_name}</div>
                        {span.error && (
                          <div className="text-xs mt-1 font-mono" style={{ color: 'var(--color-error-dark)', opacity: 0.7 }}>{span.error}</div>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* 性能瓶颈 */}
              {analysis?.bottleneck_spans && analysis.bottleneck_spans.length > 0 && (
                <div className="card overflow-hidden">
                  <div className="card-header" style={{ background: 'var(--color-warning-soft)' }}>
                    <div className="card-title" style={{ color: 'var(--color-warning-dark)' }}>
                      <Zap size={14} />
                      性能瓶颈
                    </div>
                  </div>
                  <div className="card-body space-y-2">
                    {analysis.bottleneck_spans.map((span, index) => (
                      <div key={index} className="p-3 rounded-xl" style={{ background: 'var(--color-warning-soft)', border: '1px solid #fde68a' }}>
                        <div className="flex justify-between items-center">
                          <span className="text-sm font-semibold" style={{ color: 'var(--color-warning-dark)' }}>{span.service_name}</span>
                          <span className="text-xs font-mono" style={{ color: 'var(--color-warning-dark)' }}>{formatDuration(span.duration_ms)}</span>
                        </div>
                        <div className="text-xs mt-0.5" style={{ color: 'var(--color-warning-dark)', opacity: 0.8 }}>{span.operation_name}</div>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* 优化建议 */}
              {analysis?.recommendations && analysis.recommendations.length > 0 && (
                <div className="card overflow-hidden">
                  <div className="card-header">
                    <div className="card-title">
                      <Activity size={14} style={{ color: 'var(--brand-primary)' }} />
                      优化建议
                    </div>
                  </div>
                  <div className="card-body">
                    <ul className="space-y-2">
                      {analysis.recommendations.map((rec, index) => (
                        <li key={index} className="flex items-start gap-2 text-xs">
                          <span className="mt-0.5 shrink-0" style={{ color: 'var(--brand-primary)' }}>•</span>
                          <span style={{ color: 'var(--app-text-muted)' }}>{rec}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                </div>
              )}

              {/* 关键路径 */}
              {analysis?.critical_path && analysis.critical_path.length > 0 && (
                <div className="card overflow-hidden">
                  <div className="card-header">
                    <div className="card-title">
                      <GitBranch size={14} style={{ color: '#8b5cf6' }} />
                      关键路径
                    </div>
                  </div>
                  <div className="card-body">
                    <div className="flex flex-wrap items-center gap-2">
                      {analysis.critical_path.map((service, index) => (
                        <React.Fragment key={index}>
                          <span className="px-2 py-1 rounded-lg text-xs font-medium" style={{ background: 'rgba(139,92,246,0.1)', color: '#7c3aed', border: '1px solid rgba(139,92,246,0.2)' }}>
                            {service}
                          </span>
                          {index < analysis.critical_path.length - 1 && (
                            <span style={{ color: 'var(--app-text-subtle)' }}>→</span>
                          )}
                        </React.Fragment>
                      ))}
                    </div>
                  </div>
                </div>
              )}

              {/* 快速操作 */}
              <div className="card overflow-hidden">
                <div className="card-header">
                  <div className="card-title">快速操作</div>
                </div>
                <div className="card-body space-y-2">
                  <button
                    onClick={() => navigation.goToAIAnalysis({
                      traceId: traceId,
                      message: `分析 Trace ${traceId} 的调用链问题`
                    })}
                    className="w-full flex items-center gap-2 px-3 py-2.5 rounded-xl text-sm font-medium transition-colors"
                    style={{ background: 'rgba(139,92,246,0.08)', color: '#7c3aed', border: '1px solid rgba(139,92,246,0.15)' }}
                    onMouseEnter={e => (e.currentTarget.style.background = 'rgba(139,92,246,0.15)')}
                    onMouseLeave={e => (e.currentTarget.style.background = 'rgba(139,92,246,0.08)')}
                  >
                    <BrainCircuit size={15} />
                    AI 分析调用链
                  </button>
                  {analysis?.error_spans && analysis.error_spans.length > 0 && (
                    <button
                      onClick={() => navigation.goToLogs({
                        serviceName: analysis.error_spans[0].service_name,
                        search: analysis.error_spans[0].error || 'error'
                      })}
                      className="w-full flex items-center gap-2 px-3 py-2.5 rounded-xl text-sm font-medium transition-colors"
                      style={{ background: 'var(--color-error-soft)', color: 'var(--color-error-dark)', border: '1px solid var(--color-error-border)' }}
                      onMouseEnter={e => (e.currentTarget.style.opacity = '0.85')}
                      onMouseLeave={e => (e.currentTarget.style.opacity = '1')}
                    >
                      <AlertCircle size={15} />
                      查看错误服务日志
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
